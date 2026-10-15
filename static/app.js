/* Claude 会话台 — 前端 */
"use strict";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = {
  token: new URLSearchParams(location.search).get("token") || localStorage.getItem("crw_token") || "",
  sessions: [], managed: [],
  sel: null,                    // {kind:'ext'|'mgr', id}
  tr: { file: null, after: 0, first: true },
  ws: null, term: null, fit: null,
};
if (state.token) localStorage.setItem("crw_token", state.token);

/* ---------------- API ---------------- */
async function api(path, opts = {}) {
  const authed = {
    ...opts,
    headers: { ...(opts.headers || {}), "X-Auth-Token": state.token },
  };
  const res = await fetch(path, authed);
  if (res.status === 401) { showGate(); throw new Error("unauthorized"); }
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) throw new Error((data && data.detail) || res.statusText);
  return data;
}
async function post(path, body) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
}

/* ---------------- 令牌门 ---------------- */
function showGate() {
  $("#gate").classList.remove("hidden");
  $("#app").classList.add("hidden");
}
$("#gate-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  state.token = $("#gate-token").value.trim();
  $("#gate-err").textContent = "";
  try {
    // health 是唯一只认 query token 的端点（其余走 X-Auth-Token 头）
    const h = await api("/api/health?token=" + encodeURIComponent(state.token));
    if (!h.authed) throw new Error("token 无效");
    localStorage.setItem("crw_token", state.token);
    enterApp();
  } catch {
    $("#gate-err").textContent = "令牌无效，请核对启动窗口里的 URL";
  }
});

/* ---------------- 会话列表 ---------------- */
function pidLabel(s) {
  return s.managed ? `#${s.pid ?? "?"}` : `PID ${s.pid}`;
}
function renderLists() {
  const alivePids = new Set(state.sessions.map((s) => s.pid));
  const external = state.sessions.filter((s) => !s.managed);
  $("#count-external").textContent = external.length;
  $("#count-managed").textContent = state.managed.length;

  const mk = (s, kind) => {
    const active = state.sel && state.sel.kind === kind &&
      ((kind === "mgr" && state.sel.id === s.id) || (kind === "ext" && state.sel.id === s.pid));
    const cwd = s.cwd || "(目录未知)";
    const started = s.started ? new Date(s.started).toLocaleTimeString("zh-CN", { hour12: false }) : "";
    const dirName = (s.cwd ? s.cwd.replace(/[\\/]+$/, "").split(/[\\/]/).pop() : "") || cwd;
    const title = s.title || (kind === "mgr" ? "新托管会话 · 等待首条消息" : dirName);
    const subId = kind === "mgr" ? "MGR #" + s.id : "PID " + s.pid;
    const sub = `${dirName} · ${subId}${kind === "ext" && started ? " · " + started : ""}`;
    return `<button class="s-item ${active ? "active" : ""}" data-kind="${kind}"
      data-id="${kind === "mgr" ? s.id : s.pid}">
      <div class="si-row1">
        <span class="dot ${kind === "mgr" ? "dot-live" : "dot-ext"}"></span>
        <span class="si-title">${esc(title)}</span>
        ${kind === "mgr" ? '<span class="si-badge mgr">托管</span>' : ""}
      </div>
      <div class="si-cwd" title="${esc(cwd)}">${esc(sub)}</div>
    </button>`;
  };
  $("#list-managed").innerHTML = state.managed.map((s) => mk(s, "mgr")).join("") ||
    `<div class="si-empty">暂无，左上「托管新会话」启动</div>`;
  $("#list-external").innerHTML = external.map((s) => mk(s, "ext")).join("") ||
    `<div class="si-empty">没有检测到本机 Claude 进程</div>`;

  document.querySelectorAll(".s-item").forEach((el) => {
    el.addEventListener("click", () =>
      selectSession(el.dataset.kind, Number(el.dataset.id)));
  });

  // 当前外部会话已退出 → 置灰提示（只追加一次，openExternal 会清 data-dead）
  const titleEl = $("#ext-title");
  if (state.sel && state.sel.kind === "ext" && !alivePids.has(state.sel.id)
      && !titleEl.dataset.dead) {
    titleEl.textContent += "（已退出）";
    titleEl.dataset.dead = "1";
  }
}

async function pollSessions() {
  try {
    const data = await api("/api/sessions");
    state.sessions = data.sessions;
    state.managed = data.managed;
    renderLists();
    if (state.sel && state.sel.kind === "ext") pollTranscript();
  } catch (e) { /* 静默，等下轮 */ }
}

