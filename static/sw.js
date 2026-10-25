/* 会话台 Service Worker：收 push 显示通知，点击唤起/聚焦页面。
   token 由页面在订阅成功后经 Cache API("crw-cfg") 交给本 SW，
   用于 notificationclick 时拼回带 token 的地址（无 token 打不开页面）。 */
self.addEventListener("push", (ev) => {
  let d = { title: "会话台", body: "", tag: "crw" };
  try { if (ev.data) d = { ...d, ...ev.data.json() }; } catch (e) { /* 保持默认 */ }
  ev.waitUntil(self.registration.showNotification(d.title, {
    body: d.body, tag: d.tag, icon: "/static/icon.png", badge: "/static/icon.png",
    renotify: false,
  }));
});

self.addEventListener("notificationclick", (ev) => {
  ev.notification.close();
  ev.waitUntil((async () => {
    let token = "";
    try {
      const resp = await caches.match("/crw-cfg");
      if (resp) token = (await resp.json()).token || "";
    } catch (e) { /* 无缓存就裸跳 */ }
    const url = "/" + (token ? `?token=${encodeURIComponent(token)}` : "");
    const clients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of clients) {
      if (c.url.startsWith(self.location.origin)) return c.focus();
    }
    return self.clients.openWindow(url);
  })());
});

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (ev) => ev.waitUntil(self.clients.claim()));
