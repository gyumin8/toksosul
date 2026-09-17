"""
app/turn_logic.py
서비스 규칙이 전부 여기 모여 있다. 라우터(main.py)는 이 함수들만 호출한다.

규칙 요약
 - 인원: 최소 2명 / 최대 10명
 - 턴제: 차례인 사람이 한 줄 쓰면 즉시 다음 사람으로 넘어간다
 - 미작성: 24시간 동안 안 쓰면 그 턴은 '넘김' 처리하고 다음 사람으로 간다 (AI 대필 없음)
 - 바퀴: 전체 인원이 한 번씩 돌면 1바퀴, 바퀴가 끝날 때마다 삽화 1장 생성
 - 바퀴 제한: 20바퀴
 - 완결: (1) LLM 추천 + 방장 승인 (2) 방장 직접 선언 (3) 20바퀴 도달 시 강제 종료
"""
import json
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from . import ai
from .config import END_SUGGESTION_FROM_ROUND
from .db import SessionLocal
from .models import RoomMember, RoundArt, Story, Turn


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ------------------------------------------------------------------ 조회 헬퍼
def ordered_members(db: Session, room_id: str) -> list[RoomMember]:
    return (db.query(RoomMember)
              .filter(RoomMember.room_id == room_id)
              .order_by(RoomMember.turn_order)
              .all())


def round_of(turn_index: int, member_count: int) -> int:
    """0-based 전역 턴 인덱스가 몇 바퀴째인지(1-based) 계산한다."""
    return turn_index // member_count + 1


def current_member(db: Session, story: Story) -> RoomMember | None:
    """지금 차례인 멤버. 완결됐으면 None."""
    if story.status != "in_progress":
        return None
    members = ordered_members(db, story.room_id)
    if not members:
        return None
    return members[story.turn_index % story.member_count]


def history_dicts(db: Session, story: Story) -> list[dict]:
    """ai.build_context()에 넘길 형태로 턴 기록을 변환한다."""
    out = []
    for t in story.turns:
        out.append({
            "writer": t.member.user.nickname if t.member and t.member.user else "알 수 없음",
            "user_line": t.polished_line or t.user_line or "",
            "ai_text": t.ai_text or "",
            "is_skipped": t.is_skipped,
        })
    return out


def load_sheet(story: Story) -> dict:
    """인물 외모 시트를 dict로 읽는다. 비어 있거나 깨졌으면 빈 dict."""
    if not story.character_sheet:
        return {}
    try:
        data = json.loads(story.character_sheet)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def written_turn_count(story: Story) -> int:
    return sum(1 for t in story.turns if not t.is_skipped)


def load_threads(story: Story) -> list[str]:
    """미해결 떡밥 목록(스토리 바이블)을 읽는다. 비어 있거나 깨졌으면 빈 리스트."""
    if not story.plot_threads:
        return []
    try:
        data = json.loads(story.plot_threads)
        return [str(t) for t in data] if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


# ------------------------------------------------------------------ 완결 처리
def finalize(db: Session, story: Story, status: str, reason: str) -> None:
    """공통 완결 처리: 에필로그 + 표지 생성 후 상태를 닫는다."""
    context = ai.build_context(history_dicts(db, story))
    result = ai.write_epilogue(story.genre, context, reason, load_threads(story))

    story.title = result["title"]
    story.epilogue = result["epilogue"]
    story.status = status
    story.completed_at = utcnow()
    story.room.status = "finished"

    # 표지: 마지막 바퀴 삽화를 재사용한다. (완결용 이미지를 따로 뽑지 않아 호출 1회 절약)
    if story.arts:
        story.cover_image_url = story.arts[-1].image_url
    else:
        art = ai.generate_round_art(story.genre, context,
                                    round_of(story.turn_index, story.member_count),
                                    load_sheet(story))
        story.cover_image_url = art["image_url"]

    db.commit()