/* ---------------- 选择会话 ---------------- */
function selectSession(kind, id) {
  closeManagedWs();
  state.sel = { kind, id };
  document.body.classList.add("nav-stage");
  $("#empty-state").classList.add("hidden");
  $("#view-external").classList.toggle("hidden", kind !== "ext");
  $("#view-managed").classList.toggle("hidden", kind !== "mgr");
  renderLists();
  if (kind === "ext") openExternal(id);
  else openManaged(id);
}

/* ---------------- 外部会话 ---------------- */
function currentExt() {
  return state.sessions.find((s) => s.pid === state.sel.id && !s.managed) || null;
}
async function openExternal(pid) {
  state.tr = { file: null, after: 0, first: true };
  $("#transcript").innerHTML = `<div class="tr-hint">加载会话记录…</div>`;
  $("#probe-badge").className = "badge badge-unknown";
  $("#probe-badge").textContent = "未探测";
  try {
    const info = await api(`/api/transcript/files?pid=${pid}`);
    const titleEl = $("#ext-title");
    const s0 = currentExt();
    const chosen = info.files.find((f) => f.file === info.chosen);
    const title = (s0 && s0.title) || (chosen && (chosen.title || chosen.preview));
    titleEl.removeAttribute("data-dead");
    titleEl.textContent = title || `${info.cwd || "(目录未知)"}  ·  PID ${pid}`;
    titleEl.title = title || "";
    const s = currentExt();
    $("#ext-meta").textContent = s
      ? `启动 ${s.started ? new Date(s.started).toLocaleString("zh-CN", { hour12: false }) : "?"}　${s.cmdline.slice(0, 90)}` : "会话已退出";
    const sel = $("#transcript-select");
    sel.innerHTML = info.files.length
      ? info.files.map((f) => `<option value="${esc(f.file)}"${f.file === info.chosen ? " selected" : ""}>${esc(f.title || f.file.slice(0, 18))} · ${new Date(f.mtime).toLocaleTimeString("zh-CN", { hour12: false })}</option>`).join("")
      : `<option value="">无会话记录</option>`;
    state.tr.file = info.chosen;
    pollTranscript();
  } catch (e) {
    $("#transcript").innerHTML = `<div class="tr-hint">${esc(e.message)}</div>`;
  }
}

function renderEvents(events, reset) {
  const box = $("#transcript");
  if (reset) box.innerHTML = "";
  const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 90;
  for (const ev of events) {
    const ts = ev.ts ? new Date(ev.ts).toLocaleTimeString("zh-CN", { hour12: false }) : "";
    let body;
    if (ev.kind === "tool") {
      body = `<span class="tr-tool-name">⚙ ${esc(ev.name)}</span> ${esc(ev.detail || "")}`;
    } else {
      body = `<p>${esc(ev.text)}</p>`;
    }
    const div = document.createElement("div");
    div.className = `tr ${ev.kind === "tool" ? "tool" : ev.role}`;
    div.innerHTML = `<div class="tr-who">${ev.kind === "tool" ? "TOOL" : ev.role === "user" ? "我" : "AI"}</div>
      <div class="tr-body">${body}<span class="tr-ts">${ts}</span></div>`;
    box.appendChild(div);
  }
  while (box.querySelectorAll(".tr").length > 220) box.firstElementChild?.remove();
  if (stick || reset) box.scrollTop = box.scrollHeight;
}

async function pollTranscript() {
  if (!state.sel || state.sel.kind !== "ext" || !state.tr.file) return;
  try {
    const r = await api(`/api/transcript?pid=${state.sel.id}&file=${encodeURIComponent(state.tr.file)}&after=${state.tr.after}`);
    if (state.tr.first && !r.events.length) {
      $("#transcript").innerHTML = `<div class="tr-hint">这条会话还没有消息记录（输入会出现在原终端里）</div>`;
    } else if (r.events.length || state.tr.after === 0) {
      renderEvents(r.events, state.tr.after === 0);
    }
    state.tr.after = r.size;
    state.tr.first = false;
  } catch (e) { /* 等下轮 */ }
}

$("#transcript-select").addEventListener("change", (e) => {
  state.tr.file = e.target.value || null;
  state.tr.after = 0;
  state.tr.first = true;
  pollTranscript();
});

