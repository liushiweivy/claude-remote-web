/* CDP 三段额度徽标冒烟：无头 Edge(9333) 跑桌面/手机两档视口。
 * 用法：node tools/cdp-quota-check.mjs  （需 9333 + 8765 服务）
 * 产出 docs/quota-*.png，控制台打印徽标结构/位置/console 错误。
 */
import { writeFile, mkdir } from "node:fs/promises";
import { readFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const CDP = "http://127.0.0.1:9333";
// Token is read at runtime from data/token.txt (gitignored); never hardcode it.
const TOKEN = readFileSync(new URL("../data/token.txt", import.meta.url), "utf8").trim();
const URL = `http://127.0.0.1:8765/?token=${TOKEN}`;
const OUT = join(dirname(fileURLToPath(import.meta.url)), "..", "docs");
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

await send("Page.enable");
await send("Runtime.enable");

async function shot(name) {
  const r = await send("Page.captureScreenshot", { format: "png" });
  await writeFile(join(OUT, name), Buffer.from(r.result.data, "base64"));
  console.log("shot", name);
}
async function badgeInfo() {
  return evalJs(`(() => {
    const b = document.getElementById("quota-badge");
    const br = b.getBoundingClientRect();
    const tr = document.querySelector(".brand-title").getBoundingClientRect();
    const segs = [...b.querySelectorAll(".qb-seg")].map((s) => {
      const r = s.getBoundingClientRect();
      return { text: s.textContent.trim(), cls: s.className, x: Math.round(r.x), w: Math.round(r.width) };
    });
    return {
      cls: b.className, title: b.title, hidden: b.classList.contains("hidden"),
      box: { x: Math.round(br.x), right: Math.round(window.innerWidth - br.right), w: Math.round(br.width), h: Math.round(br.height) },
      titleRight: Math.round(tr.right), segs,
    };
  })()`);
}

/* 1. 桌面 1280×800 */
await send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 800, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: URL });
await sleep(3000);
const desktop = await badgeInfo();
console.log("desktop:", JSON.stringify(desktop, null, 1));
console.log("desktop overlap? badgeStart", desktop.box.x, "<= titleRight", desktop.titleRight, "->", desktop.box.x <= desktop.titleRight);
await shot("quota-desktop.png");

/* 2. 注入 warn/danger 色阶（实时数据三段都没到阈值，这里只验 CSS 上色） */
await evalJs(`(() => {
  const segs = document.querySelectorAll("#quota-badge .qb-seg");
  segs[1].classList.add("qb-warn");
  segs[2].classList.add("qb-danger");
})()`);
await sleep(150);
await shot("quota-desktop-colored.png");

/* 3. 点击徽标 → toast 三段明细 */
await send("Input.dispatchMouseEvent", {
  type: "mousePressed", x: desktop.box.x + 60, y: 28, button: "left", clickCount: 1,
});
await sleep(60);
await send("Input.dispatchMouseEvent", {
  type: "mouseReleased", x: desktop.box.x + 60, y: 28, button: "left", clickCount: 1,
});
await sleep(800);
const toastText = await evalJs("document.getElementById('toast').textContent");
console.log("click toast:", JSON.stringify(toastText));
await shot("quota-desktop-toast.png");

/* 4. 错误降级态外观 */
await evalJs(`(() => {
  const b = document.getElementById("quota-badge");
  b.className = "quota-badge qb-error";
  b.innerHTML = '<span class="qb-seg"><span class="qb-lab">额度</span><span class="qb-num">—</span></span>';
})()`);
await shot("quota-desktop-error.png");

/* 5. 手机 390×780：列表页 */
await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 780, deviceScaleFactor: 2, mobile: true });
await sleep(400);
await evalJs(`(() => { const b = document.getElementById("quota-badge"); b.className = "quota-badge";
  b.innerHTML = '<span class="qb-seg"><span class="qb-lab">5h</span><span class="qb-num">23</span></span>'
    + '<span class="qb-seg qb-warn"><span class="qb-lab">周</span><span class="qb-num">72</span></span>'
    + '<span class="qb-seg qb-danger"><span class="qb-lab">月</span><span class="qb-num">91</span></span>'; })()`);
const mobile = await badgeInfo();
console.log("mobile list:", JSON.stringify(mobile.box), "segs:", mobile.segs.map(s => s.text).join("|"));
console.log("mobile overflow?", mobile.box.x + mobile.box.w > 390);
await shot("quota-mobile-list.png");

/* 6. 手机详情页（强制 nav-stage，检查标题让位） */
await evalJs(`document.body.classList.add("nav-stage")`);
await sleep(300);
await shot("quota-mobile-stage.png");
const marginRight = await evalJs(`getComputedStyle(document.querySelector(".vh-main")).marginRight`);
console.log("vh-main margin-right:", marginRight);

console.log("page errors:", pageErrors.length, pageErrors);
await fetch(CDP + "/json/close/" + target.id);
ws.close();
if (pageErrors.length) process.exit(1);
