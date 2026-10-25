/* Claude 会话台 — 前端 */
"use strict";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = {
  token: new URLSearchParams(location.search).get("token") || localStorage.getItem("crw_token") || "",
  sessions: [], managed: [],
  hist: [],                     // /api/history 扫盘出的历史会话（含已结束的桥任务）
  sel: null,                    // {kind:'ext'|'mgr'|'hist', id}
  tr: { file: null, after: 0, first: true, events: [], expanded: new Set() },
  htr: null,                    // 历史回看的转写状态（结构与 tr 同形，互不干扰）
  ws: null, term: null, fit: null,
  wsRetry: null, wsAttempts: 0,   // 托管 WS 自动重连：退避计时器与次数
  pingTimer: null, lastPong: 0,   // 应用层心跳（半开死链检测）
  pendingSend: null,              // 断线时点发送的消息：重连成功后自动补发
  mgrSendResize: null,            // 当前托管会话的尺寸同步函数（重连后 onopen 调）
  obInflight: false,              // 发送回执轮询去重
  entered: false,
  quota: { data: null },
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
async function del(path) {
  return api(path, { method: "DELETE" });
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
    // 标题缺失 = 后端没有可信的文件关联。绝不借用目录名冒充会话标题，
    // 否则同 cwd 多个会话会看起来一模一样（串台 bug 的表象）。
    const title = s.title || "新会话 · 等待首条消息";
    const subId = kind === "mgr" ? "MGR #" + s.id : "PID " + s.pid;
    const sub = `${dirName} · ${subId}${kind === "ext" && started ? " · " + started : ""}`;
    return `<button class="s-item ${active ? "active" : ""}" data-kind="${kind}"
      data-id="${kind === "mgr" ? s.id : s.pid}">
      <div class="si-row1">
        <span class="dot ${kind === "mgr" ? "dot-live" : "dot-ext"}"></span>
        <span class="si-title">${esc(title)}</span>
        ${kind === "mgr" ? '<span class="si-badge mgr">托管</span>' : ""}
        ${kind === "ext" && s.bridge ? '<span class="si-badge bridge">微信桥</span>' : ""}
      </div>
      <div class="si-cwd" title="${esc(cwd)}">${esc(sub)}</div>
    </button>`;
  };
  $("#list-managed").innerHTML = state.managed.map((s) => mk(s, "mgr")).join("") ||
    `<div class="si-empty">暂无，左上「托管新会话」启动</div>`;
  $("#list-external").innerHTML = external.map((s) => mk(s, "ext")).join("") ||
    `<div class="si-empty">没有检测到本机 Claude 进程</div>`;

  // 历史会话（扫盘视图）：条目 id 用数组下标，点选时从 state.hist 取回
  $("#count-history").textContent = state.hist.length;
  $("#list-history").innerHTML = state.hist.map((h, i) => {
    const active = state.sel && state.sel.kind === "hist" && state.sel.id === i;
    const title = h.title || h.preview || h.file.slice(0, 18);
    const when = new Date(h.mtime).toLocaleString("zh-CN",
      { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
    const dirShort = h.dir.replace(/^[A-Za-z]--/, "").split("-").slice(-2).join("-");
    return `<button class="s-item ${active ? "active" : ""}" data-kind="hist" data-id="${i}">
      <div class="si-row1">
        <span class="dot dot-hist"></span>
        <span class="si-title">${esc(title)}</span>
        ${h.bridge ? '<span class="si-badge bridge">微信桥</span>' : ""}
      </div>
      <div class="si-cwd" title="${esc(h.dir + "/" + h.file)}">${esc(dirShort)} · ${when}</div>
    </button>`;
  }).join("") || `<div class="si-empty">暂无历史记录</div>`;

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
    if (state.sel && state.sel.kind === "ext") {
      const cur = currentExt();
      // 后端 chosen 与当前展示文件不一致（首条消息落盘 / 锚点翻盘错配）
      // 且用户没手动选过文件 → 自动切到正确的记录；手动选择永远尊重。
      if (cur && cur.file && cur.file !== state.tr.file
          && !state.tr.manual && !state.tr.loading) {
        openExternal(cur.pid);
      } else {
        pollTranscript();
      }
    }
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
  $("#view-history").classList.toggle("hidden", kind !== "hist");
  renderLists();
  if (kind === "ext") openExternal(id);
  else if (kind === "mgr") openManaged(id);
  else openHistory(id);
}

/* ---------------- 外部会话 ---------------- */
function currentExt() {
  return state.sessions.find((s) => s.pid === state.sel.id && !s.managed) || null;
}
async function openExternal(pid) {
  // loading 期间阻止 5s 轮询重复进入本函数（否则两次加载交错会互相踩 after）
  state.tr = { file: null, after: 0, first: true, manual: false, loading: true,
               events: [], expanded: new Set(),
               filter: localStorage.getItem("crw_tf") || "all" };
  $("#outbox").classList.add("hidden");  // 切会话即清旧回执，pollOutbox 重填
  $("#outbox").innerHTML = "";
  $("#transcript").innerHTML = `<div class="tr-hint">加载会话记录…</div>`;
  $("#probe-badge").className = "badge badge-unknown";
  $("#probe-badge").textContent = "未探测";
  try {
    const info = await api(`/api/transcript/files?pid=${pid}`);
    // await 期间用户可能已切到别的会话，结果作废
    if (!state.sel || state.sel.kind !== "ext" || state.sel.id !== pid) return;
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
    state.tr.loading = false;
    pollOutbox();
    if (!info.chosen) {
      // 后端没有可信关联（新会话尚未发言 / resume 尚无写入）：
      // 明确提示并等待自动关联，绝不展示别的会话的记录。
      $("#transcript").innerHTML = `<div class="tr-hint">该会话还没有可关联的记录文件。<br>发出首条消息后会自动关联；也可以用上方下拉手动选择记录。</div>`;
    }
    pollTranscript();
  } catch (e) {
    if (state.tr) state.tr.loading = false;
    $("#transcript").innerHTML = `<div class="tr-hint">${esc(e.message)}</div>`;
  }
}

/* 会话记录镜像的渲染哲学：用户提示是主角（金框卡片最醒目），AI 过程能省则省 ——
   长文默认只给首尾两段，工具调用/结果连续过程折叠成一个可展开的组条。 */
const AI_COLLAPSE_CHARS = 520;  // AI 文本超此长度且能切出非空中段时首尾折叠
const AI_CLIP_CHARS = 260;      // 首段、尾段各最多保留字符
const AI_CLIP_LINES = 8;        // 首段、尾段各最多保留行数
const USER_COLLAPSE_CHARS = 360; // 「只看我」模式下提示词超此长度截断头部（其余模式不截）
const USER_HIDE_CHARS = 1000;   // 「只看我」模式下超此长度的提示词整段不展示（不截断、不渲染）
const TR_MAX_EVENTS = 220;

function trFmtTs(ev) {
  return ev.ts ? new Date(ev.ts).toLocaleTimeString("zh-CN", { hour12: false }) : "";
}

// 展开状态按事件内容指纹保存（事件只追加、文本不变；不用数组下标，220 修剪后不错位）
function trEventKey(ev) {
  const body = ev.kind === "tool" ? ev.name + " " + (ev.detail || "") : (ev.text || "");
  return `${ev.ts || "?"}|${ev.kind}|${body.length}|${body.slice(0, 24)}`;
}

// AI 长文切首尾：短消息返回 null 表示不折叠。按行预算在自然行边界截取，
// 保证代码块/列表不会被切出半个标记，且首+尾不覆盖全文。
function aiClips(text) {
  const lines = text.split("\n");
  if (text.length <= AI_COLLAPSE_CHARS && lines.length <= 10) return null;
  const collect = (fromStart) => {
    let n = 0;
    let chars = 0;
    for (let k = 0; k < lines.length; k++) {
      const i = fromStart ? k : lines.length - 1 - k;
      const add = lines[i].length + 1;
      // 第一行必收；再收会超行/字符预算的行不要（否则超长中间行会被首尾吞掉，
      // 三段式消息会被判成"无中段可折"）
      if (n > 0 && (n >= AI_CLIP_LINES || chars + add > AI_CLIP_CHARS)) break;
      n++;
      chars += add;
    }
    return n;
  };
  const headN = collect(true);
  const tailN = collect(false);
  if (headN + tailN >= lines.length) {
    // 单行巨长（几乎不换行的代码/数据）：按字符硬切首尾
    if (text.length <= AI_COLLAPSE_CHARS + 2 * AI_CLIP_CHARS) return null;
    return {
      head: text.slice(0, AI_CLIP_CHARS),
      tail: text.slice(text.length - AI_CLIP_CHARS),
      omitted: text.length - 2 * AI_CLIP_CHARS,
    };
  }
  const middle = lines.slice(headN, lines.length - tailN).join("\n");
  if (!middle.trim()) return null;
  const rawHead = lines.slice(0, headN).join("\n");
  const rawTail = lines.slice(lines.length - tailN).join("\n");
  // 首/尾第一行是强制收的，可能本身就是超长单行（minified 代码/长日志），仍按字符截
  const head = rawHead.length > AI_CLIP_CHARS ? rawHead.slice(0, AI_CLIP_CHARS) + "…" : rawHead;
  const tail = rawTail.length > AI_CLIP_CHARS
    ? "…" + rawTail.slice(rawTail.length - AI_CLIP_CHARS) : rawTail;
  const shown = Math.min(rawHead.length, AI_CLIP_CHARS) + Math.min(rawTail.length, AI_CLIP_CHARS);
  return { head, tail, omitted: text.length - shown };
}

// 记录过滤条：吸顶在记录区顶部。全部=原始流；只看我=服务端全量用户提示词；
// 只看AI=客户端滤出 AI 正文（隐藏工具过程与我的发言）。
function renderFilterBar(tr) {
  const f = tr.filter || "all";
  // 计数口径与渲染一致：超 USER_HIDE_CHARS 的整段不展示，不计入
  const userN = tr.events.filter((e) => e.role === "user" && e.kind === "text"
    && (e.text || "").length <= USER_HIDE_CHARS).length;
  return `<div class="tr-filter">
    ${f === "user" ? `<span class="tf-info">${userN} 条提示词 · 长文已截断（点 ▾ 展开）· 超 ${USER_HIDE_CHARS} 字或系统注入消息（任务通知/图片占位等）整段不展示 · 点卡片复制全文</span>` : ""}
    <span class="tf-group" role="group" aria-label="记录过滤">
      <button type="button" class="tf-btn${f === "all" ? " on" : ""}" data-tf="all">全部</button>
      <button type="button" class="tf-btn${f === "user" ? " on" : ""}" data-tf="user">只看我</button>
      <button type="button" class="tf-btn${f === "ai" ? " on" : ""}" data-tf="ai">只看AI</button>
    </span>
  </div>`;
}

// 「只看我」模式用于快速扫提示词列表：长提示词只留头部一段，点 ▾ 展开全文；
// 折叠态点卡片复制的仍是完整原文（data-full 携带），不是屏幕上的截断文本。
function renderUser(ev, num, tr) {
  const truncate = tr && (tr.filter || "all") === "user";
  const key = trEventKey(ev);
  const clip = truncate && ev.text.length > USER_COLLAPSE_CHARS;
  const expanded = clip && tr.expanded.has(key);
  let content;
  if (clip && !expanded) {
    // 截断点落在代理对（emoji 等）中间会出替换符：退一格保完整字符
    const cut = ev.text.slice(0, USER_COLLAPSE_CHARS);
    const head = /[\uD800-\uDBFF]$/.test(cut) ? cut.slice(0, -1) : cut;
    content = `<p>${esc(head)}…</p>
      <button type="button" class="tr-toggle tr-more"
        data-expand="${esc(key)}">⋯ 共 ${ev.text.length} 字，展开全文 ▾</button>`;
  } else {
    content = `<p>${esc(ev.text)}</p>`;
    if (clip) {
      content += `<button type="button" class="tr-toggle"
        data-collapse="${esc(key)}">收起 ▲</button>`;
    }
  }
  const full = clip && !expanded ? ` data-full="${esc(ev.text)}"` : "";
  return `<div class="tr user"><div class="tr-who">我</div>
    <div class="tr-body"><div class="user-bubble"${full} title="点击复制这条提示词">${content}
    <div class="u-foot"><span class="u-num">#${num}</span><span class="tr-ts">${trFmtTs(ev)}</span></div></div></div></div>`;
}

function renderAssistant(ev, expanded) {
  const clips = aiClips(ev.text);
  let content;
  if (!clips || expanded) {
    content = `<p>${esc(ev.text)}</p>`;
    if (clips) {
      content += `<button type="button" class="tr-toggle"
        data-collapse="${esc(trEventKey(ev))}">收起 ▲</button>`;
    }
  } else {
    content = `<p>${esc(clips.head)}</p>
      <button type="button" class="tr-toggle tr-more"
        data-expand="${esc(trEventKey(ev))}">⋯ 中间省略 ${clips.omitted} 字，展开全文 ▾</button>
      <p>${esc(clips.tail)}</p>`;
  }
  return `<div class="tr assistant"><div class="tr-who">AI</div>
    <div class="tr-body">${content}<span class="tr-ts">${trFmtTs(ev)}</span></div></div>`;
}

// 连续的工具调用 + 工具结果合成一个"过程组"：折叠态一行（首个调用 + 等N项），
// 只有一个工具调用且没有结果时就是一行明细，不必展开。
function renderToolGroup(evs, expanded) {
  const tools = evs.filter((e) => e.kind === "tool");
  const results = evs.filter((e) => e.kind === "toolresult");
  const first = tools[0] || evs[0];
  const multi = tools.length + results.length > 1;
  let bar;
  if (tools.length) {
    const names = [...new Set(tools.map((t) => t.name))];
    // 折叠条一行会被省略号截断，title 给完整明细（多行，hover 可见）
    const fullTitle = tools.map((t) => `⚙ ${t.name} ${t.detail || ""}`.trim()).join("\n")
      + (results.length ? `\n↩ ${results.length} 条工具结果` : "");
    if (!multi) {
      bar = `<div class="trg-bar-static" title="${esc(fullTitle)}"><span class="tr-tool-name">⚙ ${esc(first.name)}</span>
        ${esc(first.detail || "")}</div>`;
    } else {
      const arrow = expanded ? "▲" : "▾";
      bar = `<button type="button" class="trg-bar" title="${esc(fullTitle)}"
        data-${expanded ? "collapse" : "expand"}="${esc(trEventKey(first))}">
        <span class="tr-tool-name">⚙ ${esc(first.name)}</span>
        <span class="trg-detail">${esc(first.detail || "")}</span>
        <span class="trg-count">等 ${tools.length + results.length} 项 · ${esc(names.join(" · "))} ${arrow}</span>
      </button>`;
    }
  } else if (!multi) {
    // 尾窗口只切到 1 条结果（调用在前一窗口）：直接静态显示，别做点开为空的死按钮
    bar = `<div class="trg-bar-static trg-result${first.is_error ? " trg-error" : ""}"
      title="${esc(first.text)}">↩ ${esc(first.text)}</div>`;
  } else {
    // 多条孤立结果：折叠成组
    bar = `<button type="button" class="trg-bar"
      data-${expanded ? "collapse" : "expand"}="${esc(trEventKey(first))}">
      <span class="tr-tool-name">↩ 工具结果</span>
      <span class="trg-count">${results.length} 条 ${expanded ? "▲" : "▾"}</span>
    </button>`;
  }
  let items = "";
  if (multi && expanded) {
    items = `<div class="trg-items">` + evs.map((e) => {
      if (e.kind === "tool") {
        return `<div class="trg-item"><span class="tr-tool-name">⚙ ${esc(e.name)}</span>
          ${esc(e.detail || "")}</div>`;
      }
      return `<div class="trg-item trg-result${e.is_error ? " trg-error" : ""}">${esc(e.text)}</div>`;
    }).join("") + `</div>`;
  }
  return `<div class="tr tool"><div class="tr-who">TOOL</div>
    <div class="tr-body">${bar}${items}<span class="tr-ts">${trFmtTs(first)}</span></div></div>`;
}

// 全量重绘：事件最多 220 条、折叠态每段只画几百字，每 2.5s 重绘开销可忽略；
// 换来工具成组与展开状态的简单正确。重绘前后保持滚动位置（贴底则继续贴底）。
// 参数化 box/tr：外部会话与历史回看共用这套渲染。
function renderTranscriptInto(box, tr) {
  if (!tr) return;
  // innerHTML 全量替换后浏览器会保留 scrollTop 数值：纯追加/中部展开时视口本就不动，
  // 只有顶部 220 修剪改变了首元素位置才需补偿（旧实现按 scrollHeight 增量补偿会把
  // 上滑阅读的视口主动拽向底部）。
  const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 90;
  const prevAnchor = box.firstElementChild ? box.firstElementChild.offsetTop : 0;
  const evs = tr.events;
  const filter = tr.filter || "all";
  const parts = [renderFilterBar(tr)];
  let userSeq = 0;
  for (let i = 0; i < evs.length;) {
    const ev = evs[i];
    if (filter === "user") {
      // 服务端已只回用户提示词，这里再滤一道兜底；超 USER_HIDE_CHARS 的整段不展示
      if (ev.role === "user" && ev.kind === "text"
          && (ev.text || "").length <= USER_HIDE_CHARS) {
        parts.push(renderUser(ev, ++userSeq, tr));
      }
      i++;
    } else if (filter === "ai") {
      if (ev.role === "assistant" && ev.kind === "text") {
        parts.push(renderAssistant(ev, tr.expanded.has(trEventKey(ev))));
      }
      i++;
    } else if (ev.kind === "tool" || ev.kind === "toolresult") {
      let j = i;
      while (j < evs.length && (evs[j].kind === "tool" || evs[j].kind === "toolresult")) j++;
      const grp = evs.slice(i, j);
      parts.push(renderToolGroup(grp, tr.expanded.has(trEventKey(grp[0]))));
      i = j;
    } else if (ev.role === "user") {
      parts.push(renderUser(ev, ++userSeq, tr));
      i++;
    } else {
      parts.push(renderAssistant(ev, tr.expanded.has(trEventKey(ev))));
      i++;
    }
  }
  box.innerHTML = parts.join("");
  if (stick) {
    box.scrollTop = box.scrollHeight;
  } else {
    // 只补偿顶部被修剪掉的高度：纯追加/中部展开时新旧首元素 offsetTop 之差为 0
    const newAnchor = box.firstElementChild ? box.firstElementChild.offsetTop : 0;
    box.scrollTop = Math.max(0, box.scrollTop + newAnchor - prevAnchor);
  }
}

function renderTranscript() {
  renderTranscriptInto($("#transcript"), state.tr);
}

function appendEvents(tr, box, events, reset) {
  if (!tr) return;
  if (reset) tr.events = [];
  tr.events.push(...events);
  if (tr.events.length > TR_MAX_EVENTS) {
    tr.events.splice(0, tr.events.length - TR_MAX_EVENTS);
  }
  renderTranscriptInto(box, tr);
}

function renderEvents(events, reset) {
  appendEvents(state.tr, $("#transcript"), events, reset);
}

// 切换记录过滤。离开「只看我」必须从头重抓：该模式用全量提示词覆盖过
// tr.events，事件流已不完整，退回普通模式不能沿用旧 after 增量。
function setTrFilter(box, tr, f) {
  if (!tr || (tr.filter || "all") === f) return;
  const leavingUser = tr.filter === "user";
  tr.filter = f;
  localStorage.setItem("crw_tf", f);
  if (leavingUser) {
    tr.after = 0;
    tr.first = true;
    tr.events = [];
    box.innerHTML = `<div class="tr-hint">正在读取最近记录…</div>`;
  }
  if (f === "user" || leavingUser) {
    if (state.sel && state.sel.kind === "hist") pollHistTranscript();
    else pollTranscript();
  } else {
    renderTranscriptInto(box, tr);
  }
}

// 折叠/展开 + 过滤切换 + 提示词复制的事件委托：
// 每 2.5s 全量重绘会重建子节点，监听挂在静态父节点上不丢
function bindTrToggle(box, getTr) {
  box.addEventListener("click", (e) => {
    const tr = getTr();
    if (!tr) return;
    const tf = e.target.closest("[data-tf]");
    if (tf) { setTrFilter(box, tr, tf.dataset.tf); return; }
    // 展开/收起按钮可能嵌在金框里（「只看我」截断的提示词），必须先于复制分支判断
    const btn = e.target.closest("[data-expand],[data-collapse]");
    if (btn) {
      if (btn.dataset.expand) tr.expanded.add(btn.dataset.expand);
      else tr.expanded.delete(btn.dataset.collapse);
      renderTranscriptInto(box, tr);
      return;
    }
    const bubble = e.target.closest(".user-bubble");
    if (bubble) {
      // 折叠态复制的仍是完整原文（data-full），不是屏幕上的截断文本
      const p = bubble.querySelector("p");
      const text = bubble.dataset.full || (p ? p.textContent : "");
      if (text) copyText(text).then((ok) => toast(ok ? "已复制提示词" : "复制失败", !ok));
      return;
    }
  });
}
bindTrToggle($("#transcript"), () => state.tr);
bindTrToggle($("#hist-transcript"), () => state.htr);

async function pollTranscript() {
  if (!state.sel || state.sel.kind !== "ext" || !state.tr || !state.tr.file
      || state.tr.loading) return;
  const tr = state.tr;
  // 公网慢链路上一轮可能还没回来：2.5s 轮询/会话列表/发送后 setTimeout 三处都会
  // 调本函数，同 after 并发会把同一批事件渲染两次（且 after 推进后不自愈）
  if (tr.inflight) return;
  // 发请求前快照身份：响应回来时若会话/文件视图已切换，结果作废，
  // 否则 A 会话迟到的响应会把 A 的消息追加进 B 的视图（客户端版串台）。
  const selPid = state.sel.id;
  const file = tr.file;
  const after = tr.after;
  const userMode = tr.filter === "user";  // 全量提示词模式：不走 after 增量
  tr.inflight = true;
  try {
    const r = await api(userMode
      ? `/api/transcript?pid=${selPid}&file=${encodeURIComponent(file)}&mode=user`
      : `/api/transcript?pid=${selPid}&file=${encodeURIComponent(file)}&after=${after}`);
    // tr.file 比对：同一 tr 对象内手动切下拉时，在途旧文件响应必须作废
    if (!state.sel || state.sel.kind !== "ext" || state.sel.id !== selPid
        || state.tr !== tr || tr.file !== file) return;
    if (userMode) {
      // 无条件重绘：从「全部」切来且全文扫描为 0 条提示词时，
      // 跳过重绘会把旧内容留在「只看我」过滤条下（陈旧视图）
      renderEvents(r.events, true);
      tr.first = false;
      return;  // 不推进 tr.after：流模式游标保持切走时的位置
    }
    if (tr.first && !r.events.length) {
      // 关联文件已确定却暂时读不到事件：多半是尾部巨型行正在写入，下轮轮询会补上
      $("#transcript").innerHTML = `<div class="tr-hint">正在读取最近记录…</div>`;
    } else if (r.events.length || after === 0) {
      renderEvents(r.events, after === 0);
    }
    tr.after = r.size;
    tr.first = false;
  } catch (e) { /* 等下轮 */ } finally {
    tr.inflight = false;
  }
}

/* ---------------- 发送回执（outbox） ---------------- */
/* 「已注入」≠「已受理」：Claude 忙时注入文本在终端里排队，受理（落盘成记录）
   可能迟几分钟~几小时。这里把每条从会话台发出的消息的终态摆出来，跨天可查。 */
const OB_MAX_ROWS = 3;  // 回执区瘦身：最新 3 条，更多由 max-height 滚动兜底

function obFmtTs(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  return d.toLocaleString("zh-CN", sameDay
    ? { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" }
    : { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function obRow(it) {
  let st;
  if (it.status === "acked") {
    st = `<span class="ob-st ob-ok">✓ 已受理 ${obFmtTs(it.ack_ts)}</span>`;
  } else if (it.status === "failed") {
    st = `<span class="ob-st ob-bad" title="${esc(it.error || "")}">✗ 注入失败</span>`;
  } else {
    st = `<span class="ob-st ob-pending"
      title="已注入终端；Claude 忙时消息会排队，受理后（出现在上方记录里）自动打勾">⏳ 待受理</span>`;
  }
  const text = (it.text || "").replace(/\s+/g, " ").trim();
  const cls = it.status === "pending" ? " ob-pending-row" : "";
  return `<div class="ob-row${cls}">${st}<span class="ob-ts">${obFmtTs(it.ts)}</span>
    <span class="ob-text" title="${esc(it.text || "")}">${esc(text.slice(0, 60))}</span></div>`;
}

function renderOutbox(box, data, stale) {
  const items = (data.items || []).slice(-OB_MAX_ROWS).reverse();
  if (!items.length && !stale) { box.classList.add("hidden"); box.innerHTML = ""; return; }
  box.classList.remove("hidden");
  const notes = [];
  if (stale) notes.push("会话已退出，以下为退出前状态");
  if (data && data.persistent === false) notes.push("未配置 MySQL，仅保留近期记录");
  box.innerHTML = `<div class="ob-head">发送回执
    ${notes.length ? `<span class="ob-note">（${esc(notes.join("；"))}）</span>` : ""}</div>`
    + items.map(obRow).join("");
}

async function pollOutbox() {
  if (!state.sel || state.obInflight) return;
  const kind = state.sel.kind, id = state.sel.id;
  if (kind !== "ext" && kind !== "mgr") return;
  const box = $(kind === "ext" ? "#outbox" : "#mgr-outbox");
  const url = kind === "ext" ? `/api/session/${id}/outbox` : `/api/managed/${id}/outbox`;
  state.obInflight = true;
  try {
    const data = await api(url);
    if (!state.sel || state.sel.kind !== kind || state.sel.id !== id) return;
    renderOutbox(box, data, false);
  } catch (e) {
    // 404 = 会话已退出：保留最后一份回执（第二天来查的场景），只加陈旧标注
    if (/不存在|已退出/.test(e.message || "") && box.innerHTML) {
      const head = box.querySelector(".ob-head");
      if (head && !head.querySelector(".ob-note")) {
        head.insertAdjacentHTML("beforeend",
          `<span class="ob-note">（会话已退出，以下为退出前状态）</span>`);
      }
    }
  } finally {
    state.obInflight = false;
  }
}

$("#transcript-select").addEventListener("change", (e) => {
  state.tr.file = e.target.value || null;
  state.tr.after = 0;
  state.tr.first = true;
  state.tr.manual = true;  // 用户手动选定后，轮询不得再自动抢切
  state.tr.events = [];
  state.tr.expanded = new Set();
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
  // pid / 是否已关联都在 await 前捕获：请求期间切会话不把结果记到别的会话头上
  const pid = state.sel.id;
  const hadFile = !!(state.tr && state.tr.file);
  try {
    const r = await post(`/api/session/${pid}/input`, { text, submit: true });
    if (r.ok) {
      ta.value = "";
      $("#probe-badge").className = "badge badge-ok";
      // 注入成功 ≠ Claude 已受理（忙时排队）：终态看下方「发送回执」
      $("#probe-badge").textContent = `已注入 PID ${pid}，待受理…`;
      pollOutbox();  // prompt_log 同步落库，立即可见 pending 行
      // 记录还没关联时刷新会话列表（后端发现新 jsonl 后会自动切到正确记录）
      setTimeout(() => {
        (hadFile ? pollTranscript : pollSessions)();
        pollOutbox();
      }, 1800);
    } else toast(r.error || "注入失败", true);
  } catch (caused) { toast(caused.message, true); }
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

/* ---------------- 历史会话回看（只读，扫盘视图） ---------------- */
async function pollHistory() {
  try {
    const data = await api("/api/history");
    state.hist = data.items || [];
    renderLists();
  } catch (e) { /* 静默，等下轮 */ }
}

async function openHistory(idx) {
  const item = state.hist[idx];
  if (!item) return;
  state.htr = { dir: item.dir, file: item.file, after: 0, first: true,
                events: [], expanded: new Set(), inflight: false,
                filter: localStorage.getItem("crw_tf") || "all" };
  const title = item.title || item.preview || item.file.slice(0, 18);
  $("#hist-title").textContent = title;
  $("#hist-title").title = `${item.dir}/${item.file}`;
  $("#hist-meta").textContent =
    `最后写入 ${new Date(item.mtime).toLocaleString("zh-CN", { hour12: false })}` +
    `　${item.dir}${item.bridge ? "　·　微信桥任务" : ""}`;
  $("#hist-transcript").innerHTML = `<div class="tr-hint">加载历史记录…</div>`;
  pollHistTranscript();
}

async function pollHistTranscript() {
  const tr = state.htr;
  if (!state.sel || state.sel.kind !== "hist" || !tr || tr.inflight) return;
  // 快照身份：响应回来时若已切走，结果作废（与 pollTranscript 同款防串台）
  const selIdx = state.sel.id;
  const after = tr.after;
  const userMode = tr.filter === "user";  // 全量提示词模式：不走 after 增量
  tr.inflight = true;
  try {
    const r = await api(`/api/history/transcript?dir=${encodeURIComponent(tr.dir)}` +
      `&file=${encodeURIComponent(tr.file)}` +
      (userMode ? `&mode=user` : `&after=${after}`));
    if (!state.sel || state.sel.kind !== "hist" || state.sel.id !== selIdx
        || state.htr !== tr) return;
    if (userMode) {
      if (r.events.length || tr.first) appendEvents(tr, $("#hist-transcript"), r.events, true);
      tr.first = false;
      return;  // 不推进 tr.after：流模式游标保持切走时的位置
    }
    if (tr.first && !r.events.length) {
      $("#hist-transcript").innerHTML = `<div class="tr-hint">正在读取记录…</div>`;
    } else if (r.events.length || after === 0) {
      appendEvents(tr, $("#hist-transcript"), r.events, after === 0);
    }
    tr.after = r.size;
    tr.first = false;
  } catch (e) { /* 等下轮 */ } finally {
    tr.inflight = false;
  }
}
$("#btn-hist-refresh").addEventListener("click", pollHistory);

/* ---------------- 托管会话 / xterm ---------------- */
// 应用层心跳：浏览器 WS API 发不了协议层 ping。手机切后台/弱网会把连接掐成
// 半开（readyState 还是 OPEN 但实际死了，send 静默丢）或已关（点了发送没反应），
// 心跳 pong 超时判死 → 主动断开走自动重连。
const WS_PING_MS = 15000;
const WS_PONG_TIMEOUT = 45000;

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
  state.mgrRx = 0;  // 新终端从零起：服务端按 resume(have=0) 全量回放缓冲
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open($("#terminal"));
  requestAnimationFrame(() => { try { fit.fit(); } catch { /* 容器未稳定 */ } });

  // 终端输入走 state.ws 现查：重连换新连接后旧闭包不会把输入写进死 WS
  term.onData((d) => {
    if (state.ws && state.ws.readyState === WebSocket.OPEN)
      state.ws.send(JSON.stringify({ type: "input", data: d }));
  });
  let lastDims = "";
  const sendResize = () => {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
    try { fit.fit(); } catch { return; }
    const dims = term.cols + "x" + term.rows;
    if (dims === lastDims) return;   // 尺寸未变不回发，防止 observer 回环
    lastDims = dims;
    state.ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
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
  state.mgrSendResize = sendResize;  // 重连换新 WS 后 onopen 要重同步尺寸
  $("#mgr-outbox").classList.add("hidden");  // 切会话即清旧回执
  $("#mgr-outbox").innerHTML = "";
  connectManagedWs(id);
  pollOutbox();
}

function connectManagedWs(id) {
  if (state.ws) { try { state.ws.onclose = null; state.ws.close(); } catch { /* noop */ } state.ws = null; }
  const badge = $("#ws-badge");
  badge.className = "badge badge-unknown"; badge.textContent = "连接中…";
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/managed/${id}?token=${encodeURIComponent(state.token)}`);
  state.ws = ws;
  ws.onopen = () => {
    if (state.ws !== ws) return;  // 已被更新的连接取代
    state.wsAttempts = 0;
    state.lastPong = Date.now();
    // 断连补差握手：报已收到的字符偏移，服务端只补差额（mgrRx=0 即全量回放）
    ws.send(JSON.stringify({ type: "resume", have: state.mgrRx || 0 }));
    badge.className = "badge badge-ok"; badge.textContent = "已连接";
    if (state.mgrSendResize) state.mgrSendResize();
    startPing(ws);
    // 断线期间点过发送：重连成功自动补发（输入框原文未被清空）
    if (state.pendingSend && state.sel && state.sel.kind === "mgr" && state.sel.id === id) {
      const pending = state.pendingSend;
      state.pendingSend = null;
      ws.send(JSON.stringify({ type: "input", data: pending.replace(/\r/g, "") + "\r" }));
      const ta = $("#mgr-input");
      if (ta.value === pending) ta.value = "";
      toast("已重连并自动补发 ✓");
      setTimeout(pollOutbox, 400);
    }
  };
  ws.onmessage = (ev) => {
    // 服务端只发 JSON 帧：pong/hello/reset/d（PTY 输出也封装为 d 帧带偏移）
    let msg = null;
    try { msg = JSON.parse(ev.data); } catch { /* 非 JSON 帧忽略 */ }
    if (!msg) return;
    if (msg.type === "pong") { state.lastPong = Date.now(); return; }
    if (msg.type === "hello") return;  // 能力宣告（total/oldest 区间），无需动作
    if (msg.type === "reset") {
      // 断连太久缓冲翻篇：服务端将整段重发，终端先清屏对齐
      if (state.term) { try { state.term.reset(); } catch { /* noop */ } }
      state.mgrRx = 0;
      return;
    }
    if (msg.type === "d") {
      if (state.term) state.term.write(msg.s);
      state.mgrRx = msg.o + msg.s.length;
    }
  };
  ws.onclose = () => {
    if (state.ws !== ws) return;  // 旧连接的迟到回包
    state.ws = null;
    stopPing();
    badge.className = "badge badge-bad"; badge.textContent = "已断开，重连中…";
    if (state.sel && state.sel.kind === "mgr" && state.sel.id === id) {
      scheduleManagedReconnect(id, false);
    }
  };
}

/* 半开连接判定：readyState 是 OPEN 不代表活着——手机切后台/弱网后连接已死但
   状态仍 OPEN，ws.send 写进死 socket 静默吞消息（2026-10-25 用户实测两条
   「你好」只受理一条的根因）。发送前查 pong 新鲜度：健康链路每 15s 心跳，
   pong 年龄超 20s 即判死，消息转 pendingSend + 强制重连补发，绝不盲发。 */
const WS_SEND_STALE_MS = 20000;
function wsFreshForSend() {
  return !!(state.ws && state.ws.readyState === WebSocket.OPEN
    && Date.now() - (state.lastPong || 0) < WS_SEND_STALE_MS);
}
function wsAlive() {
  return !!(state.ws && state.ws.readyState === WebSocket.OPEN
    && Date.now() - (state.lastPong || 0) < WS_PONG_TIMEOUT);
}
function forceReconnectManaged(id) {
  if (state.wsRetry) { clearTimeout(state.wsRetry); state.wsRetry = null; }
  if (state.ws) { try { state.ws.onclose = null; state.ws.close(); } catch { /* noop */ } state.ws = null; }
  connectManagedWs(id);
}

function startPing(ws) {
  stopPing();
  state.pingTimer = setInterval(() => {
    if (state.ws !== ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: "ping" }));
    if (Date.now() - state.lastPong > WS_PONG_TIMEOUT) {
      try { ws.close(); } catch { /* noop */ }  // 半开死链：触发 onclose 走重连
    }
  }, WS_PING_MS);
}
function stopPing() {
  if (state.pingTimer) { clearInterval(state.pingTimer); state.pingTimer = null; }
}

function scheduleManagedReconnect(id, immediate) {
  if (state.wsRetry) { clearTimeout(state.wsRetry); state.wsRetry = null; }
  if (wsAlive()) return;  // 已有活连接；半开死链不拦，继续走强制重连
  if (!state.sel || state.sel.kind !== "mgr" || state.sel.id !== id) return;
  // 退避 2s→4s→…→15s 封顶；immediate 用于点发送时发现断链立即重连
  const delay = immediate ? 0 : Math.min(2000 * Math.max(1, state.wsAttempts), 15000);
  state.wsAttempts += 1;
  state.wsRetry = setTimeout(() => {
    state.wsRetry = null;
    if (state.sel && state.sel.kind === "mgr" && state.sel.id === id && !wsAlive()) {
      forceReconnectManaged(id);
    }
  }, delay);
}

/* 回到前台时若连接已死立即重连（不等退避计时器，手机解锁即恢复） */
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  if (state.sel && state.sel.kind === "mgr" && !wsAlive()) {
    scheduleManagedReconnect(state.sel.id, true);
  }
});

function closeManagedWs() {
  if (state.managedCleanup) { try { state.managedCleanup(); } catch { /* noop */ } state.managedCleanup = null; }
  if (state.wsRetry) { clearTimeout(state.wsRetry); state.wsRetry = null; }
  stopPing();
  // 切走即放弃待发：后台迟到的补发比"没发出去"更迷惑
  state.pendingSend = null;
  state.wsAttempts = 0;
  state.mgrSendResize = null;
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
  if (!text) return;
  if (!wsFreshForSend()) {
    // 断链/半开死链时绝不能静默吞掉（手机 bug：点了发送实际没发出去）。
    // 文字保留在输入框，记下待发内容并立即强制重连，onopen 里自动补发。
    state.pendingSend = text;
    toast("连接已断开，正在重连…重连后自动补发", true);
    if (state.sel && state.sel.kind === "mgr") {
      forceReconnectManaged(state.sel.id);
    }
    return;
  }
  // 与 xterm 粘贴路径一致：原文 + 回车（Claude 输入框支持多行粘贴）
  state.ws.send(JSON.stringify({ type: "input", data: text.replace(/\r/g, "") + "\r" }));
  ta.value = "";
  ta.focus();
  setTimeout(pollOutbox, 400);  // 服务端落库后立即可见 pending 回执
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

/* ---------------- 套餐额度徽标（代理本机 quota-monitor） ---------------- */
/* 三段并列常驻：5小时窗口 / 本周 / 本月，各自按阈值上色，点击立即刷新 */
const QUOTA_REFRESH_MS = 600000;  // 与 quota-monitor 的 10 分钟刷新周期对齐
const PERIOD_ORDER = ["session", "5h", "weekly", "monthly"];
const PERIOD_NAMES = { "5h": "5小时窗口", session: "5小时窗口", weekly: "本周", monthly: "本月" };
const PERIOD_TAGS  = { "5h": "5h", session: "5h", weekly: "周", monthly: "月" };

function activeQuotaPlan() {
  const d = state.quota.data;
  if (!d || !d.ok) return null;
  return (d.items || []).find((it) => it.subscribed && (it.periods || []).length) || null;
}

function resetLabel(p) {
  return p.reset_at
    ? new Date(p.reset_at).toLocaleString("zh-CN",
        { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })
    : "";
}

function periodPct(p) {
  // 上游契约保证 percent 存在；缺字段时降级为 null（显「—」），不渲染 NaN
  return typeof p.percent === "number" && Number.isFinite(p.percent) ? p.percent : null;
}

function periodLevelClass(pct) {
  return pct === null ? "" : pct >= 90 ? "qb-danger" : pct >= 70 ? "qb-warn" : "";
}

function orderedPeriods(plan) {
  const rank = (label) => {
    const i = PERIOD_ORDER.indexOf(label);
    return i < 0 ? PERIOD_ORDER.length : i;
  };
  return plan.periods.slice().sort((a, b) => rank(a.label) - rank(b.label));
}

function renderQuota() {
  const badge = $("#quota-badge");
  const data = state.quota.data;
  const plan = activeQuotaPlan();
  badge.classList.remove("hidden");
  renderCcChip();
  if (!$("#cc-panel").classList.contains("hidden")) renderCcPanel();
  if (!plan) {
    // ok=false（上游没起 / arkcli 失败）：显示低调的 —，点击可手动重试
    badge.className = "quota-badge qb-error";
    badge.innerHTML = `<span class="qb-seg"><span class="qb-lab">额度</span><span class="qb-num">—</span></span>`;
    let why;
    if (data && data.ok) {
      // 上游正常但没有已订阅套餐（如订阅失效 / 未绑定席位），重试无济于事，透出上游错误串
      const item = (data.items || []).find((it) => it.error);
      why = (item && item.error) ? item.error : "暂无已订阅套餐";
    } else {
      why = (data && data.error) ? data.error : "额度数据不可用";
    }
    badge.title = why + "（点击重试）";
    badge.setAttribute("aria-label", "套餐额度：" + why);
    return;
  }
  const periods = orderedPeriods(plan);
  const summary = [];
  badge.className = "quota-badge" + (data.stale ? " qb-stale" : "");
  badge.innerHTML = periods.map((p) => {
    const pct = periodPct(p);
    // 阈值按显示值（四舍五入后）判定，避免 89.6% 显示「90」却是金色
    const shown = pct === null ? null : Math.round(pct);
    const lvl = periodLevelClass(shown);
    const tag = PERIOD_TAGS[p.label] || esc(p.label);
    const val = shown === null ? "—" : shown;
    summary.push(`${PERIOD_NAMES[p.label] || p.label} ${val}%`);
    return `<span class="qb-seg${lvl ? " " + lvl : ""}">`
      + `<span class="qb-lab">${tag}</span><span class="qb-num">${val}</span></span>`;
  }).join("");
  const lines = [plan.display_name + "（点击立即刷新）"];
  for (const p of periods) {
    const pct = periodPct(p);
    let line = `${PERIOD_NAMES[p.label] || p.label}：${pct === null ? "用量未知" : "已用 " + pct + "%"}`;
    const reset = resetLabel(p);
    if (reset) line += "，" + reset + " 重置";
    lines.push(line);
  }
  if (data.stale) lines.push("数据已过期，后台自动重试中");
  badge.title = lines.join("\n");
  badge.setAttribute("aria-label", "套餐额度：" + summary.join("，"));
}

/* ---------------- CC KEY 面板（quota-monitor 功能简化移植） ---------------- */
/* 当前 key / AUTO 开关 / 自定义 key 库 / 双账号用量 / cpolar 公网地址。
   数据全部来自 /api/quota 的 accounts+cc_switch 与 /api/tunnels，不直连 8771。 */

const ROLE_NAMES = { primary: "主", backup: "备" };

function ccSwitch() {
  // 不门 d.ok：ok 只反映「展示账号」的取数结果，当前 key 账号挂了的时刻
  // 正是最需要切 key 的时刻，芯片不能跟着消失（cc_switch 本身仍透传完好）
  const d = state.quota.data;
  return (d && d.cc_switch) || null;
}

function renderCcChip() {
  const chip = $("#cckey-chip");
  const cc = ccSwitch();
  if (!cc || !(cc.keys || []).length) {
    chip.classList.add("hidden");
    closeCcPanel();
    return;
  }
  const cur = (cc.keys || []).find((k) => k.current);
  chip.classList.remove("hidden");
  chip.textContent = (cur ? cur.label : "KEY") + (cc.auto_enabled ? " ·A" : "");
  chip.classList.toggle("cc-backup", !!cur && cur.id !== "primary");
  chip.classList.toggle("cc-auto", !!cc.auto_enabled);
  chip.title = "CC KEY：" + (cur ? cur.label : "未知") +
    "（AUTO " + (cc.auto_enabled ? "开" : "关") + "）";
  chip.setAttribute("aria-label", chip.title);
}

function accountPeriods(acc) {
  const plan = (acc.items || []).find((it) => it.subscribed && (it.periods || []).length);
  if (!plan) return "无订阅";
  return orderedPeriods(plan).map((p) => {
    const pct = periodPct(p);
    return `${PERIOD_TAGS[p.label] || p.label} ${pct === null ? "—" : Math.round(pct) + "%"}`;
  }).join(" ");
}

function renderCcPanel() {
  const d = state.quota.data || {};
  const cc = ccSwitch();
  const curKey = cc ? (cc.keys || []).find((k) => k.current) : null;
  $("#ccp-current").textContent = cc
    ? "· 当前 " + (curKey ? curKey.label : "未知")
    : "· 数据不可用";
  const autoBtn = $("#ccp-auto");
  autoBtn.textContent = "AUTO " + (cc && cc.auto_enabled ? "开" : "关");
  autoBtn.classList.toggle("on", !!(cc && cc.auto_enabled));
  autoBtn.disabled = !cc;

  // key 列表：内置主/备 + 自定义（可删）；当前使用中的打 ✓
  $("#ccp-keys").innerHTML = cc ? (cc.keys || []).map((k) =>
    `<div class="ccp-key${k.current ? " cur" : ""}">
      <span class="ccp-klabel" title="${esc(k.label)}">${esc(k.label)}</span>
      <span class="ccp-ktail">${esc(k.token_tail || "")}</span>
      ${k.current
        ? '<span class="ccp-cur">✓当前</span>'
        : `<button type="button" class="ccp-btn" data-switch="${esc(k.id)}">切换</button>`}
      ${k.builtin ? "" : `<button type="button" class="ccp-btn danger" data-delkey="${esc(k.id)}" title="删除该 key">×</button>`}
    </div>`).join("")
    : '<div class="ccp-empty">额度服务不可用</div>';

  // 双账号用量（主/备各一行，复用徽标的周期名与四舍五入规则）
  const accs = d.accounts || [];
  $("#ccp-usage").innerHTML = accs.length ? accs.map((acc) => {
    const role = ROLE_NAMES[acc.account_role] || acc.account_role || "?";
    const body = acc.ok ? accountPeriods(acc) : (acc.error || "取数失败");
    return `<div class="ccp-acct${acc.ok ? "" : " bad"}"><span class="ccp-role">${esc(role)}</span>${esc(body)}</div>`;
  }).join("") : '<div class="ccp-empty">—</div>';
}

let ccPanelBusy = false;

function setCcBusy(busy) {
  ccPanelBusy = busy;
  document.querySelectorAll("#cc-panel button").forEach((b) => { b.disabled = busy; });
}

async function ccAction(fn, { refetch = false } = {}) {
  /* 动作成功：上游返回 switcher.status()，直接写回 quota.data.cc_switch 重绘。
     写入后把序号守卫推到所有在途请求之外——动作前发出的轮询若晚到，
     拿回的是切换前的旧 cc_switch，绝不能把刚写入的新 status 覆盖回去。
     之后新发的请求（cc_switch 上游每次实算，只有账号用量走缓存）天然是新值。 */
  if (ccPanelBusy) return;
  setCcBusy(true);
  try {
    const status = await fn();
    if (status && state.quota.data) {
      state.quota.data.cc_switch = status;
      quotaAppliedSeq = quotaReqSeq + 1;
    }
    // 切 key 后展示账号可能变（主→备），补一次拉取重选顶层 items
    if (refetch) await fetchQuota(false);
    renderQuota();
  } catch (e) {
    toast(e.message || "操作失败", true);
  } finally {
    setCcBusy(false);
  }
}

function openCcPanel() {
  renderCcPanel();
  $("#cc-panel").classList.remove("hidden");
  $("#cckey-chip").setAttribute("aria-expanded", "true");
  loadTunnels(false);
}

function closeCcPanel() {
  $("#cc-panel").classList.add("hidden");
  const chip = $("#cckey-chip");
  if (chip) chip.setAttribute("aria-expanded", "false");
}

$("#cckey-chip").addEventListener("click", (e) => {
  e.stopPropagation();
  $("#cc-panel").classList.contains("hidden") ? openCcPanel() : closeCcPanel();
});
$("#cc-panel").addEventListener("click", (e) => e.stopPropagation());
document.addEventListener("click", closeCcPanel);

$("#ccp-auto").addEventListener("click", () => {
  const cc = ccSwitch();
  if (!cc) return;
  ccAction(() => post("/api/cc-switch/auto", { enabled: !cc.auto_enabled }));
});

// key 列表的 切换/删除 走事件委托（列表每次重绘，不能逐个绑）
$("#ccp-keys").addEventListener("click", (e) => {
  const sw = e.target.closest("[data-switch]");
  const dk = e.target.closest("[data-delkey]");
  if (sw) ccAction(() => post("/api/cc-switch/mode", { key_id: sw.dataset.switch }), { refetch: true });
  else if (dk) ccAction(() => del("/api/cc-keys/" + encodeURIComponent(dk.dataset.delkey)));
});

$("#ccp-add-btn").addEventListener("click", () => {
  const label = $("#ccp-label").value.trim();
  const token = $("#ccp-token").value.trim();
  if (!label || !token) { toast("名称和 token 都要填", true); return; }
  ccAction(async () => {
    const status = await post("/api/cc-keys", { label, token });
    $("#ccp-label").value = "";
    $("#ccp-token").value = "";
    toast("已添加 " + label);
    return status;
  });
});

/* ---------------- 广播：一条提示词发全部会话 ---------------- */
let bcBusy = false;

function bcTargetRow(t, result) {
  const label = t.kind === "ext" ? "PID " + t.pid : "托管 " + t.id;
  const name = t.title || t.cwd || "";
  if (!result) {  // 预览行：无结果
    return `<div class="bc-trow"><span>·</span><span>${esc(label)}</span>` +
      `<span class="bc-cwd" title="${esc(t.cwd || "")}">${esc(name)}</span></div>`;
  }
  return `<div class="bc-trow">` +
    `<span class="${result.ok ? "bc-ok" : "bc-bad"}">${result.ok ? "✓" : "✗"}</span>` +
    `<span>${esc(label)}</span>` +
    (result.ok
      ? `<span class="bc-cwd" title="${esc(t.cwd || "")}">${esc(name)}</span>`
      : `<span class="bc-err" title="${esc(result.error || "")}">${esc(result.error || "失败")}</span>`) +
    `</div>`;
}

async function bcPreview() {
  const d = await post("/api/broadcast", { text: $("#bc-text").value || "预览", dry_run: true });
  const targets = d.targets || [];
  $("#bc-count").textContent = targets.length + " 个目标";
  $("#bc-targets").innerHTML = targets.length
    ? targets.map((t) => bcTargetRow(t, null)).join("")
    : `<div class="bc-trow"><span>·</span><span>当前没有任何会话</span></div>`;
  return targets;
}

function openBcPanel() {
  $("#bc-panel").classList.remove("hidden");
  $("#btn-broadcast").setAttribute("aria-expanded", "true");
  bcPreview().catch(() => { $("#bc-count").textContent = "预览失败"; });
  $("#bc-text").focus();
}

function closeBcPanel() {
  $("#bc-panel").classList.add("hidden");
  const b = $("#btn-broadcast");
  if (b) b.setAttribute("aria-expanded", "false");
}

$("#btn-broadcast").addEventListener("click", (e) => {
  e.stopPropagation();
  $("#bc-panel").classList.contains("hidden") ? openBcPanel() : closeBcPanel();
});
$("#bc-panel").addEventListener("click", (e) => e.stopPropagation());
document.addEventListener("click", closeBcPanel);

$("#bc-preview").addEventListener("click", () => {
  bcPreview().catch((e) => toast(e.message, true));
});

$("#bc-send").addEventListener("click", async () => {
  const text = $("#bc-text").value.trim();
  if (!text) { toast("先写提示词", true); return; }
  if (bcBusy) return;
  let targets;
  try {
    targets = await bcPreview();  // 发送前总是以最新目标数确认
  } catch (e) { toast(e.message, true); return; }
  if (!targets.length) { toast("当前没有任何会话", true); return; }
  if (!window.confirm(`将向 ${targets.length} 个会话发送并提交：\n\n${text}\n\n确定？`)) return;
  bcBusy = true;
  $("#bc-send").disabled = true;
  try {
    const d = await post("/api/broadcast", { text, submit: true });
    const results = d.results || [];
    $("#bc-targets").innerHTML = results.map((t) => bcTargetRow(t, t)).join("");
    $("#bc-count").textContent = `${d.ok_count}/${d.total} 成功`;
    if (d.ok_count === d.total) {
      toast(`已广播到全部 ${d.total} 个会话 ✓`);
      $("#bc-text").value = "";
    } else {
      toast(`广播完成：${d.ok_count}/${d.total} 成功`, true);
    }
    setTimeout(pollSessions, 1500);  // 广播后各会话会产生新 jsonl，刷新列表关联
  } catch (e) {
    toast(e.message || "广播失败", true);
  } finally {
    bcBusy = false;
    $("#bc-send").disabled = false;
  }
});
let tunnelsLoaded = false;

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text); // http 局域网可能没有 clipboard API
    return true;
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.cssText = "position:fixed;opacity:0";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch { ok = false; }
    ta.remove();
    return ok;
  }
}

function renderTunnels(list, error) {
  $("#ccp-tunnels").innerHTML = list.length ? list.map((t) =>
    `<div class="ccp-tun">
      <span class="ccp-tlabel" title="${esc(t.note || t.name)}">${esc(t.label || t.name)}</span>
      <span class="ccp-turl" title="${esc(t.url)}">${esc(t.url)}</span>
      <button type="button" class="ccp-btn" data-copy="${esc(t.url)}">复制</button>
    </div>`).join("")
    : `<div class="ccp-empty">${esc(error || "未在 cpolar 日志中找到隧道地址")}</div>`;
}

async function loadTunnels(force) {
  if (tunnelsLoaded && !force) return;
  const btn = $("#ccp-tun-refresh");
  btn.classList.add("spinning");
  try {
    const d = await api("/api/tunnels");
    tunnelsLoaded = true;
    renderTunnels(d.tunnels || [], d.ok === false ? d.error : "");
  } catch (e) {
    renderTunnels([], e.message);
  } finally {
    btn.classList.remove("spinning");
  }
}

$("#ccp-tun-refresh").addEventListener("click", () => loadTunnels(true));
$("#ccp-tunnels").addEventListener("click", async (e) => {
  const cp = e.target.closest("[data-copy]");
  if (!cp) return;
  const ok = await copyText(cp.dataset.copy);
  toast(ok ? "已复制" : "复制失败，请长按手动复制", !ok);
});

// 在途请求去重 + 旧响应防覆盖：force/非 force 各最多一个在途（非 force 时若 force
// 在途则搭车，强刷数据更新，没必要再发一次）；序号保证晚启动的 force 响应即使
// 先到，后到的缓存旧响应也不会覆盖它。返回数据是否真的更新（false=请求失败）。
let quotaInflight = null; // { force, reqId, promise }
let quotaReqSeq = 0;
let quotaAppliedSeq = 0;

function fetchQuota(force) {
  if (quotaInflight && (quotaInflight.force === force || (!force && quotaInflight.force))) {
    return quotaInflight.promise;
  }
  const reqId = ++quotaReqSeq;
  const promise = (async () => {
    try {
      const data = await api("/api/quota" + (force ? "?force=1" : ""));
      if (reqId >= quotaAppliedSeq) {
        state.quota.data = data;
        quotaAppliedSeq = reqId;
      }
      return true;
    } catch {
      // 首次就失败（data=null）也要渲染「—」降级态，否则徽标一直隐藏、重试无门；
      // 已有旧数据时 renderQuota 仍显示上次结果，且返回 false 让强刷报失败而非假成功
      return false;
    } finally {
      if (quotaInflight && quotaInflight.reqId === reqId) quotaInflight = null;
    }
  })();
  quotaInflight = { force, reqId, promise };
  return promise;
}

async function pollQuota(force) {
  const ok = await fetchQuota(!!force);
  renderQuota();
  return ok;
}

// ↻ 强刷：绕过上游 540s 缓存立即取数；转圈防连点，成功后把 10 分钟轮询计时重起
let quotaTimer = null;
async function refreshQuota() {
  const btn = $("#quota-refresh");
  if (btn.disabled) return;
  btn.disabled = true;
  btn.classList.add("spinning");
  try {
    const ok = await pollQuota(true); // pollQuota 吞异常并渲染降级态，不抛出
    const d = state.quota.data;
    const plan = activeQuotaPlan();
    // 请求失败（ok=false）时 data 可能是旧值，绝不能把旧值当新值报成功
    if (!ok) {
      toast("额度刷新失败，请稍后重试", true);
      return;
    }
    if (!plan) {
      toast((d && d.error) ? d.error : "额度刷新失败", true);
      return;
    }
    toast(orderedPeriods(plan).map((p) => {
      const pct = periodPct(p);
      return `${PERIOD_NAMES[p.label] || p.label} ${pct === null ? "—" : Math.round(pct) + "%"}`;
    }).join(" · "));
    clearInterval(quotaTimer);
    quotaTimer = setInterval(pollQuota, QUOTA_REFRESH_MS);
  } finally {
    btn.disabled = false;
    btn.classList.remove("spinning");
  }
}
$("#quota-refresh").addEventListener("click", refreshQuota);

$("#quota-badge").addEventListener("click", () => {
  // 徽标点击同样绕过缓存强刷（tooltip 承诺「点击立即刷新」），复用 ↻ 的反馈与计时
  refreshQuota();
});

document.addEventListener("visibilitychange", () => {
  // 回到页面顺手刷新（上游有 540s 缓存，代价极小）；未过门不发请求
  if (!document.hidden && state.entered) pollQuota();
});

/* ---------------- 语音输入 ---------------- */
// Web Speech API 只在安全上下文可用：本机 127.0.0.1 / localhost / HTTPS。
// cpolar 公网是 HTTP（非安全上下文），按钮保留但置灰，点击给原因提示。
function setupSpeech(btnSel, taSel) {
  const btn = $(btnSel), ta = $(taSel);
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR || !window.isSecureContext) {
    btn.disabled = true;
    btn.title = "语音输入需在本机 127.0.0.1 或 HTTPS 下使用";
    btn.addEventListener("click", () =>
      toast("语音输入需在本机 127.0.0.1 或 HTTPS 下使用（公网 HTTP 没有麦克风权限）", true));
    return;
  }
  let rec = null, listening = false, baseText = "";
  btn.addEventListener("click", () => {
    if (listening) { try { rec.stop(); } catch { /* noop */ } return; }
    rec = new SR();
    rec.lang = "zh-CN";
    rec.interimResults = true;   // 边说边出字
    rec.continuous = true;       // 长句不中途断
    baseText = ta.value;
    rec.onresult = (e) => {
      let final = "", interim = "";
      for (let i = 0; i < e.results.length; i++) {
        if (e.results[i].isFinal) final += e.results[i][0].transcript;
        else interim += e.results[i][0].transcript;
      }
      const sep = baseText && !/\s$/.test(baseText) ? " " : "";
      ta.value = baseText + sep + final + interim;
    };
    rec.onerror = (e) => {
      listening = false; btn.classList.remove("rec");
      if (e.error === "not-allowed") toast("麦克风权限被拒绝，请在浏览器里允许", true);
      else if (e.error !== "aborted") toast("语音识别失败：" + e.error, true);
    };
    rec.onend = () => { listening = false; btn.classList.remove("rec"); };
    try {
      rec.start();
      listening = true; btn.classList.add("rec");
      ta.focus();
    } catch {
      listening = false; btn.classList.remove("rec");
      toast("语音识别启动失败", true);
    }
  });
}
setupSpeech("#btn-ext-mic", "#input-text");
setupSpeech("#btn-mgr-mic", "#mgr-input");

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
  // 401 回门后重新提交令牌会再次进入；幂等守卫防止 quotaTimer/轮询 setInterval 重复泄漏
  if (state.entered) return;
  $("#gate").classList.add("hidden");
  $("#app").classList.remove("hidden");
  state.entered = true;
  history.replaceState(null, "", location.pathname);
  try {
    const n = await api("/api/net");
    $("#net-info").innerHTML = n.ips.map((ip) => `http://${ip}:${n.port}`).join("<br>");
  } catch { /* noop */ }
  await pollSessions();
  setInterval(pollSessions, 5000);
  setInterval(() => {
    if (state.sel && state.sel.kind === "ext") { pollTranscript(); pollOutbox(); }
    else if (state.sel && state.sel.kind === "mgr") pollOutbox();
    else if (state.sel && state.sel.kind === "hist") pollHistTranscript();
  }, 2500);
  pollHistory();
  setInterval(pollHistory, 30000);
  $("#quota-refresh").classList.remove("hidden");
  pollQuota();
  quotaTimer = setInterval(pollQuota, QUOTA_REFRESH_MS);
}

if (state.token) {
  api("/api/health?token=" + encodeURIComponent(state.token))
    .then((h) => h.authed ? enterApp() : showGate()).catch(showGate);
} else {
  showGate();
}

/* ---------------- Web Push 完成通知 ---------------- */
/* 服务端巡检 jsonl mtime：busy(45s)→idle 跳变 + 尾部 stop_reason 判定
   （end_turn→跑完了 / tool_use→等你批准），Web Push 推送到本设备。
   需要安全上下文（https 或 127.0.0.1）：公网 HTTP 隧道下按钮置灰提示。 */
function urlB64ToUint8Array(s) {
  const padding = "=".repeat((4 - (s.length % 4)) % 4);
  const raw = atob((s + padding).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

async function setupPushButton() {
  const btn = $("#btn-push");
  if (!btn) return;
  const supported = "serviceWorker" in navigator && "PushManager" in window
    && "Notification" in window && "caches" in window;
  const paint = (on) => {
    btn.style.opacity = on ? "1" : ".45";
    btn.title = on ? "完成通知已开通（再点一次关闭本设备通知）"
      : window.isSecureContext ? "开通完成通知：会话跑完/等你批准时推送到此设备"
                               : "完成通知需 https 或 127.0.0.1 打开";
  };
  try {
    const reg0 = supported ? await navigator.serviceWorker.getRegistration("/sw.js") : null;
    const sub0 = reg0 ? await reg0.pushManager.getSubscription() : null;
    paint(!!sub0);
  } catch { paint(false); }
  btn.addEventListener("click", async () => {
    if (!supported) { toast("此浏览器不支持推送", true); return; }
    if (!window.isSecureContext) {
      toast("推送需 https 或 127.0.0.1 打开（公网隧道请用 https 地址）", true);
      return;
    }
    try {
      const reg = await navigator.serviceWorker.register("/sw.js");
      const existing = await reg.pushManager.getSubscription();
      if (existing) {  // 开关制：已订阅 → 关闭
        await post("/api/push/unsubscribe", { subscription: existing.toJSON() })
          .catch(() => null);
        await existing.unsubscribe();
        paint(false);
        toast("已关闭本设备的完成通知");
        return;
      }
      const perm = await Notification.requestPermission();
      if (perm !== "granted") { toast("通知权限被拒绝，请在浏览器设置里放行", true); return; }
      // token 交给 SW：点击通知时拼回带 token 的地址
      const cache = await caches.open("crw-cfg");
      await cache.put("/crw-cfg", new Response(JSON.stringify({ token: state.token }),
        { headers: { "Content-Type": "application/json" } }));
      const { publicKey } = await api("/api/push/vapid");
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlB64ToUint8Array(publicKey),
      });
      await post("/api/push/subscribe", { subscription: sub.toJSON() });
      paint(true);
      const r = await post("/api/push/test", {});
      toast(r && r.sent ? "已开通，测试通知已发出 🔔" : "已开通（无订阅送达，稍后会话跑完再验证）");
    } catch (e) {
      toast("推送操作失败: " + (e && e.message ? e.message : e), true);
    }
  });
}
setupPushButton();
