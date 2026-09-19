"""
app/ai.py
AI 호출을 전부 이 파일 안에 가둔다. (기획안 6-1 "AI 개입 방식은 교체 가능하게" 원칙)

바깥에서는 plan_story / continue_story / editor_pass / write_epilogue /
generate_round_art 다섯 함수만 쓴다. 벤더나 모델을 바꿔도 이 파일만 고치면 된다.

 - plan_story()  : 방 시작 시 1회. 총 바퀴 수를 초/중/후반으로 쪼갠 계획과 등장인물을 정한다.
 - continue_story(): 매 턴. 현재 막의 목표와 등장인물 설정을 프롬프트에 주입한다.
 - editor_pass() : 매 바퀴 1회. 줄거리 요약 + 떡밥 갱신 + 완결 판단을 '한 번에' 처리한다.
                   (예전에는 요약/떡밥/완결추천을 각각 호출해 바퀴마다 3회 왕복이 걸렸다)

벤더: Google Gemini (google-genai SDK)
 - 텍스트/이미지 모두 client.interactions.create() 로 호출한다.
 - 구 버전 SDK(interactions 미지원)면 자동으로 models.generate_content()로 내려간다.
 - 구조화 출력은 API 기능 대신 "JSON만 출력하라"는 프롬프트 + 관대한 파싱으로 처리한다.
   SDK 버전에 따라 스키마 파라미터 이름이 달라서, 버전 의존성을 줄이는 쪽을 택했다.

GEMINI_API_KEY가 없으면 자동으로 목 응답을 돌려준다. 키 없이도 전체 플로우 테스트 가능.
"""
import base64
import hashlib
import io
import json
import math
import random
import re
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager

from google.genai import errors as genai_errors

from . import metrics, quota
from .config import (
    ACT_NAMES, ACT_RATIOS, CAST_MAX, CAST_MIN, CF_ACCOUNT_ID, CF_API_TOKEN,
    CF_IMAGE_DAILY_LIMIT, CF_IMAGE_MODEL, CF_IMAGE_SEND_SEED, CF_IMAGE_STEPS,
    ENABLE_IMAGE_GEN,
    GEMINI_API_KEY,
    GEMINI_IMAGE_DAILY_LIMIT, GEMINI_IMAGE_MODEL, GEMINI_TEXT_DAILY_LIMIT,
    GENAI_TIMEOUT_MS,
    GEMINI_TEXT_MODEL, IMAGE_PROVIDER, MEDIA_DIR, RECENT_TURN_LIMIT,
    SUPABASE_BUCKET, SUPABASE_PROJECT_REF, SUPABASE_S3_ACCESS_KEY_ID,
    SUPABASE_S3_REGION, SUPABASE_S3_SECRET_ACCESS_KEY,
    SYNOPSIS_MAX_CHARS, USE_MOCK_AI, USE_SUPABASE_STORAGE,
    WRAP_UP_FROM_REMAINING_ROUNDS,
)

_client = None
PLACEHOLDER_IMAGE = "/static/placeholder.svg"


# ------------------------------------------------------------------ 소요시간 계측
@contextmanager
def _timed(stage: str, **fields):
    """단계별 소요시간을 로그와 metrics.jsonl에 남긴다.

    "이미지가 느리다"를 감으로 말하지 않기 위한 장치다. /api/metrics의 latency
    항목에서 단계별 평균/최대 초를 바로 볼 수 있다. 어느 단계가 느린지 모르면
    엉뚱한 곳을 최적화하게 된다.
    """
    t0 = time.monotonic()
    try:
        yield
    finally:
        elapsed = round(time.monotonic() - t0, 2)
        print(f"[ai:time] {stage} {elapsed:.1f}초")
        metrics.log_event("latency", stage=stage, elapsed_s=elapsed, **fields)


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
        # timeout(밀리초)을 주지 않으면 SDK가 응답을 무한정 기다릴 수 있다.
        # 실제로 이미지 재시도 호출이 예외도 응답도 없이 멈춰 서서, 바퀴 후처리가
        # 통째로 막히고 '삽화 그리는 중' 표시가 안 풀린 적이 있다.
        # SDK 버전에 따라 timeout 필드가 없을 수도 있어 실패하면 없이 만든다.
        retry = genai_types.HttpRetryOptions(attempts=1)
        try:
            options = genai_types.HttpOptions(retry_options=retry,
                                              timeout=GENAI_TIMEOUT_MS)
        except (TypeError, ValueError) as e:
            print(f"[ai] HttpOptions에 timeout을 줄 수 없어 생략합니다: {e}")
            options = genai_types.HttpOptions(retry_options=retry)
        _client = genai.Client(api_key=GEMINI_API_KEY, http_options=options)
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
            metrics.log_event("rate_limit_retry", vendor=label, attempt=attempt,
                               delay_s=round(delay, 2))
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


_storage_client = None
_STORAGE_CONTENT_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}


def _get_storage_client():
    """Supabase Storage(S3 호환) 클라이언트를 지연 생성한다.

    Supabase Storage는 자체 REST API 외에 S3 호환 API도 제공한다. S3는 매 요청마다
    AWS SigV4 서명이 필요한데 직접 구현하면 서명 버그가 나기 쉬워서, 검증된
    boto3를 그대로 쓴다. (다른 Cloudflare 호출엔 urllib만 쓰는 것과 다른 예외 —
    여긴 서명 알고리즘이 걸린 문제라 직접 구현보다 표준 라이브러리를 쓰는 쪽이
    더 안전하다.)

    처음엔 Cloudflare R2로 만들었으나 R2는 카드 등록이 필요해 Supabase
    Storage(카드 불필요, 무료 1GB)로 바꿨다.
    """
    global _storage_client
    if _storage_client is None:
        import boto3
        _storage_client = boto3.client(
            "s3",
            endpoint_url=f"https://{SUPABASE_PROJECT_REF}.storage.supabase.co/storage/v1/s3",
            aws_access_key_id=SUPABASE_S3_ACCESS_KEY_ID,
            aws_secret_access_key=SUPABASE_S3_SECRET_ACCESS_KEY,
            region_name=SUPABASE_S3_REGION,
        )
    return _storage_client


def _save_image(raw_b64: str, ext: str) -> str:
    """base64 이미지를 저장하고 웹에서 접근 가능한 URL을 돌려준다.

    Supabase Storage가 설정돼 있으면(USE_SUPABASE_STORAGE) 그쪽에 올린다 —
    Render 같은 무료 호스팅은 재배포/재시작마다 로컬 디스크가 초기화돼서,
    static/media에 저장한 삽화가 전부 사라지기 때문이다. 미설정 시(로컬 개발)에는
    기존처럼 로컬 디스크에 저장한다.
    """
    filename = f"{uuid.uuid4().hex}.{ext}"
    raw = base64.b64decode(raw_b64)

    if USE_SUPABASE_STORAGE:
        _get_storage_client().put_object(
            Bucket=SUPABASE_BUCKET,
            Key=filename,
            Body=raw,
            ContentType=_STORAGE_CONTENT_TYPES.get(ext, "application/octet-stream"),
        )
        return (f"https://{SUPABASE_PROJECT_REF}.supabase.co"
                f"/storage/v1/object/public/{SUPABASE_BUCKET}/{filename}")

    (MEDIA_DIR / filename).write_bytes(raw)
    return f"/static/media/{filename}"


