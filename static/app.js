/* static/app.js
   빌드 도구 없이 그대로 동작하는 단일 스크립트.
   상태는 서버가 전부 들고 있고, 프론트는 8초마다 폴링해서 다시 그린다.
   (웹소켓은 MVP 범위 밖 — 비동기 턴제라 실시간성이 필요 없다)
*/

const API = "";
const POLL_MS = 8000;

const state = {
  user: null,      // {id, nickname}
  roomId: null,
  storyId: null,
  story: null,
  config: null,
  pollTimer: null,
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
    try { msg = (await res.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
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
  $("who").textContent = user.nickname;
}

function loadUser() {
  try {
    const raw = localStorage.getItem("toksosul_user");
    if (raw) { state.user = JSON.parse(raw); $("who").textContent = state.user.nickname; }
  } catch (_) {}
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

/* ------------------------------------------------------------ 2. 로비 */
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

  $("copyCodeBtn").onclick = () => {
    const url = `${location.origin}${location.pathname}?code=${room.invite_code}`;
    navigator.clipboard.writeText(url).then(() => toast("초대 링크를 복사했어요."));
  };
}

$("startBtn").onclick = async () => {
  try {
    const story = await api(`/api/rooms/${state.roomId}/start`, {
      method: "POST",
      body: JSON.stringify({
        user_id: state.user.id,
        genre: $("genreInput").value.trim() || "자유",
        opening: $("openingInput").value.trim() || null,
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

function renderStory(story) {
  if (story.is_finished) { renderDone(story); return; }
  show("view-story");

  const isMyTurn = story.current_user_id === state.user.id;
  const bar = $("turnbar");
  bar.classList.toggle("mine", isMyTurn);
  $("turnWho").textContent = isMyTurn ? "내 차례예요" : `${story.current_nickname} 님 차례`;
  $("turnMeta").textContent = `${story.current_round} / ${story.max_rounds}바퀴 · ${story.genre}`;
  $("countdown").textContent = formatLeft(story.seconds_left);

  const isHost = story.host_id === state.user.id;
  const sug = $("suggestBox");
  if (story.end_suggestion && isHost) {
    sug.hidden = false;
    $("suggestText").textContent = `AI가 완결을 추천했어요 — ${story.end_suggestion}`;
  } else { sug.hidden = true; }

  renderTurns(story, "thread");

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
function startPolling(fn) {
  stopPolling();
  state.pollTimer = setInterval(fn, POLL_MS);
}

function stopPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = null;
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
