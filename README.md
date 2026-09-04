# PlotCall

A single-file web app for **calling what is on the ground at a point**, from
satellite and aerial evidence, with two or more interpreters, and getting the
answers back as a table you can train on.

It was built to label land-cover *change* between two dates over Norway, and it
has been kept generic on purpose: the legend, the dates, the evidence panel and
the backend are all things you set. There is no build step and no server — you
edit one config file and upload a folder.

![PlotCall architecture](docs/architecture.svg)

---

## Why this rather than a GIS project or an off-the-shelf platform

Three constraints shaped it, and they are probably yours too.

- **The interpreter is the bottleneck, not the software.** Everything the call
  needs is on one screen, keyboard-first, and the app never makes anybody wait
  for a cloud service before they can look. Imagery, chips and time series are
  **baked to static files** ahead of time.
- **Nobody should have to sign in.** A labeller opens a URL and works. There is
  no account, no install and no Earth Engine login — an optional service account
  brokers a short-lived token so that the *deployment* is authenticated and the
  people are not.
- **Losing work is unacceptable.** Calls are drafted in the browser, queued in
  an outbox, upserted server-side, and exportable to CSV at any moment. Closing
  the tab, dropping the connection and re-dropping yesterday's export all do the
  right thing.

## What an interpreter sees

One point at a time, each one a single **10 m cell snapped to the UTM grid** —
not a dot, not a circle whose size drifts with the zoom. Around it:

| | |
| --- | --- |
| **Dated high-resolution imagery** | Esri Wayback at each of your two dates, so "before" and "after" are two specific captures rather than "the basemap" |
| **A nine-year Sentinel-2 filmstrip** | one thumbnail per year, all nine sharing one measured stretch, so a colour change means a ground change and not the atmosphere |
| **A dense index series** | NDVI/NDMI/NBR for the containing pixel, with the year markers on it |
| **Third-party products** | ESRI/Dynamic World land cover, Hansen loss, built-up layers — folded away at the foot, deliberately, so somebody else's confident classification is not the first thing read |
| **The call** | class at each date, a required 1/2/3 confidence, and flags for *mixed*, *cannot interpret*, *imagery date gap*, *transient change* |

Keyboard throughout. Progress, assignment and second-reading state come from the
batch file, so they are correct offline.

## Try it in five minutes

```bash
git clone https://github.com/Geethen/PlotCall.git
cd PlotCall
pip install pandas pyproj

python src/build_label_batches.py --placeholder   # writes app/batches/b001.json
python -m http.server 8000 --directory app
# open http://localhost:8000/
```

Labels stay in the browser and come out of the **export** button until you point
`app/config.js` at a backend. Opening the file over `file://` does not work — the
page fetches its batch manifest and browsers block that.

The demo batch has no baked evidence, so the chips fall back to live Earth
Engine (and, with nobody signed in, to flat colour). That is the app telling you
the truth: **bake, or accept the fallback.**

---

## The four pieces

### 1 · Build — your points become a batch

```bash
python src/build_label_batches.py \
    --candidates my_points.csv \
    --campaign my-campaign --batch-size 100 --experts e1,e2
```

Input is any table with `id`, `lon`, `lat` (CSV, Parquet or GeoJSON). Anything
else on the row is carried through and shown in the *about this location* panel.
The builder cuts the table into batches in the order you gave it, assigns each
point a primary reader round-robin, and draws a **5% overlap sample** that two
people both call — the only measurement of inter-rater agreement you will get,
and it is a property of the batch file rather than a checkbox somebody forgets.

Then bake the evidence (this is the part that needs Earth Engine, and it is the
only part):

```bash
pip install earthengine-api pillow
earthengine authenticate

B=app/batches/b001.json
python src/build_batch_evidence.py --batch $B   # point values + annual timeline
python src/build_batch_chips.py    --batch $B   # the nine-year sprite, ~3 MB
python src/build_batch_dense.py    --batch $B   # the index series, ~10 KB/point
```

Budget roughly half an hour for a 100-point global batch: Earth Engine
rate-limits, and points that are spread out cannot be batched into one request.
Points clustered in one study area are much faster.

### 2 · Serve — `app/` is the whole website

```
app/
  index.html          redirect, so the site root works
  label_app.html      the entire interface, one file
  config.js           your deployment — this is the file you edit
  vendor/             MapLibre, pinned and self-hosted
  batches/            batch JSON + baked sidecars
  apps_script/Code.gs paste into your Sheet's Apps Script editor
```

Push to GitHub and `.github/workflows/pages.yml` publishes it, after checking
the config parses and the bakes match the version the app reads. Enable it once
under **Settings ▸ Pages ▸ Source: GitHub Actions** — until you do, the deploy
job fails with *Get Pages site failed*, which is the setting and not the
workflow. Or drag the folder onto any static host. There is no build.

### 3 · Call — the browser

The app reads `batches/index.json`, loads a batch, and works from local storage
from then on. Each saved call is queued and flushed to the backend; the sheet is
also *read* on open, so a second machine or a colleague's progress shows up
rather than being silently duplicated.

### 4 · Store — a Google Sheet, via Apps Script

The only non-static piece, and it is 400 lines of Apps Script pasted into the
Sheet. It appends a row, reads back this campaign's rows, and (optionally) mints
an Earth Engine token. Rows are keyed by

```
(campaign, batch_id, point_id, expert_id)
```

and upserted, so re-sending a row that already arrived changes nothing, and a
second reader never overwrites the first.

---

## Deploying it for your own study

Five steps, in order.

### The Sheet backend

