"""
app/main.py
FastAPI 진입점. 실행:  uvicorn app.main:app --reload
문서:  http://127.0.0.1:8000/docs
화면:  http://127.0.0.1:8000/
"""
import random
import string
from datetime import timedelta

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from . import ai
from . import turn_logic as tl
from .config import (
    BASE_DIR, DEFAULT_INACTIVITY_HOURS, ENABLE_IMAGE_GEN, HARD_MAX_ROUNDS,
    IMAGE_PROVIDER, MAX_MEMBERS, MIN_MEMBERS, USE_MOCK_AI,
)
from .db import get_db, init_db
from .models import Room, RoomMember, Story, User
from .schemas import (
    CompleteRequest, RoomCreate, RoomJoin, StoryStart, TurnSubmit, UserCreate,
)

app = FastAPI(title="톡소설 API", version="0.1.0")
init_db()

STATIC_DIR = BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
def get_config():
    return {
        "min_members": MIN_MEMBERS,
        "max_members": MAX_MEMBERS,
        "max_rounds": HARD_MAX_ROUNDS,
        "inactivity_hours": DEFAULT_INACTIVITY_HOURS,
        "mock_ai": USE_MOCK_AI,
        "image_gen": ENABLE_IMAGE_GEN,
        "image_provider": IMAGE_PROVIDER,
    }


@app.get("/api/ai-check")
def ai_check(image: int = 0):
    """AI 연결 확인용. 브라우저에서 열어보면 키/모델 설정이 맞는지 바로 알 수 있다.

    /api/ai-check          -> 텍스트만 확인 (빠름)
    /api/ai-check?image=1  -> 이미지도 실제로 한 장 생성해 본다
    """
    return ai.ping(test_image=bool(image))


# ------------------------------------------------------------------ 유저
@app.post("/api/users")
def create_user(body: UserCreate, db: Session = Depends(get_db)):
    """비밀번호 없는 닉네임 세션. 프론트가 user_id를 localStorage에 보관한다.
    (기획안 10. 미결정 사항의 '로그인/인증 방식'은 MVP에서 이렇게 임시 처리)"""
    user = User(nickname=body.nickname.strip())
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"id": user.id, "nickname": user.nickname}


# ------------------------------------------------------------------ 방
def _new_invite_code(db: Session) -> str:
    alphabet = string.ascii_uppercase + string.digits
    for _ in range(30):
        code = "".join(random.choices(alphabet, k=6))
        if not db.query(Room).filter(Room.invite_code == code).first():
            return code
    raise HTTPException(500, "초대 코드를 만들지 못했습니다. 다시 시도해 주세요.")


def _room_payload(db: Session, room: Room) -> dict:
    members = tl.ordered_members(db, room.id)
    story = room.stories[-1] if room.stories else None
    return {
        "id": room.id,
        "name": room.name,
        "invite_code": room.invite_code,
        "host_id": room.host_id,
        "max_rounds": room.max_rounds,
        "inactivity_hours": room.inactivity_hours,
        "status": room.status,
        "members": [
            {"member_id": m.id, "user_id": m.user_id,
             "nickname": m.user.nickname, "turn_order": m.turn_order}
            for m in members
        ],
        "story_id": story.id if story else None,
    }


@app.post("/api/rooms")
def create_room(body: RoomCreate, db: Session = Depends(get_db)):
    user = db.get(User, body.user_id)
    if not user:
        raise HTTPException(404, "유저를 찾을 수 없습니다.")

    room = Room(
        name=body.name.strip(),
        invite_code=_new_invite_code(db),
        host_id=user.id,
        max_rounds=min(body.max_rounds, HARD_MAX_ROUNDS),
        inactivity_hours=body.inactivity_hours,
    )
    db.add(room)
    db.flush()
    db.add(RoomMember(room_id=room.id, user_id=user.id, turn_order=0))  # 방장이 0번
    db.commit()
    db.refresh(room)
    return _room_payload(db, room)


@app.post("/api/rooms/join")
def join_room(body: RoomJoin, db: Session = Depends(get_db)):
    room = db.query(Room).filter(Room.invite_code == body.invite_code.upper().strip()).first()
    if not room:
        raise HTTPException(404, "그런 초대 코드가 없습니다. 코드를 다시 확인해 주세요.")

    existing = db.query(RoomMember).filter(
        RoomMember.room_id == room.id, RoomMember.user_id == body.user_id
    ).first()
    if existing:
        return _room_payload(db, room)

    if room.status != "waiting":
        raise HTTPException(400, "이미 시작된 방에는 중간에 들어올 수 없습니다.")

    members = tl.ordered_members(db, room.id)
    if len(members) >= MAX_MEMBERS:
        raise HTTPException(400, f"정원이 찼습니다. 한 방에 최대 {MAX_MEMBERS}명까지 들어올 수 있어요.")

    db.add(RoomMember(room_id=room.id, user_id=body.user_id, turn_order=len(members)))
    db.commit()
    db.refresh(room)
    return _room_payload(db, room)