def _http_body(e: urllib.error.HTTPError) -> str:
    """HTTPError 본문을 읽어 문자열로 준다. 여러 번 불러도 같은 값이 나온다.

    본문은 한 번 읽으면 스트림이 비어서, 상위 호출부가 다시 읽으면 빈 문자열만
    나온다. 400의 원인 메시지가 로그에 안 찍히던 이유가 이것이다.

    주의: HTTPError는 addinfourl을 상속하고, read()를 self.fp가 아니라 self.file로
    위임한다. 그래서 e.fp만 갈아끼우면 복원이 안 된다 (예전 코드의 실수). 둘 다
    갈아끼우고, 읽은 본문은 예외 객체에 캐시해 둔다.
    """
    cached = getattr(e, "_cached_body", None)
    if cached is not None:
        return cached

    try:
        raw = e.read()
    except Exception:
        raw = b""

    buffer = io.BytesIO(raw)
    e.fp = buffer
    try:
        # addinfourl은 tempfile._TemporaryFileWrapper를 상속하는데, 이 래퍼는
        # 처음 e.read를 꺼낼 때 원본 파일의 read를 인스턴스에 캐시해 버린다.
        # 그래서 self.file만 바꿔서는 소용이 없고, read 자체를 갈아끼워야 한다.
        e.file = buffer
        e.read = buffer.read
    except Exception:
        pass

    text = raw.decode("utf-8", "replace")
    e._cached_body = text
    return text


def _image_cloudflare(prompt: str, seed: int | None = None) -> str | None:
    """Cloudflare Workers AI (FLUX.1 schnell)로 이미지를 생성한다.

    requests 대신 표준 라이브러리 urllib를 쓴다. 의존성을 늘리지 않기 위함이다.

    steps는 4로 둔다. schnell은 4스텝으로 증류된 모델이라 그 위로 올려도 품질이
    거의 안 오르고 시간만 늘어난다(최대 8). 품질 문제는 스텝이 아니라 프롬프트
    구조에서 해결하는 게 맞다.
    """
    quota.check("cloudflare_image", CF_IMAGE_DAILY_LIMIT, "Cloudflare 이미지")

    url = (f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
           f"/ai/run/{CF_IMAGE_MODEL}")

    def _post(payload_in: dict) -> dict:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload_in).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {CF_API_TOKEN}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        def _call():
            quota.increment("cloudflare_image")
            # 타임아웃 90초는 너무 길었다. flux-1-schnell(4스텝 증류 모델)은 정상이면
            # 수 초 안에 돌아온다. 60초를 넘겼다면 사실상 실패한 요청이고, 그때까지
            # 기다리면 바퀴 후처리 전체가 그만큼 멈춘다. 빨리 포기하고 플레이스홀더로 간다.
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))

        return _call_with_retry(_call, "Cloudflare 이미지")

    # 문서의 Parameters 항목에 있는 것은 prompt와 steps뿐이다. 그 밖의 필드를
    # 넣으면 스키마 검증에 걸려 400이 난다(실제로 seed를 넣었다가 400을 받았다).
    # 그래서 기본 요청에는 문서에 있는 것만 담는다.
    # 프롬프트 상한은 2048자다. 넘치면 요청 자체가 거절되므로 미리 자른다.
    base = {"prompt": prompt[:2040], "steps": CF_IMAGE_STEPS}
    extras = {"seed": int(seed)} if (seed is not None and CF_IMAGE_SEND_SEED) else {}

    try:
        payload = _post({**base, **extras})
    except urllib.error.HTTPError as e:
        # 추가 필드 때문에 거절당한 것이라면, 문서에 있는 필드만으로 한 번 더 시도한다.
        # (모델/계정에 따라 seed를 받아주는 곳도 있어서 무조건 빼지는 않는다)
        if not extras or e.code != 400:
            raise
        detail = _http_body(e)
        print(f"[art] Cloudflare 400 — 추가 파라미터({', '.join(extras)}) 제거 후 재시도. {detail[:200]}")
        payload = _post(base)

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


def _generate_image(prompt: str, seed: int | None = None) -> str | None:
    """설정된 벤더로 이미지를 생성한다. 실패하면 None.

    Cloudflare가 NSFW 오탐으로 거부하면(에러 본문에 'nsfw' 포함) 같은 프롬프트로
    Gemini에 한 번만 재시도한다. _image_gemini를 직접 한 번 호출할 뿐 재귀적으로
    다시 타지 않으므로 재시도는 구조적으로 1회로 고정된다. Gemini도 실패하면
    그대로 실패시켜(None) 상위 generate_round_art가 플레이스홀더로 폴백하게 둔다.
    """
    if IMAGE_PROVIDER == "cloudflare":
        try:
            return _image_cloudflare(prompt, seed)
        except urllib.error.HTTPError as e:
            # NSFW 판정을 위해 본문을 읽되, 이 예외를 다시 읽을 수도 있는
            # 호출부(generate_round_art의 로그, ping()의 진단)를 위해 스트림을 되돌려놓는다.
            body = _http_body(e)
            if "nsfw" not in body.lower():
                raise
            # 벤더가 뭐라고 했는지 그대로 남긴다. "오탐 감지"만 찍혀서는 어떤 표현이
            # 걸렸는지 알 수 없어 프롬프트를 고칠 수가 없다.
            print(f"[art] Cloudflare가 프롬프트를 거절했습니다(NSFW 판정). "
                  f"응답: {body[:200]}")
            print(f"[art] 거절된 프롬프트: {prompt[:200]}")
            print("[art] Gemini로 1회 재시도")
            try:
                url = _image_gemini(prompt)
                print(f"[art] Gemini 재시도 {'성공' if url else '실패(응답에 이미지 없음)'}")
                metrics.log_event("nsfw_false_positive", recovered=bool(url))
                return url
            except Exception as e2:
                print(f"[art] Gemini 재시도 실패: {type(e2).__name__}: {e2}")
                metrics.log_event("nsfw_false_positive", recovered=False)
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
            detail = _http_body(e)[:300] or "(응답 본문 없음)"
            result["image"] |= {"ok": False, "detail": f"HTTP {e.code}: {detail}"}
        except Exception as e:
            result["image"] |= {"ok": False, "detail": f"{type(e).__name__}: {e}"[:400]}

    result["ok"] = bool(result["text"].get("ok"))
    return result


