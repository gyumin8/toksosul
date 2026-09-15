"""
app/ai.py
AI 호출을 전부 이 파일 안에 가둔다. (기획안 6-1 "AI 개입 방식은 교체 가능하게" 원칙)

바깥에서는 continue_story / suggest_ending / write_epilogue / generate_round_art /
update_plot_threads 다섯 함수만 쓴다. 벤더나 모델을 바꿔도 이 파일만 고치면 된다.

벤더: Google Gemini (google-genai SDK)
 - 텍스트/이미지 모두 client.interactions.create() 로 호출한다.
 - 구 버전 SDK(interactions 미지원)면 자동으로 models.generate_content()로 내려간다.
 - 구조화 출력은 API 기능 대신 "JSON만 출력하라"는 프롬프트 + 관대한 파싱으로 처리한다.
   SDK 버전에 따라 스키마 파라미터 이름이 달라서, 버전 의존성을 줄이는 쪽을 택했다.

GEMINI_API_KEY가 없으면 자동으로 목 응답을 돌려준다. 키 없이도 전체 플로우 테스트 가능.
"""
import base64
import io
import json
import random
import re
import time
import urllib.error
import urllib.request
import uuid

from google.genai import errors as genai_errors

from . import quota
from .config import (
    CF_ACCOUNT_ID, CF_API_TOKEN, CF_IMAGE_DAILY_LIMIT, CF_IMAGE_MODEL,
    ENABLE_IMAGE_GEN, GEMINI_API_KEY, GEMINI_IMAGE_DAILY_LIMIT, GEMINI_IMAGE_MODEL,
    GEMINI_TEXT_DAILY_LIMIT, GEMINI_TEXT_MODEL, IMAGE_PROVIDER, MEDIA_DIR,
    USE_MOCK_AI, WRAP_UP_FROM_REMAINING_ROUNDS,
)

_client = None
PLACEHOLDER_IMAGE = "/static/placeholder.svg"


def _get_client():
    global _client
    if _client is None:
        from google import genai
        from google.genai import types as genai_types
        # SDK가 interactions.create()에서 408/409/429/5xx를 자체적으로 최대 5회,
        # 지수 백오프로 최대 60초까지 재시도한다. 그대로 두면 진짜 429가 났을 때
        # 우리 코드가 예외를 보기도 전에 SDK 안에서 수십 초가 날아가 버려서,
        # 아래 _call_with_retry()가 약속하는 "총합 5초"가 무의미해진다. 그래서
        # SDK 자체 재시도는 여기서 끄고(attempts=1 = 재시도 없음), 재시도 시점·
        # 횟수·시간을 전부 우리 쪽(_call_with_retry)이 통제한다.
        _client = genai.Client(
            api_key=GEMINI_API_KEY,
            http_options=genai_types.HttpOptions(
                retry_options=genai_types.HttpRetryOptions(attempts=1),
            ),
        )
    return _client


# ------------------------------------------------------------------ 순간적인 429 재시도
# quota.py(오늘 한도 다 씀)와는 다른 문제다. 이건 "순간적으로 요청이 몰려 잠깐
# 거부됨"에 대한 재시도이므로, 짧게 몇 번만 시도하고 quota 카운터는 건드리지 않는다.
_RETRY_MAX_ATTEMPTS = 3          # 최초 시도 1회 + 재시도 최대 2회
_RETRY_BASE_DELAYS = (1.0, 2.0)  # 1차 재시도 전 1초, 2차 재시도 전 2초
_RETRY_JITTER_MAX = 0.3          # 대기 시간에 0~0.3초 무작위로 더한다
_RETRY_TOTAL_BUDGET = 5.0        # 재시도 대기 시간 총합 상한(초). 초과분은 잘라낸다


