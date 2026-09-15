# 톡소설 (toksosul)

친구들이 한 줄씩 던지면 AI가 이어 쓰는 비동기 릴레이 소설 웹서비스. MVP 코드베이스.

## 확정된 규칙

| 항목 | 값 | 구현 위치 |
|---|---|---|
| 인원 | 최소 2명, 최대 10명 | `app/config.py` → `MIN_MEMBERS` / `MAX_MEMBERS` |
| 턴제 | 한 줄 제출 즉시 다음 사람 | `app/turn_logic.py` → `submit_turn` → `_advance` |
| 미작성 | 24시간 경과 시 턴 넘김 (AI 대필 없음) | `app/turn_logic.py` → `sync_deadline` |
| 이미지 생성 | 전체 인원 한 바퀴마다 1장 | `app/turn_logic.py` → `_on_round_complete` |
| 바퀴 제한 | 20바퀴 | `app/config.py` → `HARD_MAX_ROUNDS` |
| 완결 | LLM 추천 / 방장 선언 / 20바퀴 강제 종료 | `suggest_ending`, `/complete`, `_advance` |

## 실행 (VS Code)

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env     # Windows: copy .env.example .env
uvicorn app.main:app --reload
```

- 화면: http://127.0.0.1:8000
- API 문서: http://127.0.0.1:8000/docs

`.env`의 `GEMINI_API_KEY`를 비워두면 **목(mock) 응답 모드**로 돌아간다.
키 없이도 방 생성 → 턴 진행 → 바퀴 삽화 → 완결 → 내보내기 전 과정을 테스트할 수 있다.

### AI 연결 확인

- `http://127.0.0.1:8000/api/ai-check` — 텍스트 호출을 실제로 해보고 성공/실패 사유를 보여준다
- `http://127.0.0.1:8000/api/ai-check?image=1` — 이미지까지 한 장 생성해본다 (느림)

응답의 `text.ok`와 `image.ok`를 각각 보면 어느 쪽이 막혔는지 바로 알 수 있다.

- `http://127.0.0.1:8000/api/quota` — 오늘 벤더별 호출 수/한도. AI를 호출하지
  않으므로 데모 중에 계속 열어둬도 된다. `.env`에 `*_DAILY_LIMIT`을 채워야 한도가
  걸린다(기본값 0 = 무제한, 사용량만 보임).
- `http://127.0.0.1:8000/api/metrics` — 이어쓰기/삽화 성공률, NSFW 오탐률,
  429 재시도 횟수 집계. 이것도 AI를 호출하지 않는다. 발표 자료용 숫자가
  필요할 때 이 값을 그대로 쓰면 된다.

**벤더 구성**

| 용도 | 벤더 | 비용 | 키 |
|---|---|---|---|
| 텍스트 (이어쓰기/완결추천/에필로그) | Google Gemini | 무료 등급 | `GEMINI_API_KEY` |
| 이미지 (바퀴별 삽화/표지) | Cloudflare Workers AI (FLUX.1 schnell) | 무료 일일 할당량 | `CF_ACCOUNT_ID` + `CF_API_TOKEN` |

Gemini 키는 https://aistudio.google.com/apikey 에서,
Cloudflare Account ID는 대시보드 우측에서, API 토큰은 내 프로필 > API Tokens에서 발급한다.
Gemini 이미지 모델(Nano Banana)로 되돌리려면 `.env`에 `IMAGE_PROVIDER=gemini`를 넣으면 되지만,
Gemini 이미지 생성은 유료 등급이 필요하다.
`.env`는 서버 시작 시 한 번만 읽으므로, 바꾼 뒤에는 `Ctrl+C`로 껐다 다시 켜야 한다.

### 혼자 테스트할 때

`.env`에 `MIN_MEMBERS=1`을 두면 1명으로도 시작할 수 있다. 단 **1턴 = 1바퀴**가 되어
턴마다 삽화가 생성되므로, 할당량을 아끼려면 `ENABLE_IMAGE_GEN=0`으로 꺼두는 편이 낫다.
제출 전에는 `MIN_MEMBERS` 줄을 지워 원래 규칙(2명)으로 되돌린다.

### 혼자서 여러 명 테스트하기

닉네임 세션이 `localStorage`에 저장되므로, 브라우저 **일반 창 + 시크릿 창 + 다른 브라우저**를
각각 띄우면 서로 다른 유저로 붙는다. 방장 창에서 '복사' 버튼으로 나온 초대 링크를 다른 창에 붙여넣으면 된다.

### 24시간 대기 없이 턴 넘김 확인하기

방 만들 때 '턴 대기 시간'을 1시간으로 낮추거나, 아래처럼 DB를 직접 건드린다.