def _on_round_complete(db: Session, story: Story, finished_round: int) -> None:
    """한 바퀴가 끝난 직후 호출된다. 삽화 생성 + 완결 추천 판단."""
    # 전원이 넘김 처리된 빈 바퀴면 그릴 게 없다.
    turns_in_round = [t for t in story.turns if t.round_number == finished_round and not t.is_skipped]
    if not turns_in_round:
        return

    context = ai.build_context(history_dicts(db, story))

    already = db.query(RoundArt).filter(
        RoundArt.story_id == story.id, RoundArt.round_number == finished_round
    ).first()
    if not already:
        art = ai.generate_round_art(story.genre, context, finished_round, load_sheet(story))
        db.add(RoundArt(
            story_id=story.id,
            round_number=finished_round,
            image_url=art["image_url"],
            caption=art["caption"],
        ))
        # 새로 등장한 인물의 외모 묘사를 누적 저장한다. 다음 바퀴 삽화에서 재사용된다.
        if art.get("character_sheet"):
            story.character_sheet = json.dumps(art["character_sheet"], ensure_ascii=False)

    # 미해결 떡밥 목록(스토리 바이블) 갱신. 이미지 생성 여부와 무관하게 매 바퀴 돈다.
    threads = ai.update_plot_threads(story.genre, context, load_threads(story))
    story.plot_threads = json.dumps(threads, ensure_ascii=False)

    # 완결 추천: 초반 바퀴는 물어봐도 의미가 없어서 건너뛴다.
    if finished_round >= END_SUGGESTION_FROM_ROUND:
        verdict = ai.suggest_ending(
            story.genre, context, finished_round, story.room.max_rounds
        )
        if verdict["should_end"]:
            story.end_suggestion = verdict["reason"]
            story.end_suggestion_round = finished_round
        else:
            story.end_suggestion = None
            story.end_suggestion_round = None

    db.commit()


def _advance(db: Session, story: Story) -> None:
    """턴 인덱스를 하나 밀고, 바퀴가 끝났으면 후처리를 예약한다.

    무거운 작업(삽화 생성, 떡밥 추적, 완결 추천, 마지막 바퀴면 에필로그까지)은 여기서
    직접 하지 않는다. art_pending_round만 표시해두고, 실제 처리는
    run_round_completion()이 백그라운드로 수행한다 (main.py가 스케줄).
    이렇게 해야 제출한 사람 본인의 응답(continue_story 결과)이 지연 없이 나간다.
    """
    finished_round = round_of(story.turn_index, story.member_count)
    story.turn_index += 1
    story.deadline_at = utcnow() + timedelta(hours=story.room.inactivity_hours)
    db.commit()

    crossed_round = story.turn_index % story.member_count == 0
    if not crossed_round:
        return

    # 전원이 넘김 처리된 빈 바퀴면 처리할 게 없다.
    turns_in_round = [t for t in story.turns if t.round_number == finished_round and not t.is_skipped]
    if not turns_in_round:
        return

    story.art_pending_round = finished_round
    if finished_round >= story.room.max_rounds:
        story.status = "finishing"
    db.commit()


# 백그라운드 작업 중복 스케줄 방지용. GET 폴링이 여러 번 들어와도 같은 (story, round)에
# 대해 run_round_completion이 동시에 여러 번 뜨지 않게 막는다. 프로세스 내 메모리라서
# 단일 프로세스 배포(README 참고)를 벗어나면(워커 여러 개) 별도 처리가 필요하다.
_rounds_in_flight: set[tuple[str, int]] = set()
_rounds_in_flight_lock = threading.Lock()


def claim_round_for_background(story_id: str, round_number: int) -> bool:
    """이 (story, round)를 백그라운드로 처리할 권리를 얻는다.

    먼저 호출한 쪽만 True를 받는다. 이미 누군가 처리 중이면 False — main.py는
    이 경우 추가로 스케줄하지 않는다. run_round_completion이 끝나면 반드시 놓아준다.
    """
    key = (story_id, round_number)
    with _rounds_in_flight_lock:
        if key in _rounds_in_flight:
            return False
        _rounds_in_flight.add(key)
        return True