@app.get("/api/rooms/{room_id}")
def get_room(room_id: str, db: Session = Depends(get_db)):
    room = db.get(Room, room_id)
    if not room:
        raise HTTPException(404, "방을 찾을 수 없습니다.")
    return _room_payload(db, room)


def _schedule_pending_round_work(story: Story, background_tasks: BackgroundTasks) -> None:
    """바퀴 완성 후처리(삽화/떡밥추적/완결추천, 필요시 에필로그)가 예약돼 있으면
    백그라운드로 스케줄한다. tl._advance()가 세팅해둔 art_pending_round를 본다.

    조회(GET) 폴링마다 이 함수가 반복 호출되므로, claim_round_for_background()로
    이미 처리 중인 바퀴는 다시 스케줄하지 않는다 (안 그러면 같은 바퀴를 여러 백그라운드
    작업이 동시에 만들려고 경합해 DB 유니크 제약 위반이 난다)."""
    round_number = story.art_pending_round
    if round_number is not None and tl.claim_round_for_background(story.id, round_number):
        background_tasks.add_task(tl.run_round_completion, story.id, round_number)


# ------------------------------------------------------------------ 소설
@app.post("/api/rooms/{room_id}/start")
def start_story(room_id: str, body: StoryStart, background_tasks: BackgroundTasks,
                 db: Session = Depends(get_db)):
    room = db.get(Room, room_id)
    if not room:
        raise HTTPException(404, "방을 찾을 수 없습니다.")
    if room.host_id != body.user_id:
        raise HTTPException(403, "방장만 시작할 수 있습니다.")
    if room.status != "waiting":
        raise HTTPException(400, "이미 시작된 방입니다.")

    members = tl.ordered_members(db, room.id)
    if len(members) < MIN_MEMBERS:
        raise HTTPException(400, f"최소 {MIN_MEMBERS}명이 모여야 시작할 수 있습니다.")
    if len(members) > MAX_MEMBERS:
        raise HTTPException(400, f"최대 {MAX_MEMBERS}명까지만 참여할 수 있습니다.")

    story = Story(
        room_id=room.id,
        genre=body.genre.strip() or "자유",
        member_count=len(members),  # 시작 시점 인원 고정 -> 바퀴 계산이 흔들리지 않는다
        turn_index=0,
        deadline_at=tl.utcnow() + timedelta(hours=room.inactivity_hours),
    )
    room.status = "playing"
    db.add(story)
    db.commit()
    db.refresh(story)

    # 방장이 첫 상황을 적었으면 그대로 1번 턴으로 처리한다.
    if body.opening and body.opening.strip():
        try:
            tl.submit_turn(db, story, body.user_id, body.opening)
        except tl.TurnError as e:
            raise HTTPException(400, str(e))
        db.refresh(story)
        _schedule_pending_round_work(story, background_tasks)

    return _story_payload(db, story)


def _story_payload(db: Session, story: Story) -> dict:
    cur = tl.current_member(db, story)
    remaining = None
    if story.status == "in_progress":
        remaining = max(0, int((story.deadline_at - tl.utcnow()).total_seconds()))

    arts = {a.round_number: {"image_url": a.image_url, "caption": a.caption} for a in story.arts}

    return {
        "id": story.id,
        "room_id": story.room_id,
        "room_name": story.room.name,
        "invite_code": story.room.invite_code,
        "host_id": story.room.host_id,
        "title": story.title,
        "genre": story.genre,
        "status": story.status,
        # "finishing"은 마지막 바퀴 후처리(에필로그/표지)가 아직 끝나기 전이라 완결로 안 친다.
        "is_finished": story.status not in ("in_progress", "finishing"),
        "art_pending_round": story.art_pending_round,
        "member_count": story.member_count,
        "members": [
            {"member_id": m.id, "user_id": m.user_id,
             "nickname": m.user.nickname, "turn_order": m.turn_order}
            for m in tl.ordered_members(db, story.room_id)
        ],
        "turn_index": story.turn_index,
        "current_round": tl.round_of(story.turn_index, story.member_count),
        "max_rounds": story.room.max_rounds,
        "current_user_id": cur.user_id if cur else None,
        "current_nickname": cur.user.nickname if cur else None,
        "deadline_at": story.deadline_at.isoformat() + "Z",
        "seconds_left": remaining,
        "end_suggestion": story.end_suggestion,
        "end_suggestion_round": story.end_suggestion_round,
        "cover_image_url": story.cover_image_url,
        "epilogue": story.epilogue,
        "turns": [
            {
                "turn_number": t.turn_number,
                "round_number": t.round_number,
                "nickname": t.member.user.nickname if t.member else "?",
                "user_id": t.member.user_id if t.member else None,
                "user_line": t.polished_line or t.user_line,
                "raw_line": t.user_line,
                "was_polished": bool(t.polished_line and t.polished_line != t.user_line),
                "ai_text": t.ai_text,
                "is_skipped": t.is_skipped,
                "created_at": t.created_at.isoformat() + "Z",
            }
            for t in story.turns
        ],
        "arts": arts,
    }