# ------------------------------------------------------------------ 컨텍스트
def build_context(turns: list[dict], synopsis: str | None = None) -> str:
    """프롬프트에 넣을 '지금까지의 이야기'를 만든다.

    [줄거리 요약] + [최근 RECENT_TURN_LIMIT턴 원문] 두 층으로 쌓는다.
    예전에는 최근 40턴을 통째로 넣었는데, 그러면 (1) 40턴을 넘어간 초반 설정이
    그냥 사라져서 이야기가 중구난방이 되고 (2) 프롬프트가 길어져 생성도 느려졌다.
    요약이 앞부분을 대신하므로 원문은 최근 몇 턴만 넣으면 된다.

    turns: [{"writer": "규민", "user_line": "...", "ai_text": "...", "is_skipped": False}, ...]
    synopsis: editor_pass()가 매 바퀴 갱신해 DB에 저장해둔 압축 요약.
    """
    written = [t for t in turns if not t["is_skipped"] and t.get("ai_text")]
    recent = written[-RECENT_TURN_LIMIT:]
    blocks = [f"[{t['writer']}의 한 줄] {t['user_line']}\n{t['ai_text']}" for t in recent]
    body = "\n\n".join(blocks) if blocks else "(아직 아무것도 쓰이지 않았다)"

    if not synopsis:
        return body

    # 요약이 덮지 못한 최근 구간만 원문으로 붙인다는 걸 LLM에게 명시한다.
    omitted = len(written) - len(recent)
    note = f"(앞의 {omitted}턴은 위 줄거리로 갈음한다)\n\n" if omitted > 0 else ""
    return f"[여기까지의 줄거리]\n{synopsis}\n\n[최근 장면 원문]\n{note}{body}"


# ------------------------------------------------------------------ 3막 구조
def split_acts(max_rounds: int) -> list[dict]:
    """총 바퀴 수를 ACT_RATIOS 비율로 초반/중반/후반에 나눈다. (LLM 호출 없음)

    반환: [{"name": "초반", "from": 1, "to": 6}, ...]
    바퀴 수가 적어도(예: 3바퀴) 각 막이 최소 1바퀴는 갖도록 보정한다.
    """
    max_rounds = max(1, int(max_rounds))
    if max_rounds < len(ACT_RATIOS):
        # 3바퀴 미만이면 막을 나눌 수 없다. 전부 한 막으로 친다.
        return [{"name": "전체", "from": 1, "to": max_rounds}]

    bounds, used = [], 0
    for i, ratio in enumerate(ACT_RATIOS):
        if i == len(ACT_RATIOS) - 1:
            size = max_rounds - used
        else:
            # 남은 막들이 최소 1바퀴씩은 가져갈 수 있도록 상한을 둔다.
            remaining_acts = len(ACT_RATIOS) - i - 1
            size = max(1, min(math.ceil(max_rounds * ratio),
                              max_rounds - used - remaining_acts))
        bounds.append({"name": ACT_NAMES[i], "from": used + 1, "to": used + size})
        used += size
    return bounds


def act_for_round(arc_plan: dict | None, round_number: int, max_rounds: int) -> dict:
    """현재 바퀴가 속한 막과 그 막의 목표를 돌려준다. (LLM 호출 없음)

    반환: {"name", "from", "to", "goal", "index", "total", "rounds_left_in_act"}
    arc_plan이 없거나 깨졌으면 목표 없이 구간 정보만 채워서 돌려준다.
    """
    acts = (arc_plan or {}).get("acts")
    if not isinstance(acts, list) or not acts:
        acts = split_acts(max_rounds)

    chosen, index = acts[-1], len(acts) - 1
    for i, act in enumerate(acts):
        try:
            if int(act["from"]) <= round_number <= int(act["to"]):
                chosen, index = act, i
                break
        except (KeyError, TypeError, ValueError):
            continue

    to_round = int(chosen.get("to", max_rounds) or max_rounds)
    return {
        "name": str(chosen.get("name") or "전개"),
        "from": int(chosen.get("from", 1) or 1),
        "to": to_round,
        "goal": str(chosen.get("goal") or ""),
        "index": index,
        "total": len(acts),
        "rounds_left_in_act": max(0, to_round - round_number + 1),
    }


# ------------------------------------------------------------------ 등장인물 시트
# character_sheet의 값은 두 가지 형태를 모두 허용한다.
#  - 신형: {"gender": "female", "age": "17", "appearance": "...", "personality": "..."}
#  - 구형: "teenage girl, short black hair"   (예전 DB에 남아 있는 문자열)
# 아래 두 헬퍼가 형태 차이를 흡수하므로, 나머지 코드는 형태를 신경 쓰지 않는다.
_GENDER_KO = {"male": "남성", "female": "여성", "nonbinary": "논바이너리"}


def sheet_appearance(entry) -> str:
    """이미지 프롬프트에 넣을 영어 외모 묘사. 성별/나이도 앞에 붙여 고정한다."""
    if isinstance(entry, str):
        return entry
    if not isinstance(entry, dict):
        return ""
    parts = [str(entry.get(k) or "").strip()
             for k in ("gender", "age", "appearance")]
    return ", ".join(p for p in parts if p)


def needs_appearance(entry) -> bool:
    """외모 묘사가 아직 안 정해진 인물인지. (방장이 이름만 지정한 경우 등)

    이런 인물은 generate_round_art가 첫 삽화를 그릴 때 묘사를 받아 채워 넣는다.
    비워둔 채로 그리면 매 바퀴 딴사람이 나온다.
    """
    if isinstance(entry, str):
        return not entry.strip()
    return isinstance(entry, dict) and not str(entry.get("appearance") or "").strip()


def sheet_profile_ko(name: str, entry) -> str:
    """본문 생성 프롬프트에 넣을 한국어 인물 한 줄."""
    if isinstance(entry, str):
        return f"- {name}: {entry}"
    if not isinstance(entry, dict):
        return f"- {name}"
    gender = _GENDER_KO.get(str(entry.get("gender") or "").lower(), entry.get("gender") or "")
    bits = [str(b).strip() for b in (gender, entry.get("age"), entry.get("role"),
                                     entry.get("personality")) if str(b or "").strip()]
    return f"- {name}: " + " / ".join(bits) if bits else f"- {name}"


def hero_of(sheet: dict | None) -> str:
    """시트에서 주인공 이름을 찾는다. 없으면 빈 문자열."""
    for name, entry in (sheet or {}).items():
        if isinstance(entry, dict) and "주인공" in str(entry.get("role") or ""):
            return name
    return ""