def run_round_completion(story_id: str, finished_round: int) -> None:
    """_advance()가 예약해둔 바퀴 후처리를 백그라운드에서 수행한다.

    main.py가 claim_round_for_background()로 선점한 뒤 FastAPI BackgroundTasks로
    호출한다. 요청 스코프 세션은 응답과 함께 닫히므로 여기서는 독립된 세션을 새로 연다.
    무슨 일이 있어도 art_pending_round와 in-flight 표시는 끝에 반드시 풀어준다.
    안 그러면 '생성 중' 표시가 영원히 안 풀리고, 완결 후처리(finishing) 중 예외가 나면
    게임 자체가 막혀버리기 때문이다.
    """
    db = SessionLocal()
    try:
        story = db.get(Story, story_id)
        if not story or story.art_pending_round != finished_round:
            return  # 이미 처리됐거나 상태가 바뀜

        try:
            _on_round_complete(db, story, finished_round)
        except Exception as e:
            print(f"[art] {finished_round}바퀴 후처리 중 예외: {type(e).__name__}: {e}")
        finally:
            story.art_pending_round = None
            db.commit()

        if finished_round >= story.room.max_rounds and story.status == "finishing":
            try:
                finalize(db, story, "completed_forced",
                         f"{story.room.max_rounds}바퀴 제한에 도달해 자동 완결")
            except Exception as e:
                print(f"[art] {finished_round}바퀴 완결(에필로그) 처리 중 예외: {type(e).__name__}: {e}")
                story.status = "in_progress"  # 완결 실패 시 게임이 막히지 않게 되돌린다
                db.commit()
    finally:
        db.close()
        with _rounds_in_flight_lock:
            _rounds_in_flight.discard((story_id, finished_round))


# ------------------------------------------------------------------ 마감 처리
def sync_deadline(db: Session, story: Story) -> int:
    """
    조회/제출 때마다 호출한다. 마감이 지난 턴이 있으면 '넘김'으로 기록하고 다음 사람에게 넘긴다.
    별도 스케줄러(Celery 등) 없이 요청 시점에 지연 평가하는 방식.
    -> 데모/심사 환경에서 워커를 따로 띄울 필요가 없다.
    반환값: 이번에 넘김 처리된 턴 수
    """
    skipped = 0
    guard = 0
    while (story.status == "in_progress"
           and story.deadline_at <= utcnow()
           and guard < story.member_count * story.room.max_rounds + 5):
        guard += 1
        member = current_member(db, story)
        if member is None:
            break

        db.add(Turn(
            story_id=story.id,
            turn_number=story.turn_index + 1,
            round_number=round_of(story.turn_index, story.member_count),
            member_id=member.id,
            user_line=None,
            ai_text=None,
            is_skipped=True,
        ))
        db.commit()
        db.refresh(story)
        _advance(db, story)
        db.refresh(story)
        skipped += 1
    return skipped


# ------------------------------------------------------------------ 턴 제출
class TurnError(Exception):
    pass


def submit_turn(db: Session, story: Story, user_id: str, line: str) -> Turn:
    """차례인 사람이 한 줄을 제출한다. AI 단락 생성까지 마치고 즉시 다음 사람으로 넘긴다."""
    sync_deadline(db, story)
    db.refresh(story)

    if story.status != "in_progress":
        raise TurnError("이미 완결된 이야기입니다.")

    member = current_member(db, story)
    if member is None:
        raise TurnError("현재 차례를 찾을 수 없습니다.")
    if member.user_id != user_id:
        raise TurnError(f"지금은 {member.user.nickname} 님의 차례입니다.")

    line = (line or "").strip()
    if not line:
        raise TurnError("한 줄을 입력해 주세요.")
    if len(line) > 200:
        raise TurnError("한 줄은 200자까지만 쓸 수 있습니다.")

    current_round = round_of(story.turn_index, story.member_count)
    remaining_rounds = story.room.max_rounds - current_round + 1

    context = ai.build_context(history_dicts(db, story))
    result = ai.continue_story(story.genre, context, line, member.user.nickname,
                               remaining_rounds)

    turn = Turn(
        story_id=story.id,
        turn_number=story.turn_index + 1,
        round_number=current_round,
        member_id=member.id,
        user_line=line,                              # 사용자가 실제로 친 원문
        polished_line=result["polished_line"],       # 맞춤법/어순만 다듬은 버전
        ai_text=result["text"],
        is_skipped=False,
    )
    db.add(turn)
    db.commit()
    db.refresh(story)

    _advance(db, story)  # 제출 즉시 턴 넘김
    db.refresh(turn)
    return turn