def _is_rate_limited(exc: BaseException) -> bool:
    """429 / RESOURCE_EXHAUSTED로 명확히 식별되는 에러에만 True를 준다.

    그 외 에러(400 INVALID_ARGUMENT 같은 잘못된 요청, 네트워크 오류 등)는 재시도
    해도 똑같이 실패할 뿐이므로 여기서 걸러내 즉시 상위 폴백으로 넘긴다.

    실측 결과, client.interactions.create()(우리가 실제로 쓰는 신형 API)는
    google.genai.errors가 아니라 SDK 내부(_gaos)의 별도 예외 계층
    (RateLimitError 등, status_code 속성을 가짐)을 던진다. 그 계층은 공개된
    임포트 경로가 없어서 private 모듈을 직접 import하는 대신 status_code
    속성으로 덕 타이핑한다 — 상태코드별 서브클래스가 OpenAI 호환 관례를
    따르고 있어 이 속성 계약은 SDK 버전이 바뀌어도 잘 안 바뀐다.
    client.models.generate_content()(구형 폴백)는 google.genai.errors.APIError
    계열(code/status 속성)을 던지므로 그쪽도 같이 본다.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429
    if isinstance(exc, genai_errors.APIError):
        return exc.code == 429 or exc.status == "RESOURCE_EXHAUSTED"
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code == 429
    return False


def _call_with_retry(fn, label: str):
    """짧은 지수 백오프(1초→2초 + 지터)로 최대 2회만 재시도한다.

    429가 아닌 에러는 즉시 그대로 올려 재시도 없이 폴백으로 보낸다. 대기 시간
    총합은 _RETRY_TOTAL_BUDGET을 넘지 않게 잘라낸다 — 턴 제출처럼 동기 경로에
    물려 있는 호출(continue_story)이 재시도 때문에 너무 오래 걸리지 않게 하기
    위함이다.
    """
    remaining_budget = _RETRY_TOTAL_BUDGET
    for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as e:
            if not _is_rate_limited(e):
                raise  # 429가 아니면 재시도하지 않는다
            if attempt == _RETRY_MAX_ATTEMPTS:
                raise  # 재시도 소진 — 상위 폴백에 맡긴다
            delay = min(_RETRY_BASE_DELAYS[attempt - 1] + random.uniform(0, _RETRY_JITTER_MAX),
                        remaining_budget)
            remaining_budget -= delay
            print(f"[ai:retry] {label} 429(요청 몰림), {attempt}번째 재시도 전 {delay:.1f}초 대기")
            if delay > 0:
                time.sleep(delay)


# ------------------------------------------------------------------ 저수준 호출
def _generate_text(prompt: str) -> str:
    """Gemini에 텍스트를 요청하고 문자열을 돌려준다.

    호출 직전에 일일 한도(GEMINI_TEXT_DAILY_LIMIT)를 확인한다. 한도에 도달했으면
    실제 벤더에 요청을 보내지 않고 quota.QuotaExceeded를 던진다 — 어차피 거절당할
    호출로 시간을 쓰지 않기 위함이다. 호출부(continue_story 등)가 이 예외를 잡아
    폴백으로 넘어간다. 429(순간적으로 몰림)는 이것과 별개로 _call_with_retry가
    짧게 재시도한다.
    """
    quota.check("gemini_text", GEMINI_TEXT_DAILY_LIMIT, "Gemini 텍스트")
    client = _get_client()

    # 신형 Interactions API 우선
    if hasattr(client, "interactions"):
        def _call():
            quota.increment("gemini_text")
            return client.interactions.create(model=GEMINI_TEXT_MODEL, input=prompt)

        interaction = _call_with_retry(_call, "Gemini 텍스트")
        text = getattr(interaction, "output_text", None)
        if text:
            return text.strip()

    # 구형 SDK 폴백 (또는 신형 API가 빈 응답을 준 경우)
    def _legacy_call():
        quota.increment("gemini_text")
        return client.models.generate_content(model=GEMINI_TEXT_MODEL, contents=prompt)

    resp = _call_with_retry(_legacy_call, "Gemini 텍스트(구형)")
    return (resp.text or "").strip()


def _extract_tag(raw: str, tag: str) -> str:
    """[태그] 뒤에서 다음 [태그] 전까지를 뽑아낸다. 없으면 빈 문자열."""
    match = re.search(rf"\[{tag}\]\s*(.*?)(?=\n\s*\[[^\]]{{1,10}}\]|$)", raw, flags=re.DOTALL)
    return match.group(1).strip() if match else ""


def _parse_json(raw: str) -> dict | None:
    """모델 응답에서 JSON을 최대한 건져낸다. 실패하면 None."""
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # 앞뒤에 설명이 붙어 나온 경우, 가장 바깥 중괄호만 잘라서 재시도
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return None


def _generate_json(prompt: str, attempts: int = 2, label: str = "json") -> dict:
    """JSON만 출력하도록 시킨 뒤 관대하게 파싱한다.

    LLM은 형식을 종종 어기므로 한 번 더 시도한다. 그래도 실패하면 빈 dict를 주되,
    조용히 넘어가지 않고 로그를 남긴다. (원인 모를 '가끔 실패'를 없애기 위함)
    """
    last = ""
    for i in range(attempts):
        try:
            last = _generate_text(prompt if i == 0 else prompt + "\n\n반드시 JSON만 출력해라.")
        except Exception as e:
            print(f"[ai:{label}] 호출 실패({i + 1}/{attempts}): {type(e).__name__}: {e}")
            continue
        parsed = _parse_json(last)
        if parsed is not None:
            return parsed
        print(f"[ai:{label}] JSON 파싱 실패({i + 1}/{attempts}). 응답 앞부분: {last[:120]!r}")
    return {}


def _save_image(raw_b64: str, ext: str) -> str:
    """base64 이미지를 static/media에 저장하고 웹 경로를 돌려준다."""
    filename = f"{uuid.uuid4().hex}.{ext}"
    (MEDIA_DIR / filename).write_bytes(base64.b64decode(raw_b64))
    return f"/static/media/{filename}"


def _image_cloudflare(prompt: str) -> str | None:
    """Cloudflare Workers AI (FLUX.1 schnell)로 이미지를 생성한다.

    requests 대신 표준 라이브러리 urllib를 쓴다. 의존성을 늘리지 않기 위함이다.
    """
    quota.check("cloudflare_image", CF_IMAGE_DAILY_LIMIT, "Cloudflare 이미지")

    url = (f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
           f"/ai/run/{CF_IMAGE_MODEL}")
    # 프롬프트 상한이 2048자다. 넘치면 요청 자체가 거절되므로 미리 자른다.
    body = json.dumps({"prompt": prompt[:2040], "steps": 4}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {CF_API_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    def _call():
        quota.increment("cloudflare_image")
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read().decode("utf-8"))

    payload = _call_with_retry(_call, "Cloudflare 이미지")

    if not payload.get("success"):
        raise RuntimeError(f"Cloudflare 응답 실패: {payload.get('errors')}")

    image_b64 = (payload.get("result") or {}).get("image")
    if not image_b64:
        return None
    return _save_image(image_b64, "jpg")  # flux-1-schnell은 JPEG를 돌려준다


def _image_gemini(prompt: str) -> str | None:
    """Gemini(Nano Banana)로 이미지를 생성한다. 유료 등급이 필요하다."""
    client = _get_client()
    if not hasattr(client, "interactions"):
        return None

    quota.check("gemini_image", GEMINI_IMAGE_DAILY_LIMIT, "Gemini 이미지")

    def _call():
        quota.increment("gemini_image")
        return client.interactions.create(
            model=GEMINI_IMAGE_MODEL,
            input=prompt,
            # image/png는 400을 낸다. 이 API는 image/jpeg만 지원한다.
            response_format={"type": "image", "mime_type": "image/jpeg", "aspect_ratio": "16:9"},
        )

    interaction = _call_with_retry(_call, "Gemini 이미지")
    image = getattr(interaction, "output_image", None)
    if not image or not getattr(image, "data", None):
        return None
    return _save_image(image.data, "jpg")


def _generate_image(prompt: str) -> str | None:
    """설정된 벤더로 이미지를 생성한다. 실패하면 None.

    Cloudflare가 NSFW 오탐으로 거부하면(에러 본문에 'nsfw' 포함) 같은 프롬프트로
    Gemini에 한 번만 재시도한다. _image_gemini를 직접 한 번 호출할 뿐 재귀적으로
    다시 타지 않으므로 재시도는 구조적으로 1회로 고정된다. Gemini도 실패하면
    그대로 실패시켜(None) 상위 generate_round_art가 플레이스홀더로 폴백하게 둔다.
    """
    if IMAGE_PROVIDER == "cloudflare":
        try:
            return _image_cloudflare(prompt)
        except urllib.error.HTTPError as e:
            body = e.read()
            # NSFW 판정을 위해 본문을 한 번 읽었으니, 이 예외를 다시 읽을 수도 있는
            # 호출부(generate_round_art의 로그, ping()의 진단)를 위해 스트림을 되돌려놓는다.
            e.fp = io.BytesIO(body)
            if b"nsfw" not in body.lower():
                raise
            print(f"[art] Cloudflare NSFW 오탐 감지, Gemini로 1회 재시도")
            try:
                url = _image_gemini(prompt)
                print(f"[art] Gemini 재시도 {'성공' if url else '실패(응답에 이미지 없음)'}")
                return url
            except Exception as e2:
                print(f"[art] Gemini 재시도 실패: {type(e2).__name__}: {e2}")
                return None
    if IMAGE_PROVIDER == "gemini":
        return _image_gemini(prompt)
    return None


def _image_model_name() -> str:
    return {"cloudflare": CF_IMAGE_MODEL, "gemini": GEMINI_IMAGE_MODEL}.get(IMAGE_PROVIDER, "-")


def quota_status() -> dict:
    """오늘 날짜 기준 벤더별 호출 수/한도 스냅샷. /api/quota가 그대로 내려준다.
    AI를 실제로 호출하지 않으므로 데모 중에 자주 폴링해도 할당량을 쓰지 않는다."""
    return quota.status({
        "gemini_text": GEMINI_TEXT_DAILY_LIMIT,
        "gemini_image": GEMINI_IMAGE_DAILY_LIMIT,
        "cloudflare_image": CF_IMAGE_DAILY_LIMIT,
    })


def ping(test_image: bool = False) -> dict:
    """연결 확인용. /api/ai-check 에서 호출한다.

    test_image=True면 실제로 이미지를 한 장 생성해 본다. (/api/ai-check?image=1)
    """
    result = {
        "text": {"mode": "mock" if USE_MOCK_AI else "gemini", "model": GEMINI_TEXT_MODEL},
        "image": {"provider": IMAGE_PROVIDER, "model": _image_model_name(),
                  "enabled": ENABLE_IMAGE_GEN},
    }

    # --- 텍스트 ---
    if USE_MOCK_AI:
        result["text"] |= {"ok": False, "detail": "GEMINI_API_KEY가 .env에 없습니다."}
    else:
        try:
            sample = _generate_text("한국어로 '연결 성공'이라고만 답해라.")
            result["text"] |= {"ok": True, "sample": sample[:60]}
        except Exception as e:  # 모델명 오타, 키 오류, 쿼터 초과 등을 그대로 보여준다
            result["text"] |= {"ok": False, "detail": f"{type(e).__name__}: {e}"[:400]}

    # --- 이미지 ---
    if not test_image:
        result["image"]["ok"] = None  # 아직 확인 안 함
    elif IMAGE_PROVIDER == "off" or not ENABLE_IMAGE_GEN:
        result["image"] |= {"ok": False, "detail": "이미지 생성이 꺼져 있거나 키가 없습니다."}
    else:
        try:
            url = _generate_image("A quiet empty classroom at dusk, soft watercolor, no text.")
            result["image"] |= ({"ok": True, "url": url} if url
                                else {"ok": False, "detail": "응답에 이미지가 없습니다."})
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            result["image"] |= {"ok": False, "detail": f"HTTP {e.code}: {detail}"}
        except Exception as e:
            result["image"] |= {"ok": False, "detail": f"{type(e).__name__}: {e}"[:400]}

    result["ok"] = bool(result["text"].get("ok"))
    return result


# ------------------------------------------------------------------ 컨텍스트
# 히스토리를 프롬프트 문자열로 만드는 지점. 나중에 '요약 기반'으로 바꿀 때 여기만 고친다.
HISTORY_TURN_LIMIT = 40  # 최근 몇 턴을 통째로 넘길지


def build_context(turns: list[dict]) -> str:
    """turns: [{"writer": "규민", "user_line": "...", "ai_text": "...", "is_skipped": False}, ...]"""
    written = [t for t in turns if not t["is_skipped"] and t.get("ai_text")]
    recent = written[-HISTORY_TURN_LIMIT:]
    blocks = [f"[{t['writer']}의 한 줄] {t['user_line']}\n{t['ai_text']}" for t in recent]
    return "\n\n".join(blocks) if blocks else "(아직 아무것도 쓰이지 않았다)"


STYLE_RULES = """너는 친구들이 한 줄씩 던지는 상황을 받아 소설로 이어 쓰는 공동 창작 파트너다.
너는 주인공이 아니라 받아쓰는 사람이다. 이야기를 끌고 가는 건 친구의 한 줄이다.

