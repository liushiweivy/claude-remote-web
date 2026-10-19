/* CDP 会话记录渲染冒烟：用户提示金框醒目、AI 长文首尾折叠可展开、连续工具过程成组折叠、
 * 工具结果不得伪装成用户消息、零 console 错误。
 * 用法：node tools/cdp-transcript-check.mjs（需 9333 无头 Edge + 8765 服务，token 从 data/token.txt 读）
 * 产出 docs/transcript-*.png。 */
import { writeFile, mkdir, readFile } from "node:fs/promises";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const CDP = "http://127.0.0.1:9333";
const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const TOKEN = (await readFile(join(ROOT, "data", "token.txt"), "utf8")).trim();
const URL = `http://127.0.0.1:8765/?token=${TOKEN}`;
const OUT = join(ROOT, "docs");
await mkdir(OUT, { recursive: true });

const target = await (await fetch(CDP + "/json/new?about:blank", { method: "PUT" })).json();
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r) => (ws.onopen = r));

let mid = 0;
const pending = new Map();
const pageErrors = [];
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  if (m.method === "Runtime.exceptionThrown") {
    pageErrors.push(m.params.exceptionDetails.exception?.description || JSON.stringify(m.params.exceptionDetails));
  }
  if (m.method === "Log.entryAdded" && m.params.entry.level === "error") {
    pageErrors.push("console: " + m.params.entry.text + " @" + (m.params.entry.url || "?"));
  }
};
const send = (method, params = {}) => new Promise((res) => {
  const id = ++mid;
  pending.set(id, res);
  ws.send(JSON.stringify({ id, method, params }));
});
async function evalJs(expression) {
  const r = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails));
  return r.result?.result?.value;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitFor(fn, label, timeout = 8000, interval = 200) {
  const deadline = Date.now() + timeout;
  let value = false;
  while (Date.now() < deadline) {
    value = await fn();
    if (value) return value;
    await sleep(interval);
  }
  console.log("TIMEOUT waiting:", label, "last:", value);
  return value;
}
async function shot(name) {
  const r = await send("Page.captureScreenshot", { format: "png" });
  await writeFile(join(OUT, name), Buffer.from(r.result.data, "base64"));
  console.log("shot", name);
}

await send("Page.enable");
await send("Runtime.enable");
await send("Log.enable");

await send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 860, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: URL });
await waitFor(() => evalJs(`document.getElementById("app") && !document.getElementById("app").classList.contains("hidden")`), "app entered");

/* 后台 2.5s 轮询会对 mock 假文件发真请求产生预期内 404 console 错误（功能上被 catch 吞掉），
 * 桩成空增量响应保持静默；inflight 用例会自行替换/还原 fetch，不受影响。 */
await evalJs(`(() => {
  const realFetch = window.fetch.bind(window);
  window.fetch = (u, opts) => {
    if (String(u).includes("/api/transcript")) {
      return Promise.resolve({ status: 200, ok: true,
        json: () => Promise.resolve({ events: [], size: state.tr ? state.tr.after : 0 }) });
    }
    return realFetch(u, opts);
  };
  return true;
})()`);

