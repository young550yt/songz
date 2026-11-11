/* Offline shell for the music player (served mode only).

   Caches the page, manifest and icons so the player still opens when the
   laptop is unreachable. The library and the songs themselves are NOT
   cached: audio streams over HTTP Range and would eat the phone's storage,
   and /api/* must always reach the real server.

   Needs a secure context: https:, localhost, or the Chrome flag
   chrome://flags/#unsafely-treat-insecure-origin-as-secure for
   http://<laptop-ip>:8390 */
const CACHE = "music-v1";
const SHELL = ["/", "/manifest.webmanifest", "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", function(e){
  e.waitUntil(
    caches.open(CACHE).then(function(c){
      /* one missing file must not abort the install */
      return Promise.all(SHELL.map(function(u){
        return c.add(u).catch(function(){ return null; });
      }));
    }).then(function(){ return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function(e){
  e.waitUntil(
    caches.keys().then(function(keys){
      return Promise.all(keys.filter(function(k){ return k !== CACHE; })
                             .map(function(k){ return caches.delete(k); }));
    }).then(function(){ return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function(e){
  var req = e.request;
  if(req.method !== "GET") return;                 /* POSTs: straight out */
  var url = new URL(req.url);
  if(url.origin !== location.origin) return;
  /* the library and every audio byte stay on the network */
  if(url.pathname.indexOf("/api/") === 0 ||
     url.pathname.indexOf("/audio/") === 0) return;

  if(url.pathname === "/"){
    /* the page: network first, so a rescan or a code fix shows up at once;
       the last copy is what opens with no network at all */
    e.respondWith(
      fetch(req, {cache:"no-cache"}).then(function(r){
        if(!r.ok) throw new Error("http "+r.status);
        var copy = r.clone();
        caches.open(CACHE).then(function(c){ c.put(req, copy).catch(function(){}); });
        return r;
      }).catch(function(){ return caches.match(req); })
    );
    return;
  }

  /* manifest + icons: the cached copy answers instantly, refreshed behind it */
  e.respondWith(
    caches.match(req).then(function(hit){
      var net = fetch(req).then(function(r){
        if(r && r.ok){
          var copy = r.clone();
          caches.open(CACHE).then(function(c){ c.put(req, copy).catch(function(){}); });
        }
        return r;
      }).catch(function(){ return hit; });
      return hit || net;
    })
  );
});
