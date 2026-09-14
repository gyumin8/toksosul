"""
app/models.py
DB 스키마. 기획안 6-3 스키마를 기준으로 하되 이번 규칙에 맞춰 조정했다.

기획안 대비 변경점
 - groups -> rooms 로 명칭 통일 (프론트 용어 '방'과 일치시키기 위함)
 - stories.max_turns -> rooms.max_rounds ('바퀴' 단위로 제한하기로 했으므로)
 - turns.is_ai_filled -> turns.is_skipped
   (무응답 시 'AI 대필'이 아니라 '턴 넘김'으로 규칙이 바뀌었음)
 - round_arts 테이블 신설 (이미지가 완결 1회가 아니라 '매 바퀴' 생성되므로)
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    """DB에는 항상 tz 정보를 뗀 UTC로 저장한다.
    SQLite에서 읽어온 값은 naive라서, aware 값과 섞으면 비교 시 TypeError가 난다."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    nickname: Mapped[str] = mapped_column(String(30), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Room(Base):
    __tablename__ = "rooms"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    invite_code: Mapped[str] = mapped_column(String(8), unique=True, index=True)
    host_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    max_rounds: Mapped[int] = mapped_column(Integer, default=20)
    inactivity_hours: Mapped[int] = mapped_column(Integer, default=24)
    # waiting: 모집 중 / playing: 집필 중 / finished: 완결
    status: Mapped[str] = mapped_column(String(20), default="waiting")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    members: Mapped[list["RoomMember"]] = relationship(
        back_populates="room", cascade="all, delete-orphan", order_by="RoomMember.turn_order"
    )
    stories: Mapped[list["Story"]] = relationship(
        back_populates="room", cascade="all, delete-orphan"
    )


class RoomMember(Base):
    __tablename__ = "room_members"
    __table_args__ = (UniqueConstraint("room_id", "user_id", name="uq_room_user"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    room_id: Mapped[str] = mapped_column(String(36), ForeignKey("rooms.id"))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    turn_order: Mapped[int] = mapped_column(Integer)  # 0부터 시작하는 라운드로빈 순번
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    room: Mapped["Room"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship()


class Story(Base):
    __tablename__ = "stories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    room_id: Mapped[str] = mapped_column(String(36), ForeignKey("rooms.id"))
    title: Mapped[str] = mapped_column(String(120), default="제목 없는 이야기")
    genre: Mapped[str] = mapped_column(String(40), default="자유")

    # in_progress / completed_forced / completed_host / completed_llm
    status: Mapped[str] = mapped_column(String(24), default="in_progress")

    # 전역 턴 카운터. 현재 차례 = members[turn_index % member_count]
    turn_index: Mapped[int] = mapped_column(Integer, default=0)
    # 시작 시점의 인원 수를 고정 저장한다. (집필 중 인원이 바뀌면 바퀴 계산이 깨지므로)
    member_count: Mapped[int] = mapped_column(Integer)

    deadline_at: Mapped[datetime] = mapped_column(DateTime)  # 현재 차례의 마감 시각
    cover_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    epilogue: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 인물 외모 고정용 시트. {"안승주": "teenage boy, short black hair, navy uniform"} 형태의 JSON.
    # 삽화를 그릴 때마다 같은 묘사를 주입해 인물이 매번 딴사람으로 나오는 것을 막는다.
    character_sheet: Mapped[str | None] = mapped_column(Text, nullable=True)

    # LLM이 "이쯤에서 끝내도 좋겠다"고 판단했을 때 채워지는 필드 (방장 승인 대기)
    end_suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    end_suggestion_round: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    room: Mapped["Room"] = relationship(back_populates="stories")
    turns: Mapped[list["Turn"]] = relationship(
        back_populates="story", cascade="all, delete-orphan", order_by="Turn.turn_number"
    )
    arts: Mapped[list["RoundArt"]] = relationship(
        back_populates="story", cascade="all, delete-orphan", order_by="RoundArt.round_number"
    )


class Turn(Base):
    __tablename__ = "turns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    story_id: Mapped[str] = mapped_column(String(36), ForeignKey("stories.id"))
    turn_number: Mapped[int] = mapped_column(Integer)   # 1부터
    round_number: Mapped[int] = mapped_column(Integer)  # 1부터 (몇 바퀴째인지)
    member_id: Mapped[str] = mapped_column(String(36), ForeignKey("room_members.id"))
    user_line: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 맞춤법/어순만 다듬은 버전. 원문이 자연스러우면 user_line과 같다.
    polished_line: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 24시간 무응답으로 그냥 넘어간 턴 (사람 입력도, AI 생성도 없음)
    is_skipped: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    story: Mapped["Story"] = relationship(back_populates="turns")
    member: Mapped["RoomMember"] = relationship()


class RoundArt(Base):
    __tablename__ = "round_arts"
    __table_args__ = (UniqueConstraint("story_id", "round_number", name="uq_story_round"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    story_id: Mapped[str] = mapped_column(String(36), ForeignKey("stories.id"))
    round_number: Mapped[int] = mapped_column(Integer)
    image_url: Mapped[str] = mapped_column(Text)
    caption: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    story: Mapped["Story"] = relationship(back_populates="arts")
