/* 真浏览器复现公网打不开：无头 Edge(9333) 打开 cpolar 公网地址，抓每个请求耗时 */
const URL = "http://2de292aa.r39.cpolar.top/?token=1234";
const target = await (await fetch("http://127.0.0.1:9333/json/new?about:blank", { method: "PUT" })).json();
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r) => (ws.onopen = r));
let mid = 0;
const pending = new Map();
const reqs = new Map();
const failed = [];
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  if (m.method === "Network.requestWillBeSent") {
    reqs.set(m.params.requestId, { url: m.params.request.url, t: m.params.timestamp });
  }
  if (m.method === "Network.responseReceived") {
    const r = reqs.get(m.params.requestId);
    if (r) { r.status = m.params.response.status; r.proto = m.params.response.protocol; r.end = m.params.timestamp; }
  }
  if (m.method === "Network.loadingFinished") {
    const r = reqs.get(m.params.requestId);
    if (r) r.done = m.params.timestamp;
  }
  if (m.method === "Network.loadingFailed") {
    failed.push({ ...m.params, url: reqs.get(m.params.requestId)?.url });
  }
};
const send = (method, params = {}) => new Promise((res) => {
  const id = ++mid; pending.set(id, res); ws.send(JSON.stringify({ id, method, params }));
});
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
await send("Network.enable");
await send("Page.enable");
const t0 = Date.now();
await send("Page.navigate", { url: URL });
await sleep(12000);
const ready = await send("Runtime.evaluate", { expression: "({title:document.title, ready:document.readyState, body:document.body?document.body.innerText.slice(0,120):''})", returnByValue: true });
console.log("elapsed ms:", Date.now() - t0);
console.log("doc:", JSON.stringify(ready.result.result.value));
for (const [, r] of reqs) {
  const ms = r.done ? Math.round((r.done - r.t) * 1000) : (r.end ? Math.round((r.end - r.t) * 1000) : null);
  console.log(`${r.status ?? "PENDING"} ${r.proto ?? "?"} ${ms ?? "TIMEOUT"}ms ${r.url.replace(URL.split("?")[0], "")}`);
}
console.log("loadingFailed:", JSON.stringify(failed.map(f => ({ type: f.type, err: f.errorText, url: f.url })), null, 1));
await fetch("http://127.0.0.1:9333/json/close/" + target.id);
ws.close();