def cast_block_ko(sheet: dict | None) -> str:
    """등장인물 전체를 한국어 블록으로. 본문 프롬프트에 그대로 붙인다."""
    if not sheet:
        return ""
    lines = [sheet_profile_ko(name, entry) for name, entry in sheet.items()]
    hero = hero_of(sheet)
    hero_line = (f"\n이야기의 주인공은 {hero}다. 시점과 무게중심을 {hero}에게 둔다."
                 if hero else "")
    return ("\n\n[등장인물] 이 설정을 절대 바꾸지 마라. 성별과 이름을 헷갈리지 마라.\n"
            + "\n".join(lines) + hero_line)


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


def _act_note(act: dict | None, round_number: int | None, max_rounds: int | None) -> str:
    """지금이 이야기의 어디쯤인지, 이 막에서 무엇을 해야 하는지 알려주는 문구.

    이게 없으면 LLM은 매 턴 '지금 막 시작한 이야기'처럼 써서, 언제 끝날지 모르게
    한없이 뻗어나간다. 전체 길이(총 바퀴) 대비 현재 위치를 매 턴 알려주는 게 핵심이다.
    """
    if not act:
        return ""
    progress = ""
    if round_number and max_rounds:
        progress = f" (전체 {max_rounds}바퀴 중 {round_number}바퀴째)"

    lines = [
        f"\n\n[현재 위치] 지금은 이야기의 '{act['name']}'부다"
        f"{progress}. 이 막은 {act['from']}~{act['to']}바퀴이고 "
        f"{act['rounds_left_in_act']}바퀴 남았다."
    ]
    if act.get("goal"):
        lines.append(f"[이 막에서 할 일] {act['goal']}")

    # 막마다 무게중심을 다르게 준다. 이 3줄이 '중구난방'을 막는 실제 장치다.
    guidance = {
        0: "아직 초반이다. 인물과 배경, 앞으로 풀어갈 문제를 자연스럽게 깔아라. "
           "아직 큰 사건을 터뜨리거나 결말로 달려가지 마라.",
        1: "중반이다. 초반에 깔아둔 문제를 실제 갈등으로 키우고 상황을 악화시켜라. "
           "새 설정을 늘리기보다 이미 나온 것들을 부딪히게 만들어라.",
        2: "후반이다. 새 인물이나 새 떡밥을 절대 추가하지 마라. "
           "이미 나온 갈등을 결말로 수렴시켜라.",
    }.get(act.get("index"))
    if guidance:
        lines.append(guidance)
    return "\n".join(lines)


# ------------------------------------------------------------------ 공개 함수
def _person_lines(person: dict, label: str) -> str:
    """방장이 지정한 인물 하나를 프롬프트 지시문 몇 줄로 바꾼다."""
    given = []
    if person.get("name"):
        given.append(f'이름은 반드시 "{person["name"]}"로 한다. 다른 이름으로 바꾸지 마라.')
    if person.get("gender"):
        given.append(f'성별은 반드시 "{person["gender"]}"다. appearance도 이 성별로 쓴다.')
    if person.get("traits"):
        given.append(
            f'다음 특징을 반드시 반영한다: {person["traits"]}\n'
            "  이 중 겉모습으로 드러나는 것(키, 체격, 머리, 표정, 옷차림 등)은 "
            "appearance에 영어로 옮기고, 성격은 personality에 넣는다. "
            '(예: "키가 작고" -> appearance에 "short"; "잘 웃는" -> appearance에 '
            '"a bright grin")'
        )
    if not given:
        return ""
    return f"\n[{label}] " + "\n".join(f"- {g}" for g in given)


def _cast_note(hero: dict | None, characters: list[dict] | None) -> str:
    """방장이 직접 정한 주인공/등장인물을 통째로 지시문으로 만든다."""
    blocks = []
    if hero and any(hero.get(k) for k in ("name", "gender", "traits")):
        blocks.append(_person_lines(hero, '주인공 지정 (role을 "주인공"으로)'))
    for i, person in enumerate(characters or [], start=1):
        if any(person.get(k) for k in ("name", "gender", "traits")):
            blocks.append(_person_lines(person, f'등장인물 지정 {i} (role은 "주인공"이 아님)'))
    blocks = [b for b in blocks if b]
    if not blocks:
        return ""
    return ("\n\n[방장이 직접 정한 인물] 아래 인물은 이름·성별이 이미 확정이다. "
            "바꾸지 말고 그대로 cast에 넣어라. 외모(appearance)만 새로 채운다."
            + "".join(blocks))


def _normalize_cast(cast: dict, hero: dict | None) -> dict:
    """주인공이 정확히 한 명 있도록 보정한다.

    LLM이 role을 비우거나 전원을 '주인공'이라고 적는 경우가 있어서, 저장 전에
    코드가 확정한다. 방장이 이름을 지정했으면 그 인물이 무조건 주인공이다.
    """
    if not cast:
        return cast

    hero_name = (hero or {}).get("name") or ""
    target = None
    if hero_name and hero_name in cast:
        target = hero_name
    else:
        # LLM이 주인공이라고 표시한 첫 번째 인물, 없으면 그냥 첫 번째 인물.
        target = next((n for n, e in cast.items()
                       if isinstance(e, dict) and "주인공" in str(e.get("role") or "")), None)
        target = target or next(iter(cast))

    for name, entry in cast.items():
        if not isinstance(entry, dict):
            continue
        if name == target:
            entry["role"] = "주인공"
        elif "주인공" in str(entry.get("role") or ""):
            entry["role"] = "주요 인물"   # 주인공은 한 명뿐
    return cast


def _seed_cast(hero: dict | None, characters: list[dict] | None) -> dict:
    """방장이 직접 정한 인물들로 시트의 뼈대를 만든다. (LLM 호출 전)

    외모(appearance)는 비워둔다. LLM이 채우거나, 못 채우면 첫 삽화에서 채워진다.
    """
    seeded = {}
    if (hero or {}).get("name"):
        seeded[hero["name"][:20]] = {
            "gender": (hero.get("gender") or "")[:12],
            "age": "", "role": "주인공",
            "personality": (hero.get("traits") or "")[:80],
            "appearance": "",
        }
    for person in (characters or []):
        name = str(person.get("name") or "").strip()[:20]
        if not name or name in seeded:
            continue
        seeded[name] = {
            "gender": str(person.get("gender") or "").strip().lower()[:12],
            "age": "", "role": "등장인물",
            "personality": str(person.get("traits") or "").strip()[:80],
            "appearance": "",
        }
    return seeded