```python
from app.db import SessionLocal
from app.models import Story
import app.turn_logic as tl
from datetime import timedelta

db = SessionLocal()
s = db.query(Story).first()
s.deadline_at = tl.utcnow() - timedelta(minutes=1)
db.commit()
```
이후 화면을 새로고침하면 그 턴이 넘김 처리된다.

## 구조

```
app/
  config.py      환경변수 + 규칙 상수 (수치는 전부 여기서만 바꾼다)
  db.py          SQLAlchemy 엔진/세션
  models.py      테이블 6개 (users, rooms, room_members, stories, turns, round_arts)
  schemas.py     요청 바디 검증
  ai.py          Gemini 호출 전담 — 이어쓰기 / 완결 추천 / 에필로그 / 삽화
  turn_logic.py  규칙 엔진 — 라운드로빈, 마감, 바퀴, 완결
  main.py        FastAPI 라우터
static/
  index.html     화면 5개 (닉네임 / 로비 / 대기실 / 집필 / 완결)
  styles.css
  app.js         폴링 기반 상태 갱신, 빌드 도구 없음
```

## 설계 판단 메모

- **마감 처리는 스케줄러 없이 지연 평가한다.** 조회·제출 때마다 `sync_deadline()`이
  마감 지난 턴을 넘김 처리한다. Celery 워커를 따로 띄우지 않아도 되므로 배포와 데모가 단순해진다.
  단점: 아무도 접속하지 않으면 넘김이 기록되지 않는다 (접속하는 순간 한꺼번에 정리됨).
- **인원은 시작 시점에 고정한다** (`stories.member_count`). 집필 중 인원이 바뀌면
  "몇 바퀴째인지" 계산이 어긋나기 때문에, 시작 후 참여를 막았다.
- **표지는 마지막 바퀴 삽화를 재사용한다.** 완결용 이미지를 따로 생성하지 않아 호출 1회를 아낀다.
- **AI 호출은 `ai.py` 바깥으로 새지 않는다.** '전체 히스토리 → 요약 기반' 전환이 필요해지면
  `build_context()` 한 함수만 고치면 된다. 벤더 교체도 이 파일 하나로 끝난다.
- **구조화 출력은 API 기능 대신 프롬프트 + 관대한 파싱으로 처리한다.** SDK 버전마다
  스키마 파라미터가 달라서, 버전 의존성을 줄이는 쪽을 택했다. (`_generate_json`)
- **삽화 생성 실패는 턴을 막지 않는다.** 쿼터 초과나 모델 접근 불가 시 플레이스홀더로
  대체하고 진행한다. 데모 도중 이미지 때문에 500이 나는 상황을 피하기 위함이다.
  다만 조용히 넘어가지 않고 `[art]` 로그를 남긴다. 어느 단계에서 깨졌는지 보이게 하기 위함.
- **인물 외모를 문장으로 고정한다** (`stories.character_sheet`). 이미지 모델이 참조 이미지를
  받지 못해 같은 인물이 매번 딴사람으로 나오는 문제를, 외모 묘사를 누적 저장했다가
  매 삽화에 똑같이 주입하는 방식으로 완화했다. 한 번 정해진 인물은 덮어쓰지 않는다.
- **사용자 원문과 다듬은 문장을 둘 다 보관한다** (`turns.user_line` / `turns.polished_line`).
  맞춤법과 어순만 고치고 내용은 건드리지 않으며, 화면에는 다듬은 쪽을 보여주되
  '다듬음' 표시에 마우스를 올리면 원문이 뜬다. 누가 뭘 썼는지 흐려지지 않게 하기 위함이다.
- **이어쓰기 응답은 JSON이 아니라 태그 형식으로 받는다.** 소설 본문에 따옴표와 줄바꿈이
  섞여 있어 JSON으로 받으면 파싱이 자주 깨진다. `[다듬은줄]` / `[본문]` 태그로 나눠 받는다.
- **모델 이름은 `.env`에 뺐다.** Gemini 모델명은 자주 바뀌므로 404가 나면 코드가 아니라
  `.env`만 고치면 된다.
- **텍스트와 이미지 벤더를 분리했다.** Gemini 이미지 생성은 유료 등급이 필요한 반면
  Cloudflare Workers AI는 무료 할당량이 있어, 이미지만 Cloudflare로 뺐다.
  `IMAGE_PROVIDER` 한 줄로 되돌릴 수 있고, 벤더 선택은 `_generate_image()` 한 곳에서만 갈린다.
- **Cloudflare 호출에 requests를 쓰지 않는다.** 표준 라이브러리 `urllib`로 처리해
  의존성을 늘리지 않았다. 호출이 한 종류뿐이라 굳이 패키지를 추가할 이유가 없다.
