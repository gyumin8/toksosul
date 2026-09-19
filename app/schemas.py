"""
app/schemas.py
요청 바디 스키마. 응답은 딕셔너리로 직접 조립해서 내려준다(프론트가 쓰기 편한 평평한 형태).
"""
from pydantic import BaseModel, Field


class UserCreate(BaseModel):
    nickname: str = Field(min_length=1, max_length=30)


class CharacterSpec(BaseModel):
    """방장이 '등장인물 추가'로 직접 정의한 인물 하나."""
    name: str = Field(default="", max_length=20)
    gender: str = Field(default="", max_length=10)   # female/male/""
    traits: str = Field(default="", max_length=100)


class RoomCreate(BaseModel):
    user_id: str
    name: str = Field(min_length=1, max_length=60)
    max_rounds: int = Field(default=20, ge=1, le=20)
    inactivity_hours: int = Field(default=24, ge=1, le=168)

    # 주인공 설정(선택). 방 설정과 같이 받아서 방에 저장해둔다.
    hero_name: str | None = Field(default=None, max_length=20)
    hero_gender: str | None = Field(default=None, max_length=10)   # female/male/""
    hero_traits: str | None = Field(default=None, max_length=100)

    # 주인공 외의 등장인물(선택). '등장인물 추가' 버튼으로 몇 명이든 넣을 수 있다.
    characters: list[CharacterSpec] = Field(default_factory=list, max_length=8)


class RoomJoin(BaseModel):
    user_id: str
    invite_code: str = Field(min_length=4, max_length=8)


class StoryStart(BaseModel):
    user_id: str
    genre: str = Field(default="자유", max_length=40)
    opening: str | None = Field(default=None, max_length=200)

    # 인물 설정은 방 만들 때 받아 방에 저장해두므로 여기서는 받지 않는다.


class TurnSubmit(BaseModel):
    user_id: str
    line: str = Field(min_length=1, max_length=200)


class CompleteRequest(BaseModel):
    user_id: str