$("#btn-probe").addEventListener("click", async () => {
  const badge = $("#probe-badge");
  badge.className = "badge badge-unknown"; badge.textContent = "探测中…";
  try {
    const r = await api(`/api/session/${state.sel.id}/probe`);
    if (r.injectable) {
      badge.className = "badge badge-ok";
      badge.textContent = "可注入 · " + (r.conpty ? "Windows Terminal" : "conhost");
    } else {
      badge.className = "badge badge-bad"; badge.textContent = "不可注入";
      toast(r.error || "注入失败", true);
    }
  } catch (e) { badge.className = "badge badge-bad"; badge.textContent = "不可注入"; toast(e.message, true); }
});

async function sendInput() {
  const ta = $("#input-text");
  const text = ta.value.trim();
  if (!text || !state.sel) return;
  try {
    const r = await post(`/api/session/${state.sel.id}/input`, { text, submit: true });
    if (r.ok) {
      ta.value = "";
      $("#probe-badge").className = "badge badge-ok";
      $("#probe-badge").textContent = "已注入 ✓";
      setTimeout(pollTranscript, 1800);
    } else toast(r.error || "注入失败", true);
  } catch (e) { toast(e.message, true); }
}
$("#btn-send").addEventListener("click", sendInput);
$("#input-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendInput(); }
});
document.querySelectorAll("#view-external .key-btn").forEach((b) => b.addEventListener("click", async () => {
  if (!state.sel) return;
  try {
    await post(`/api/session/${state.sel.id}/key`, { key: b.dataset.key });
    $("#probe-badge").className = "badge badge-ok";
    $("#probe-badge").textContent = "已发送 " + b.textContent;
    setTimeout(pollTranscript, 1200);
  } catch (e) { toast(e.message, true); }
}));