/* 注入一组有代表性的事件：用户提示 ×2、3 工具调用 + 1 工具结果、AI 超长回复、AI 短回复 */
const HEAD = "开头第一段：我先梳理一下这个页面的结构，给出整体改造方向。";
const MIDDLE = "神秘中间段落ZQX：这一段是冗长的过程细节，默认不该出现在屏幕上。".repeat(12);
const TAIL = "最后一段总结：改完了，按上面三步验证即可。";
await evalJs(`(() => {
  document.getElementById("view-external").classList.remove("hidden");
  document.getElementById("empty-state").classList.add("hidden");
  document.body.classList.add("nav-stage");
  state.sel = { kind: "ext", id: 1 };
  state.tr = { file: "mock.jsonl", after: 1, first: false, events: [], expanded: new Set() };
  const longAi = ${JSON.stringify(HEAD)} + "\\n\\n" + ${JSON.stringify(MIDDLE.repeat(20))} + "\\n\\n" + ${JSON.stringify(TAIL)};
  renderEvents([
    { role: "user", kind: "text", text: "会话台帮我优化一下：AI 过程别显示太细，我的提示词要醒目", ts: "2026-10-17T01:00:00.000Z" },
    { role: "assistant", kind: "tool", name: "Read", detail: "static/app.js", ts: "2026-10-17T01:00:01.000Z" },
    { role: "assistant", kind: "tool", name: "Read", detail: "static/styles.css", ts: "2026-10-17T01:00:02.000Z" },
    { role: "assistant", kind: "tool", name: "Bash", detail: "pytest -q", ts: "2026-10-17T01:00:03.000Z" },
    { role: "user", kind: "toolresult", is_error: false, text: "↩ 工具结果: 59 passed", ts: "2026-10-17T01:00:04.000Z" },
    { role: "assistant", kind: "text", text: longAi, ts: "2026-10-17T01:00:05.000Z" },
    { role: "user", kind: "text", text: "第二条提示：再确认下手机端", ts: "2026-10-17T01:00:06.000Z" },
    { role: "assistant", kind: "text", text: "短回复：好的。", ts: "2026-10-17T01:00:07.000Z" },
    { role: "assistant", kind: "text", text: "中间一条正常长度的过渡回复。", ts: "2026-10-17T01:00:08.000Z" },
    // 孤立单条工具结果（调用在尾窗口之外）：必须静态显示，不能是点开为空的死按钮
    { role: "user", kind: "toolresult", is_error: false, text: "↩ 工具结果: orphan-only 42", ts: "2026-10-17T01:00:09.000Z" },
    { role: "assistant", kind: "text", text: "尾部过渡回复二。", ts: "2026-10-17T01:00:10.000Z" },
    // 病态结构：首行 3000 字（minified 代码风）+ 12 个中段行 + 短尾：折叠态首行也必须截断
    { role: "assistant", kind: "text",
      text: "Y".repeat(3000) + "\\n\\n" + Array.from({length: 12}, (_, k) => "MIDLINE-" + k).join("\\n") + "\\n\\n结束尾巴ZZ",
      ts: "2026-10-17T01:00:11.000Z" },
  ], true);
  return true;
})()`);

const collapsed = await evalJs(`(() => {
  const box = document.getElementById("transcript");
  const html = box.innerHTML;
  return {
    userCards: box.querySelectorAll(".tr.user").length,
    bubbles: box.querySelectorAll(".user-bubble").length,
    toolGroups: box.querySelectorAll(".tr.tool").length,
    groupBars: box.querySelectorAll(".trg-bar").length,
    groupText: box.querySelector(".trg-bar")?.textContent.replace(/\\s+/g, " ").trim() || "",
    // 组折叠时明细节点不存在（完整明细只在 hover title 与展开项里，故按节点而非全文判）
    groupItemsHidden: box.querySelectorAll(".trg-item").length === 0,
    barTitleHasDetails: /styles\\.css/.test(box.querySelector(".trg-bar")?.title || ""),
    middleHidden: !html.includes("神秘中间段落ZQX"),
    headShown: html.includes("开头第一段"),
    tailShown: html.includes("最后一段总结"),
    moreBtn: !!box.querySelector("[data-expand].tr-more"),
    moreText: box.querySelector("[data-expand].tr-more")?.textContent.trim() || "",
    // 只有"短回复：好的"这一条短 AI 消息不带折叠条（按文本定位，不依赖节点顺序）
    shortAiToggle: [...box.querySelectorAll(".tr.assistant")]
      .some((a) => a.textContent.includes("短回复：好的") && a.querySelector(".tr-toggle")),
    orphanStatic: (() => {
      const n = box.querySelector(".trg-bar-static.trg-result");
      return !!(n && n.tagName === "DIV" && /orphan-only/.test(n.textContent) && !n.closest("button"));
    })(),
    // 只有 4 项工具组是按钮条；孤立单条结果走静态 div
    groupBarButtons: box.querySelectorAll(".trg-bar").length,
    toolGroupCount: box.querySelectorAll(".tr.tool").length,
    // 超长首行：折叠态 DOM 里没有 3000 个 Y，但有截断后的 ~260 Y + 短尾
    longHeadCapped: !html.includes("Y".repeat(3000))
      && html.includes("Y".repeat(250)) && html.includes("结束尾巴ZZ"),
  };
})()`);
console.log("collapsed:", JSON.stringify(collapsed, null, 2));
await shot("transcript-collapsed.png");

