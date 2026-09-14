// @ts-check
// Everything that has to happen BEFORE the app, in the order it has to happen.
//
// These are the three inline <script> blocks that used to be scattered through
// label_app.html -- the MapLibre fallback in the head, the theme block after
// the stylesheet, and the config.js loader just above the app. They are one
// external file now for one reason: with no inline script left in the page, a
// real `script-src` Content-Security-Policy becomes possible without a build
// step to hash them. See the CSP comment in label_app.html.
//
// This file is a CLASSIC script, deliberately parser-blocking, and must stay
// that way. Both `document.write` calls below only work while the parser is
// waiting on this script; `async`, `defer` or `type="module"` would turn each
// of them into a no-op that wipes the document instead, and the failure looks
// like a blank page rather than an error.

// --- 1. Theme -------------------------------------------------------------
// Read here, in the head, rather than in initChrome(): a theme applied after
// the first paint is a white flash on every load for whoever chose dark, and
// the flash is on the imagery panel, which is the thing being looked at.
// Light is the default -- `prefers-color-scheme` is deliberately NOT consulted,
// because the choice here is about reading imagery, not about the OS.
try {
  var t0 = localStorage.getItem('recover-labels:theme');
  if (t0 === 'dark') document.documentElement.dataset.theme = 'dark';
} catch (e) { /* private mode */ }

// --- 2. MapLibre fallback -------------------------------------------------
// The fallback, and only the fallback: if the local copy is not there,
// maplibregl is undefined and the CDN gets one chance before buildMap() runs.
if (typeof maplibregl === 'undefined') {
  // Subresource integrity, because this is a third-party script entering a
  // page that carries the submit token. Both digests are of the files in
  // app/vendor/, which are byte-identical to what unpkg serves for 4.7.1 --
  // verified, not assumed. `crossorigin` is required for SRI to be checked at
  // all on a cross-origin fetch. Regenerate both alongside the curl in
  // app/README.md:
  //   openssl dgst -sha384 -binary <file> | openssl base64 -A
  document.write('<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/'
    + 'maplibre-gl.js" integrity="sha384-SYKAG6cglRMN0RVvhNeBY0r3FYKNOJtznw'
    + 'A0v7B5Vp9tr31xAHsZC0DqkQ/pZDmj" crossorigin="anonymous"><\/script>');
  document.write('<link href="https://unpkg.com/maplibre-gl@4.7.1/dist/'
    + 'maplibre-gl.css" rel="stylesheet" integrity="sha384-MinO0mNliZ3vwppuPO'
    + 'UnGa+iq619pfMhLVUXfC4LHwSCvF9H+6P/KO4Q7qBOYV5V" crossorigin="anonymous">');
}

// --- 3. Deployment config -------------------------------------------------
  // WRITTEN, NOT WRITTEN OUT, because `config.js` is the one file that changes
  // on every deployment and the one whose stale copy fails invisibly: a cached
  // config is an app pointing at the previous Sheet, or at no service account,
  // with nothing on screen to say so. A `?v=` the browser has never seen cannot
  // come out of its cache, and this still works when the HTML itself came from
  // one. `document.write` during parse keeps the ordering the next block needs.
  document.write('<script src="config.js?v=' + Date.now() + '"><\/script>');