- **PDF는 브라우저 인쇄로 처리한다.** 서버 사이드 PDF는 한글 폰트 임베딩 문제가 붙어서,
  MVP 단계에서는 `window.print()` + 인쇄용 CSS로 대체했다. 원고 원본은 `.md`로 내려받는다.
- **후반 바퀴에서는 새 떡밥 대신 회수를 유도한다.** 남은 바퀴가
  `WRAP_UP_FROM_REMAINING_ROUNDS` 이하로 들어오면 `continue_story` 프롬프트에
  "정리 모드" 안내 문구가 자동으로 붙는다. 완결이 억지로 끊기지 않고 자연스럽게
  닫히도록 하기 위함.
- **미해결 떡밥은 별도로 누적 추적한다** (`stories.plot_threads`). `build_context()`가
  최근 턴만 넘기므로, 초반에 등장한 떡밥은 완결 시점엔 이미 컨텍스트 밖으로 밀려나
  있을 수 있다. `character_sheet`와 같은 패턴으로 매 바퀴 `update_plot_threads()`가
  기존 목록 + 최근 전개를 보고 갱신본을 돌려주면 호출부가 저장하고, 완결 때
  `write_epilogue()`에 통째로 넘겨 회수를 유도한다. 이미지 생성 여부와 무관하게
  매 바퀴 돈다 (`ENABLE_IMAGE_GEN=0`이어도 떡밥 추적은 계속돼야 하므로).
- **바퀴 완성 후처리는 백그라운드로 뺀다.** 실측해보니 삽화 생성 + 떡밥 추적 +
  완결 추천(+ 마지막 바퀴는 에필로그까지)이 한 턴 제출 안에서 순차 실행되면
  15~25초가 걸렸다. `continue_story`(본인 턴 응답)만 동기로 즉시 반환하고, 나머지는
  `stories.art_pending_round`에 처리 중인 바퀴 번호만 표시해둔 뒤 FastAPI
  `BackgroundTasks`로 넘긴다(`turn_logic.run_round_completion`). 백그라운드 작업은
  요청 세션이 응답과 함께 닫히므로 자체 `SessionLocal()`을 새로 연다. 프론트는
  `art_pending_round`를 보고 "생성 중" 표시 후 폴링하면 된다.
  마지막 바퀴는 `stories.status`가 `finishing`(전이 상태)을 거쳐 완결 상태로
  넘어간다 — 표지가 마지막 바퀴 삽화를 재사용하는 로직과 순서가 꼬이지 않으려면
  (그 삽화 자체가 아직 백그라운드에서 생성 중일 수 있으므로) 에필로그+표지 생성도
  같은 배경 작업 안에서 처리해야 하기 때문. 예외가 나도 `art_pending_round`는
  반드시 풀리고, `finishing` 중 에필로그 생성이 실패하면 `in_progress`로 되돌려
  게임이 영구히 막히지 않게 한다.
- **Cloudflare NSFW 오탐은 Gemini로 1회만 재시도한다.** 실사용 중 평범한 장면이
  NSFW로 오탐 거부되는 걸 실측으로 확인했다. `_generate_image()`가 Cloudflare의
  HTTPError 본문에서 `nsfw`를 감지하면 같은 프롬프트로 Gemini에 딱 한 번만
  재시도하고(재귀 없음, 무한 재시도 방지), 그래도 안 되면 기존처럼 플레이스홀더로
  폴백한다. 본문을 재시도 판단에 쓰려고 한 번 읽고 나면 스트림이 소진되므로,
  다른 호출부(`generate_round_art` 로그, `ping()` 진단)가 다시 읽을 수 있게
  `e.fp`를 새 `BytesIO`로 되돌려놓는다.
- **성공률/오탐률은 JSONL 이벤트 로그로 뽑는다** (`app/metrics.py`, `/api/metrics`).
  기존 `print(f"[art] ...")` 로그는 사람이 실시간으로 읽기용으로 그대로 두고,
  같은 시점에 같은 사건을 `metrics.jsonl`에 JSON 한 줄로도 남긴다. 이어쓰기
  성공/폴백, 삽화 성공/플레이스홀더(벤더별), NSFW 오탐 발생·Gemini 복구 여부,
  429 재시도 횟수를 각각 기록한다. `/api/metrics`가 파일을 다시 읽어 성공률
  같은 비율을 계산해 내려주므로, 발표 때 "이어쓰기 성공률 OO%, NSFW 오탐률
  OO%" 같은 숫자를 그 자리에서 뽑을 수 있다. `/api/ai-check`(연결 확인용
  진단 호출)는 실제 게임 플레이가 아니므로 집계에서 뺐다 — 안 그러면 헬스체크
  호출이 성공률을 실제보다 부풀린다. `metrics.jsonl`은 `quota_state.json`과
  같은 이유로(런타임 상태) 커밋하지 않는다.
