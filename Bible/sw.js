/* Offline cache for Daily Verse.

   The phone opens http://192.168.18.46:8395. When the laptop is asleep or
   the phone is away from home that address is unreachable, and without
   this file the app could not open at all. With it, the first online visit
   saves the app shell, the whole Bible (bible_data.js) and the last record
   (data.js); every later visit opens from that copy and refreshes it in
   the background. Changes made offline sit in the page's queue
   (localStorage) and are POSTed the moment the server answers again -
   POSTs are never intercepted here, they always go to the real network.

   Needs a secure context: Chrome only exposes service workers on https:,
   localhost, or an origin switched on via
   chrome://flags/#unsafely-treat-insecure-origin-as-secure.

   Updating: the cached files refresh themselves behind the cached copy
   (stale-while-revalidate), and the page re-checks for a new sw.js whenever
   it comes online or becomes visible. Bump the CACHE name below on any
   structural change (files added to SHELL, new caching rules) - old caches
   are deleted on activate. */
const CACHE = "bible-v2";
const SHELL = ["/", "/bible.html", "/bible_data.js", "/data.js",
               "/config.js", "/manifest.webmanifest",
               "/icon-192.png", "/icon-512.png"];

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
  if(req.method !== "GET") return;                 /* POSTs: straight to the server */
  var url = new URL(req.url);
  if(url.origin !== location.origin) return;

  if(url.pathname === "/data.js"){
    /* The record: network first, so a reachable server always wins. The
       last copy is what lets an offline open show real data instead of
       inventing a fresh day. Any bad answer - refused, timed out, or a
       proxy's 502 - counts as unreachable and falls back to that copy. */
    e.respondWith(
      fetch(req, {cache:"no-store"}).then(function(r){
        if(!r.ok) throw new Error("http "+r.status);
        var copy = r.clone();
        caches.open(CACHE).then(function(c){ c.put(req, copy).catch(function(){}); });
        return r;
      }).catch(function(){ return caches.match(req); })
    );
    return;
  }

  /* App shell + verse database: the cached copy opens instantly and is
     the only thing that works with no network; it is revalidated behind
     it so code and verse fixes arrive on the next visit. */
  e.respondWith(
    caches.match(req).then(function(hit){
      var net = fetch(req, {cache:"no-cache"}).then(function(r){
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
