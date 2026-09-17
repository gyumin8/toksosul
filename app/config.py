"""
app/config.py
환경변수 + 서비스 규칙 상수를 한 곳에서 관리한다.
규칙 수치(인원/바퀴/타임아웃)를 코드 곳곳에 흩뿌리지 않고 여기서만 바꾸도록 한다.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# ---------------------------------------------------------------- 외부 API (Google Gemini)
# 키는 https://aistudio.google.com/apikey 에서 발급. 반드시 .env에만 넣는다.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# 모델 이름은 자주 바뀌므로 .env에서 교체할 수 있게 뺐다.
# 기본값은 '싸고 빠른 안정판' 조합. 품질을 올리려면 .env에서 아래로 바꾼다.
#   텍스트: gemini-3.5-flash / gemini-3.8-flash
#   이미지: gemini-3.1-flash-image (고품질), gemini-3-pro-image (최고품질)
GEMINI_TEXT_MODEL = os.getenv("GEMINI_TEXT_MODEL", "gemini-3.5-flash-lite")
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-lite-image")

# ---------------------------------------------------------------- 이미지 생성
# Cloudflare Workers AI: 무료 일일 할당량이 있고 카드 등록이 필요 없다.
# 대시보드 우측에 있는 32자리 Account ID와, AI 권한을 준 API 토큰이 필요하다.
CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID", "").strip()
CF_API_TOKEN = os.getenv("CF_API_TOKEN", "").strip()
CF_IMAGE_MODEL = os.getenv("CF_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell")

# 삽화 생성을 끄고 플레이스홀더만 쓰고 싶을 때 (테스트 중 할당량 절약)
ENABLE_IMAGE_GEN = os.getenv("ENABLE_IMAGE_GEN", "1") not in ("0", "false", "False")


def _resolve_image_provider() -> str:
    """어떤 이미지 벤더를 쓸지 결정한다.
    .env의 IMAGE_PROVIDER로 강제할 수 있고, 비워두면 있는 키를 보고 알아서 고른다.
    Cloudflare가 무료라서 둘 다 있으면 Cloudflare를 먼저 쓴다."""
    forced = os.getenv("IMAGE_PROVIDER", "").strip().lower()
    if forced in ("cloudflare", "gemini", "off"):
        return forced
    if CF_ACCOUNT_ID and CF_API_TOKEN:
        return "cloudflare"
    if GEMINI_API_KEY:
        return "gemini"
    return "off"


IMAGE_PROVIDER = _resolve_image_provider()

# API 키가 없으면 자동으로 목(mock) 모드로 동작한다.
# -> 키 발급 전에도 팀원 전원이 전체 플로우를 돌려볼 수 있다.
USE_MOCK_AI = not GEMINI_API_KEY

# ---------------------------------------------------------------- 무료 할당량 보호
# 데모 당일 할당량 소진으로 전체가 막히는 걸 막기 위한 벤더별 일일 호출 한도.
# 0이면 한도 없음(카운트만 하고 막지 않음). 각 벤더 대시보드에서 실제 무료 한도를
# 확인한 뒤, 안전 마진(예: 실제 한도의 80~90%)을 두고 채워 넣는다.
#   Gemini: https://aistudio.google.com/usage
#   Cloudflare: 대시보드 > Workers AI > Usage
GEMINI_TEXT_DAILY_LIMIT = int(os.getenv("GEMINI_TEXT_DAILY_LIMIT", "0"))
GEMINI_IMAGE_DAILY_LIMIT = int(os.getenv("GEMINI_IMAGE_DAILY_LIMIT", "0"))
CF_IMAGE_DAILY_LIMIT = int(os.getenv("CF_IMAGE_DAILY_LIMIT", "0"))

# ---------------------------------------------------------------- DB
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'toksosul.db'}")

# ---------------------------------------------------------------- 서비스 규칙
# 최소 인원. 서비스 규칙은 2명이지만, 혼자 테스트할 때 .env에서 1로 낮출 수 있게 해둔다.
# .env의 MIN_MEMBERS 줄을 지우면 자동으로 2로 돌아간다.
MIN_MEMBERS = max(1, min(10, int(os.getenv("MIN_MEMBERS", "2"))))
MAX_MEMBERS = 10              # 최대 인원
DEFAULT_MAX_ROUNDS = 20       # 바퀴 제한 (전체 인원 1회 순환 = 1바퀴)
HARD_MAX_ROUNDS = 20          # 방장이 올려도 넘을 수 없는 상한
DEFAULT_INACTIVITY_HOURS = 24 # 무응답 시 턴 넘김까지 대기 시간

# LLM 완결 추천을 몇 바퀴째부터 물어볼지 (초반엔 물어봐도 의미 없어서 비용 낭비)
END_SUGGESTION_FROM_ROUND = 3

# 남은 바퀴가 이 수 이하로 들어오면 이어쓰기 프롬프트가 '정리 모드'로 바뀐다.
# (새 떡밥을 던지지 않고 기존 전개를 회수하는 쪽으로 유도)
WRAP_UP_FROM_REMAINING_ROUNDS = 3

# 생성 이미지 저장 위치
MEDIA_DIR = BASE_DIR / "static" / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
