const retryPage = `<!doctype html>
<meta charset="utf-8">
<title>Đang chờ máy chủ</title>
<style>body{font:16px system-ui;text-align:center;padding:18vh 1rem;color:#333}h1{font-size:1.4rem}p{color:#666}</style>
<h1>Đang chờ ứng dụng khởi động lại…</h1>
<p>Trang sẽ tự tải lại khi build xong.</p>
<script>setInterval(async()=>{try{const r=await fetch(location.href,{cache:"no-store"});if(r.ok)location.reload()}catch{}} ,2000)</script>`;

const IMAGE_CACHE = "hannom-images-v1";

self.addEventListener("fetch", (event) => {
  if (new URL(event.request.url).pathname.startsWith("/img/")) {
    event.respondWith(
      caches.open(IMAGE_CACHE).then(async (cache) => {
        const cached = await cache.match(event.request);
        const fresh = fetch(event.request).then((response) => {
          if (response.ok) cache.put(event.request, response.clone());
          return response;
        }).catch(() => cached);
        return cached || fresh;
      })
    );
    return;
  }
  if (event.request.mode !== "navigate") return;
  event.respondWith(
    fetch(event.request, { cache: "no-store" }).then((response) => {
      if (response.status >= 500) return new Response(retryPage, {
        headers: { "Content-Type": "text/html; charset=utf-8" },
      });
      return response;
    }).catch(() => new Response(retryPage, {
      headers: { "Content-Type": "text/html; charset=utf-8" },
    }))
  );
});
