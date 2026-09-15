"""
app/quota.py
무료 등급 API 호출 횟수를 벤더별로, 날짜별로 센다.

목적: 데모 당일 할당량이 소진돼 벤더가 429를 뱉기 시작하면, 매 요청마다 그 실패를
      (그리고 느린 타임아웃을) 다시 겪는 대신 설정된 한도에 가까워졌을 때 미리 감지해
      호출 자체를 건너뛰고 폴백으로 넘어간다. ai.py가 이 모듈을 통해서만 카운트를 만진다.

파일(BASE_DIR/quota_state.json)에 저장해 `uvicorn --reload` 재시작에도 카운트가
유지되게 한다. 날짜가 바뀌면(UTC 기준) 자동으로 리셋된다.
"""
import json
import threading
from datetime import datetime, timezone
from pathlib import Path


class DailyQuota:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._state = self._load()

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load(self) -> dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text("utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {"date": self._today(), "counts": {}}

    def _save(self) -> None:
        try:
            self._path.write_text(json.dumps(self._state, ensure_ascii=False), "utf-8")
        except OSError as e:
            # 카운터 저장이 실패했다고 서비스 진행을 막으면 안 된다. 로그만 남긴다.
            print(f"[quota] 상태 저장 실패: {e}")

    def _counts(self) -> dict:
        """날짜가 바뀌었으면(자정 넘어감) 카운트를 리셋한다. 락 안에서만 호출."""
        today = self._today()
        if self._state.get("date") != today:
            self._state = {"date": today, "counts": {}}
        return self._state["counts"]

    def count(self, key: str) -> int:
        with self._lock:
            return self._counts().get(key, 0)

    def increment(self, key: str) -> int:
        with self._lock:
            counts = self._counts()
            counts[key] = counts.get(key, 0) + 1
            self._save()
            return counts[key]

    def would_exceed(self, key: str, limit: int) -> bool:
        """limit이 0 이하면(=한도 미설정) 항상 False — 추적만 하고 막지 않는다."""
        if limit <= 0:
            return False
        return self.count(key) >= limit


class QuotaExceeded(RuntimeError):
    """설정된 일일 호출 한도에 도달했을 때. 실제 벤더의 429/RESOURCE_EXHAUSTED와
    구분되는 로그를 남기기 위해 별도 예외로 둔다 (원인이 '우리가 미리 막음'인지
    '벤더가 실제로 거절함'인지 로그만 보고 알 수 있게)."""


_quota = DailyQuota(Path(__file__).resolve().parent.parent / "quota_state.json")


def check(key: str, limit: int, label: str) -> None:
    """한도를 넘었으면 QuotaExceeded를 던진다. 통과했다고 해서 카운트를 올리지는
    않는다 — 실제 호출 직전에 increment()를 따로 불러야 한다 (재시도/폴백 경로에서
    이중으로 세는 걸 피하기 위함)."""
    if _quota.would_exceed(key, limit):
        raise QuotaExceeded(f"{label} 일일 한도({limit}회) 도달, 폴백으로 전환합니다.")


def increment(key: str) -> int:
    return _quota.increment(key)


def status(limits: dict[str, int]) -> dict:
    """key -> limit 매핑을 받아 현재 사용량 스냅샷을 돌려준다. /api/quota가 사용."""
    out = {}
    for key, limit in limits.items():
        used = _quota.count(key)
        out[key] = {
            "used": used,
            "limit": limit if limit > 0 else None,
            "remaining": max(0, limit - used) if limit > 0 else None,
        }
    return out
