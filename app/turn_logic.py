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
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from . import ai
from .config import END_SUGGESTION_FROM_ROUND
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


# ------------------------------------------------------------------ 완결 처리
def finalize(db: Session, story: Story, status: str, reason: str) -> None:
    """공통 완결 처리: 에필로그 + 표지 생성 후 상태를 닫는다."""
    context = ai.build_context(history_dicts(db, story))
    result = ai.write_epilogue(story.genre, context, reason)

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
    """턴 인덱스를 하나 밀고, 바퀴가 끝났으면 후처리하고, 20바퀴를 넘겼으면 강제 종료한다."""
    finished_round = round_of(story.turn_index, story.member_count)
    story.turn_index += 1
    story.deadline_at = utcnow() + timedelta(hours=story.room.inactivity_hours)
    db.commit()

    crossed_round = story.turn_index % story.member_count == 0
    if crossed_round:
        _on_round_complete(db, story, finished_round)

        if finished_round >= story.room.max_rounds:
            finalize(db, story, "completed_forced",
                     f"{story.room.max_rounds}바퀴 제한에 도달해 자동 완결")


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

    context = ai.build_context(history_dicts(db, story))
    result = ai.continue_story(story.genre, context, line, member.user.nickname)

    turn = Turn(
        story_id=story.id,
        turn_number=story.turn_index + 1,
        round_number=round_of(story.turn_index, story.member_count),
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