def plan_story(genre: str, max_rounds: int, opening: str = "",
               member_count: int = 2, hero: dict | None = None,
               characters: list[dict] | None = None) -> dict:
    """방을 시작할 때 딱 1회 호출한다. 이야기의 뼈대와 등장인물을 한 번에 정한다.

    반환: {"premise": str, "acts": [...], "cast": {이름: {gender, age, role,
           appearance, personality}}}

    acts는 split_acts()가 계산한 바퀴 구간에 LLM이 목표(goal)만 채워 넣는 구조다.
    구간 계산까지 LLM에게 맡기면 숫자를 자주 틀려서, 구간은 코드가 정하고
    LLM은 "그 구간에 무슨 일이 일어나야 하는가"만 쓰게 했다.

    cast의 외모(appearance)는 영어로 받는다. 그대로 이미지 프롬프트에 들어가
    매 바퀴 삽화에서 같은 인물이 같은 모습으로 나오게 하는 값이기 때문이다.
    실패해도 이야기는 시작돼야 하므로, 실패 시 구간만 채운 기본 계획을 돌려준다.
    """
    skeleton = split_acts(max_rounds)

    # 설계가 실패해도 방장이 직접 정한 인물들은 살려서 시작한다.
    seeded = _seed_cast(hero, characters)
    fallback = {"premise": "", "acts": skeleton, "cast": _normalize_cast(dict(seeded), hero)}

    if USE_MOCK_AI:
        mock_cast = dict(seeded)
        if not mock_cast:
            mock_cast = {
                "하린": {"gender": "female", "age": "17", "role": "주인공",
                         "appearance": "a quiet teenage girl with long black hair, "
                                       "in a navy school uniform",
                         "personality": "조용하지만 고집이 세다 [목 응답]"},
                "도현": {"gender": "male", "age": "17", "role": "친구",
                         "appearance": "a cheerful teenage boy with short cropped hair "
                                       "and round glasses, in a grey hoodie",
                         "personality": "말이 많고 눈치가 빠르다 [목 응답]"},
            }
        return {
            "premise": f"{genre} 이야기 [목 응답]",
            "acts": [{**a, "goal": f"{a['name']}부 전개 [목 응답]"} for a in skeleton],
            "cast": _normalize_cast(mock_cast, hero),
        }

    act_lines = "\n".join(
        f'- {a["name"]}: {a["from"]}~{a["to"]}바퀴' for a in skeleton
    )
    opening_note = f"\n방장이 적어둔 첫 상황: {opening.strip()}" if opening and opening.strip() else ""

    with _timed("plan_story"):
        data = _generate_json(
            "너는 이야기의 뼈대를 잡는 기획자다. 친구 여러 명이 한 줄씩 번갈아 쓰는 "
            "릴레이 소설을 시작하려 한다. 아래 조건으로 전체 설계를 짜라.\n"
            f"- 장르: {genre}\n"
            f"- 참여자 {member_count}명이 번갈아 쓰며, 총 {max_rounds}바퀴로 끝난다.\n"
            f"- 막 구간은 이미 정해져 있다. 구간 숫자는 바꾸지 말고 goal만 채워라.\n{act_lines}"
            f"{opening_note}\n\n"
            f"{_cast_note(hero, characters)}\n\n"
            "요구사항\n"
            f"- cast: 등장인물 {CAST_MIN}~{CAST_MAX}명을 미리 정한다. 한국어 이름.\n"
            "  위에 방장이 지정한 인물이 있으면 전부 포함한 뒤, 모자라면 새로 추가한다.\n"
            '  반드시 정확히 한 명만 role을 "주인공"으로 하고, 나머지는 다른 역할을 준다.\n'
            "  각 인물은 gender(male/female 중 하나), age(숫자 또는 '17' 같은 문자열), "
            "role(한국어, 예: 주인공/친구/선배), personality(한국어 1문장), "
            "appearance(영어)를 반드시 채운다.\n"
            "  appearance 규칙 — 이 값은 그림 모델에 그대로 들어가므로 정확히 지켜라:\n"
            "   * 'a'로 시작하는 영어 명사구 하나, 20단어 이내. 쉼표로 단어만 나열하지 말고 "
            "자연스러운 구로 쓴다.\n"
            "   * 성별이 드러나는 명사(a young man / a young woman / a student 등)로 시작한다.\n"
            "   * 나이는 쓰지 마라. 'teenage', '17-year-old' 같은 표현을 넣지 않는다. "
            "그림에 보탬이 안 되면서 이미지 생성이 거절당하는 원인이 된다.\n"
            "   * 머리 모양과 머리색을 정확히 하나, 옷차림을 정확히 하나 넣는다. "
            "인물마다 머리색을 서로 다르게 해서 그림에서 구분되게 한다.\n"
            "   * 키·체격 같은 신체 특징이 지정돼 있으면 반드시 넣는다(short, tall, small build 등).\n"
            "   * 성격을 넣지 마라. 성격은 personality에만 쓴다.\n"
            '   * 예: "a short teenage boy with messy brown hair and a wide grin, '
            'in an oversized grey hoodie"\n'
            "- premise: 이야기 한 줄 요약(한국어 60자 이내).\n"
            "- acts[].goal: 그 막에서 일어나야 할 일(한국어 40자 이내). "
            "초반은 깔기, 중반은 갈등 심화, 후반은 수렴으로 잡는다.\n\n"
            '{"premise": "...", "acts": [{"name": "초반", "from": 1, "to": 6, "goal": "..."}], '
            '"cast": {"이름": {"gender": "...", "age": "...", "role": "...", '
            '"personality": "...", "appearance": "..."}}} '
            "형식의 JSON만 출력한다. 다른 말은 쓰지 않는다.",
            label="plan",
        )

    if not data:
        print("[ai:plan] 이야기 설계 생성 실패 → 구간만 채운 기본 계획으로 시작합니다.")
        return fallback

    # 구간(from/to)은 코드가 정한 값을 그대로 쓰고, LLM에게서는 goal만 받는다.
    goals = {}
    for raw in (data.get("acts") or []):
        if isinstance(raw, dict) and raw.get("name"):
            goals[str(raw["name"]).strip()] = str(raw.get("goal") or "").strip()[:80]
    acts = [{**a, "goal": goals.get(a["name"], "")} for a in skeleton]

    cast = {}
    for name, raw in (data.get("cast") or {}).items():
        name = str(name).strip()
        if not name or not isinstance(raw, dict):
            continue
        cast[name[:20]] = {
            "gender": str(raw.get("gender") or "").strip().lower()[:12],
            "age": str(raw.get("age") or "").strip()[:12],
            "role": str(raw.get("role") or "").strip()[:20],
            "personality": str(raw.get("personality") or "").strip()[:80],
            "appearance": str(raw.get("appearance") or "").strip()[:200],
        }
        if len(cast) >= CAST_MAX:
            break

    # 방장이 지정한 인물을 LLM이 빼먹었거나 다른 이름으로 바꿨으면 강제로 되돌린다.
    # 지정한 이름·성별은 언제나 LLM 값보다 우선한다.
    for name, seed_entry in seeded.items():
        if name not in cast:
            cast[name] = dict(seed_entry)
        if seed_entry["gender"]:
            cast[name]["gender"] = seed_entry["gender"]
        if seed_entry["personality"] and not cast[name].get("personality"):
            cast[name]["personality"] = seed_entry["personality"]

    return {"premise": str(data.get("premise") or "")[:120], "acts": acts,
            "cast": _normalize_cast(cast, hero)}