/* 展开 AI 全文 */
await evalJs(`document.querySelector(".tr-more").click()`);
const aiExpanded = await evalJs(`(() => {
  const html = document.getElementById("transcript").innerHTML;
  return {
    middleShown: html.includes("神秘中间段落ZQX"),
    collapseBtn: !!document.querySelector("[data-collapse].tr-toggle"),
  };
})()`);
console.log("ai expanded:", JSON.stringify(aiExpanded));

/* 展开工具组：明细与工具结果出现，工具结果不带金框 */
await evalJs(`document.querySelector(".trg-bar").click()`);
const grpExpanded = await evalJs(`(() => {
  const box = document.getElementById("transcript");
  const items = box.querySelectorAll(".trg-item");
  return {
    itemCount: items.length,
    secondDetail: [...items].some((i) => i.textContent.includes("styles.css")),
    resultShown: [...items].some((i) => i.textContent.includes("59 passed")),
    userCardsStill2: box.querySelectorAll(".tr.user").length,
  };
})()`);
console.log("group expanded:", JSON.stringify(grpExpanded));
await shot("transcript-expanded.png");

/* HIGH 回归：上滑停留时全量重绘/底部追加/顶部展开都不得把视口拖向底部 */
const scrollKeep = await evalJs(`(() => {
  const b = document.getElementById("transcript");
  const ytr = [...b.querySelectorAll(".tr.assistant")].find((a) => a.textContent.includes("结束尾巴ZZ"));
  ytr?.querySelector(".tr-more")?.click();   // 展开 3000 字首行撑出可滚动高度
  b.style.height = "240px";
  b.scrollTop = 0;
  const atZero = b.scrollTop;
  renderTranscript();                          // 同内容重绘
  const afterRerender = b.scrollTop;
  renderEvents([{ role: "assistant", kind: "text",
    text: "追加新消息".repeat(60), ts: "2026-10-17T02:00:00.000Z" }], false);
  const afterAppend = b.scrollTop;             // 旧实现会加上高度增量把视口拽下去
  document.querySelector(".trg-bar")?.click(); // 顶部工具组展开（插入点在视口上方）
  const afterTopExpand = b.scrollTop;
  b.style.height = "";
  return { atZero, afterRerender, afterAppend, afterTopExpand };
})()`);
console.log("scroll keep:", JSON.stringify(scrollKeep));

/* MEDIUM 回归：在途的轮询必须去重，慢链路同 after 并发不得发第二次请求/重复渲染 */
const inflight = await evalJs(`(() => {
  let calls = 0;
  const realFetch = window.fetch.bind(window);
  window.fetch = (u) => {
    if (String(u).includes("/api/transcript")) { calls++; return new Promise(() => {}); }
    return realFetch(u);
  };
  state.tr.inflight = false;
  pollTranscript(); pollTranscript(); pollTranscript();
  window.fetch = realFetch;
  const res = { calls, inflight: state.tr.inflight };
  state.tr.inflight = false;
  return res;})()`);
console.log("inflight guard:", JSON.stringify(inflight));

/* 「只看我」模式：长提示词截断头部 + 展开/收起 + 折叠态 data-full 仍是全文；「全部」模式不截 */
const userClip = await evalJs(`(() => {
  const LONG = "长提示词开头MARK。" + "过程细节".repeat(120) + "结尾TAIL。";
  renderEvents([
    { role: "user", kind: "text", text: LONG, ts: "2026-10-17T03:00:00.000Z" },
  ], false);
  const box = document.getElementById("transcript");
  // 定位长提示词那张卡（前面注入的短提示词也在列表里，不能按第一个/最后一个取）
  const findCard = () => [...box.querySelectorAll(".tr.user")]
    .find((c) => c.textContent.includes("长提示词开头MARK"));
  const pText = () => findCard().querySelector("p").textContent;
  // 「全部」模式：长提示词不截断、无折叠钮
  renderTranscript();
  const allFull = pText().includes("结尾TAIL");
  const allNoToggle = !findCard().querySelector(".tr-toggle");
  // 切到「只看我」（直接设 filter 重绘，绕开服务端全量重抓，只测渲染层）
  state.tr.filter = "user";
  renderTranscript();
  const clippedHidden = !pText().includes("结尾TAIL");
  const headShown = pText().includes("长提示词开头MARK");
  const moreBtn = findCard().querySelector("[data-expand].tr-more");
  const fullAttr = findCard().querySelector(".user-bubble").getAttribute("data-full") || "";
  const fullAttrComplete = fullAttr === LONG;
  moreBtn.click();
  const expandedShown = pText().includes("结尾TAIL");
  const collapseBtn = findCard().querySelector("[data-collapse]");
  collapseBtn.click();
  const reClipped = !pText().includes("结尾TAIL");
  // 短提示词不截断
  renderEvents([{ role: "user", kind: "text", text: "短提示词NEW", ts: "2026-10-17T03:01:00.000Z" }], false);
  const shortCard = [...box.querySelectorAll(".tr.user")]
    .find((c) => c.textContent.includes("短提示词NEW"));
  const shortNoToggle = !shortCard.querySelector(".tr-toggle");
  // 还原，别污染后续用例
  state.tr.filter = "all";
  renderTranscript();
  return { allFull, allNoToggle, clippedHidden, headShown, moreBtn: !!moreBtn,
    fullAttrComplete, expandedShown, collapseBtn: !!collapseBtn, reClipped, shortNoToggle };
})()`);
console.log("user clip:", JSON.stringify(userClip));

