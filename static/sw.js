/*
 * Youtify service worker: offline/instant audio cache.
 *
 * Cache-first for /library/{id}/audio: cached tracks play from local disk
 * (Range requests answered by slicing the stored blob); uncached tracks
 * stream from the network while a full copy is fetched in the background
 * ("cache on play"). The page additionally prefetches the server's
 * /cache-plan (favorites / most played / recent) via postMessage.
 *
 * Only registered on HTTPS or localhost — browsers refuse SWs elsewhere.
 */
const CACHE = 'youtify-audio-v1';
const AUDIO_RE = /^\/library\/\d+\/audio$/;

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

async function cacheFull(url) {
    const cache = await caches.open(CACHE);
    if (await cache.match(url)) return;
    // Plain fetch (no Range header) so we store the complete file.
    const res = await fetch(url);
    if (res.ok && res.status === 200) await cache.put(url, res);
}

async function fromCache(req) {
    const cache = await caches.open(CACHE);
    const hit = await cache.match(req.url);
    if (!hit) return null;
    const range = req.headers.get('range');
    if (!range) return hit.clone();
    const m = /bytes=(\d+)-(\d*)/.exec(range);
    if (!m) return hit.clone();
    const blob = await hit.blob();
    const start = +m[1];
    const end = m[2] ? Math.min(+m[2], blob.size - 1) : blob.size - 1;
    if (start >= blob.size) return hit.clone();
    return new Response(blob.slice(start, end + 1), {
        status: 206,
        headers: {
            'Content-Type': hit.headers.get('Content-Type') || 'audio/mpeg',
            'Content-Range': `bytes ${start}-${end}/${blob.size}`,
            'Content-Length': String(end - start + 1),
            'Accept-Ranges': 'bytes',
        },
    });
}

self.addEventListener('fetch', e => {
    const u = new URL(e.request.url);
    if (e.request.method !== 'GET' || u.origin !== self.location.origin
        || !AUDIO_RE.test(u.pathname)) return;
    e.respondWith((async () => {
        const cached = await fromCache(e.request);
        if (cached) return cached;
        // Cache-on-play: full copy fetched in the background, network serves now.
        e.waitUntil(cacheFull(e.request.url).catch(() => { }));
        return fetch(e.request);
    })());
});

self.addEventListener('message', e => {
    const msg = e.data || {};
    if (msg.type === 'prefetch' && Array.isArray(msg.urls)) {
        e.waitUntil((async () => {
            const cache = await caches.open(CACHE);
            const wanted = new Set(msg.urls.map(u => new URL(u, self.location.origin).href));
            const wantedBases = new Set([...wanted].map(u => u.split('?')[0]));
            // Drop stale versions of planned tracks (updated_at changed).
            for (const req of await cache.keys()) {
                if (wantedBases.has(req.url.split('?')[0]) && !wanted.has(req.url)) {
                    await cache.delete(req);
                }
            }
            for (const url of wanted) {
                try { await cacheFull(url); } catch (err) { /* keep going */ }
            }
        })());
    } else if (msg.type === 'clear') {
        e.waitUntil(caches.delete(CACHE));
    }
});
