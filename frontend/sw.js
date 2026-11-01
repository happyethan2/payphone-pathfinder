// Offline support: the app shell (network first, so online it's always current) and the
// basemap tiles saved for a route (Settings -> Save Route Map Offline). Phone data and every
// other API call pass straight through; the page keeps its own copy of the last phone load.
//
// Kill switch: to retire this worker, deploy a sw.js containing only
//   self.addEventListener('install', () => self.skipWaiting());
//   self.addEventListener('activate', e => e.waitUntil(caches.keys()
//     .then(ks => Promise.all(ks.map(k => caches.delete(k))))
//     .then(() => self.registration.unregister())));
// Browsers check sw.js on every visit, so each one drops it (and its caches) on its next load.

const SHELL = 'shell-v1';  // bump when SHELL_URLS changes: old shell caches are deleted on activate
const SHELL_URLS = [
  '/', 'styles.css?v=2', 'fonts/satoshi.css', 'fonts/Satoshi-Regular.woff2', 'fonts/Satoshi-Medium.woff2',
  'fonts/Satoshi-Bold.woff2', 'icon.png', 'manifest.json', '/api/config.js', '/api/phone-hints',
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',
  'https://cdnjs.cloudflare.com/ajax/libs/leaflet-minimap/3.6.1/Control.MiniMap.min.css',
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js',
  'https://unpkg.com/leaflet-polylinedecorator@1.6.0/dist/leaflet.polylineDecorator.js',
  'https://cdnjs.cloudflare.com/ajax/libs/leaflet-minimap/3.6.1/Control.MiniMap.min.js',
  'https://unpkg.com/leaflet-rotate@0.2.8/dist/leaflet-rotate-src.js',
];
const CDN = new Set(SHELL_URLS.filter(u => u.startsWith('https:')));
const SHELL_API = new Set(['/api/config.js', '/api/phone-hints']);
const NET_WAIT_MS = 4000;

// Saved tiles are keyed without the subdomain and API key; the page saves them under the same key
const tileKey = url => `https://basemaps.cartocdn.com${url.pathname}`;

self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(SHELL_URLS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(ks => Promise.all(ks.filter(k => k.startsWith('shell-') && k !== SHELL).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.hostname.endsWith('.basemaps.cartocdn.com')) {
    // Cache first: a miss goes to the network and isn't stored
    e.respondWith(caches.match(tileKey(url), { cacheName: 'tiles', ignoreVary: true })  // doesn't create the cache
      .then(hit => hit || fetch(req)).catch(() => fetch(req)));
    return;
  }
  const same = url.origin === location.origin;
  // The page itself under '/' whatever its query (share links); other pages (cert.pem) pass through
  const key = req.mode === 'navigate' ? (same && url.pathname === '/' ? '/' : null)
    : (same ? !url.pathname.startsWith('/api/') || SHELL_API.has(url.pathname) : CDN.has(req.url)) ? req : null;
  if (!key) return;
  e.respondWith(shell(e, req, key).catch(() => fetch(req)));
});

// Network first. After 4 s fall back to the saved copy if there is one, otherwise keep waiting.
function shell(e, req, key) {
  const net = fetch(req);
  // Copy a good answer into the cache, even when it lands after the saved copy was served.
  // Cloned in the first callback, before the page can start reading the body.
  e.waitUntil(net.then(res => {
    if (!res.ok) return;
    const copy = res.clone();
    return caches.open(SHELL).then(c => c.put(key, copy));
  }).catch(() => {}));
  const saved = () => caches.open(SHELL).then(c => c.match(key, { ignoreVary: true }));
  const late = new Promise(r => setTimeout(r, NET_WAIT_MS, 'late'));
  return Promise.race([net.catch(() => 'down'), late]).then(async r => {
    // A page that came back an error (a proxy's 502, say) gives way to the saved one
    if (r !== 'late' && r !== 'down') return r.ok || req.mode !== 'navigate' ? r : (await saved()) || r;
    return (await saved()) || net;  // offline with no copy: the network's error
  });
}