1. Make a Google Sheet. **Extensions ▸ Apps Script**, and paste
   [app/apps_script/Code.gs](app/apps_script/Code.gs) over `Code.gs`.
2. **Project Settings ▸ Script Properties**, add `SUBMIT_TOKEN` with any random
   string. This is anti-spam, not authentication — it stops whoever finds the
   URL from writing junk rows.
3. **Deploy ▸ New deployment ▸ Web app**, execute as *me*, access
   *anyone*. Copy the `/exec` URL.
4. Check it: `curl '<exec-url>?action=ping'` should answer with JSON.

### Your `app/config.js`

```js
window.LABEL_APP_CONFIG = {
  sheetUrl:    'https://script.google.com/macros/s/…/exec',
  submitToken: '…the same string…',
  campaign:    'my-campaign',
  title:       'My study · interpretation',
  heading:     'My study',
  experts: [
    { id: 'e1', name: 'Ada' },
    { id: 'e2', name: 'Grace' }
  ],
  eeAuthMode: 'auto',
  pointZoom: 15
};
```

**On committing these values.** Neither `sheetUrl` nor `submitToken` is a
secret and neither can be: the browser downloads this file, so anyone who can
open the app already has both. Commit them if the repository is yours. What must
*never* be committed is the Earth Engine **service-account private key** — the
whole reason the token broker exists is that a browser may not hold one. If your
repository is shared with people who should not see the backend, put the whole
file in the `LABEL_APP_CONFIG_JS` Actions secret instead and the Pages workflow
will inject it.

`.githooks/pre-commit` refuses a commit containing a private key. Enable it once
per clone with `git config core.hooksPath .githooks`.

### The legend is yours

`CLASSES` near the top of the script block in
[app/label_app.html](app/label_app.html) is the whole legend: a key, two shortcut
keys, a CSS class and the one-line hint the interpreter reads. Change it, change
the matching colours below it, and point `cribsheetUrl` at your own full class
definitions.

Two things not to get wrong:

- **Say what the labelling unit is, and draw it.** Here it is *majority cover of
  one 10 m Sentinel-2 pixel*, snapped to the UTM grid by
  [src/label_cell.py](src/label_cell.py) and drawn identically in the app — the
  two are checked against each other in CI. If your model consumes a different
  grid, change both together. A brief that says one thing while the map draws
  another produces labels for a footprint nobody judged.
- **Never teach two legends.** If `cribsheetUrl` and the in-app hints drift
  apart, different people label to different standards and you find out from the
  round report.

### Calibrate before anybody labels anything

Two stages, in this order, from points whose answer you already agree on:

```bash
python src/build_label_batches.py --candidates agreed.csv \
    --calibration --stage teach   --reference-col transition --prefix cal
python src/build_label_batches.py --candidates agreed.csv \
    --calibration --stage qualify --reference-col transition --prefix qual
```

`teach` tells the interpreter the answer after every call; `qualify` is blind and
tells them once at the end. Read the **confusion pairs**, not the headline
percentage: one person consistently calling long fallow *Cropland* is a briefing
you can fix in ten minutes and looks nothing like the same number made of
scattered singletons.

### Close the round

```bash
# rows back out of the Sheet, with a report of what the round bought
python src/label_rounds.py --url '<exec-url>' --campaign my-campaign

# cut the next round without repeating any of it
python src/build_label_batches.py --candidates next.csv \
    --exclude-labelled data/analysis_results/label_rounds.csv
```

## Earth Engine, optionally

The app works with Earth Engine switched off entirely (`eeAuthMode: 'off'`) —
the baked chips and series carry it. Turn it on for extra overlays and you have
two choices:

- **`'service'` / `'auto'` (recommended).** Put a service-account key in the
  Apps Script's Script Properties as `EE_SERVICE_ACCOUNT_KEY`. The script mints a
  one-hour read-only token per browser and nobody signs in. The account needs
  `earthengine.computations.create` **and** `earthengine.maps.create` — the
  stock *viewer* role has the first and not the second, which fails only when a
  tile is requested.
- **`'oauth'`.** Everyone signs in with their own Google account and you register
  every serving origin on an OAuth *Web* client. An unregistered origin fails
  **silently** — the popup shows `origin_mismatch` and the SDK reports nothing.

## Repository layout

| | |
| --- | --- |
| [app/](app/) | the deployable folder — and [app/README.md](app/README.md), the operator's manual, which is much longer than this one and worth reading before a real campaign |
| [src/](src/) | the builders: batches, evidence, chips, dense series, round pull-back |
| [tests/](tests/) | Playwright regression tests, each pinned to a bug that silently lost or blocked work |
| [docs/](docs/) | the architecture diagram |
| [demo/](demo/) | 24 points so `--placeholder` runs on a fresh clone |

## Tests

```bash
pip install pytest playwright pillow pandas pyproj
playwright install --with-deps chromium
pytest tests -q
```

They are not decoration. Each one is a fault that reached production and was
invisible: an annotation key that dropped the second reader, a `point_id` of
`0012` coerced to `12`, an outbox that discarded a mid-flight correction, a chip
bake stamped with a version three quarters of its pixels did not have. Two of
them run the app's own JavaScript in node against the Python that must agree
with it.

## Credits

MapLibre GL JS, Esri Wayback, EOX Sentinel-2 cloudless, Google Earth Engine,
Google Apps Script. Built for the RECOVER / habloss land-cover change work at
NINA; the modelling side, and the research ledger explaining why the campaign is
shaped this way, are in
[recoverHabloss](https://github.com/Geethen/recoverHabloss).

MIT licensed.