- **순간적인 429는 짧게, 진짜 할당량 소진은 즉시 폴백한다** (`ai._call_with_retry`).
  요청이 몰려 잠깐 거부되는 429/RESOURCE_EXHAUSTED만 1초→2초(+지터) 백오프로
  최대 2회 재시도하고, 그 외 에러(400 등 잘못된 요청)는 재시도 없이 바로 기존
  폴백으로 넘어간다. 재시도 대기 총합은 5초로 캡을 씌워, 턴 제출처럼 동기
  경로에 물린 호출(`continue_story`)이 재시도 때문에 오래 걸리는 걸 막는다.
  실측 중 알게 된 함정: `client.interactions.create()`(우리가 쓰는 신형 API)가
  던지는 429는 `google.genai.errors.APIError`가 아니라 SDK 내부(`_gaos`)의
  별도 예외 계층(`status_code` 속성만 가짐)이라, 처음엔 분류를 놓쳐 재시도가
  전혀 안 걸리고 있었다. 두 계층을 모두 보도록 고쳤다. 또한 Gemini SDK가
  `interactions.create()`에 자체적으로 거는 내장 재시도(최대 5회, 최대 60초)는
  `_get_client()`에서 꺼뒀다 — 안 그러면 우리 쪽 "총합 5초" 약속이 SDK 내부에서
  이미 깨져버린다.
- **인물 일관성 매칭에 안전망을 뒀다** (`ai._match_known_names`,
  `generate_round_art`). 삽화용 장면을 요약하는 LLM이 내놓는 `cast` 배열의
  이름이 조사가 붙거나(규민이/서연을/규민과) 표기가 살짝 달라
  `character_sheet` 키와 정확히 안 맞으면, 그 인물은 고정 외모 묘사 없이
  그려져 매번 딴사람처럼 나온다. 조사 제거 → 부분 포함 매칭 순으로 시도해
  기존 인물에 붙이고, LLM이 `cast`에 넣는 걸 아예 깜빡했더라도 최근 맥락에
  이름이 그대로 언급된 기존 인물이면 강제로 포함시킨다.
- **무료 할당량은 카운터로 미리 막는다** (`app/quota.py`). 데모 당일 할당량이
  소진돼 벤더가 429를 뱉기 시작하면, 매 요청마다 그 실패(+ Cloudflare는 최대 90초
  타임아웃)를 다시 겪게 된다. 그래서 `.env`의 `*_DAILY_LIMIT`으로 한도를 정해두면
  벤더별 호출 직전에 오늘 카운트를 확인해, 한도에 닿은 벤더는 아예 호출하지 않고
  바로 폴백으로 넘어간다. 카운트는 `quota_state.json`에 저장돼 `--reload` 재시작에도
  유지되고, 날짜가 바뀌면(UTC) 자동 리셋된다. 한도를 안 채우면(기본 0) 종전과 동일하게
  무제한으로 동작하고 카운트만 쌓인다. **이어쓰기(`continue_story`)도 삽화와 같은
  원칙을 적용했다** — 예전에는 텍스트 생성 실패(할당량 소진 포함)가 그대로 턴 제출
  API를 500으로 떨어뜨려 게임이 멈췄는데, 이제는 실패 시 원문을 그대로 쓰고 짧은
  연결 문장으로 이어 다음 사람이 계속 쓸 수 있게 한다. 상태는 `/api/quota`에서
  AI를 호출하지 않고 확인할 수 있다.

> **프론트 담당자께**: 백그라운드 처리 도입으로 `stories.status`에 `finishing`
> (완결 직전, 에필로그 생성 중) 상태가 새로 생겼습니다. 이 구간에서
> `current_user_id`/`current_nickname`이 `null`이 되는데, `app.js`가 이 값을
> 널 체크 없이 문자열에 바로 끼워 넣는 곳이 있어서(`turnWho`, `composerLocked`)
> 짧게 "null 님 차례"처럼 보일 수 있습니다. 완결 화면 쪽 코드라 제가 직접 고치지
> 않았어요 — `is_finished`(정말 완결)와 `status === "finishing"`(완결 처리 중)을
> 구분해서 "완결 처리 중..." 같은 문구로 보여주면 됩니다.

## 남은 작업

- 알림 (현재는 새로고침/폴링으로 확인)
- 소셜 로그인 (현재는 닉네임 + localStorage 세션)
- 서버 사이드 PDF 생성 (한글 폰트 임베딩 필요)
- 인원 미달일 때 '이야기 시작' 버튼 비활성 표시 (현재는 눌리는 것처럼 보임)
- 배포: 프론트/백이 한 서버라 Render·Railway에 단일 배포 가능, DB만 PostgreSQL로 교체