def continue_story(genre: str, context: str, user_line: str, writer: str,
                    remaining_rounds: int | None = None, act: dict | None = None,
                    character_sheet: dict | None = None,
                    round_number: int | None = None,
                    max_rounds: int | None = None,
                    premise: str = "") -> dict:
    """친구의 한 줄을 받아 {"polished_line": str, "text": str} 를 돌려준다.

    JSON 대신 태그 형식을 쓴다. 소설 본문에 따옴표와 줄바꿈이 섞여 있어
    JSON으로 받으면 파싱이 자주 깨지기 때문이다.

    remaining_rounds: 이번 턴이 속한 바퀴부터 끝까지 남은 바퀴 수(포함).
    act: act_for_round()가 계산한 현재 막 정보. 이 막에서 뭘 해야 하는지 주입한다.
    character_sheet: 시작 때 정해둔 등장인물. 성별/성격이 매 턴 흔들리지 않게 한다.
    """
    user_line = user_line.strip()
    if USE_MOCK_AI:
        act_name = (act or {}).get("name", "")
        return {
            "polished_line": user_line,
            "text": f"{user_line} 아무도 먼저 움직이지 않았다. "
                    f"[목 응답{('/' + act_name + '부') if act_name else ''}: "
                    "GEMINI_API_KEY를 설정하면 실제 생성됩니다]",
        }

    premise_note = f"\n이야기 한 줄 요약: {premise}" if premise else ""

    try:
        with _timed("continue_story"):
            raw = _generate_text(
                f"{STYLE_RULES}"
                f"{cast_block_ko(character_sheet)}"
                f"{_act_note(act, round_number, max_rounds)}"
                f"{_wrap_up_note(remaining_rounds)}\n\n"
                f"장르: {genre}{premise_note}\n\n"
                f"[지금까지의 이야기]\n{context}\n\n"
                f"[{writer}가 방금 던진 한 줄]\n{user_line}\n\n"
                "위 한 줄을 다듬고, 그 사건을 실제로 일어나게 해서 본문을 이어라."
            )
    except Exception as e:
        # 삽화 생성과 같은 원칙: AI 호출 실패(할당량 소진 포함)가 턴 제출 자체를
        # 막으면 안 된다. 다듬기 없이 원문을 그대로 쓰고, 짧은 연결 문장으로 이어
        # 다음 사람이 계속 쓸 수 있게 한다. 조용히 넘어가지 않고 로그를 남긴다.
        print(f"[ai:continue] 텍스트 생성 실패, 이어쓰기 없이 진행: {type(e).__name__}: {e}")
        metrics.log_event("text_generate", outcome="fallback", reason=type(e).__name__)
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

    metrics.log_event("text_generate", outcome="success")
    return {"polished_line": polished.strip()[:200], "text": text.strip()}


