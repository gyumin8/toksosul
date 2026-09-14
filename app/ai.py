"""
app/ai.py
AI 호출을 전부 이 파일 안에 가둔다. (기획안 6-1 "AI 개입 방식은 교체 가능하게" 원칙)

바깥에서는 continue_story / suggest_ending / write_epilogue / generate_round_art
네 함수만 쓴다. 벤더나 모델을 바꿔도 이 파일만 고치면 된다.

벤더: Google Gemini (google-genai SDK)
 - 텍스트/이미지 모두 client.interactions.create() 로 호출한다.
 - 구 버전 SDK(interactions 미지원)면 자동으로 models.generate_content()로 내려간다.
 - 구조화 출력은 API 기능 대신 "JSON만 출력하라"는 프롬프트 + 관대한 파싱으로 처리한다.
   SDK 버전에 따라 스키마 파라미터 이름이 달라서, 버전 의존성을 줄이는 쪽을 택했다.

GEMINI_API_KEY가 없으면 자동으로 목 응답을 돌려준다. 키 없이도 전체 플로우 테스트 가능.
"""
import base64
import json
import random
import re
import urllib.error
import urllib.request
import uuid

from .config import (
    CF_ACCOUNT_ID, CF_API_TOKEN, CF_IMAGE_MODEL, ENABLE_IMAGE_GEN,
    GEMINI_API_KEY, GEMINI_IMAGE_MODEL, GEMINI_TEXT_MODEL,
    IMAGE_PROVIDER, MEDIA_DIR, USE_MOCK_AI,
)

_client = None
PLACEHOLDER_IMAGE = "/static/placeholder.svg"


def _get_client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


# ------------------------------------------------------------------ 저수준 호출
def _generate_text(prompt: str) -> str:
    """Gemini에 텍스트를 요청하고 문자열을 돌려준다."""
    client = _get_client()

    # 신형 Interactions API 우선
    if hasattr(client, "interactions"):
        interaction = client.interactions.create(model=GEMINI_TEXT_MODEL, input=prompt)
        text = getattr(interaction, "output_text", None)
        if text:
            return text.strip()

    # 구형 SDK 폴백
    resp = client.models.generate_content(model=GEMINI_TEXT_MODEL, contents=prompt)
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
    with urllib.request.urlopen(req, timeout=90) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

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

    interaction = client.interactions.create(
        model=GEMINI_IMAGE_MODEL,
        input=prompt,
        response_format={"type": "image", "mime_type": "image/png", "aspect_ratio": "16:9"},
    )
    image = getattr(interaction, "output_image", None)
    if not image or not getattr(image, "data", None):
        return None
    return _save_image(image.data, "png")


def _generate_image(prompt: str) -> str | None:
    """설정된 벤더로 이미지를 생성한다. 실패하면 None."""
    if IMAGE_PROVIDER == "cloudflare":
        return _image_cloudflare(prompt)
    if IMAGE_PROVIDER == "gemini":
        return _image_gemini(prompt)
    return None


def _image_model_name() -> str:
    return {"cloudflare": CF_IMAGE_MODEL, "gemini": GEMINI_IMAGE_MODEL}.get(IMAGE_PROVIDER, "-")


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


# ------------------------------------------------------------------ 공개 함수
def continue_story(genre: str, context: str, user_line: str, writer: str) -> dict:
    """친구의 한 줄을 받아 {"polished_line": str, "text": str} 를 돌려준다.

    JSON 대신 태그 형식을 쓴다. 소설 본문에 따옴표와 줄바꿈이 섞여 있어
    JSON으로 받으면 파싱이 자주 깨지기 때문이다.
    """
    user_line = user_line.strip()
    if USE_MOCK_AI:
        return {
            "polished_line": user_line,
            "text": f"{user_line} 아무도 먼저 움직이지 않았다. "
                    "[목 응답: GEMINI_API_KEY를 설정하면 실제 생성됩니다]",
        }

    raw = _generate_text(
        f"{STYLE_RULES}\n\n"
        f"장르: {genre}\n\n"
        f"[지금까지의 이야기]\n{context}\n\n"
        f"[{writer}가 방금 던진 한 줄]\n{user_line}\n\n"
        "위 한 줄을 다듬고, 그 사건을 실제로 일어나게 해서 본문을 이어라."
    )

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


def write_epilogue(genre: str, context: str, reason: str) -> dict:
    """완결 처리 시 마지막 단락과 제목을 생성한다."""
    if USE_MOCK_AI:
        return {
            "title": random.choice(["그날의 우리", "끝나지 않은 방", "마지막 한 줄"]),
            "epilogue": "그리고 아무도 그 밤에 대해 다시 말하지 않았다. "
                        "다만 각자의 자리에서, 가끔씩 그 문장을 떠올렸을 뿐이다. [목 응답]",
        }

    data = _generate_json(
        "너는 소설의 마지막 단락을 쓰는 작가다. 지금까지의 내용을 바탕으로 여운 있게 이야기를 닫아라.\n"
        "새 인물이나 새 설정을 등장시키지 않는다.\n"
        '반드시 {"title": "제목 20자 이내", "epilogue": "마지막 단락 400자 이내"} '
        "형식의 JSON만 출력한다. 다른 말은 쓰지 않는다.\n\n"
        f"장르: {genre}\n완결 사유: {reason}\n\n{context}"
    )
    return {
        "title": str(data.get("title") or "제목 없는 이야기")[:120],
        "epilogue": str(data.get("epilogue") or ""),
    }


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
        cast = [str(x) for x in (meta.get("cast") or []) if x]
        caption = str(meta.get("caption") or caption)[:40]
        for name, desc in (meta.get("characters") or {}).items():
            # 이미 있는 인물은 덮어쓰지 않는다. 외모가 바뀌면 일관성이 깨지므로.
            if name not in sheet and desc:
                sheet[name] = str(desc)

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
