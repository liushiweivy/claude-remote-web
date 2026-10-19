/* CDP 额度强刷冒烟：验证 ↻ 按钮/徽标点击都发 ?force=1、转圈反馈、toast 明细、零 console 错误。
 * 用法：node tools/cdp-quota-refresh.mjs （需 9333 无头 Edge + 8765 服务，token 从 data/token.txt 读）
 * 产出 docs/quota-refresh-*.png。 */
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
const forceHits = [];
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  if (m.method === "Runtime.exceptionThrown") {
    pageErrors.push(m.params.exceptionDetails.exception?.description || JSON.stringify(m.params.exceptionDetails));
  }
  if (m.method === "Network.requestWillBeSent" && /\/api\/quota/.test(m.params.request.url)) {
    forceHits.push(m.params.request.url);
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
/* 轮询等待（force=1 触发上游真实取数，端到端可能逼近 4s 超时，固定 sleep 会假阴性） */
async function waitFor(fn, label, timeout = 9000, interval = 200) {
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
const spinActive = () => evalJs(`document.getElementById("quota-refresh").classList.contains("spinning")`);
async function shot(name) {
  const r = await send("Page.captureScreenshot", { format: "png" });
  await writeFile(join(OUT, name), Buffer.from(r.result.data, "base64"));
  console.log("shot", name);
}

await send("Page.enable");
await send("Runtime.enable");
await send("Network.enable");

/* 1. 桌面布局：徽标三段 + ↻ 并排，侧栏文档流内（不压品牌、不出栏） */
await send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 800, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: URL });
await sleep(2500);
const layout = await evalJs(`(() => {
  const w = document.getElementById("quota-wrap");
  const b = document.getElementById("quota-badge");
  const r = document.getElementById("quota-refresh");
  const rail = document.querySelector(".rail").getBoundingClientRect();
  const actions = document.querySelector(".rail-actions").getBoundingClientRect();
  const brand = document.querySelector(".brand").getBoundingClientRect();
  const wr = w.getBoundingClientRect(), br = b.getBoundingClientRect(), rr = r.getBoundingClientRect();
  const intersect = (a, c) => !(a.right <= c.left || c.right <= a.left || a.bottom <= c.top || c.bottom <= a.top);
  return {
    wrapHidden: w.classList.contains("hidden"),
    badgeHidden: b.classList.contains("hidden"),
    refreshHidden: r.classList.contains("hidden"),
    segs: [...b.querySelectorAll(".qb-seg")].map((s) => s.textContent.trim()),
    sameRow: Math.abs(br.top - rr.top) < 4,
    badgeRightEdgeToRefresh: Math.round(rr.left - br.right),
    position: getComputedStyle(w).position,
    insideRail: wr.left >= rail.left && wr.right <= rail.right,
    belowActions: wr.top >= actions.bottom - 1,
    clearsBrand: !intersect(wr, brand),
    wrapBox: { x: Math.round(wr.x), y: Math.round(wr.y), w: Math.round(wr.width) },
  };
})()`);
console.log("layout:", JSON.stringify(layout));
await shot("quota-refresh-desktop.png");

/* 2. 点 ↻：记录 force 请求、转圈 class、toast 文本 */
forceHits.length = 0;
await evalJs(`document.getElementById("quota-refresh").click()`);
await sleep(150);
const spinning = await spinActive();
console.log("spinning during request:", spinning);
const spinEnded = await waitFor(async () => !(await spinActive()), "spin stop");
const toast1 = await evalJs(`document.getElementById("toast").textContent`);
const stillSpinning = !spinEnded;
const disabled = await evalJs(`document.getElementById("quota-refresh").disabled`);
console.log("after ↻: forceHits=", JSON.stringify(forceHits), "spinning=", stillSpinning, "disabled=", disabled);
console.log("toast:", JSON.stringify(toast1));
await shot("quota-refresh-toast.png");

/* 3. 点徽标本体也必须发 force=1 */
forceHits.length = 0;
await evalJs(`document.getElementById("quota-badge").click()`);
const badgeHit = await waitFor(async () => forceHits.some((u) => u.includes("force=1")), "badge force hit");
await waitFor(async () => !(await spinActive()), "spin stop after badge");
console.log("after badge click forceHits=", JSON.stringify(forceHits), "hit=", !!badgeHit);

/* 3.5 失败路径（MEDIUM 回归）：请求失败且本地有旧值时必须 toast 失败，不得把旧值当新值报成功 */
await evalJs(`(() => {
  window.__realFetch = window.fetch.bind(window);
  window.fetch = (u, o) => String(u).includes("/api/quota")
    ? Promise.reject(new Error("net down"))
    : window.__realFetch(u, o);
})()`);
await evalJs(`document.getElementById("quota-refresh").click()`);
const failToast = await waitFor(async () => {
  const t = await evalJs(`document.getElementById("toast").textContent`);
  return /刷新失败/.test(t) ? t : false;
}, "failure toast");
console.log("failure toast:", JSON.stringify(failToast));
await evalJs(`window.fetch = window.__realFetch;`);

/* 4. 手机视口：不溢出、详情页标题让位 */
await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 780, deviceScaleFactor: 2, mobile: true });
await sleep(400);
const mobile = await evalJs(`(() => {
  const node = document.getElementById("quota-wrap");
  const w = node.getBoundingClientRect();
  return {
    rightOverflow: Math.round(w.right) > 380,
    rightGap: Math.round(390 - w.right),
    width: Math.round(w.width),
    position: getComputedStyle(node).position,
  };
})()`);
console.log("mobile:", JSON.stringify(mobile));
await shot("quota-refresh-mobile-list.png");
await evalJs(`document.body.classList.add("nav-stage")`);
await sleep(200);
const marginRight = await evalJs(`getComputedStyle(document.querySelector(".vh-main")).marginRight`);
/* 关键回归：徽标在侧栏 DOM 内，详情页侧栏被隐藏时，fixed 徽标必须仍可见（display:none 祖先会连坐） */
const stageBadge = await evalJs(`(() => {
  const node = document.getElementById("quota-wrap");
  const r = node.getBoundingClientRect();
  return { width: Math.round(r.width), visibility: getComputedStyle(node).visibility, rightGap: Math.round(390 - r.right) };
})()`);
console.log("vh-main margin-right:", marginRight, "stage badge:", JSON.stringify(stageBadge));
await shot("quota-refresh-mobile-stage.png");

console.log("page errors:", pageErrors.length, pageErrors);
await fetch(CDP + "/json/close/" + target.id);
ws.close();

const ok = layout.segs.length === 3 && layout.sameRow && layout.refreshHidden === false
  && layout.position === "static" && layout.insideRail && layout.belowActions && layout.clearsBrand
  && spinning === true && stillSpinning === false && disabled === false
  && forceHits.some((u) => u.includes("force=1"))
  && !mobile.rightOverflow && mobile.position === "fixed" && mobile.rightGap === 10
  && stageBadge.width === 171 && stageBadge.visibility === "visible" && stageBadge.rightGap === 10
  && !!failToast
  && pageErrors.length === 0;
console.log(ok ? "PASS" : "FAIL");
if (!ok) process.exit(1);