def editor_pass(genre: str, context: str, round_number: int, max_rounds: int,
                 open_threads: list[str], synopsis: str = "",
                 act: dict | None = None) -> dict:
    """바퀴가 끝날 때마다 1회. 편집자 역할을 '한 번의 호출'로 몰아서 처리한다.

    반환: {"synopsis": str, "threads": [str], "should_end": bool, "reason": str}

    예전에는 요약·떡밥갱신·완결추천을 각각 따로 호출해서 바퀴마다 텍스트 왕복이
    3번 났다. 셋 다 "지금까지의 내용을 읽고 판단한다"는 같은 입력을 쓰므로
    한 번에 받는 게 맞다. 왕복이 3→1로 줄어 바퀴 후처리가 눈에 띄게 빨라진다.

    synopsis는 '누적 갱신'이다. 이전 요약을 입력으로 주고 최근 전개를 반영해
    다시 쓰게 한다. 그래야 컨텍스트 밖으로 밀려난 초반 내용이 계속 살아남는다.
    """
    if USE_MOCK_AI:
        should = round_number >= max(3, max_rounds // 2)
        return {
            "synopsis": (synopsis or "") + f" {round_number}바퀴까지의 줄거리. [목 응답]",
            "threads": open_threads,
            "should_end": should,
            "reason": "갈등이 정리되는 흐름이라 여기서 마무리해도 자연스러워 보여요. [목 응답]"
            if should else "아직 풀리지 않은 떡밥이 남아 있어요. [목 응답]",
        }

    known = "\n".join(f"- {t}" for t in open_threads) or "(아직 없음)"
    prev = synopsis.strip() or "(아직 없음 — 이번이 첫 요약이다)"
    act_note = ""
    if act:
        act_note = (f"\n현재 위치: '{act['name']}'부 ({act['from']}~{act['to']}바퀴)"
                    + (f" / 이 막의 목표: {act['goal']}" if act.get("goal") else ""))

    with _timed("editor_pass", round=round_number):
        data = _generate_json(
            "너는 이 릴레이 소설의 편집자다. 아래 세 가지를 한 번에 처리해라.\n\n"
            "1) synopsis — 이야기 전체 줄거리를 다시 쓴다.\n"
            "   - [이전 요약]에 [최근 전개]를 합쳐 하나의 줄거리로 만든다.\n"
            "   - 이전 요약에만 있는 인물·설정·사건도 반드시 살려서 포함한다. 버리지 마라.\n"
            f"   - 한국어 {SYNOPSIS_MAX_CHARS}자 이내. 시간 순서대로. 평가나 감상은 쓰지 않는다.\n\n"
            "2) threads — 아직 회수되지 않은 떡밥 목록을 갱신한다.\n"
            "   - 최근 전개에서 해소된 건 빼고, 남은 건 문구 그대로 두고, 새로 생긴 건 추가한다.\n"
            "   - 사소한 디테일 말고 나중에 갚아야 할 약속(비밀, 목표, 갈등)만 담는다.\n\n"
            "3) should_end / reason — 지금 완결해도 좋은 지점인지 판단한다.\n"
            "   - 갈등이 해소됐거나 전개가 정체됐으면 true, 아직 뻗어나가는 중이면 false.\n"
            "   - 아직 '후반'부에 들어서지 않았다면 웬만하면 false로 둔다.\n"
            "   - reason은 친구에게 말하듯 1문장, 60자 이내.\n\n"
            f"장르: {genre} / 현재 {round_number}바퀴 (전체 {max_rounds}바퀴){act_note}\n\n"
            f"[이전 요약]\n{prev}\n\n"
            f"[추적 중인 떡밥]\n{known}\n\n"
            f"[최근 전개]\n{context}\n\n"
            '{"synopsis": "...", "threads": ["..."], "should_end": true 또는 false, '
            '"reason": "..."} 형식의 JSON만 출력한다. 다른 말은 쓰지 않는다.',
            label=f"editor{round_number}",
        )

    # 실패해도 기존 값을 그대로 유지한다. 바퀴 후처리가 게임을 막으면 안 된다.
    threads = data.get("threads")
    if not isinstance(threads, list):
        threads = open_threads
    new_synopsis = str(data.get("synopsis") or "").strip()[:SYNOPSIS_MAX_CHARS]

    return {
        "synopsis": new_synopsis or synopsis,
        "threads": [str(t).strip()[:120] for t in threads if str(t).strip()][:20],
        "should_end": bool(data.get("should_end")),
        "reason": str(data.get("reason", "")),
    }


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

    with _timed("write_epilogue"):
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


# 모든 삽화에 똑같이 붙는 화풍. 20장이 한 권처럼 보이게 하는 장치다.
#
# "no text, no letters, no watermark"를 뺐다. FLUX는 부정문을 처리하지 못하고,
# 오히려 언급된 대상을 그리는 쪽으로 끌린다 (실제로 워터마크가 찍혀 나왔다).
# 지우고 싶은 것은 적지 않는 것이 맞다.
ART_STYLE = ("Soft ink and watercolor illustration in a muted violet and indigo palette, "
             "gentle paper grain and cinematic lighting.")

# 한 장에 넣을 인물 수 상한. 확산 모델은 인물이 늘수록 속성이 서로 섞인다
# (금발이 파란 머리가 되고, 갈색 단발이 주황 장발이 되는 현상). 3명을 넘기지 않는다.
MAX_SUBJECTS_PER_IMAGE = 3

# 위치 단서. 인물마다 화면상 자리를 지정해주면 속성 섞임이 크게 줄어든다.
_POSITIONS = ("On the left", "In the center", "On the right")


# 이미지 프롬프트에서 나이 표현을 걷어내는 치환 규칙.
#
# 나이 토큰("teenage", "17-year-old")은 그림에 보태는 정보가 거의 없으면서
# 벤더의 안전 분류기를 자주 건드린다. 실제로 교실 장면 프롬프트가 통째로
# 거절당했다. 인물 구분에 쓰이는 건 머리 모양·머리색·옷차림이므로, 나이는
# 빼고 그 정보만 남긴다. (분류기가 그래도 막으면 플레이스홀더로 넘어간다)
_AGE_REWRITES = (
    (re.compile(r"\b(?:teenaged?|teen)\s+girl\b", re.I), "young woman"),
    (re.compile(r"\b(?:teenaged?|teen)\s+boy\b", re.I), "young man"),
    (re.compile(r"\bschoolgirl\b", re.I), "young woman"),
    (re.compile(r"\bschoolboy\b", re.I), "young man"),
    (re.compile(r"\b\d{1,2}[\s-]?year[\s-]?old\b", re.I), ""),
    (re.compile(r"\b(?:high|middle)[\s-]?school\s+student\b", re.I), "student"),
    (re.compile(r"\b(?:teenaged?|teen)\b", re.I), "young"),
)


def neutralize_age(text: str) -> str:
    """묘사에서 나이 표현을 걷어낸다. 머리·옷차림 같은 시각 정보는 그대로 둔다."""
    for pattern, replacement in _AGE_REWRITES:
        text = pattern.sub(replacement, text)
    return re.sub(r"\s{2,}", " ", text).replace(" ,", ",").strip()


def visual_phrase(entry) -> str:
    """이미지 프롬프트에 넣을 '자연어 명사구' 하나를 만든다.

    예전에는 "female, 17, teenage girl, long black hair"처럼 태그를 쉼표로
    이어붙였다. FLUX는 태그 나열보다 자연어 문장을 훨씬 잘 따르고, 앞에 붙은
    "female, 17,"은 뒤의 묘사와 중복되면서 주의만 분산시킨다.
    그래서 appearance를 그대로 쓰되, 비어 있을 때만 성별/나이로 최소한의
    명사구를 만들어 준다.
    """
    if isinstance(entry, str):
        return entry.strip()
    if not isinstance(entry, dict):
        return ""

    appearance = str(entry.get("appearance") or "").strip()
    if appearance:
        return neutralize_age(appearance)

    gender = str(entry.get("gender") or "").strip().lower()
    noun = {"female": "young woman", "male": "young man"}.get(gender, "person")
    return f"a {noun}"


def _build_image_prompt(scene: str, cast: list[str], sheet: dict) -> str:
    """장면 + 인물을 하나의 자연어 프롬프트로 조립한다.

    이전 구조의 문제를 전부 고친 버전이다.
      1) 한국어 이름을 넣지 않는다. FLUX의 텍스트 인코더는 한글을 제대로 못 읽고,
         이름 토큰이 주의를 잡아먹으며, 화면에 글자를 그리려는 경향까지 생긴다.
      2) "keep consistent", "do not change gender" 같은 지시문을 넣지 않는다.
         그림으로 그릴 수 없는 문장이고, 부정문은 역효과만 난다.
      3) 인물마다 화면상 위치를 지정한다(왼쪽/가운데/오른쪽). 다인물 이미지에서
         머리색·옷차림이 서로 뒤섞이는 것을 막는 가장 효과적인 방법이다.
      4) 인물 수를 MAX_SUBJECTS_PER_IMAGE로 자른다.
      5) 화풍을 맨 앞에 짧게 두고 세부는 뒤에 둔다. FLUX는 앞쪽 토큰에
         더 큰 가중치를 주므로, 매 장 유지돼야 하는 화풍이 앞에 오는 게 맞다.
    """
    phrases = []
    for name in cast[:MAX_SUBJECTS_PER_IMAGE]:
        phrase = visual_phrase(sheet.get(name))
        if phrase:
            phrases.append(phrase)

    parts = ["A soft ink-and-watercolor illustration."]
    if scene:
        parts.append(neutralize_age(scene.strip().rstrip(".")) + ".")

    if len(phrases) == 1:
        parts.append(f"The figure is {phrases[0]}.")
    elif phrases:
        # 두 명이면 왼쪽/오른쪽, 세 명이면 왼쪽/가운데/오른쪽으로 배치한다.
        slots = (_POSITIONS[0], _POSITIONS[2]) if len(phrases) == 2 else _POSITIONS
        parts.append(" ".join(f"{slot}, {phrase}."
                              for slot, phrase in zip(slots, phrases)))

    parts.append(ART_STYLE)
    # 프롬프트 상한은 2048자다. 넘치면 요청이 거절되므로 여기서도 여유를 둔다.
    return " ".join(parts)[:1900]


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


def seed_for(story_id: str, round_number: int) -> int:
    """(이야기, 바퀴)마다 고정된 시드. 같은 입력이면 같은 그림이 나온다.

    무작위 시드를 쓰면 같은 프롬프트로도 매번 다른 그림이 나와서, 프롬프트를
    고쳤을 때 좋아진 건지 운이 좋았던 건지 구분할 수 없다. 고정해두면 프롬프트
    변경의 효과를 실제로 비교할 수 있다.
    """
    digest = hashlib.md5(f"{story_id}:{round_number}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def generate_round_art(genre: str, context: str, round_number: int,
                       character_sheet: dict | None = None,
                       story_id: str = "") -> dict:
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
    #
    # 시작할 때 plan_story()가 이미 인물을 정해뒀으므로(성별 포함), 여기서는
    # 새 인물이 튀어나왔을 때만 추가로 정의하게 한다. 기존 인물의 외모를 다시
    # 지어내면 바퀴마다 얼굴과 성별이 바뀐다.
    # 외모가 이미 확정된 인물만 '건드리지 말 것' 목록에 넣는다. 방장이 이름만
    # 지정해서 외모가 빈 인물은 여기서 한 번 받아 채운다(그 뒤로는 고정된다).
    fixed = {k: v for k, v in sheet.items() if not needs_appearance(v)}
    pending = [k for k, v in sheet.items() if needs_appearance(v)]
    known = ", ".join(f"{k}=({sheet_appearance(v)})" for k, v in fixed.items()) or "(아직 없음)"
    pending_note = ""
    if pending:
        hints = []
        for name in pending:
            entry = sheet.get(name)
            gender = entry.get("gender") if isinstance(entry, dict) else ""
            hints.append(f"{name}({gender or '성별 미정'})")
        pending_note = ("\n[외모를 정해야 하는 인물] 아래 인물의 영어 외모 묘사를 "
                        "characters에 반드시 포함해라. 괄호 안 성별을 지켜라: "
                        + ", ".join(hints))
    with _timed("art_meta", round=round_number):
        meta = _generate_json(
            "소설 장면을 삽화로 그리기 위한 영어 프롬프트 재료를 만든다.\n"
            "- scene: 장소·시간대·분위기와 그 순간의 동작을 영어 1~2문장으로.\n"
            "  **인물의 이름을 절대 쓰지 마라.** 이름 대신 동작과 배경만 묘사한다.\n"
            "  (좋은 예: \"In a sunlit classroom after school, students gather around "
            "spilled pasta on the floor.\")\n"
            "- cast: 그 장면에 실제로 보이는 인물 이름 배열. 한국어 이름 그대로 쓰고, "
            f"중요한 순서대로 최대 {MAX_SUBJECTS_PER_IMAGE}명까지만. 주인공이 있으면 맨 앞에 둔다.\n"
            "- characters: **새로 등장한 인물만** 영어 외모를 추가한다. "
            "아래 [이미 정해진 인물]에 있는 이름은 characters에 넣지 마라. "
            "그 인물들의 외모와 성별은 이미 확정돼 있고 바꿀 수 없다.\n"
            "  형식은 'a'로 시작하는 영어 명사구 한 개, 20단어 이내로, "
            "머리 모양·머리색 하나와 옷차림 하나를 반드시 포함한다. "
            "나이 표현('teenage', '17-year-old')은 쓰지 않는다.\n"
            "  (예: \"a tall young man with messy black hair and round glasses, "
            "in a navy school uniform\")\n"
            "- caption: 한국어 장면 설명 20자 이내\n"
            f"[이미 정해진 인물] {known}{pending_note}\n\n"
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
            if not desc:
                continue
            existing = sheet.get(name)
            if name not in sheet:
                sheet[name] = str(desc)                 # 새로 등장한 인물
            elif needs_appearance(existing):
                # 이름만 정해져 있던 인물의 외모를 이번에 확정한다 (성별은 유지).
                if isinstance(existing, dict):
                    existing["appearance"] = str(desc)
                else:
                    sheet[name] = str(desc)
            # 외모가 이미 확정된 인물은 덮어쓰지 않는다. 바뀌면 일관성이 깨지므로.

        # 조사가 붙거나 표기가 살짝 다른 이름이 기존 인물과 매칭되지 않으면
        # 그 인물은 고정 외모 없이 그려져 매번 딴사람처럼 나온다. cast 목록을
        # sheet 키에 맞춰 정규화하고, LLM이 cast에 넣는 걸 깜빡했더라도 최근
        # 맥락에 이름이 그대로 언급된 기존 인물이면 강제로 포함시킨다.
        context_tail = context[-2000:]
        mentioned = [name for name in sheet if name and name in context_tail]
        cast = list(dict.fromkeys(_match_known_names(raw_cast, sheet) + mentioned))

        # 주인공이 이 장면에 있으면 맨 앞(가장 큰 주의를 받는 자리)에 둔다.
        hero = hero_of(sheet)
        if hero in cast:
            cast = [hero] + [n for n in cast if n != hero]
        # 인원이 많을수록 머리색·옷차림이 서로 섞인다. 중요한 순서대로 잘라낸다.
        cast = cast[:MAX_SUBJECTS_PER_IMAGE]

    if not scene:
        # 1단계가 실패해도 그림은 나오게 한다. 장면 없는 분위기 컷으로 대체.
        print(f"[art] {round_number}바퀴 장면 묘사 생성 실패 → 기본 프롬프트로 대체")
        scene = (f"An atmospheric establishing shot for chapter {round_number} of a Korean "
                 f"{genre} story. Empty space, quiet mood, no people's faces.")

    # --- 2단계: 실제 이미지 생성 ---
    try:
        prompt = _build_image_prompt(scene, cast, sheet)
        print(f"[art] {round_number}바퀴 프롬프트: {prompt[:200]}")
        with _timed("image_generate", round=round_number, vendor=IMAGE_PROVIDER):
            url = _generate_image(prompt, seed_for(story_id, round_number) if story_id else None)
    except urllib.error.HTTPError as e:
        body = _http_body(e)[:300] or "(응답 본문 없음)"
        print(f"[art] {round_number}바퀴 이미지 생성 실패 (HTTP {e.code}): {body}")
        metrics.log_event("image_generate", vendor=IMAGE_PROVIDER, outcome="placeholder",
                           reason=f"HTTP {e.code}", round=round_number)
        return {**fallback, "character_sheet": sheet}
    except Exception as e:
        print(f"[art] {round_number}바퀴 이미지 생성 실패: {type(e).__name__}: {e}")
        metrics.log_event("image_generate", vendor=IMAGE_PROVIDER, outcome="placeholder",
                           reason=type(e).__name__, round=round_number)
        return {**fallback, "character_sheet": sheet}

    if not url:
        print(f"[art] {round_number}바퀴 응답에 이미지가 없습니다.")
        metrics.log_event("image_generate", vendor=IMAGE_PROVIDER, outcome="placeholder",
                           reason="empty_response", round=round_number)
        return {**fallback, "character_sheet": sheet}

    metrics.log_event("image_generate", vendor=IMAGE_PROVIDER, outcome="success",
                       round=round_number)
    return {"image_url": url, "caption": caption, "character_sheet": sheet}