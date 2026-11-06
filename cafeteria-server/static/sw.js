// 밥먹을래말래 서비스 워커: 화면은 항상 서버에서 새로 받아 오고(캐시 안 씀),
// 인터넷이 끊겨 못 받을 때만 "연결이 없어요" 화면을 대신 보여준다.
const CACHE = "babmeok-offline-v3";
const OFFLINE = "/static/offline.html";
self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll([OFFLINE, "/static/icon.svg"])).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  if (e.request.mode !== "navigate") return;           // 데이터·그림 요청은 건드리지 않음
  e.respondWith(fetch(e.request).catch(() => caches.match(OFFLINE)));
});