@app.get("/api/stories/{story_id}")
def get_story(story_id: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    story = db.get(Story, story_id)
    if not story:
        raise HTTPException(404, "이야기를 찾을 수 없습니다.")
    tl.sync_deadline(db, story)  # 조회할 때마다 마감 지난 턴을 정리한다 (바퀴가 완성될 수도 있다)
    db.refresh(story)
    _schedule_pending_round_work(story, background_tasks)
    return _story_payload(db, story)


@app.post("/api/stories/{story_id}/turns")
def post_turn(story_id: str, body: TurnSubmit, background_tasks: BackgroundTasks,
              db: Session = Depends(get_db)):
    story = db.get(Story, story_id)
    if not story:
        raise HTTPException(404, "이야기를 찾을 수 없습니다.")
    try:
        tl.submit_turn(db, story, body.user_id, body.line)
    except tl.TurnError as e:
        raise HTTPException(400, str(e))
    db.refresh(story)
    _schedule_pending_round_work(story, background_tasks)
    return _story_payload(db, story)


@app.post("/api/stories/{story_id}/complete")
def complete_story(story_id: str, body: CompleteRequest, db: Session = Depends(get_db)):
    """방장이 직접 완결을 선언한다. LLM 추천을 받아들이는 경로도 같은 엔드포인트를 쓴다."""
    story = db.get(Story, story_id)
    if not story:
        raise HTTPException(404, "이야기를 찾을 수 없습니다.")
    if story.room.host_id != body.user_id:
        raise HTTPException(403, "완결은 방장만 선언할 수 있습니다.")
    if story.status != "in_progress":
        raise HTTPException(400, "이미 완결된 이야기입니다.")
    if tl.written_turn_count(story) == 0:
        raise HTTPException(400, "아직 쓰인 내용이 없어 완결할 수 없습니다.")

    accepted_llm = bool(story.end_suggestion)
    tl.finalize(
        db, story,
        "completed_llm" if accepted_llm else "completed_host",
        story.end_suggestion if accepted_llm else "방장이 완결을 선언",
    )
    db.refresh(story)
    return _story_payload(db, story)


@app.post("/api/stories/{story_id}/dismiss-suggestion")
def dismiss_suggestion(story_id: str, body: CompleteRequest, db: Session = Depends(get_db)):
    """LLM의 완결 추천을 거절하고 계속 쓴다."""
    story = db.get(Story, story_id)
    if not story:
        raise HTTPException(404, "이야기를 찾을 수 없습니다.")
    if story.room.host_id != body.user_id:
        raise HTTPException(403, "방장만 처리할 수 있습니다.")
    story.end_suggestion = None
    story.end_suggestion_round = None
    db.commit()
    db.refresh(story)
    return _story_payload(db, story)


# ------------------------------------------------------------------ 내보내기
@app.get("/api/stories/{story_id}/export.md", response_class=PlainTextResponse)
def export_markdown(story_id: str, db: Session = Depends(get_db)):
    story = db.get(Story, story_id)
    if not story:
        raise HTTPException(404, "이야기를 찾을 수 없습니다.")

    writers = ", ".join(m.user.nickname for m in tl.ordered_members(db, story.room_id))
    lines = [f"# {story.title}", "", f"> {story.genre} · {writers} 함께 씀", ""]

    last_round = 0
    for t in story.turns:
        if t.is_skipped:
            continue
        if t.round_number != last_round:
            last_round = t.round_number
            lines += ["", f"## {last_round}바퀴", ""]
        writer = t.member.user.nickname if t.member else "?"
        lines += [f"*{writer}: {t.polished_line or t.user_line}*", "", t.ai_text, ""]

    if story.epilogue:
        lines += ["", "---", "", story.epilogue, ""]

    return "\n".join(lines)
