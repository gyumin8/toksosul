/* static/app.js
   빌드 도구 없이 그대로 동작하는 단일 스크립트.
   상태는 서버가 전부 들고 있고, 프론트는 8초마다 폴링해서 다시 그린다.
   (웹소켓은 MVP 범위 밖 — 비동기 턴제라 실시간성이 필요 없다)
*/

const API = "";
const POLL_MS = 8000;        // 평소 폴링 간격
const POLL_MS_BUSY = 2500;   // 바퀴 후처리(삽화 생성) 중일 때의 폴링 간격

const state = {
  user: null,      // {id, nickname}
  roomId: null,
  storyId: null,
  story: null,
  config: null,
  pollTimer: null,
  pollFn: null,        // 현재 돌고 있는 폴링 함수 (간격만 바꿔 재시작할 때 재사용)
  pollInterval: null,  // 현재 폴링 간격(ms)
  tickTimer: null,
};

/* ------------------------------------------------------------ 유틸 */
const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const res = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let msg = "요청을 처리하지 못했습니다.";
    let code = null;
    try {
      const detail = (await res.json()).detail;
      // detail은 문자열일 수도 있고, 서버가 코드까지 실어 보내는 dict일 수도 있다.
      if (typeof detail === "string") msg = detail;
      else if (detail && typeof detail === "object") {
        msg = detail.message || msg;
        code = detail.code || null;
      }
    } catch (_) {}
    const err = new Error(msg);
    err.status = res.status;
    err.code = code;
    // 저장된 user_id가 서버에 없으면(= DB 초기화/서버 재생성) 여기서 바로 세션을 정리하고
    // 이름 입력 화면으로 되돌린다. 죽은 user_id로 계속 요청이 나가지 않게 하는 게 핵심.
    if (code === "user_not_found") {
      const nickname = state.user && state.user.nickname;
      resetSession();
      goToNameView(nickname);
    }
    throw err;
  }
  return res.headers.get("content-type")?.includes("json") ? res.json() : res.text();
}

let toastTimer;
function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 2800);
}

function show(viewId) {
  document.querySelectorAll(".view").forEach((v) => { v.hidden = v.id !== viewId; });
}

function formatLeft(sec) {
  if (sec == null) return "";
  if (sec <= 0) return "곧 넘어갑니다";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h > 0) return `${h}시간 ${m}분 남음`;
  if (m > 0) return `${m}분 ${s}초 남음`;
  return `${s}초 남음`;
}

