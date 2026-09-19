"""
app/metrics.py
발표/데모 후 "성공률 몇 %, NSFW 오탐률 몇 %" 같은 정량 지표를 뽑기 위한
구조화 이벤트 로그. 기존 print(f"[art] ...") 로그는 사람이 읽기용으로 그대로
두고, 이 모듈은 같은 사건을 JSON 한 줄(JSONL)로도 남긴다.

이벤트는 BASE_DIR/metrics.jsonl에 append-only로 쌓인다. summarize()가 파일을
다시 읽어 이벤트 종류별 집계(총 시도/성공/폴백 수, 성공률, NSFW 오탐률 등)를
계산한다. quota.py와 마찬가지로 파일 쓰기가 실패해도 서비스 진행은 막지 않는다.
"""
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

_LOG_PATH = Path(__file__).resolve().parent.parent / "metrics.jsonl"
_lock = threading.Lock()


def log_event(event: str, **fields) -> None:
    """이벤트 하나를 JSON 한 줄로 기록한다. event 예: "text_generate",
    "image_generate", "nsfw_false_positive", "rate_limit_retry".
    나머지 fields는 이벤트마다 자유롭게 붙인다(vendor, outcome, reason 등)."""
    record = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    line = json.dumps(record, ensure_ascii=False)
    try:
        with _lock:
            with _LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError as e:
        print(f"[metrics] 로그 기록 실패: {e}")


def _read_events() -> list[dict]:
    if not _LOG_PATH.exists():
        return []
    events = []
    with _LOG_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 손상된 줄은 건너뛴다(중간에 프로세스가 죽어 반쯤 써진 줄 등)
    return events


def _rate(numer: int, denom: int) -> float | None:
    return round(numer / denom, 4) if denom else None


def summarize() -> dict:
    """/api/metrics가 그대로 내려주는 집계. 파일을 매번 다시 읽으므로 최신
    상태를 반영하지만, 데모 중 자주 호출해도 AI를 호출하지 않으므로 비용은
    없다(quota_status()와 같은 원칙)."""
    events = _read_events()

    text_events = [e for e in events if e.get("event") == "text_generate"]
    text_success = sum(1 for e in text_events if e.get("outcome") == "success")

    image_events = [e for e in events if e.get("event") == "image_generate"]
    image_success = sum(1 for e in image_events if e.get("outcome") == "success")
    by_vendor: dict[str, dict] = {}
    for e in image_events:
        vendor = e.get("vendor") or "unknown"
        bucket = by_vendor.setdefault(vendor, {"total": 0, "success": 0})
        bucket["total"] += 1
        if e.get("outcome") == "success":
            bucket["success"] += 1
    for bucket in by_vendor.values():
        bucket["success_rate"] = _rate(bucket["success"], bucket["total"])

    nsfw_events = [e for e in events if e.get("event") == "nsfw_false_positive"]
    nsfw_recovered = sum(1 for e in nsfw_events if e.get("recovered"))

    retry_events = [e for e in events if e.get("event") == "rate_limit_retry"]
    retry_by_vendor: dict[str, int] = {}
    for e in retry_events:
        vendor = e.get("vendor") or "unknown"
        retry_by_vendor[vendor] = retry_by_vendor.get(vendor, 0) + 1

    # 단계별 소요시간. "이미지가 느리다"를 감이 아니라 숫자로 확인하는 용도다.
    # ai._timed()가 남기는 latency 이벤트를 단계 이름으로 묶어 평균/최대/중앙값을 낸다.
    latency: dict[str, dict] = {}
    for e in events:
        if e.get("event") != "latency":
            continue
        seconds = e.get("elapsed_s")
        if not isinstance(seconds, (int, float)):
            continue
        latency.setdefault(str(e.get("stage") or "unknown"), []).append(float(seconds))

    latency_summary = {}
    for stage, samples in sorted(latency.items()):
        ordered = sorted(samples)
        mid = len(ordered) // 2
        median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
        latency_summary[stage] = {
            "count": len(ordered),
            "avg_s": round(sum(ordered) / len(ordered), 2),
            "median_s": round(median, 2),
            "max_s": round(ordered[-1], 2),
        }

    return {
        "latency": latency_summary,
        "text_generate": {
            "total": len(text_events),
            "success": text_success,
            "fallback": len(text_events) - text_success,
            "success_rate": _rate(text_success, len(text_events)),
        },
        "image_generate": {
            "total": len(image_events),
            "success": image_success,
            "placeholder": len(image_events) - image_success,
            "success_rate": _rate(image_success, len(image_events)),
            "by_vendor": by_vendor,
        },
        "nsfw_false_positive": {
            # cloudflare_image 시도 중 몇 %가 NSFW 오탐이었는지 (분모가 있어야 의미 있음)
            "count": len(nsfw_events),
            "recovered_by_gemini": nsfw_recovered,
            "rate_of_cloudflare_attempts": _rate(
                len(nsfw_events), by_vendor.get("cloudflare", {}).get("total", 0)
            ),
        },
        "rate_limit_retry": {
            "total": len(retry_events),
            "by_vendor": retry_by_vendor,
        },
        "event_count": len(events),
    }