/* 手机视口：金框卡片不溢出 */
await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 780, deviceScaleFactor: 2, mobile: true });
await sleep(300);
const mobile = await evalJs(`(() => {
  const box = document.getElementById("transcript");
  const bubble = box.querySelector(".user-bubble").getBoundingClientRect();
  return {
    noHorizontalOverflow: box.scrollWidth <= box.clientWidth + 1,
    bubbleLeft: Math.round(bubble.left),
    bubbleWithinScreen: bubble.right <= 390,
  };
})()`);
console.log("mobile:", JSON.stringify(mobile));
await shot("transcript-mobile.png");

/* 醒目度量化：用户金框与 AI 普通底在背景色上必须有显著区别 */
const contrast = await evalJs(`(() => {
  const bubble = getComputedStyle(document.querySelector(".user-bubble")).backgroundImage;
  const aiBody = document.querySelector(".tr.assistant .tr-body");
  const whoBadge = getComputedStyle(document.querySelector(".tr.user .tr-who")).backgroundImage;
  return { bubbleHasGoldBg: /201, 162, 105/.test(bubble), badgeGold: /183, 141, 82|224, 193, 139/.test(whoBadge),
           aiBodyTransparent: getComputedStyle(aiBody).backgroundImage === "none" };
})()`);
console.log("contrast:", JSON.stringify(contrast));

console.log("page errors:", pageErrors.length, pageErrors);
await fetch(CDP + "/json/close/" + target.id);
ws.close();
await sleep(200);  // 等 ws 关完再 exit，否则 libuv 退出断言 UV_HANDLE_CLOSING

const scrollOk = scrollKeep.atZero === 0 && scrollKeep.afterRerender === 0
  && scrollKeep.afterAppend === 0 && scrollKeep.afterTopExpand === 0;
const ok = collapsed.userCards === 2 && collapsed.bubbles === 2
  && collapsed.toolGroupCount === 2 && collapsed.groupBarButtons === 1
  && /等 4 项/.test(collapsed.groupText) && collapsed.groupItemsHidden === true
  && collapsed.barTitleHasDetails === true
  && collapsed.middleHidden && collapsed.headShown && collapsed.tailShown
  && collapsed.moreBtn && /中间省略/.test(collapsed.moreText)
  && collapsed.shortAiToggle === false
  && collapsed.orphanStatic === true && collapsed.longHeadCapped === true
  && aiExpanded.middleShown && aiExpanded.collapseBtn
  && grpExpanded.itemCount === 4 && grpExpanded.secondDetail && grpExpanded.resultShown
  && grpExpanded.userCardsStill2
  // calls=1 是去重证据；挂起期间 inflight=true 是正确状态（采样在手动复位之前）
  && scrollOk && inflight.calls === 1 && inflight.inflight === true
  && userClip.allFull && userClip.allNoToggle && userClip.clippedHidden
  && userClip.headShown && userClip.moreBtn && userClip.fullAttrComplete
  && userClip.expandedShown && userClip.collapseBtn && userClip.reClipped
  && userClip.shortNoToggle
  && mobile.noHorizontalOverflow && mobile.bubbleWithinScreen
  && contrast.bubbleHasGoldBg && contrast.badgeGold && contrast.aiBodyTransparent
  && pageErrors.length === 0;
console.log(ok ? "PASS" : "FAIL");
if (!ok) process.exit(1);
