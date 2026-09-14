"""
app/schemas.py
요청 바디 스키마. 응답은 딕셔너리로 직접 조립해서 내려준다(프론트가 쓰기 편한 평평한 형태).
"""
from pydantic import BaseModel, Field


class UserCreate(BaseModel):
    nickname: str = Field(min_length=1, max_length=30)


class RoomCreate(BaseModel):
    user_id: str
    name: str = Field(min_length=1, max_length=60)
    max_rounds: int = Field(default=20, ge=1, le=20)
    inactivity_hours: int = Field(default=24, ge=1, le=168)


class RoomJoin(BaseModel):
    user_id: str
    invite_code: str = Field(min_length=4, max_length=8)


class StoryStart(BaseModel):
    user_id: str
    genre: str = Field(default="자유", max_length=40)
    opening: str | None = Field(default=None, max_length=200)


class TurnSubmit(BaseModel):
    user_id: str
    line: str = Field(min_length=1, max_length=200)


class CompleteRequest(BaseModel):
    user_id: str