/* ---------------- 托管会话 / xterm ---------------- */
async function openManaged(id) {
  const s = state.managed.find((x) => x.id === id);
  $("#mgr-title").textContent = s
    ? (s.title || `${s.cwd}  ·  MGR #${id}`)
    : `MGR #${id}（已结束）`;
  $("#mgr-title").title = s ? `${s.cwd}  ·  MGR #${id}  ·  PID ${s.pid}` : "";
  const term = new Terminal({
    fontFamily: '"Cascadia Code", Consolas, monospace', fontSize: 13.5, cursorBlink: true,
    theme: { background: "#0a0907", foreground: "#d8cbb2", cursor: "#c9a269",
             selectionBackground: "rgba(201,162,105,.28)", black: "#0a0907",
             brightBlack: "#6b6251", white: "#ede3d1", brightWhite: "#f6ecd9" },
  });
  state.term = term;
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open($("#terminal"));
  requestAnimationFrame(() => { try { fit.fit(); } catch { /* 容器未稳定 */ } });

  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/managed/${id}?token=${encodeURIComponent(state.token)}`);
  state.ws = ws;
  ws.onopen = () => {
    $("#ws-badge").className = "badge badge-ok"; $("#ws-badge").textContent = "已连接";
    sendResize();
  };
  ws.onmessage = (ev) => term.write(ev.data);
  ws.onclose = () => { $("#ws-badge").className = "badge badge-bad"; $("#ws-badge").textContent = "已断开"; };
  term.onData((d) => { if (ws.readyState === 1) ws.send(JSON.stringify({ type: "input", data: d })); });
  let lastDims = "";
  const sendResize = () => {
    if (ws.readyState !== 1) return;
    try { fit.fit(); } catch { return; }
    const dims = term.cols + "x" + term.rows;
    if (dims === lastDims) return;   // 尺寸未变不回发，防止 observer 回环
    lastDims = dims;
    ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
  };
  window.addEventListener("resize", sendResize);
  const resizeObserver = new ResizeObserver(sendResize);
  resizeObserver.observe($("#terminal"));
  // 监听器/观察器随本次会话登记，closeManagedWs 时必须摘干净，
  // 否则每切一次会话累积一组持有旧 term/ws 闭包的监听（评审 HIGH）。
  state.managedCleanup = () => {
    window.removeEventListener("resize", sendResize);
    resizeObserver.disconnect();
  };
}

function closeManagedWs() {
  if (state.managedCleanup) { try { state.managedCleanup(); } catch { /* noop */ } state.managedCleanup = null; }
  if (state.ws) { try { state.ws.onclose = null; state.ws.close(); } catch { /* noop */ } state.ws = null; }
  if (state.term) { try { state.term.dispose(); } catch { /* noop */ } state.term = null; }
  $("#terminal").innerHTML = "";
}

/* 手机端：返回会话列表（不断开会话，回来可继续） */
document.querySelectorAll("[data-back]").forEach((b) =>
  b.addEventListener("click", () => document.body.classList.remove("nav-stage")));

/* 手机端：托管终端输入条，经当前 WebSocket 写入 PTY */
function sendManagedInput() {
  const ta = $("#mgr-input");
  const text = ta.value;
  if (!text || !state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  // 与 xterm 粘贴路径一致：原文 + 回车（Claude 输入框支持多行粘贴）
  state.ws.send(JSON.stringify({ type: "input", data: text.replace(/\r/g, "") + "\r" }));
  ta.value = "";
  ta.focus();
}
$("#btn-mgr-send").addEventListener("click", sendManagedInput);
$("#mgr-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendManagedInput(); }
});
document.querySelectorAll("[data-mkey]").forEach((b) =>
  b.addEventListener("click", () => {
    const map = { escape: "\x1b", ctrl_c: "\x03" };
    if (state.ws && state.ws.readyState === WebSocket.OPEN)
      state.ws.send(JSON.stringify({ type: "input", data: map[b.dataset.mkey] }));
  }));

$("#btn-mgr-stop").addEventListener("click", async () => {
  if (!state.sel || state.sel.kind !== "mgr") return;
  if (!confirm("结束该托管会话？其中的 Claude 进程会被终止。")) return;
  const sid = state.sel.id;
  // 先摘 WS/终端与 resize 观察器，避免视图隐藏后零尺寸 resize 发往将死会话
  closeManagedWs();
  state.sel = null;
  document.body.classList.remove("nav-stage");
  $("#view-managed").classList.add("hidden");
  $("#empty-state").classList.remove("hidden");
  try {
    await fetch(`/api/managed/${sid}`, {
      method: "DELETE",
      headers: { "X-Auth-Token": state.token },
    });
    pollSessions();
  } catch (e) { toast(e.message, true); }
});

/* ---------------- 新建托管会话 ---------------- */
$("#btn-new").addEventListener("click", async () => {
  $("#modal-new").classList.remove("hidden");
  try {
    const data = await api("/api/dirs");
    const list = $("#dir-list");
    const quick = $("#dir-quick");
    const all = data.roots.flatMap((r) => r.dirs);
    list.innerHTML = all.map((d) => `<option value="${esc(d)}">`).join("");
    quick.innerHTML = data.roots.filter((r) => r.root !== "C:\\" && r.root !== "E:\\")
      .flatMap((r) => r.dirs.slice(0, 8)).slice(0, 14)
      .map((d) => `<button class="dir-chip" data-d="${esc(d)}">${esc(d.replace(/^.*[\\/]/, ""))}</button>`).join("");
    quick.querySelectorAll(".dir-chip").forEach((b) =>
      b.addEventListener("click", () => { $("#new-cwd").value = b.dataset.d; }));
    if (!$("#new-cwd").value) $("#new-cwd").value = "E:\\1workspace";
  } catch (e) { toast(e.message, true); }
});
$("#new-cancel").addEventListener("click", () => $("#modal-new").classList.add("hidden"));
$("#new-ok").addEventListener("click", async () => {
  const cwd = $("#new-cwd").value.trim();
  if (!cwd) return;
  try {
    const s = await post("/api/managed", { cwd });
    $("#modal-new").classList.add("hidden");
    await pollSessions();
    selectSession("mgr", s.id);
  } catch (e) { toast(e.message, true); }
});

/* ---------------- 其他 ---------------- */
$("#btn-refresh").addEventListener("click", pollSessions);
let toastTimer = null;
function toast(msg, bad) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (bad ? " bad" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 3200);
  t.classList.remove("hidden");
}

async function enterApp() {
  $("#gate").classList.add("hidden");
  $("#app").classList.remove("hidden");
  history.replaceState(null, "", location.pathname);
  try {
    const n = await api("/api/net");
    $("#net-info").innerHTML = n.ips.map((ip) => `http://${ip}:${n.port}`).join("<br>");
  } catch { /* noop */ }
  await pollSessions();
  setInterval(pollSessions, 5000);
  setInterval(() => { if (state.sel && state.sel.kind === "ext") pollTranscript(); }, 2500);
}

if (state.token) {
  api("/api/health?token=" + encodeURIComponent(state.token))
    .then((h) => h.authed ? enterApp() : showGate()).catch(showGate);
} else {
  showGate();
}