[다듬기 규칙]
친구가 쓴 한 줄에 맞춤법 오류, 어색한 조사, 꼬인 어순이 있으면 그것만 고친다.
- 내용, 사건, 인물, 뉘앙스는 절대 바꾸지 않는다. 표현을 더 멋있게 만들지도 않는다.
- 원문이 이미 자연스러우면 한 글자도 건드리지 말고 그대로 둔다.
- 구어체나 반말은 오류가 아니다. 그대로 둔다.

[본문 규칙]
- 첫 문장은 반드시 친구가 던진 한 줄의 사건을 그대로 서술로 옮긴 것이어야 한다.
  그 사건이 실제로 일어나야 하고, 미루거나 비틀거나 무시하면 안 된다.
- 친구의 한 줄에 없는 새로운 사건은 최대 하나까지만 덧붙인다. 이야기를 앞질러 가지 않는다.
- 분량은 1~2문장, 120자 이내. 짧게 끊어 쓴다. 길게 늘여 쓰지 않는다.
- 앞선 내용의 인물 이름, 관계, 설정, 시점을 절대 바꾸지 않는다.
- 다음 사람이 이어붙일 여지를 남긴다. 상황을 완전히 닫지 않는다.
- 해설, 요약, 메타 발언, 말머리를 붙이지 않는다.