function escapeHtml(str) {
  return (str ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ------------------------------------------------------------ 세션 */
function saveUser(user) {
  state.user = user;
  localStorage.setItem("toksosul_user", JSON.stringify(user));
  showWho(user.nickname);
}

/** 상단바의 닉네임 표시를 갱신한다. 이름이 없으면 버튼 자체를 숨긴다. */
function showWho(nickname) {
  const el = $("who");
  el.textContent = nickname || "";
  el.hidden = !nickname;
}

function loadUser() {
  try {
    const raw = localStorage.getItem("toksosul_user");
    if (!raw) return;
    const user = JSON.parse(raw);
    // 형태가 깨진 값(옛 버전/수동 편집)이 남아 있으면 없는 것으로 친다.
    if (!user || typeof user.id !== "string" || !user.id) {
      localStorage.removeItem("toksosul_user");
      return;
    }
    state.user = user;
    showWho(user.nickname);
  } catch (_) {
    localStorage.removeItem("toksosul_user");
  }
}

/** 서버에 저장된 user_id가 아직 살아 있는지 확인한다.
 *  살아 있으면 true, 죽었으면 세션을 지우고 false. (네트워크 오류는 세션 문제가
 *  아니므로 true로 둬서, 서버가 잠깐 느릴 때 이름을 다시 묻지 않는다) */
async function verifySession() {
  if (!state.user) return false;
  try {
    const fresh = await api(`/api/users/${encodeURIComponent(state.user.id)}`);
    saveUser(fresh);  // 닉네임이 서버 기준으로 다시 맞춰진다
    return true;
  } catch (e) {
    if (e.code === "user_not_found" || e.status === 404) return false;
    return true;
  }
}

/** 죽은 세션 정리: localStorage를 비우고 화면 상태를 초기화한다.
 *  api()에서 자동으로 불리므로, 어떤 요청이든 세션이 끊긴 게 확인되면
 *  옛 user_id로 계속 요청을 날리는 일이 없다. */
function resetSession() {
  state.user = null;
  state.roomId = null;
  state.storyId = null;
  state.story = null;
  try { localStorage.removeItem("toksosul_user"); } catch (_) {}
  showWho("");
}

/** 세션이 끊겼을 때 이름 입력 화면으로 되돌린다.
 *  쓰던 닉네임은 입력칸에 미리 채워줘서 한 번만 누르면 되게 한다. */
function goToNameView(previousNickname) {
  stopPolling();
  history.replaceState({}, "", location.pathname);
  if (previousNickname) $("nicknameInput").value = previousNickname;
  show("view-name");
}

/* ------------------------------------------------------------ 초기화 */
async function boot() {
  state.config = await api("/api/config");
  $("ruleHint").textContent =
    `${state.config.min_members}~${state.config.max_members}명, 최대 ${state.config.max_rounds}바퀴까지 쓸 수 있어요.`
    + (state.config.mock_ai ? " (지금은 목 응답 모드)" : "");
  $("maxRounds").max = state.config.max_rounds;

  loadUser();

  const params = new URLSearchParams(location.search);
  const codeFromUrl = params.get("code");
  const storyFromUrl = params.get("story");

  // ?new=1 로 열면 저장된 이름을 무시하고 항상 이름 입력부터 시작한다.
  // 개발 중에 다른 사람인 척 테스트할 때(한 브라우저에서 2명 흉내) 쓴다.
  if (params.get("new") !== null) {
    resetSession();
    if (codeFromUrl) $("joinCode").value = codeFromUrl;
    goToNameView("");
    return;
  }

  // 저장된 세션이 서버에 아직 있는지 먼저 확인한다.
  // (이 확인이 없으면, 서버/DB가 새로 만들어진 뒤 방을 만들 때마다
  //  "유저를 찾을 수 없습니다." 404가 떴다)
  if (state.user) {
    const nickname = state.user.nickname;
    const alive = await verifySession();
    if (!alive) {
      resetSession();
      if (codeFromUrl) $("joinCode").value = codeFromUrl;
      goToNameView(nickname);   // 조용히 이름 화면으로. 에러 토스트는 띄우지 않는다
      return;
    }
  }

  if (!state.user) { show("view-name"); return; }

  if (storyFromUrl) { state.storyId = storyFromUrl; startStoryView(); return; }
  if (codeFromUrl) { $("joinCode").value = codeFromUrl; show("view-lobby"); return; }
  show("view-lobby");
}

/* ------------------------------------------------------------ 1. 닉네임 */
$("nameBtn").onclick = async () => {
  const nickname = $("nicknameInput").value.trim();
  if (!nickname) return toast("이름을 입력해 주세요.");
  try {
    saveUser(await api("/api/users", { method: "POST", body: JSON.stringify({ nickname }) }));
    const code = new URLSearchParams(location.search).get("code");
    if (code) $("joinCode").value = code;
    show("view-lobby");
  } catch (e) { toast(e.message); }
};

$("homeBtn").onclick = () => {
  stopPolling();
  history.replaceState({}, "", location.pathname);
  show(state.user ? "view-lobby" : "view-name");
};

/* 이름(세션)은 localStorage에 남아서 다음에 열 때 이름 입력 화면을 건너뛴다.
   친구들이 매번 이름을 다시 치지 않게 하려는 의도지만, 바꿀 방법이 없으면
   "왜 자꾸 로비로 넘어가지?"가 된다. 상단바의 닉네임이 그 탈출구다. */
$("who").onclick = () => {
  const nickname = state.user && state.user.nickname;
  if (!confirm(`'${nickname}' 이름을 지우고 다시 시작할까요?`)) return;
  resetSession();
  goToNameView(nickname);
  toast("이름을 다시 입력해 주세요.");
};

/* ------------------------------------------------------------ 2. 로비 */
const MAX_USER_CHARACTERS = 4;

/** '등장인물 추가' 한 줄을 만든다. 이름을 비우면 저장할 때 무시된다. */
function addCastRow() {
  const rows = $("castRows");
  if (rows.children.length >= MAX_USER_CHARACTERS) {
    return toast(`등장인물은 ${MAX_USER_CHARACTERS}명까지 넣을 수 있어요.`);
  }
  const row = document.createElement("div");
  row.className = "cast-row";
  row.innerHTML = `
    <div class="row">
      <label class="field small">
        <span>이름</span>
        <input class="cast-name" maxlength="12" placeholder="도진우" autocomplete="off">
      </label>
      <label class="field small">
        <span>성별</span>
        <select class="cast-gender">
          <option value="">AI가 정함</option>
          <option value="female">여성</option>
          <option value="male">남성</option>
        </select>
      </label>
    </div>
    <label class="field">
      <span>특징</span>
      <input class="cast-traits" maxlength="60" placeholder="키 크고 안경, 무뚝뚝하다" autocomplete="off">
    </label>
    <button type="button" class="remove-cast">이 인물 빼기</button>`;
  row.querySelector(".remove-cast").onclick = () => row.remove();
  rows.appendChild(row);
  row.querySelector(".cast-name").focus();
}

$("addCastBtn").onclick = addCastRow;

/** 입력된 등장인물 줄들을 서버로 보낼 형태로 모은다. */
function collectCast() {
  return [...$("castRows").querySelectorAll(".cast-row")]
    .map((row) => ({
      name: row.querySelector(".cast-name").value.trim(),
      gender: row.querySelector(".cast-gender").value,
      traits: row.querySelector(".cast-traits").value.trim(),
    }))
    .filter((c) => c.name);   // 이름 없는 줄은 버린다
}

$("createBtn").onclick = async () => {
  const name = $("roomName").value.trim();
  if (!name) return toast("방 이름을 입력해 주세요.");
  try {
    const room = await api("/api/rooms", {
      method: "POST",
      body: JSON.stringify({
        user_id: state.user.id,
        name,
        max_rounds: Number($("maxRounds").value) || 20,
        inactivity_hours: Number($("timeoutHours").value) || 24,
        // 주인공 설정. 비운 칸은 null로 보내 AI가 알아서 정하게 둔다.
        hero_name: $("heroName").value.trim() || null,
        hero_gender: $("heroGender").value || null,
        hero_traits: $("heroTraits").value.trim() || null,
        characters: collectCast(),
      }),
    });
    enterRoom(room);
  } catch (e) { toast(e.message); }
};

$("joinBtn").onclick = async () => {
  const code = $("joinCode").value.trim().toUpperCase();
  if (code.length < 4) return toast("초대 코드 6자리를 입력해 주세요.");
  try {
    const room = await api("/api/rooms/join", {
      method: "POST",
      body: JSON.stringify({ user_id: state.user.id, invite_code: code }),
    });
    enterRoom(room);
  } catch (e) { toast(e.message); }
};

/* ------------------------------------------------------------ 3. 대기실 */
function enterRoom(room) {
  state.roomId = room.id;
  if (room.story_id) { state.storyId = room.story_id; startStoryView(); return; }
  renderWaiting(room);
  show("view-waiting");
  startPolling(async () => {
    const fresh = await api(`/api/rooms/${state.roomId}`);
    if (fresh.story_id) { state.storyId = fresh.story_id; startStoryView(); return; }
    renderWaiting(fresh);
  });
}

function renderWaiting(room) {
  $("waitRoomName").textContent = room.name;
  $("inviteCode").textContent = room.invite_code;
  $("waitMembers").innerHTML = room.members.map((m) =>
    `<li class="${m.user_id === room.host_id ? "host" : ""}">${escapeHtml(m.nickname)}</li>`
  ).join("");

  const isHost = room.host_id === state.user.id;
  const n = room.members.length;
  const enough = n >= state.config.min_members;

  $("waitHint").textContent = enough
    ? `${n}명 모였어요. 방장이 시작하면 순서대로 돌아갑니다.`
    : `${state.config.min_members}명부터 시작할 수 있어요. 지금 ${n}명.`;

  $("startBox").hidden = !isHost;
  $("startBtn").disabled = !enough;
  if (isHost) renderHeroSummary(room.hero, room.characters);

  $("copyCodeBtn").onclick = () => {
    const url = `${location.origin}${location.pathname}?code=${room.invite_code}`;
    navigator.clipboard.writeText(url).then(() => toast("초대 링크를 복사했어요."));
  };
}

/** 방 만들 때 정한 인물들을 대기실에 한 줄로 확인시켜 준다. (여기서 다시 묻지 않는다) */
function renderHeroSummary(hero, characters) {
  const label = (p) => {
    const bits = [p.name];
    if (p.gender) bits.push(GENDER_KO[p.gender] || p.gender);
    return bits.join("/");
  };
  const parts = [];
  if (hero && hero.name) parts.push(`주인공 ${label(hero)}`);
  (characters || []).forEach((cch) => parts.push(label(cch)));

  $("castHint").textContent = parts.length
    ? `등장인물: ${parts.join(", ")} · 나머지는 AI가 채웁니다.`
    : "등장인물은 AI가 장르에 맞춰 정합니다.";
}

$("startBtn").onclick = async () => {
  try {
    const story = await api(`/api/rooms/${state.roomId}/start`, {
      method: "POST",
      body: JSON.stringify({
        user_id: state.user.id,
        genre: $("genreInput").value.trim() || "자유",
        opening: $("openingInput").value.trim() || null,
        // 인물은 방 만들 때 정해서 방에 저장돼 있다. 여기서 다시 보내지 않는다.
      }),
    });
    state.storyId = story.id;
    state.story = story;
    startStoryView();
  } catch (e) { toast(e.message); }
};

/* ------------------------------------------------------------ 4. 집필 */
function startStoryView() {
  stopPolling();
  history.replaceState({}, "", `${location.pathname}?story=${state.storyId}`);
  refreshStory();
  startPolling(refreshStory);
  startTicking();
}

async function refreshStory() {
  try {
    state.story = await api(`/api/stories/${state.storyId}`);
    tunePolling(state.story);
    renderStory(state.story);
  } catch (e) { toast(e.message); }
}

function renderTurns(story, containerId) {
  const arts = story.arts || {};
  let html = "";
  let lastRound = 0;

  story.turns.forEach((t) => {
    if (t.round_number !== lastRound) {
      if (lastRound && arts[lastRound]) {
        html += `<figure class="round-art">
            <img src="${arts[lastRound].image_url}" alt="${escapeHtml(arts[lastRound].caption)}">
            <figcaption>${escapeHtml(arts[lastRound].caption)}</figcaption>
          </figure>`;
      }
      lastRound = t.round_number;
      html += `<div class="round-mark">${lastRound}바퀴</div>`;
    }

    if (t.is_skipped) {
      html += `<p class="turn skipped">${escapeHtml(t.nickname)} 님의 차례는 시간이 지나 넘어갔습니다</p>`;
      return;
    }
    const polished = t.was_polished
      ? `<span class="polished" title="원문: ${escapeHtml(t.raw_line)}">다듬음</span>` : "";
    html += `<div class="turn">
        <p class="turn-line"><b>${escapeHtml(t.nickname)}</b>${escapeHtml(t.user_line)}${polished}</p>
        <p class="turn-body">${escapeHtml(t.ai_text)}</p>
      </div>`;
  });

  // 마지막 바퀴 삽화
  if (lastRound && arts[lastRound]) {
    html += `<figure class="round-art">
        <img src="${arts[lastRound].image_url}" alt="${escapeHtml(arts[lastRound].caption)}">
        <figcaption>${escapeHtml(arts[lastRound].caption)}</figcaption>
      </figure>`;
  }

  if (!html) html = `<p class="turn skipped">아직 아무도 쓰지 않았어요. 첫 줄을 던져보세요.</p>`;
  $(containerId).innerHTML = html;
}

const GENDER_KO = { female: "여성", male: "남성" };

/** 시작할 때 정해진 등장인물을 보여준다. 주인공은 따로 표시한다. */
function renderCast(story) {
  const box = $("castBox");
  const cast = story.cast || [];
  if (!cast.length) { box.hidden = true; return; }

  box.hidden = false;
  $("premiseText").textContent = story.premise || "";
  $("castList").innerHTML = cast.map((c) => {
    const isHero = c.role === "주인공";
    const bits = [GENDER_KO[c.gender] || c.gender, c.age, c.role]
      .filter(Boolean).map(escapeHtml).join(" · ");
    return `<li class="${isHero ? "hero" : ""}">
        <b>${escapeHtml(c.name)}</b>
        <span class="cast-meta">${bits}</span>
        ${c.personality ? `<span class="cast-note">${escapeHtml(c.personality)}</span>` : ""}
      </li>`;
  }).join("");
}

function renderStory(story) {
  if (story.is_finished) { renderDone(story); return; }
  show("view-story");
  renderCast(story);

  const isMyTurn = story.current_user_id === state.user.id;
  const bar = $("turnbar");
  bar.classList.toggle("mine", isMyTurn);
  $("turnWho").textContent = isMyTurn ? "내 차례예요" : `${story.current_nickname} 님 차례`;
  // 지금이 이야기의 어디쯤인지(초반/중반/후반) 같이 보여준다.
  const actName = story.act && story.act.name ? `${story.act.name}부 · ` : "";
  $("turnMeta").textContent =
    `${actName}${story.current_round} / ${story.max_rounds}바퀴 · ${story.genre}`;
  $("countdown").textContent = formatLeft(story.seconds_left);

  const isHost = story.host_id === state.user.id;
  const sug = $("suggestBox");
  if (story.end_suggestion && isHost) {
    sug.hidden = false;
    $("suggestText").textContent = `AI가 완결을 추천했어요 — ${story.end_suggestion}`;
  } else { sug.hidden = true; }

  renderTurns(story, "thread");

  // 바퀴가 끝나 삽화를 만드는 중이면 그 자리에 표시해준다.
  // (빈 화면으로 기다리면 실제보다 훨씬 오래 걸리는 것처럼 느껴진다)
  if (story.art_pending_round != null) {
    $("thread").insertAdjacentHTML(
      "beforeend",
      `<p class="turn skipped">${story.art_pending_round}바퀴 삽화를 그리는 중이에요…</p>`
    );
  }

  $("composer").hidden = !isMyTurn;
  $("composerLocked").hidden = isMyTurn;
  $("composerLocked").textContent =
    `${story.current_nickname} 님이 쓰는 중입니다 · ${formatLeft(story.seconds_left)}`;
  $("hostControls").hidden = !isHost;
}

$("lineInput").addEventListener("input", (e) => {
  $("counter").textContent = `${e.target.value.length} / 200`;
});

$("lineInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("sendBtn").click();
});

$("sendBtn").onclick = async () => {
  const line = $("lineInput").value.trim();
  if (!line) return toast("한 줄을 입력해 주세요.");
  const btn = $("sendBtn");
  btn.disabled = true;
  btn.textContent = "AI가 이어 쓰는 중";
  try {
    state.story = await api(`/api/stories/${state.storyId}/turns`, {
      method: "POST",
      body: JSON.stringify({ user_id: state.user.id, line }),
    });
    $("lineInput").value = "";
    $("counter").textContent = "0 / 200";
    tunePolling(state.story);   // 이 턴으로 바퀴가 끝났다면 바로 촘촘한 폴링으로 전환
    renderStory(state.story);
    window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "이어쓰기";
  }
};

async function completeStory() {
  try {
    state.story = await api(`/api/stories/${state.storyId}/complete`, {
      method: "POST",
      body: JSON.stringify({ user_id: state.user.id }),
    });
    renderStory(state.story);
  } catch (e) { toast(e.message); }
}

$("acceptEndBtn").onclick = completeStory;
$("forceEndBtn").onclick = () => {
  if (confirm("여기서 이야기를 완결할까요? 되돌릴 수 없어요.")) completeStory();
};

$("dismissEndBtn").onclick = async () => {
  try {
    state.story = await api(`/api/stories/${state.storyId}/dismiss-suggestion`, {
      method: "POST",
      body: JSON.stringify({ user_id: state.user.id }),
    });
    renderStory(state.story);
  } catch (e) { toast(e.message); }
};

/* ------------------------------------------------------------ 5. 완결 */
function renderDone(story) {
  stopPolling();
  show("view-done");

  const cover = $("coverImg");
  if (story.cover_image_url) { cover.src = story.cover_image_url; cover.hidden = false; }

  $("doneTitle").textContent = story.title;
  const writers = story.members.map((m) => m.nickname).join(", ");
  const label = { completed_forced: "바퀴 제한 도달", completed_host: "방장 완결",
                  completed_llm: "AI 추천 완결" }[story.status] || "완결";
  $("doneMeta").textContent = `${story.genre} · ${writers} 함께 씀 · ${label}`;
  $("doneEpilogue").textContent = story.epilogue || "";

  renderTurns(story, "doneThread");

  $("downloadBtn").onclick = () => {
    location.href = `/api/stories/${story.id}/export.md`;
  };
  $("printBtn").onclick = () => window.print();
  $("shareBtn").onclick = () => {
    navigator.clipboard.writeText(`${location.origin}${location.pathname}?story=${story.id}`)
      .then(() => toast("공유 링크를 복사했어요."));
  };
}

/* ------------------------------------------------------------ 폴링 */
function startPolling(fn, intervalMs = POLL_MS) {
  stopPolling();
  state.pollFn = fn;
  state.pollInterval = intervalMs;
  state.pollTimer = setInterval(fn, intervalMs);
}

function stopPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = null;
  state.pollFn = null;
  state.pollInterval = null;
}

/** 삽화가 만들어지는 동안만 폴링을 촘촘하게 한다.
 *  이미지 자체가 3초 만에 나와도 다음 폴링이 8초 뒤면 사용자는 8초를 기다린 것처럼
 *  느낀다. 후처리 중(art_pending_round != null)에만 간격을 좁히고, 끝나면 되돌린다. */
function tunePolling(story) {
  if (!state.pollFn) return;
  const wanted = story && story.art_pending_round != null ? POLL_MS_BUSY : POLL_MS;
  if (wanted !== state.pollInterval) startPolling(state.pollFn, wanted);
}

function startTicking() {
  if (state.tickTimer) clearInterval(state.tickTimer);
  // 카운트다운은 서버 재호출 없이 1초마다 로컬에서 깎는다.
  state.tickTimer = setInterval(() => {
    const s = state.story;
    if (!s || s.is_finished || s.seconds_left == null) return;
    s.seconds_left = Math.max(0, s.seconds_left - 1);
    $("countdown").textContent = formatLeft(s.seconds_left);
    if (!$("composerLocked").hidden) {
      $("composerLocked").textContent =
        `${s.current_nickname} 님이 쓰는 중입니다 · ${formatLeft(s.seconds_left)}`;
    }
  }, 1000);
}

boot();