[출력 형식] 아래 두 줄만 출력한다. 다른 말은 쓰지 않는다.
[다듬은줄] (다듬은 친구의 한 줄. 고칠 게 없으면 원문 그대로)
[본문] (이어지는 소설 본문)"""


def _wrap_up_note(remaining_rounds: int | None) -> str:
    """남은 바퀴가 얼마 없으면 새 떡밥 대신 기존 전개를 정리하도록 안내 문구를 만든다.

    진행률 인지 프롬프트: 마지막 WRAP_UP_FROM_REMAINING_ROUNDS 바퀴 이내에서는
    이 문구가 STYLE_RULES의 "새 사건은 최대 하나까지" 규칙 위에 덧붙어
    회수 쪽으로 무게를 옮긴다.
    """
    if remaining_rounds is None or remaining_rounds > WRAP_UP_FROM_REMAINING_ROUNDS:
        return ""
    return (
        f"\n\n[마무리 안내] 이제 {remaining_rounds}바퀴 남았다. "
        "새로운 떡밥이나 인물을 새로 던지지 말고, 지금까지 나온 갈등과 복선을 "
        "정리하고 회수하는 방향으로 이어 써라."
    )


# ------------------------------------------------------------------ 공개 함수
def continue_story(genre: str, context: str, user_line: str, writer: str,
                    remaining_rounds: int | None = None) -> dict:
    """친구의 한 줄을 받아 {"polished_line": str, "text": str} 를 돌려준다.

    JSON 대신 태그 형식을 쓴다. 소설 본문에 따옴표와 줄바꿈이 섞여 있어
    JSON으로 받으면 파싱이 자주 깨지기 때문이다.

    remaining_rounds: 이번 턴이 속한 바퀴부터 끝까지 남은 바퀴 수(포함).
    None이거나 넉넉히 남았으면 평소대로, 얼마 안 남았으면 정리 모드로 바뀐다.
    """
    user_line = user_line.strip()
    if USE_MOCK_AI:
        return {
            "polished_line": user_line,
            "text": f"{user_line} 아무도 먼저 움직이지 않았다. "
                    "[목 응답: GEMINI_API_KEY를 설정하면 실제 생성됩니다]",
        }

    try:
        raw = _generate_text(
            f"{STYLE_RULES}{_wrap_up_note(remaining_rounds)}\n\n"
            f"장르: {genre}\n\n"
            f"[지금까지의 이야기]\n{context}\n\n"
            f"[{writer}가 방금 던진 한 줄]\n{user_line}\n\n"
            "위 한 줄을 다듬고, 그 사건을 실제로 일어나게 해서 본문을 이어라."
        )
    except Exception as e:
        # 삽화 생성과 같은 원칙: AI 호출 실패(할당량 소진 포함)가 턴 제출 자체를
        # 막으면 안 된다. 다듬기 없이 원문을 그대로 쓰고, 짧은 연결 문장으로 이어
        # 다음 사람이 계속 쓸 수 있게 한다. 조용히 넘어가지 않고 로그를 남긴다.
        print(f"[ai:continue] 텍스트 생성 실패, 이어쓰기 없이 진행: {type(e).__name__}: {e}")
        return {
            "polished_line": user_line[:200],
            "text": "（이어지는 장면이 잠시 흐려졌다. 다음 사람이 이어서 써 주세요.）",
        }

    polished = _extract_tag(raw, "다듬은줄") or user_line
    text = _extract_tag(raw, "본문")
    if not text:
        # 형식을 어긴 경우: 태그를 걷어낸 전체를 본문으로 본다. 턴은 진행시킨다.
        print(f"[ai:continue] 출력 형식 위반. 응답 앞부분: {raw[:120]!r}")
        text = re.sub(r"\[[^\]]{1,10}\]", "", raw).strip()

    return {"polished_line": polished.strip()[:200], "text": text.strip()}


def suggest_ending(genre: str, context: str, round_number: int, max_rounds: int) -> dict:
    """이쯤에서 완결해도 좋을지 판단한다. {"should_end": bool, "reason": str} 반환."""
    if USE_MOCK_AI:
        should = round_number >= max(3, max_rounds // 2)
        return {
            "should_end": should,
            "reason": "갈등이 정리되는 흐름이라 여기서 마무리해도 자연스러워 보여요. [목 응답]"
            if should else "아직 풀리지 않은 떡밥이 남아 있어요. [목 응답]",
        }

    data = _generate_json(
        "너는 편집자다. 아래 이야기가 지금 완결해도 좋은 지점에 왔는지 판단해라.\n"
        "갈등이 해소됐거나 전개가 정체됐으면 should_end는 true, "
        "떡밥이 살아있고 흐름이 뻗어나가는 중이면 false.\n"
        '반드시 {"should_end": true 또는 false, "reason": "친구에게 말하듯 1문장, 60자 이내"} '
        "형식의 JSON만 출력한다. 다른 말은 쓰지 않는다.\n\n"
        f"장르: {genre} / 현재 {round_number}바퀴 (최대 {max_rounds}바퀴)\n\n{context}"
    )
    return {"should_end": bool(data.get("should_end")), "reason": str(data.get("reason", ""))}


def write_epilogue(genre: str, context: str, reason: str,
                    open_threads: list[str] | None = None) -> dict:
    """완결 처리 시 마지막 단락과 제목을 생성한다.

    open_threads: update_plot_threads()가 누적 추적해온 미해결 떡밥 목록.
    context는 최근 일부 턴만 담고 있어 초반 떡밥이 빠져 있을 수 있으므로, 따로 넘겨서
    에필로그가 회수하도록 한다.
    """
    if USE_MOCK_AI:
        return {
            "title": random.choice(["그날의 우리", "끝나지 않은 방", "마지막 한 줄"]),
            "epilogue": "그리고 아무도 그 밤에 대해 다시 말하지 않았다. "
                        "다만 각자의 자리에서, 가끔씩 그 문장을 떠올렸을 뿐이다. [목 응답]",
        }

    threads_note = ""
    if open_threads:
        listed = "\n".join(f"- {t}" for t in open_threads)
        threads_note = f"\n\n[아직 해소되지 않은 떡밥] 가능한 한 이 안에서 자연스럽게 회수해라.\n{listed}"

    data = _generate_json(
        "너는 소설의 마지막 단락을 쓰는 작가다. 지금까지의 내용을 바탕으로 여운 있게 이야기를 닫아라.\n"
        "새 인물이나 새 설정을 등장시키지 않는다.\n"
        '반드시 {"title": "제목 20자 이내", "epilogue": "마지막 단락 400자 이내"} '
        f"형식의 JSON만 출력한다. 다른 말은 쓰지 않는다.{threads_note}\n\n"
        f"장르: {genre}\n완결 사유: {reason}\n\n{context}"
    )
    return {
        "title": str(data.get("title") or "제목 없는 이야기")[:120],
        "epilogue": str(data.get("epilogue") or ""),
    }


def update_plot_threads(genre: str, context: str, open_threads: list[str]) -> list[str]:
    """추적 중인 미해결 떡밥 목록을 최근 전개를 반영해 갱신한다.

    build_context()가 최근 일부 턴만 넘기므로, 초반에 등장한 떡밥이 나중 바퀴에서
    컨텍스트 밖으로 밀려나도 여기서 누적 보관해 write_epilogue가 회수할 수 있게 한다.
    실패하면 기존 목록을 그대로 돌려준다. (턴 진행을 막지 않기 위함)
    """
    if USE_MOCK_AI:
        return open_threads

    known = "\n".join(f"- {t}" for t in open_threads) or "(아직 없음)"
    data = _generate_json(
        "너는 이 소설의 복선을 추적하는 편집자다. 추적 중이던 미해결 떡밥 목록을 "
        "최근 전개를 반영해 갱신해라.\n"
        "- 최근 전개에서 이미 해소된 떡밥은 목록에서 뺀다.\n"
        "- 아직 해소되지 않은 기존 떡밥은 문구를 바꾸지 말고 그대로 남긴다.\n"
        "- 최근 전개에서 새로 생긴, 나중에 회수될 법한 떡밥이 있으면 한국어로 짧게 추가한다.\n"
        "- 사소한 디테일 말고 나중에 갚아야 할 약속(비밀, 목표, 갈등)만 담는다.\n"
        f"[추적 중인 떡밥]\n{known}\n\n"
        '{"threads": ["...", "..."]} 형식의 JSON만 출력한다. 다른 말은 쓰지 않는다.\n\n'
        f"장르: {genre}\n\n{context}",
        label="threads",
    )
    threads = data.get("threads")
    if not isinstance(threads, list):
        return open_threads
    return [str(t).strip()[:120] for t in threads if str(t).strip()][:20]


# 모든 삽화에 똑같이 붙는 화풍. 20장이 한 권처럼 보이게 하는 장치다.
ART_STYLE = ("Soft ink and watercolor illustration, muted violet and indigo palette, "
             "gentle grain, cinematic lighting, no text, no letters, no watermark.")


def _build_image_prompt(scene: str, cast: list[str], sheet: dict) -> str:
    """장면 묘사에 등장인물 고정 묘사를 붙인다.

    이미지 모델은 참조 이미지를 받지 못하므로, 같은 인물이 매번 딴사람으로 나온다.
    그래서 인물 외모를 문장으로 고정해두고 매번 똑같이 주입한다.
    """
    descriptors = [f"{name}: {sheet[name]}" for name in cast if name in sheet]
    parts = [scene]
    if descriptors:
        parts.append("Character appearance (keep consistent): " + "; ".join(descriptors))
    parts.append(ART_STYLE)
    return " ".join(parts)


# 조사가 붙은 이름(규민이/서연을/규민과 등)이 character_sheet 키와 정확히
# 일치하지 않아 매칭에 실패하는 걸 막는다. 긴 조사부터 검사해야 "은/는" 같은
# 짧은 조사가 먼저 걸려 이름을 잘못 잘라내는 걸 피할 수 있다.
_KOREAN_PARTICLES = (
    "께서는", "에게서", "이라서", "라서", "에게", "한테", "에서", "으로",
    "로는", "이나", "이랑", "랑", "와", "과", "은", "는", "이", "가",
    "을", "를", "의", "도", "만", "에", "로",
)


def _strip_particle(name: str) -> str:
    for p in _KOREAN_PARTICLES:
        if len(name) > len(p) and name.endswith(p):
            return name[: -len(p)]
    return name


def _match_known_names(candidates: list[str], sheet: dict) -> list[str]:
    """LLM이 내놓은 cast 이름이 조사가 붙거나 표기가 살짝 달라도 이미 정해진
    인물(character_sheet)과 매칭되게 한다.

    정확히 일치 -> 조사 제거 후 일치 -> sheet 키가 이름 문자열에 부분 포함되는
    경우 순으로 시도한다. 아무것도 안 맞으면 새 인물로 보고 원본 문자열을
    그대로 둔다 (뒤에서 새 인물로 등록되는 기존 흐름과 호환).
    """
    matched = []
    for raw in candidates:
        name = raw.strip()
        if name in sheet:
            matched.append(name)
            continue
        stripped = _strip_particle(name)
        if stripped in sheet:
            matched.append(stripped)
            continue
        hit = next((k for k in sheet if k and k in name), None)
        matched.append(hit or name)
    return matched


def generate_round_art(genre: str, context: str, round_number: int,
                       character_sheet: dict | None = None) -> dict:
    """한 바퀴가 끝날 때마다 그 바퀴 분량의 삽화를 생성한다.

    반환: {"image_url", "caption", "character_sheet"}
    character_sheet는 새로 등장한 인물이 추가된 갱신본이다. 호출부가 DB에 저장한다.

    이미지 생성은 실패해도 턴 진행을 막지 않는다. 다만 조용히 넘어가지는 않고
    어느 단계에서 실패했는지 로그를 남긴다.
    """
    sheet = dict(character_sheet or {})
    fallback = {"image_url": f"{PLACEHOLDER_IMAGE}?r={round_number}",
                "caption": f"{round_number}바퀴의 장면",
                "character_sheet": sheet}

    if USE_MOCK_AI:
        fallback["caption"] += " [목 이미지]"
        return fallback
    if not ENABLE_IMAGE_GEN or IMAGE_PROVIDER == "off":
        return fallback

    scene, cast, caption = "", [], f"{round_number}바퀴"

    # --- 1단계: 최근 내용을 영어 장면 묘사로 압축하고, 인물 외모를 받아온다 ---
    # 원문을 그대로 이미지 모델에 넣으면 분량에 눌려 엉뚱한 그림이 나온다.
    known = ", ".join(f"{k}={v}" for k, v in sheet.items()) or "(아직 없음)"
    meta = _generate_json(
        "소설 장면을 삽화로 그리기 위한 영어 프롬프트를 만든다.\n"
        "- scene: 마지막 장면을 영어 1~2문장으로. 구도와 분위기 중심.\n"
        "- cast: 그 장면에 등장하는 인물 이름 배열 (한국어 이름 그대로, 없으면 빈 배열)\n"
        "- characters: 인물별 외모를 영어로 고정 묘사. 이미 정해진 인물은 아래 값을 "
        "**그대로 복사**하고 절대 바꾸지 않는다. 새로 등장한 인물만 추가한다.\n"
        "  (예: \"teenage boy, short black hair, round glasses, navy school uniform\")\n"
        "- caption: 한국어 장면 설명 20자 이내\n"
        f"[이미 정해진 인물] {known}\n\n"
        '{"scene": "...", "cast": ["..."], "characters": {"이름": "..."}, "caption": "..."} '
        "형식의 JSON만 출력한다.\n\n"
        f"장르: {genre}\n\n{context[-2000:]}",
        label=f"art{round_number}",
    )

    if meta:
        scene = str(meta.get("scene") or "")
        raw_cast = [str(x) for x in (meta.get("cast") or []) if x]
        caption = str(meta.get("caption") or caption)[:40]
        for name, desc in (meta.get("characters") or {}).items():
            # 이미 있는 인물은 덮어쓰지 않는다. 외모가 바뀌면 일관성이 깨지므로.
            if name not in sheet and desc:
                sheet[name] = str(desc)

        # 조사가 붙거나 표기가 살짝 다른 이름이 기존 인물과 매칭되지 않으면
        # 그 인물은 고정 외모 없이 그려져 매번 딴사람처럼 나온다. cast 목록을
        # sheet 키에 맞춰 정규화하고, LLM이 cast에 넣는 걸 깜빡했더라도 최근
        # 맥락에 이름이 그대로 언급된 기존 인물이면 강제로 포함시킨다.
        context_tail = context[-2000:]
        mentioned = [name for name in sheet if name and name in context_tail]
        cast = list(dict.fromkeys(_match_known_names(raw_cast, sheet) + mentioned))

    if not scene:
        # 1단계가 실패해도 그림은 나오게 한다. 장면 없는 분위기 컷으로 대체.
        print(f"[art] {round_number}바퀴 장면 묘사 생성 실패 → 기본 프롬프트로 대체")
        scene = (f"An atmospheric establishing shot for chapter {round_number} of a Korean "
                 f"{genre} story. Empty space, quiet mood, no people's faces.")

    # --- 2단계: 실제 이미지 생성 ---
    try:
        url = _generate_image(_build_image_prompt(scene, cast, sheet))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        print(f"[art] {round_number}바퀴 이미지 생성 실패 (HTTP {e.code}): {body}")
        return {**fallback, "character_sheet": sheet}
    except Exception as e:
        print(f"[art] {round_number}바퀴 이미지 생성 실패: {type(e).__name__}: {e}")
        return {**fallback, "character_sheet": sheet}

    if not url:
        print(f"[art] {round_number}바퀴 응답에 이미지가 없습니다.")
        return {**fallback, "character_sheet": sheet}

    return {"image_url": url, "caption": caption, "character_sheet": sheet}
