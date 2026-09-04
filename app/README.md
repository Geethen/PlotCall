# PlotCall — the operator's manual

This is the long document. [The repository README](../README.md) is the tour and
the deployment steps; this one is everything an operator has to know to run a
real campaign, and much of it was learned from a fault in production. It is
written against the campaign PlotCall was built for — calling land cover at
**2018** and **2024** over Norway — so read the specifics as an example and the
reasoning as the transferable part.

A single-page web app for calling land cover at two dates on a batch of points,
shareable with collaborators by URL. MapLibre, Esri Wayback for the two dated
looks, a Google Sheet for the labels, and an optional Earth Engine service
account for auxiliary layers.

```
app/
  label_app.html        the whole app, one file
  config.js             your deployment's URLs and expert roster — edit this, not the app
  vendor/               MapLibre, self-hosted (see "Vendored MapLibre" below)
  batches/              batch JSON + index.json (written by build_label_batches.py)
  apps_script/Code.gs   paste into the Sheet's Apps Script editor
```

Related code:

| | |
| --- | --- |
| [`src/build_label_batches.py`](../src/build_label_batches.py) | cuts a ranked candidate table into batches |
| [`src/build_batch_evidence.py`](../src/build_batch_evidence.py) | bakes the point values and the annual timeline into a batch |
| [`src/label_rounds.py`](../src/label_rounds.py) | pulls the labels back and reads the yield |
| [`ACTIVE_LEARNING.md`](https://github.com/Geethen/recoverHabloss/blob/main/docs/research/ACTIVE_LEARNING.md) | why the campaign is shaped this way; §AL7 covers this app |

---

## Try it in 60 seconds

```bash
G=python   # whichever interpreter has the deps
$G src/build_label_batches.py --placeholder      # writes app/batches/b001.json
$G -m http.server 8000 --directory app
# open http://localhost:8000/label_app.html
```

Labels stay in the browser and come out of the **export** button until you point
`config.js` at a Sheet. Opening the file directly with `file://` does not work —
the app fetches its batch manifest, and browsers block that on `file://`.

**Dropping an export back in restores it; dropping a batch replaces the batch.**
The two files look alike and mean opposite things, so the branch is on the
content — a labels CSV carries answer columns (`point_id`, `labelled_at`,
`class_2018`) that no batch file has. Until 2026-08-31 every dropped file went to
`adoptBatch`, so re-dropping your own export replaced the batch's 100 points with
the 52 you had finished: the count read `52 / 52` and every outstanding point
was gone from the strip. Exactly backwards for a file whose purpose is *carry on
where I stopped* — and it is the file you reach for precisely when sync has
failed, so the state it destroyed was the state with no copy on the server.

A restore is grouped by batch, so an export covering a batch that is not on
screen still lands in that batch's storage and is there when you open it. Rows
are filed under the expert who **made** them (`LS.key` is per-expert), and a
colleague's rows still go to the outbox — which groups by expert — so recovering
someone else's export and syncing it for them works and stays correctly
attributed. Everything is re-queued; the Sheet upserts, so re-sending a row that
already arrived changes nothing.

## Deploying it for other people

Any static host serves it: GitHub Pages, an S3 bucket, a folder behind nginx.
There is no server-side component — the Sheet is the only backend.

1. **Put a Sheet behind it.** In the target spreadsheet, *Extensions ▸ Apps
   Script*, paste [`apps_script/Code.gs`](apps_script/Code.gs), then *Deploy ▸
   New deployment ▸ Web app* with **Execute as: Me** and **Who has access:
   Anyone**. Copy the `/exec` URL.
2. **Point the app at it.** Put that URL in `config.js` as `sheetUrl`. Check it
   with `curl '<exec-url>'` — it should answer
   `{"ok":true,"service":"recover-labelling",...}`.
   **Set a token while you are there:** put the same random string in
   `SUBMIT_TOKEN` (top of `Code.gs`) and `submitToken` (in `config.js`). It is
   not a secret — it ships inside the page — but "Who has access: Anyone" means
   exactly that, and this raises the bar from *anyone who finds the URL* to
   *anyone you gave the app to*.
3. **Configure the public app.** The checked-in [`config.js`](config.js) is a
  safe local-only template. Before sharing the Pages URL, fill in its
  `sheetUrl`, `submitToken`, Earth Engine settings, and `experts` roster in
  your deployment copy. Do not commit a live token or collaborator roster to
  a public repository; GitHub Pages has no private runtime configuration.
4. **List your experts.** `config.js` carries an `experts:` roster of
   `{ id, name }`. See *Who is labelling* below — this is not cosmetic, it is
   half the annotation key.
5. **Give the campaign an Earth Engine account.** Optional, and the thing
   that decides whether your labellers ever see a Google popup: paste a service
   -account key into the script's properties and they never do. See *Nobody
   signs in* under **Earth Engine** below — it is five steps and one of them
   (re-deploying the web app) is the one people skip.
6. **Upload `app/` somewhere.** Send each person their own URL,
   `…/label_app.html?expert=e1`. They pick a batch and work.

### GitHub Pages

This repository includes a workflow at `.github/workflows/pages.yml` that
publishes only `app/` when changes land on `main`. In the GitHub repository,
set **Settings ▸ Pages ▸ Source** to **GitHub Actions**, then add the complete
deployment-specific `config.js` as the `LABEL_APP_CONFIG_JS` Actions secret and
push the app. The workflow injects that secret only into the Pages artifact.

**Without the secret the build fails, on purpose.** It used to publish anyway,
which meant a deployment with no Sheet, no roster and no Earth Engine that looks
completely normal until somebody tries to save. The workflow also `node
--check`s the secret — a stray quote would otherwise deploy a page whose config
never defines `LABEL_APP_CONFIG`, which reads as "unconfigured" rather than as
"broken" — and **checks that every batch declaring `chips` or `dense` has its
sidecar directory in the artifact**, because a batch deployed without its bakes
falls back to live Earth Engine, or to flat colour swatches for anyone not
signed in.

**The checked-in `config.js` now carries this campaign's real `sheetUrl`,
`submitToken` and roster** (2026-09-04). That is deliberate and it is not a
leak: the page downloads that file, so every labeller already has all three, and
a blank template only produced a dragged-in copy of `app/` that did not work and
a reader who could not tell "unconfigured" from "redacted". The `/exec` URL is
not an account — it has exactly the powers `Code.gs` grants it — and the token
is **anti-spam rather than authentication**: it stops junk rows from whoever
finds the URL and draws no line between the two experts. Rotate it in Apps
Script *and* in `config.js` together. The one value that is a real secret, the
Earth Engine service-account key, is not in either file and cannot be.

**Two sources of truth, kept in step by hand.** The workflow still overwrites
`config.js` from `LABEL_APP_CONFIG_JS`, so the committed file governs a local
serve and the secret governs the published app. Change one and change the
other, or Pages will keep serving the older roster or a rotated-away token.

**Set `eeAuthMode: 'service'` in that secret.** On Pages specifically, the
service account is not just the nicer path, it is the one that works without
extra setup:

* no labeller is ever shown a Google popup, which is what §AL8 wanted;
* **no OAuth client id is used at all**, so the Pages origin does not have to be
  added to a client's *Authorised JavaScript origins* — and an unregistered
  origin fails **silently**, printing `origin_mismatch` inside a popup that
  looks exactly like a blocked one;
* Earth Engine roles are granted once, to one identity, instead of per labeller.

`'auto'` keeps the sign-in button as a fallback; `'service'` makes the button
retry the campaign account and never offer Google. Either way the account needs
`roles/serviceusage.serviceUsageConsumer` **and** an Earth Engine role on the
project — see *If an Earth Engine overlay does not appear*, and run both
`eeTokenSelfTest()` and `eeMapsSelfTest()` in the Apps Script editor before a
campaign, because they test different permissions.

**`app/` has to be committed** for any of this to run: the workflow uploads
`path: app` from a fresh checkout, so an untracked `app/` deploys an empty site.
That includes the bakes — ~17 MB for a 100-point batch across four vis schemes —
and every re-bake adds that again to git history. For a campaign of a few
batches that is the pragmatic choice; Git LFS or baking in CI are the
alternatives if it ever stops being.

One URL can serve several campaigns: `?sheetUrl=`, `?campaign=`, `?manifest=`,
`?batch=`, `?submitToken=`, `?expert=`, `?eeAuth=` and `?zoom=` all override
`config.js`.

**The view is addressable.** `?point=<id>`, `?scheme=` and `?w=` open a named
point under a named filmstrip scheme and width, and the app rewrites them as
the interpreter moves — so the link to send someone is always the one in the
address bar. It is how a disagreement gets settled: the other reader opens
exactly what you were looking at, on the same imagery and the same stretch. A
point the batch does not carry resumes normally rather than showing nothing,
and `expert` is never added to the link — that identity is the reader's own.
`?debug=1` turns on a console performance summary (`perfReport()`).

### Vendored MapLibre

`app/vendor/` holds `maplibre-gl.js` and `maplibre-gl.css`, served from the same
folder as the app. ~800 KB on the critical path from a third-party CDN makes the
map hostage to `unpkg.com` being reachable, and a blocked CDN is a dead map on a
page whose whole job is looking at imagery. The unpkg URLs remain as a fallback,
so a checkout without `vendor/` still runs; to refresh them:

```bash
curl -fsSL https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js  -o app/vendor/maplibre-gl.js
curl -fsSL https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css -o app/vendor/maplibre-gl.css
```

**The CDN fallback carries an `integrity` hash, so bumping the version is two
steps, not one.** It is a third-party script entering a page that holds the
submit token. Regenerate both digests and paste them into the `document.write`
fallback at the top of `label_app.html`:

```bash
for f in app/vendor/maplibre-gl.js app/vendor/maplibre-gl.css; do
  echo "$f  sha384-$(openssl dgst -sha384 -binary "$f" | openssl base64 -A)"
done
```

A stale hash does not degrade — the browser refuses the file outright, and only
on the machine that is missing `vendor/`. Change the URL and the digest
together.

### Security, and what a single static file can and cannot do

The page carries three `<meta>`-able defences — `object-src 'none'`,
`base-uri 'self'`, `form-action 'none'` — plus a referrer policy. The app has
no `<form>`, no `<object>` and no `eval`, so all of them cost nothing, and
`base-uri` is the one that matters: an injected `<base>` would re-point every
relative fetch in the app, sprites and batch JSON included.

**`script-src` and `connect-src` are deliberately absent, and that is not an
oversight.** The Apps Script `/exec` URL and the Earth Engine hosts are
per-deployment and arrive from `config.js` at runtime, so any host list written
into the page would be wrong for somebody — and would fail as *"the overlays do
not draw"*, which is the same silent class of failure the deploy workflow exists
to catch. `frame-ancestors` is header-only and is ignored in a meta tag; a host
that can set response headers should add `frame-ancestors 'none'` and
`X-Content-Type-Options: nosniff` there.

Locking `script-src` down properly needs the inline scripts hashed, which needs
a build step, and there is no build step on purpose — this is a folder you
upload. The groundwork is done either way: there are **no inline event handler
attributes** left in the page. Dialog buttons carry `data-act` and are dispatched
by one delegated listener (`LOADING_ACTIONS`), and CI fails the build if an
`onclick=` reappears.

### Who is labelling

**The header is a roster dropdown, not a text box, and the reason is the
annotation key.** Every row is upserted on
`(campaign, batch_id, point_id, expert_id)`. With a typed name in that key,
"Ann", "ann", "Ann " and "Anne" are four experts to the round report and the
failure is silent — the inter-rater agreement number, which is the campaign's
only measurement of the label noise the ledger says caps change-F1, is computed
over nothing and comes back a clean 100%.

```js
experts: [
  { id: 'e1', name: 'Ann Example' },
  { id: 'e2', name: 'Bo Example' }
]
```

`id` is permanent and goes in the sheet; `name` is a display label and can change
at will. **Never re-use an id for a different person.** `?expert=e1` is a
per-person bookmark. "someone else…" takes free text for anyone not on the
roster yet and gives them a slugged id (`x-ann-example`) so the same person
typing their name four ways is still one expert.

A batch **does not open** until an identity is chosen, and the identity **locks**
once the first row of a batch is saved. Switching opens a separate workspace —
localStorage is namespaced per `expert_id`, so expert B never sees expert A's
answers pre-filled in the form, which would be a confirmation measurement
wearing an agreement measurement's clothes. Unsent rows keep the identity that
made them and are still delivered under it.

### Assignments, and what the app asks the Sheet

**Overlap is a property of the batch file, not of a checkbox.**
`build_label_batches.py --experts e1,e2` assigns every point a `primary_expert`
and its `required_readers`, and draws the ~5% deliberate double-reading §AL asks
for. The app then shows two queues — **My points** and **Second readings** — and
both are correct with no network at all. The old checkbox ("skip points others
have done") survives as a debug override behind *⚙ batch setup*, for a batch cut
without assignments.

`index.json` carries `assigned` (points per expert) as well as `assigned_to`, so
the app can resume *your* unfinished batch without downloading every batch file
in the campaign.

When `sheetUrl` is set, opening a batch asks the Sheet two questions **in
parallel** (Apps Script cold start is 2–5 s and they are independent), and they
are deliberately separate endpoints:

| | |
| --- | --- |
| `?action=mine&batch=&expert=` | **your own rows, in full.** Open the batch on another laptop and your work comes back instead of showing 0/100. Filtered on `expert_id`, never on the display name. |
| `?action=labelled&batch=` | **which expert holds each point, and nothing about what they said.** Three columns, cached for 45 s, and not even the display name goes over the wire — the app resolves that from the roster. |

The second one must never return the transition. When two people read the same
point on purpose, showing the first reading to the second destroys the
independence that makes the agreement number mean anything — so the app marks
the point ("already labelled by Ann") and shows nothing else. `Code.gs` carries
the same warning; don't helpfully add the call to it.

<kbd>Enter</kbd> walks the queue you are in. The second-reading queue carries a
banner saying the other call is hidden deliberately rather than missing.

`GET <exec-url>?action=status` returns per-batch counts and who has contributed,
if you want a progress dashboard. `?action=ping` is the health check the app
runs on boot before the header pill claims to be connected to anything.

> **Updating `Code.gs` needs a new deployment version.** Editing the script is
> not enough — the `/exec` URL keeps serving the version it was deployed with.
> *Deploy ▸ Manage deployments ▸ edit ▸ Version: New version ▸ Deploy*, which
> keeps the same URL. If `?action=mine` answers with the health-check JSON
> instead of rows, this is why.

---

## The batch format

`build_label_batches.py` writes these, but the app will read anything of this
shape, and also plain **CSV** or **GeoJSON** — drag a file onto the window.

```json
{
  "campaign": "recover-habloss",
  "batch_id": "cov001",
  "channel": "coverage",
  "instructions": "shown once when the batch opens",
  "points": [
    {
      "id": "c000123",
      "lon": 10.7522, "lat": 59.9139,
      "channel": "coverage",
      "rank": 1,
      "score": 0.8134,
      "cell_km": 5,
      "primary_expert": "e1",
      "required_readers": ["e1", "e2"],
      "meta": { "worldcover": "bare", "slope_deg": 3.2, "biome": "boreal" },
      "prior": { "Nature -> Artificial": 0.41, "Nature -> Nature": 0.38 },
      "conformal_set": ["Nature -> Artificial", "Nature -> Nature"],
      "evidence": { "v": { "dw_2018": "trees", "hansen_lossyear": 2019 },
                    "t": { "ndvi": [0.71, 0.69, "…"], "bands": { "B2": ["…"] } } }
    }
  ],
  "evidence_schema": { "version": "ev1", "rows": ["…"], "timeline": { "…": 1 } },
  "evidence_version": "ev1"
}
```

Only `lon` and `lat` are required; `id` is generated if absent. `lon`/`lat` also
accept `longitude`/`latitude`/`x`/`y`, and `id` accepts
`point_id`/`plot_id`/`cell_id`.

**Ids are strings, always.** A CSV `point_id` of `0012` stays `"0012"` — only
`lon`, `lat`, `score`, `rank` and `cell_km` are read as numbers. Coercing ids
would silently break the join back to the candidate table, and you would not
find out until the *next* round's `--exclude-labelled` excluded nothing.

* **`meta`** is free-form and is rendered as *"about this location"*, visible
  from the start. Terrain stratum, land-cover class and biome belong here —
  §AL-T's coverage gap is the reason those points are in the batch, so the
  interpreter should be able to see when they are in it.
* **`rank`, `score` and `channel` are hidden until the point is saved**, then
  revealed as *"why it was selected"*. "rank 1, uncertainty" tells the
  interpreter the model finds this point hard before they have looked at it,
  which is the same anchoring the collapsed posterior exists to prevent.
* **`primary_expert` / `required_readers`** drive the two queues. Written by
  `--experts`; see *Assignments* above.
* **`evidence`** and the batch-level **`evidence_schema`** are written by
  `build_batch_evidence.py`; see *Evidence* below. The schema is per batch and
  the values are per point, which is what keeps a 100-point batch near 1 MB.
* **`prior`** and **`conformal_set`** are optional and render **collapsed**. See
  below.
* **`reference`** is the calibration answer, and is first-class rather than
  metadata for a reason: `meta` renders next to the buttons, and an answer shown
  before the call measures nothing. Batch-level `"calibration": true` and
  `"feedback": "immediate" | "end"` go with it.
* Anything else on a CSV row lands in `meta` automatically.

---

## Three decisions in the app that are design, not taste

**The whole call is pinned; only the evidence scrolls.** The panel is two
bands. The head is the *form* — both dates, the transition it derives, both flag
groups, *what the imagery was like*, confidence, notes, and **Save** — and it
does not move. Everything below it is evidence: the spectral profile, the index
series, the filmstrip, the other people's maps, the model's opinion. The
evidence block is nine years long, so anything left inside it is past the bottom
of the panel, and the last four items in that form used to be. If the window is
short enough that the form itself will not fit, `#panel-head-scroll` takes the
overflow — the form shortens, the evidence is not deleted — and the transition
read-out, the two call flags and the buttons stay pinned below it even then.

**Batches are small and sequential.** §AL4 measured the same 2,000 acquisitions
at **−0.003** change-F1 delivered as one batch and **+0.031** delivered as
twenty, against a paired floor of 0.016. The default batch size is 100. If the
labelling workflow cannot return batches at all, §AL4's own conclusion is to
delete the model-in-the-loop half of the campaign rather than run it once.

**The model's opinion is hidden by default.** The two map errors visible to the
naked eye are ones where the model is *confident and wrong* — bare ground read as
built-up (§AL-T). A posterior shown next to the buttons would launder that error
into the label set. `prior` and `conformal_set` sit behind a closed disclosure
with that warning next to them. If you pass a `conformal_set`, pass one built
with **per-class (Mondrian)** thresholds: the marginal `SplitPredictor` reads
0.8999 coverage while covering `Cropland -> Nature` 13% of the time.

**The imagery the call was made on is recorded with the call.** Every saved row
carries `imagery_a` and `imagery_b` — the true local capture dates from Wayback's
metadata service, not the release dates in the dropdown. Without it, a
disagreement between two interpreters cannot be told apart from two different
capture dates, and most of them will turn out to be the latter.

---

## Using it

### The unit is one Sentinel-2 pixel, by majority cover

**Judge the yellow square, and there is no point at its centre to prefer.**
Whichever class covers most of it is the call: a house in the corner of an
otherwise grassy square is Nature, a square that is two-thirds rooftop is
Artificial.

That square is a **real 10 m Sentinel-2 pixel** — the point's UTM coordinates
floored to a multiple of 10 m in the zone MGRS puts it in, which is the grid
Sentinel-2 granules are delivered on *and* the grid the deployed map is written
on (EPSG:32632, 10 m, origin an exact multiple of 10). That is why it is not
centred on the marker. A 10 m square centred on the point would straddle four
pixels and be none of them, which is what it was until 2026-08-31: the
interpreter judged one footprint, the dense series read a second, and the model
predicts a third.

This is not a style preference. The original RECOVER sampling called each 10 m
cell by majority cover, and the model these labels train is a 10 m model — so an
interpreter judging the centre pixel is generating targets for a different
question than the one the training set was defined on, invisibly, on the
Cropland / Nature boundary the ledger already names as the change-F1 ceiling.
The app said "judge the point, not the whole square" until 2026-08-31 and drew
nothing at all at 10 m: the marker was a 2.6 px dot with a 13 px halo in
*screen* pixels, identical at every zoom, naming no ground area. Two
interpreters each guessing their own footprint arrives in the data as a
disagreement about the legend.

The definition lives in **`src/label_cell.py`** and in **`s2Cell()`** in the
app, which are the same arithmetic in two languages and are run against each
other in node by `tests/test_label_cell.py`. A batch built by
`build_label_batches.py` carries its cells baked (`cell` on each point) and the
baked one is what gets drawn; a `.csv`/`.geojson` dropped on the window is
computed in the browser. Everything that reads the cell moves as one:

* the **`cell` map layer** draws the pixel (checkbox: *the Sentinel-2 pixel*),
  on the Wayback compare map as well as the main one;
* **no centre dot.** It was 2.6 screen pixels at every zoom and it was the only
  thing on the map naming the call, so it said *judge this point*. The red ring
  that remains is a **locator** and fades out between z15 and z17, as the cell
  itself becomes big enough to see;
* the **dense series** reads `reduceRegion` over the **point** at scale 10,
  which is the value of the pixel containing it in the granule's own grid — the
  cell exactly, with no geometry to get wrong. It read a **30 m radius circle at
  20 m** until 2026-08-31, roughly 28× the area, then a point-centred square;
* the **chip filmstrip** paints the cell into every year (see below);
* the **brief** states the rule on first run.

The recipe change bumps the bake to **`dense3`**. An older sidecar is the old
circle and is deliberately *not* served — the app falls back to live Earth
Engine on a version it does not know, so a batch baked before this must be
re-baked with `--force`.

### Confidence is required

Both classes are not enough to save: `1` / `2` / `3` are mandatory on a real
call. Those keys were bound all along and simply never taught, so this costs one
keystroke. It is *not* asked on the **cannot interpret** path, where the row
carries no call.

It was optional until 2026-08-31 and therefore mostly absent, which cost more
than it looks: an unresolved disagreement between two experts is a different
object depending on whether both were sure, and without the number there is no
way to tell "the legend is ambiguous here" from "the imagery is".

### The classes

Pick 2018 and 2024 from {Nature, Cropland, Artificial}; the transition is
derived. **The classes are LUCAS as the campaign cribsheet states them**, and
the legend links the full document from its summary and from inside the fold —
`cribsheetUrl` in `config.js`, with the RECOVER cribsheet as the default so a
dragged-in copy of `app/` still has it. Three of the cribsheet's rules decide
most of the arguable points and the legend now teaches all three:

* **Ploughing, not grass.** Cropland is planted and cultivated, and it includes
  grassland *only* where it is cleared or sown inside a rotation. Permanent
  pasture and rough grazing are **Nature**. This is the boundary the ledger says
  caps change-F1.
* **Bare ground is Nature unless it is being worked.** Sand and rock belonging
  to a mine, a quarry or a development site are **Artificial**. This is §AL-T's
  error in one sentence: the largest on the map, and one the model is *more*
  confident about when it is wrong.
* **A feature is Artificial for what it is, not what covers it.** A grassed car
  park, a farmyard, a cemetery and an unsealed road are Artificial; a road,
  railway or runway counts over 3 m wide; greenhouses, solar farms and dumps are
  Artificial. A park inside a city is coded by its cover, so it is Nature.

**A felling is whichever class was growing**, and this is the fourth rule.
B84 makes a *crop* plantation Cropland — oil palm, rubber, coffee, tea, cocoa,
coconut, and with B70/B80 also fruit, olives, vines and short-rotation willow —
while a stand grown for timber is in no B class and falls to Nature with
everything else that is neither farmed nor built. So clearing either one and
leaving it is Nature → Nature or Cropland → Cropland, and the three-class
transition cannot carry a clearance on its own: call the dates as you see them
and tick **change seen, ends match**, the only place a clearance with no class
change is recorded. Natural forest cleared **to** a crop plantation is
**Nature → Cropland**, and that one is the habitat loss the campaign exists to
count — so what replaced the trees decides the second date. `cannot interpret` is not a class call — it clears both dates and the
row is excluded from training, so use it for cloud and missing imagery rather
than guessing.

**An identity is required, and it is asked before the batch opens.** Rows are
keyed by `campaign + batch + point + expert_id`, so with everyone writing as the
same person a second reader's call overwrites the first instead of sitting
beside it and the agreement measurement silently reports nothing. See *Who is
labelling* above.

**`cannot interpret` is reversible and needs a reason.** <kbd>K</kbd> sits
between <kbd>M</kbd> and <kbd>G</kbd>; pressing it stashes the class pair and
un-pressing it gives the pair back. It also asks *why* — cloud / no imagery at
one date / resolution too coarse / capture dates too far from targets / artefact
/ other — into a `uninterpretable_reason` column, because these rows never reach
the training set and a countable cause is the only thing they can still buy.
`cloud` says draw elsewhere; `no imagery at one date` says the Wayback archive
is thin there; `capture dates too far from targets` says the label *window* is
the problem rather than the points. Confidence and change-year are disabled
while the flag is set: both are meaningless on a row excluded from training.

**Defer, do not skip.** The button next to *Save & next* keeps whatever is in the
form — classes, flags, confidence, notes — as a draft keyed to you and the
point, restores it when you come back, marks the point on the strip, and takes
it off the <kbd>Enter</kbd> path. The batch is **not complete** while anything is
deferred: the end-of-batch screen lists them. The arrow keys keep drafts too.

**Every save gets a toast** — `saved · Nature → Artificial · ⌫ undo`. It also
makes a stuck key visible: three identical toasts in a row is a thing you
notice. It holds for four seconds and stops its own timer while the pointer or
the keyboard is on it, because reaching for an undo must not race the thing that
takes the undo away.

**The end-of-batch screen carries the two actions it names.** It says "sync it,
then take the next batch", and it now has a **sync** button that reports what is
still held and a **next batch** button that opens the next manifest entry
assigned to you. Both used to mean closing the dialog and going to find controls
in the card stack, at the one moment in the loop where §AL4's whole finding
depends on the next batch actually being taken.

**Two flags sit with the call, four sit with the imagery.** *unsure* and *flag
for review* qualify the answer, so they are pinned in the same pane as the two
date buttons and the transition read-out — they used to scroll away with the
evidence, which is the wrong pane for something you decide at the moment you
decide the label. *cannot interpret*, *mixed cell*, *imagery date gap* and
*change seen, ends match* describe what you were looking at and stay with it.
The split is a `where: 'call'` field on `FLAGS`, and both panes render from the
one list so a flag cannot end up in neither.

**When there is a change, say when.** A year field appears on any change call.
Wayback has already made you step through the dates, so it costs a click, and it
is the only thing here that could ever support an annual model — two endpoints
is a modelling choice you can revisit, an unrecorded observation you cannot.
The same field appears on a stable call flagged *change seen, ends match*: land
cleared in 2020 and regrown by 2024 is honestly `Nature -> Nature`, and that
label throws away what you saw.

| | |
| --- | --- |
| <kbd>A</kbd> <kbd>S</kbd> <kbd>D</kbd> | 2018 = Nature / Cropland / Artificial |
| <kbd>Z</kbd> <kbd>X</kbd> <kbd>C</kbd> | 2024 = Nature / Cropland / Artificial |
| <kbd>1</kbd> <kbd>2</kbd> <kbd>3</kbd> | confidence |
| <kbd>U</kbd> <kbd>K</kbd> <kbd>M</kbd> <kbd>G</kbd> <kbd>T</kbd> <kbd>R</kbd> | unsure / cannot interpret / mixed / imagery gap / change seen but ends match / flag for review |
| <kbd>Enter</kbd> | save and jump to the next point still worth doing |
| <kbd>←</kbd> <kbd>→</kbd> | move without saving |
| <kbd>Backspace</kbd> | back to the point you just left, answer reloaded (the toast's ⌫ undo) |
| <kbd>Space</kbd> *hold* | flicker the swipe to the B date (turns the swipe on if it is off) |
| <kbd>Tab</kbd> | walk the controls. A tabbed control owns its own <kbd>Enter</kbd> / <kbd>Space</kbd>; a *clicked* one does not |
| <kbd>Esc</kbd> | close the chip lightbox, or any dialog. **Not** the identity gate |

<kbd>←</kbd> / <kbd>→</kbd> step the **chip lightbox** while it is open; point
navigation resumes when it is closed.

**The whole app is operable from the keyboard, and the shortcut table is not the
whole of that.** The class buttons, the flags, the filmstrip cells, the point
strip and the ESRI annual swatches are all reachable with <kbd>Tab</kbd> and
carry a visible focus ring. Two rules make the two keyboards coexist, and both
were bugs first:

* **A control the keyboard is sitting on owns <kbd>Enter</kbd> and
  <kbd>Space</kbd>**; everything else stays an app-wide hotkey. The shortcut
  handler is on `window`, and before this it `preventDefault`ed both keys out
  from under every button on the page — Tab to *Defer*, press Enter, and the
  point was **saved**.
* **Only a <kbd>Tab</kbd> hands a control that ownership**, never a mouse click.
  The obvious implementation, `:focus-visible`, is wrong here: Chromium flips it
  true the moment a key arrives at the focused element, so a button clicked with
  the *mouse* claimed the next Enter — clicking a change-year and pressing Enter
  re-toggled the year off instead of saving.

The point strip uses a **roving** <kbd>Tab</kbd> stop — one for the strip, not
1,250 — and <kbd>←</kbd> <kbd>→</kbd> <kbd>Home</kbd> <kbd>End</kbd> walk it
once you are in it.

**Wayback is the instrument.** Turn it on, press **snap to 2018 ⇆ 2024**, and the
swipe is the release nearest 2018 on the left and the release nearest **2024** on
the right — not the newest, which now serves 2025/2026 imagery. The snap runs in
two stages: release dates immediately, so the button stays instant, then the
per-point *capture* dates once they arrive, moving each side to the release whose
imagery is actually nearest its target. Each swipe label shows the gap
(`◀ 2018-05-11 (−0.2 yr from 2018)`), and past ~1.5 years the panel offers the
`imagery date gap` flag rather than leaving you to notice. Then hold
<kbd>Space</kbd>: blink comparison finds a new building or a cleared field far
faster than a static divider, which only tells you the two dates differ
*somewhere*. *only dates with new imagery here* filters the dropdown to releases
whose tiles actually changed at this point — most releases re-serve older tiles.

The panel shows the **true capture date** per side, which is often years from the
release date. **It always describes the point**, never wherever you last clicked:
clicking the map gives a separate, labelled read-out, because an interpreter
comparing a neighbouring field used to record that field's dates as the
provenance of their call.

Nothing is lost if the network drops. Every label is written to `localStorage`
before it is sent, and rows stay in an **outbox** until the Sheet acknowledges
them by id — the pill in the header says how many are held, and closing the tab
with unsent rows asks for confirmation. The outbox is keyed by
`campaign + batch + point + expert` and is independent of whichever batch is on
screen, so unsent rows from batch A still go up after you have moved on to
batch B.

**The pill never claims a state it has not observed.** It starts at
`connecting…`, runs `?action=ping` before it says anything else, and then reads
`saved locally` → `sending N…` → `accepted`, or `offline — N held`, or
`rejected: <reason>` when the sheet answers and refuses. Click it for the last
error and the exec URL. Rows are batched: the debounce is 10 s (or 10 rows, or
the sync button), because a row is durable in localStorage the instant it is
made and a 1.2 s debounce turned a 100-point batch into ~100 lock-taking POSTs
against a backend where every write takes one global script lock.

### Light or dark chrome

The **☾ dark / ☀ light** button in the header. Light is the default; the choice
is remembered per browser and applied in the `<head>`, before the first paint,
so a returning dark labeller does not get a white flash across the imagery on
every load. `prefers-color-scheme` is deliberately *not* consulted — the choice
here is about reading imagery for hours, not about the operating system.

The chrome is the only thing that changes. The imagery, the chips, the three
class swatches and every dataset's own legend colours are identical in both,
because they are the *measurement*: a swatch that means one thing on the raster
and another in the panel is worse than a swatch that is slightly dark for its
ground.

The palette and the control grammar are Weather Lab's. Note *Weather Lab*, not
`deepmind.google` — the marketing site and the app disagree on the two things
that decide whether this reads right, and copying the site (which is what the
first pass did, because the app is behind a Google sign-in and its CSS cannot
be fetched) gets both backwards:

- **The greys are warm.** `#e9e9e7` ground, `#f2f2ef` panels, `#d7d7d2`
  dividers, `#6f6f6b` secondary text — a faintly yellow-green neutral, not the
  cool blue-grey of the marketing pages. And **nothing is pure white**; a stark
  white panel is the single most obvious tell. This is load-bearing rather than
  cosmetic: `--control` is a *translucent white*, so if a panel here ever goes
  back to `#fff` every control on it disappears.
- **The chrome is quiet.** A control is that translucent white with a soft
  `--line-strong` hairline — not a saturated tonal fill, which is what the
  brand site's `.button--tonal` would have you write. Their own status chip is
  exactly this, and their toggles are `#d3d3cd` with a `#5c5c58` knob. Colour is
  spent only where it *means* something: the three classes, and the accent.
- **The shape scale is coarse and consistent** — 8 px chip, 16 px row or field
  or tile, ~24 px floating panel, full pill for anything that toggles. Nothing
  lands between those.
- **A set of exclusive options is a track, not a row of boxes**: one filled
  track, `padding: 4px`, `gap: 4px`, transparent pill segments, the chosen one
  raised to white. Confidence, the Wayback year picker and the chart's series
  toggle are all that shape. The year picker wraps to three rows, so its track
  is a rounded rect rather than a pill.
- **Weight 700 appears three times in 688 KB of DeepMind's CSS.** The scale is
  500 for a title, 450 for a control, 400 for body. Section labels are
  **sentence case in a quiet grey**, not small-caps eyebrows — that was the
  loudest thing on the panel and it is a different design language.

The primary action is charcoal rather than the accent, which is what keeps blue
meaning one thing everywhere: *selected*.

- **The panel is a layer over the map, frosted and rounded** — a translucent
  warm ground behind a blur, with a 30 px rounded left edge, so the imagery
  reads *through* the chrome. That needs map underneath it, and a first attempt
  that pulled the panel 30 px left with a negative margin only frosted a 30 px
  strip: the other 360 px sat on the page ground and read as a flat slab. So
  **`#panel` is `position: absolute` over a full-width `#map`** — and the one
  thing that then has to be right is the camera. With the map running under the
  panel, `jumpTo({center})` puts the point being labelled in the middle of the
  *canvas*, which is behind the glass. MapLibre's `padding` is exactly the fix,
  and `applyMapPad()` sets it on the main map and the Wayback compare map with
  the same value — `wbCmpSync()` mirrors `map.getCenter()`, which **is** the
  padded centre, so the two only stay registered while both pad identically.
  `fitBounds` takes its own padding argument, which *replaces* the transform's,
  so the panel width is added back there by hand. The padding is capped at 60%
  of the canvas: a narrow window loses the effect, not the point.

  Anything anchored to the map's right edge clears the whole panel now
  (`calc(var(--panel-w) + 12px)`), and the lightbox backdrop stops at
  `var(--panel-w)`.

- **Glass alpha is set by the darkest scene, not the prettiest one.** Their
  panel is `.92`. A 60%-opaque one looks better over farmland and then fails
  over open water or hill shadow — a third of black through the panel drags it
  to mid-grey and `--ink` on it falls to ~4.5:1. This app is looked at over
  every scene a global draw contains, so the panel sits near their number and
  only the small cards, which the eye can leave, stay lighter. For the same
  reason the blur **de-saturates** (`saturate(88%)`): the `125%` it started
  with was meant to put back what the blur washes out, and over forest it took
  the map's green, amplified it and spread it through 26 px of blur until the
  whole panel read olive. Large single-hue fields are the normal case in
  satellite imagery, not an edge one.
- **The type is larger than a dense tool would choose.** Their row is 16 px,
  section 18, title 24, on a 520–560 px panel; this is 14/16/17 on a 390 px
  one — the same order of sizes, one notch down. The chips were 11.5 px before,
  which is two notches below anything in the reference and the main reason it
  read as a different product.

Below ~810 px of window the scale steps back down one notch. That is not
taste: what falls off the bottom of `#panel-head-scroll` first is
**confidence**, which is required to save, and a control the interpreter has to
go looking for to finish a point is worse than a control set one size smaller.
The step-down block lives at the *end* of the stylesheet — `@media` adds no
specificity, so the first version of it sat at the top and was overridden by
every rule that followed, changing nothing. At 1280×720 and 1366×768 the form
now overflows *less* than it did before the type came up at all.

The numbers are theirs; the *sizes* are scaled to this app's 13 px base. Their
base button gutter is `12px 24px`, and a 24 px gutter in a panel that has to
hold nine years of evidence is a different mistake —
`test_the_control_stack_does_not_swallow_the_map` is the ceiling, and it caught
exactly that.

Everything in `<style>` is written against the tokens at the top of that block
and nothing else; `test_every_token_has_a_dark_reading` is what keeps that true,
because a rule with a literal colour in it has no dark reading and shows up as
one white panel in an otherwise dark app.

---

## Basemaps, and the two that answer the question directly

The picker offers Esri World Imagery (with or without place labels),
OpenStreetMap, and **EOX Sentinel-2 cloudless for every year 2017–2025**.

Those earn their place for a reason no sub-metre survey can: they are Sentinel-2
at 10 m — the model's own sensor at the model's own pixel — for the years being
called. Every other imagery source in the app shows what somebody else's camera
saw on a date somebody else chose.

The **whole series**, not just the two endpoints, because dating a change is a
separate question from calling it: stepping the basemap year by year answers
"when did this happen" at full map scale, which the 88 px filmstrip cannot.
`s2cloudless-YYYY` exists for 2017–2025 — **2016 does not** (404) — and the year
list has one definition, `EOX_YEARS`, which fills both `BASES` and the
`<select>`.

**2017 is thin, and the picker says so.** Measured 2026-08-31 over the first
twelve points of b001: EOX serve a 116-byte *blank* tile for 2017 at ten of them,
against 4–28 KB for 2018 and 2024 at the same tile — it was their first year and
the mosaic is partial. It is still offered, because where it exists it is the
only 10 m look before the label window opens, but it is labelled *partial
coverage*: a blank basemap that does not say it is blank reads as the app being
broken, and it is the same fault as a layer with no data reading as "no
disturbance" (T2.4). Note also that s2cloudless is **land-only**, so a water
point is blank in every year and that is correct rather than missing. `maxzoom` is capped at 14 because 10 m *is* z14 and EOX answers above
it only by upsampling, so MapLibre stretches locally rather than fetching tiles
carrying no extra information.

**Carto Positron was removed** on 2026-08-31 — it did not draw for the
interpreters. Note before re-adding it: the tile URL answers `200` from outside
a browser, so the fault is not the path and putting it back unchanged will not
fix it.

### The Google Earth link

Earth carries far denser historical imagery than the Wayback archive at most
points, so it is where a hard call actually gets resolved. The per-point **Earth**
link now opens with the **historical imagery panel already on** and at a fixed
eye distance, so the interpreter does not re-open the time slider a thousand
times.

That is done by constructing Earth's opaque `/data=` protobuf. The toggle turns
out to be one point-independent field — `1.5 = {1:1, 2:{}, 3:1}`, eight bytes —
and everything point-specific is `1.3.2`: two coordinate doubles and the literal
`"lat,lon"` search string. Verified against a hand-captured URL: the generated
bytes match exactly but for one ULP in the longitude double, which is Google's
own parse of the decimal string and is about a nanometre of ground.

**It degrades safely by construction.** `search/` and the `@camera` segment sit
*outside* the blob, so if Google changes the encoding the link still lands on
the right point at the right scale and merely loses the panel — which is the
whole reason not to put the position in `data=` alone.

One thing this cannot reach: **Settings ▸ Flight animation speed**. It is a
client-side preference, and on the default Earth spends several seconds flying
per point. Set it to *very fast* once, per browser. The brief says so.

### The Explorer link

Beside **Earth** and **Wayback** is **Explorer** —
[jdbcode/ee-rgb-timeseries](https://github.com/jdbcode/ee-rgb-timeseries), an
Earth Engine App that draws every Sentinel-2 scene at the point as a chip series
plus an index plot. It is the full-resolution version of what this app's
filmstrip and chart approximate: every scene rather than nine annual composites.

Linked, not rebuilt — it runs on its author's Earth Engine quota, needs no
sign-in from us, and §AL9 is explicit that heavy per-scene diagnostics belong
outside the labelling loop.

**The link carries the interpreter's current settings**, so arriving is a
continuation rather than a fresh start. Parameter names and legal values are read
off that repo's source, not guessed: `sensor`, `index` (NBR, NDVI, Blue, Green,
Red, NIR, SWIR1, SWIR2), `rgb` (all four of our chip schemes are in its list
verbatim), `duration`, `cloud`, `chipwidth`. It parses its own **hash**, so the
form is `#k=v;k=v;` and not a query string. `index` has no NDMI — there is no
moisture index on that side — so NDMI falls back to NDVI.

---

## Evidence

Every point carries an **evidence** block, baked at batch build time by
[`src/build_batch_evidence.py`](../src/build_batch_evidence.py) and rendered
open by default in the panel.

**The order is measurement first, other people's models last, and it is not a
matter of taste.** What is being collected here is training data for a 10 m
model over AlphaEarth, so the reading that decides the call has to be the one
made at 10 m from the sensor. Twenty-three rows of confident classifications at
the *top* of the panel is an anchor: the interpreter spends their attention
agreeing with Dynamic World instead of looking at the point. So:

1. a **spectral profile** — median reflectance against wavelength, one line per
   year, **open**. A cropped field and rough grazing look the same in a 10 m RGB
   chip and separate cleanly in SWIR, and that boundary is the one the ledger
   says caps change-F1. It used to be behind a fold.
2. an **index over time** 2017–2025, NDVI by default and switchable to
   NDMI or NBR, with the 2018 and 2024 targets marked and the ledger's 0.31
   vegetation cut drawn on the NDVI series. **Every dot is painted the colour
   that year's chip would be** under the current vis scheme, mixed from the same
   baked bands — so the run of dots is itself a filmstrip, available instantly
   and with Earth Engine never connected. Clicking a year is the sync hub: it
   highlights the chart, spotlights the spectral profile, scrolls the filmstrip,
   moves the Wayback release, and fills in *When did it change?*
   Behind it, **on by default**, the **dense series**: *every* clear-sky
   Sentinel-2 observation at the point, all seasons, not one composite a year.
   It is here for the Cropland / Nature boundary specifically — a cropped field
   and rough grazing have the same annual-composite NDVI and completely
   different shapes *inside* the year, which one composite a year cannot see by
   construction — and it is **baked**, one sidecar per point, so it costs a
   static file and no Earth Engine. See *Baking the dense series* below.
3. **what the existing land cover maps say**, in a **collapsed fold at the
   foot**, in four labelled blocks — *what is here*, *built*, *farmed or
   felled*, *terrain & water*. Dynamic World 2018/2024 plus built/crop/tree
   probabilities, ESA WorldCover 2020 *and* 2021, ESRI annual land cover
   **2017–2025**, JRC forest cover 2020, Hansen `lossyear`, GHSL built surface
   per epoch, slope, a bare-ground flag and JRC water occurrence. Every line
   carries **its end year** and resolution, amber when it cannot see 2024.
   Values render as what they are: a class comes with **its own dataset's
   colour**, a probability as a bar and a signed delta, and the ESRI annual
   series as **a row of nine colour swatches** — the sequence is the reading,
   and a flip and a flip back is visible in it in a way it is not in nine
   comma-separated words. Click a swatch to select that year.

```bash
$G src/build_label_batches.py --candidates … --experts e1,e2 --evidence
$G src/build_batch_evidence.py --batch app/batches/b001.json   # or after the fact
$G src/build_batch_evidence.py --show-seasons                  # no Earth Engine needed
```

**A batch built without `--evidence` shows none of this**, and shows it by
simply not being there — the panel and the filmstrip hide themselves. That is a
confusing half-hour, so `build_label_batches.py` now says so at the end of a
build. The shipped demo batch (`app/batches/b001.json`) carries real baked
evidence.

**Budget ~35 minutes per 100 points.** Earth Engine evaluates in **tiles**, so
spatially clustered points batch cheaply and widely-spread ones must be mapped
over independently. These points are a *global* draw, so the Sentinel-2 timeline
is one request per **(point, year)** — batching 100, or even 20, of them into
one request answers `User memory limit exceeded`, and lowering the chunk size
does not help because the cost is the spread, not the count. The per-request
time is ~2.4 s, but EE rate-limits `getInfo`, so the thread pool buys much less
than its width. It is a build step run once per round; nobody waits on it.

**Why it is baked.** Hosting is static and Earth Engine sign-in is on the
critical path for **imagery only**. The point values, the timeline and the whole
labelling loop render and work with Earth Engine never signed in. Losing the
Sentinel-2 chips is acceptable; losing the app is not. Bump `EVIDENCE_VERSION`
whenever a recipe changes — the app renders it beside the heading, and a
silently changed recipe is the one way a point-values table can be wrong and
still look right.

**The growing season is latitude-aware, and this is the part worth checking.**
`c2c_ts_server.py`, which the Sentinel-2 recipe comes from, hardcodes June–
September because it is a Europe-only tool. These points are drawn globally: a
southern-hemisphere point composited over June–September is its *dry* season,
and a dry-season NDVI series read as a growing-season one says "vegetation loss"
about a place where nothing happened. The window flips by hemisphere (and the
southern one *starts in the previous calendar year*), the tropics get the full
year, and `--show-seasons` prints the table. `growingSeason()` in the app mirrors
`growing_season()` in the builder exactly, because the chips are rendered live
and the chart is baked — if they diverge, every disagreement between the
filmstrip and the chart is an artefact.

**These datasets sit visibly apart from the collapsed model hint**, with a line
saying why: their errors are independent of the model being corrected (§P), and
the posterior's are not — on the two visible map errors it is confident and
wrong.

### Sentinel-2 chips

The one part that *is* live Earth Engine: a filmstrip along the bottom of the
map, one cloud-masked growing-season composite per year, each carrying **two
marks**: a red square, which is the labelling cell — the Sentinel-2 pixel, the
same one the map outlines — and a white ring around it, which is a **locator**
and names no ground area (it is a fixed fraction of the chip width, so ~7
screen pixels in the strip whatever the width is set to). Without something to
locate, a 10 m chip of an agricultural landscape is unlocatable and worse than
nothing; without the square, the only footprint on the picture is one nothing
measured, which is what the ring alone was — 12.8 m radius at the default
width, growing when the width slider moved. At the strip's 176 px the cell is
~3 px and reads as a dot inside the ring; at the lightbox's 512 px it is ~8 px
and is plainly a square.

**The vis scheme is one setting with two kinds of member**, and it drives three
things: the chip pixels, the colour of a chip cell before its image lands, and
— for an index — which series the chart plots. Four three-band mixes (default
`SWIR1/NIR/GREEN`) and three colour-ramped indices (`NDVI`, `NDMI`, `NBR`). The
index band pairs are defined **once**, in `CHIP_INDEX`, so the chip, the dot and
the plotted line are three views of one number rather than three definitions of
it. Scheme and chip width are remembered per browser.

**The chart's dots follow the SERIES, not the scheme.** A dot is painted on the
ramp of whichever index is plotted under it, so its colour and its height are
the same number said twice and on NDVI the ledger's 0.31 cut is a colour
boundary as well as a line. They used to follow the chip scheme, which meant
that plotting NDVI over a `SWIR1/NIR/GREEN` filmstrip coloured them from three
bands with nothing to do with the curve drawn through them. `evVisColor` takes
the combo as an argument for exactly this reason — the strip previews the chip,
the chart reads the series — and `CHIP_INDEX` is still the single definition
both go through.

**Every cell opens already carrying that year's colour**, mixed from the baked
reflectance the batch already holds. It costs nothing, it works with Earth
Engine never connected, and it means the strip answers *which year is
different* the instant the point opens rather than half a minute later — the
"connect Earth Engine" note now sits **beside** the coloured strip rather than
in place of it.

**On making it fast**, which was measured rather than guessed — see
[`ACTIVE_LEARNING.md` §AL9](https://github.com/Geethen/recoverHabloss/blob/main/docs/research/ACTIVE_LEARNING.md). The thing to
know first: **a cold chip costs what it costs**, in the DIST-ALERT inspector
this panel was ported from as much as here. That inspector's server computes
nothing before a click; it feels instant because a previous click or
`warm_ts_cache.py` already paid Earth Engine for that pixel. Everything below is
therefore about making the interpreter's look at a point **not be the first
one**:

* **the whole filmstrip can be baked to static files before anybody labels** —
  `src/build_batch_chips.py`, below. This is the big one: after a bake, opening
  a point costs one file and **no Earth Engine at all**.
* a chip composites **at most `CHIP_SCENE_CAP` = 12 scenes**, the least cloudy
  in the same season. 9 chips went from ~34 s to ~5 s on 9 of 10 points, for a
  median 0.2% difference in reflectance at the plot. The season is untouched,
  which is the property that must not drift; the lightbox drops the cap.
* **all nine years are issued at once.** They used to be *awaited* in two
  waves — the other seven did not start until 2018 and 2024 had both fully
  arrived — which was a guaranteed second wait for nothing.
* **the whole batch is warmed once a bake exists**, image bytes included, four
  lanes at low priority behind whatever is on screen. Warming *N points ahead*
  is a live-Earth-Engine compromise — each point is 5–40 s of quota, so you buy
  only what the interpreter is about to reach — and it is the wrong shape for a
  static file: a window never covers stepping **backwards**, a point revisited
  after a `?` flag, or the second expert opening the same batch cold. A hundred
  sprites is ~3 MB. The live path keeps the window (six points), because there
  it is quota rather than bandwidth. The old prefetch minted the URL and
  stopped, which bought the 2–6 s round trip and none of the 5–40 s the GET of
  that URL actually costs.
* **the cache is no longer cleared when the scheme changes.** The key already
  carries the scheme and the width, so clearing threw away every image the
  interpreter had waited for.
* **the strip requests 176 px**, not 220, for an 88 px cell.

#### The display ramp is measured per point

The fixed `min`/`max` in `CHIP_RGB` are **one ramp for a global draw**, and
3500 DN is 0.35 reflectance. SWIR1 over bare and arid ground and NIR over dense
canopy both run 0.35–0.5, so two of three channels peg at 255 while Green
(~0.08) does not. Measured over all 900 year-cells of the first bake of b001:
**55 cells more than half saturated, 38 more than half floored, 101 more at
sd < 6, and a median 98th percentile of 252 of 255.** A quarter of the filmstrip
was one flat colour — a desert baked as cream, a lake as black — and neither is
a picture of anything.

The ramp is now measured from **each point's own nine years** (p2–p98 at 20 m,
one extra Earth Engine request per point, cached in the batch so a second scheme
re-uses it) and written into `chips.stretch`. Both sides read it: the sprite is
rendered through it in Python, the **tint under an unarrived chip and the live
request** through it in JavaScript. `tests/test_chip_ramp.py` runs the app's own
`comboBounds()` in node against the baker's `combo_bounds()`.

Same audit after the re-bake: clipped-bright **55 → 3**, clipped-dark
**38 → 21**, flat-midtone **101 → 29**, median 98th percentile **252 → 240**.
Cells reading as one flat colour: **236 (26%) → 91 (10%)**, and 37 of the 91 are
the ones the app already hatches as *"no clear"*.

Two properties, and both were bought the hard way:

* **The three channels share one ramp.** Per-band bounds are the textbook
  stretch and were tried first. They are a decorrelation stretch: they move
  **hue**, and hue here is a convention the tips and the legend teach
  ("vegetation is green in SWIR/NIR/GREEN"). On the trial bake they turned a
  field point's greens magenta.
* **The nine years share one ramp.** A per-year auto-stretch renormalises each
  cell independently, which makes a real brightening invisible and a stable
  point flicker. Even the shared-but-narrow version showed this: a uniform
  desert cycled yellow / black / teal / blue across nine years, because a
  250 DN ramp turns the ordinary atmospheric drift between two years into a
  full-scale colour swing. A filmstrip that manufactures change is worse than
  one that is flat.

`--stretch fixed` bakes the old global ramp, and a point the reduce could not
measure keeps it.

#### Baking the filmstrip

```bash
# all four three-band schemes, which is what b001 ships with
$G src/build_batch_chips.py --batch app/batches/b001.json \
    --combo SWIR1/NIR/GREEN NIR/RED/GREEN NIR/SWIR1/RED RED/GREEN/BLUE
$G src/build_batch_chips.py --batch app/batches/b001.json --dry-run # no Earth Engine
```

**All four schemes are baked for `app/batches/b001.json`** — 400 sprites,
**15 MB**, 33–43 KB each, about half an hour at eight workers including the
per-point ramp pass. The first version baked the default scheme only, on an
estimate of ~40 KB per point and "30 MB for six schemes almost nobody switches
to", and what that estimate traded away is sharp: switching scheme on an unbaked
one drops to live Earth Engine at ~30 s a point, or to **no image at all** for
anyone not signed in. (Sprites are *larger* than the 24 KB `chip1` measurement,
because a chip with real texture in it compresses worse than a flat one.) The
index schemes (NDVI/NDMI/NBR) stay live: one normalised difference through a
ramp, they do not clip, and they are cheap.

Each point's nine years go into **one sprite**, sliced in the browser with
`background-position` — so a point is one static file, cacheable forever, with
Earth Engine never signed in. It writes `chips` into the batch JSON (which
`parseBatch` allow-lists; a batch-level key that is not on that list is dropped
silently) and puts the sprites in `<batch_id>_chips/<scheme>/`. **Deploy that
directory alongside the batch.**

A year with **no cloud-free composite in the growing season** — 3.3% of cells
in b001, and one point in every year — comes back from Earth Engine as a *black*
chip, which reads as a broken image rather than as an absence. The app knows
which years those are from the baked timeline, so it hatches them and says
**"no clear"** instead, skips them in the sprite, and does not spend a live
request on them.

Re-runs are **resumable** — a point that already has a sprite is skipped, which
matters when a hundred points is 10–60 minutes of Earth Engine and a token can
expire halfway. `--force` re-bakes the sprites and keeps the measured ramp;
`--force-stretch` re-measures that too, which is what makes a second scheme
cheap.

Everything falls back to live Earth Engine on its own — no bake, an unbaked
scheme, a different `--width` than the app is set to, an unknown `version`, a
batch dropped in as a file, a sprite that 404s. The bake is a latency
optimisation and is never the only way to see a chip.

**And the strip now says which of those happened**, with the fix where there is
one. Silent fallback is correct behaviour and unreadable behaviour at the same
time: all six conditions produce the same strip of flat tints, which is also
what no bake at all produces. The one that actually bites is a **stale
`chipVis.w`** — the width slider is remembered per browser, so a 1280 left over
from a previous session disables every sprite in the batch, for one person, and
survives a reload. The note names it and offers the width back.

**A re-bake writes new pixels to the same path**, so `chips.built` (and
`dense.built`) is stamped onto the URL as `?v=`. Without it every browser and
CDN that already fetched a sprite keeps serving the old one — which is invisible
from a fresh profile and looks exactly like the re-bake not having worked. If
you re-bake by hand, or copy sprites in from somewhere, bump that field.

#### If an Earth Engine overlay does not appear

The overlay is minted per point over a ~7 km box and inserted below `labels`,
above the basemap and above the Wayback raster. Three things it can be, and the
panel now distinguishes them:

* **The note under the layer buttons says something.** `getMapId` validates the
  request; most Earth Engine expression errors only surface when a **tile** is
  rendered, and those used to go to `console.warn` and nowhere else. The note
  now carries the HTTP status *and* Earth Engine's own message, fetched from the
  tile the map could not get.
* **Nothing is drawn and there is no note.** Several overlays are masked
  differences — `dwbuilt` masks to |Δ| > 0.15, Open Buildings likewise — so a
  fully transparent result is the *correct* answer at a stable point. Select
  **Dynamic World 2018** or **2024**, which are unmasked class maps, to check
  whether the overlays work at all.
* **The layer buttons are not there.** `#ee-layers` is hidden until `EE.ready`;
  the Earth Engine card says why.

**`Permission 'earthengine.maps.create' denied`** is the one that has actually
happened, and it is IAM rather than the app: a brand-new service account has no
roles at all, so the campaign account mints a token, computes, and is refused
every map tile. **`roles/earthengine.viewer` does not fix it** — measured on
this deployment, viewer grants `earthengine.computations.create` and *not*
`earthengine.maps.create`, so a viewer account passes `eeTokenSelfTest()` and
draws nothing. Grant the service account — its address is logged by
`eeTokenSelfTest()` — `roles/earthengine.writer` and
`roles/serviceusage.serviceUsageConsumer` on the project:

```bash
gcloud projects add-iam-policy-binding YOUR-EE-PROJECT \
  --member=serviceAccount:<the address eeTokenSelfTest logged> \
  --role=roles/earthengine.writer
```

or the same two roles at `console.cloud.google.com/iam-admin/iam?project=…`.
Check what the account actually holds, rather than what you meant to grant:

```bash
gcloud projects get-iam-policy YOUR-EE-PROJECT \
  --flatten="bindings[].members" \
  --filter="bindings.members:<the address>" --format="table(bindings.role)"
```

A new grant takes a minute or two to propagate. **Run `eeMapsSelfTest()` in the
Apps Script editor to confirm** — `eeTokenSelfTest()` alone cannot see this,
because `earthengine.maps.create` is a separate permission from
`earthengine.computations.create` and the compute probe passes without it.

Both of those live inside the Apps Script editor, which means they are run by
hand and never before a campaign. The same walk from a terminal, against the
deployment as a labeller's browser sees it, is:

```bash
$G src/check_ee_service.py --url '<the /exec URL>' [--token <submitToken>]
```

It pings the deployment, mints a token through it, and then asks Earth Engine
for a computation, a map, a tile of that map and a real overlay from the panel
(`dw24` over a batch point) — the last two being what neither self-test does.
`getMapId` validates the *request*, so an expression that mints and then fails
per tile passes both editor probes and draws nothing. Exit status is 0 only if
every step passes, so it can gate a campaign rather than being remembered.

#### If the chips still look like flat colours

**Read the note under the strip first — there are three of them and they say
different things.** A grey box saying *baked chips are not being used* is a
fault and names which condition failed. A box with a blue edge is not a fault:
either the scheme is an **index**, which is never baked and is always computed
live (a few seconds a point), or the strip is **flat because the ground is** —
at a uniform surface the three channels' shared ramp is thousands of DN wide
against a per-band spread of a hundred or two, and nine identical orange squares
are the right picture of a desert. That last one is *not* fixable by narrowing
the ramp; see **The display ramp is measured per point** above for why both
alternatives are worse.

Two things that used to look like a broken bake and are now handled:

* **A remembered width outliving its batch.** `chipVis.w` is persisted and the
  sprite path needs it to equal `chips.width_m` exactly, so one nudge of the
  width slider disabled every baked chip in every batch for that browser, across
  reloads. Opening a batch that carries a bake now snaps the width to it. Moving
  the slider mid-batch still drops to live Earth Engine, on purpose.
* **An index scheme reported as a bake miss.** NDVI / NDMI / NBR were reported
  down the same channel as a stale bake, so a design decision read as a fault.

If none of that applies, in the browser console, in order:

```js
S.batch.chips.version          // 'chip2', or the bake is stale
S.batchUrl                     // '' means the batch was opened as a FILE
chipVis                        // .w must equal S.batch.chips.width_m
chipSpriteUrl(S.points[S.i])   // the URL, or null
chipBakeMiss(S.points[S.i])    // the reason, in words
chipFlatNote(S.points[S.i])    // set when the flatness is the ground, not a bug
```

A hard reload (<kbd>Ctrl</kbd>/<kbd>Cmd</kbd> + <kbd>Shift</kbd> + <kbd>R</kbd>)
rules out a cached `label_app.html`, and `localStorage.removeItem('recover-labels:chips')`
rules out a remembered width or scheme.

`SCENE_CAP`, `CELL`, `COMBOS` and `STRETCH_MIN_SPAN` in the script must match
`CHIP_SCENE_CAP`, `CHIP_DIM`, `CHIP_RGB` and `STRETCH_MIN_SPAN` in the app —
`tests/test_chip_ramp.py` checks the last of those by running the app's own
function — and the season comes from `build_batch_evidence.growing_season` by
**import**, never restated.

#### Baking the dense series

```bash
$G src/build_batch_dense.py --batch app/batches/b001.json   # ~1 MB, ~10 min
$G src/build_batch_dense.py --batch app/batches/b001.json --dry-run
```

`build_batch_chips.py` for the numbers instead of the pictures, and the reason
it exists is that the note saying the dense series *could not* be baked — "nine
years of Sentinel-2 at a point is a few hundred rows, and a hundred points of
that is a batch file nobody can download" — was true of the batch JSON and
irrelevant once the chip bake established the **sidecar**. Measured: **298
observations, 10.5 KB** for the first point of b001, one request, 38 s.

So it is **on by default**, and it works with Earth Engine never signed in. A
series the interpreter has to ask for is one they ask for *after* they have
already made the call, which is the wrong way round for the only instrument in
the panel that separates the two classes it exists for.

`series_for` **is** `denseFetchLive()` in Python — same collection, pre-filter,
mask, 30 m buffer, 20 m scale — and `tests/test_chip_ramp.py` reads the
JavaScript to check it. Half the batch may be served from the sidecar and half
from the live call; a drift here puts two different measurements on one chart.
It deliberately does not use the s2cloudless join the composites use: that made
a first attempt take 103 s for 269 scenes. Sidecars go in
`<batch_id>_dense/<point_id>.json` — **deploy that directory alongside the
batch** — and a 404 or a malformed file falls straight back to the live path.

A chip that hangs becomes an explicit **↻ retry** rather than spinning forever,
because a hung chip and a failed one otherwise look identical and an
interpreter will wait. Double-click or **⤢** opens the lightbox, which shows
the capped chip immediately and then replaces it with the **uncapped** composite
at 512 px, and owns <kbd>←</kbd>/<kbd>→</kbd> while it is up.

**The lightbox covers the map, not the panel.** Stepping years in it has always
driven the spectral profile and the index chart — `renderLightbox` calls
`selectEvYear(…, {quiet: true})` — but the backdrop was `inset: 0` and they
updated underneath it, which is the whole linked reading rendered invisible. It
now stops at the panel edge (`right: var(--panel-w)`; full-width under 900 px,
where there is no room for both), scrolls the chart into view on open, and hides
the standing explanations under each reading while it is up so both fit. The
panel stays live, so the call can be made from the enlarged chip.

---

## Earth Engine (optional)

The app is fully usable without it; these are auxiliary priors, not the imagery
you label from. Their value is that their errors are **independent** of the model
being corrected — §P's standing negative is on adding a path whose errors
correlate with the one you already have.

**Overlays are grouped as questions**, one active at a time: *land cover*
(Dynamic World 2018/2024 and their disagreement, ESA WorldCover, and **ESRI
annual 2018 / 2024 / their disagreement** — the only annual 10 m product that
answers for both ends of the question), *forest loss* (Hansen loss year and
**JRC forest cover 2020 V3**, whose EUDR definition excludes agricultural
plantations, which is exactly the Hansen-says-tree-cover / this-legend-says-
Cropland case), *built change* (Dynamic World built probability, **Open
Buildings Temporal presence 2018 → 2023** at 4 m, and GHSL) and *terrain &
water*. Every layer carries a
**legend**, its **end year**, its resolution and its coverage period, and each
is computed over a box around the current point with the mapid re-minted per
point — `dwMode` over a global collection was reducing roughly 70 images per
tile at every new location. Five specifics worth knowing:

* Hansen is the **2025 v1_13** vintage, ramped 18–25. The v1_11 vintage's
  `lossyear` stops at 23 and the label window ends in 2024, so a 2024 clearance
  was invisible with nothing on screen to say so.
* GHSL's epochs are **2015 → 2020** against a 2018 → 2024 question, and it is
  labelled with the years it actually covers. The asset also holds 2025 and
  2030 and it would be easy to reach for them: they are **extrapolated, not
  observed**, and are not used. The **Dynamic World built probability
  difference** is on the right window and is the one to reach for.
* **ESRI annual land cover now runs to 2025**, not 2023. The end-year rule
  paying off in the other direction: a vintage that moved *forward* unnoticed
  is a dataset answering 2023 to a 2024 question for no reason. It is the only
  annual 10 m product that can answer both ends.
* **Open Buildings Temporal is regional** — Africa, South and South-East Asia,
  Latin America and the Caribbean. Its `cover` string says so in those words,
  because the draw is global and most points fall outside it.
* An empty overlay reads **"outside coverage"**, never blank. A layer with no
  data must not look like "no disturbance".
* **Wayback is a basemap and sits under every overlay.** Both used to insert
  before the `labels` layer, and `addLayer(l, beforeId)` puts a layer
  immediately *below* `beforeId` — so whichever was touched last won the top
  slot, and both orders happen in normal use. Pick an overlay and then turn the
  archive on and Wayback covered the overlay; step to the next point, which
  re-mints the overlay per point, and a 70%-opaque class raster covered the
  imagery being labelled from. The archive now has a slot of its own
  (`imagery-slot`, declared in the style), so the order is a property of the
  style rather than of what was clicked when.

### The pixel inspector

Everything else in the panel is baked **at the plot centre**, which is the right
default and the wrong instrument in front of a mixed cell — the interpreter's
question there is about *that* field, not this one. Tick **🔎 Inspect pixel
values** in the Earth Engine card and click anywhere on the map:

* **Sentinel-2 at the clicked pixel, 2018 and 2024, with the difference** — six
  bands as reflectance, then NDVI, NDMI and NBR. It is the *same* composite: it
  goes through `s2Chip`, the one definition of the growing-season median that
  the filmstrip and the chip ramp also use, and the indices are computed in the
  browser from the six numbers printed above them rather than asked of Earth
  Engine separately, so the table cannot disagree with itself, with the chart,
  or with the chips. The read-out names the season it used, because the season
  comes from the **click's** latitude and a click across the equator from the
  point is a different window.
* **the active overlay, decoded** — `6` is not a reading, `built (6)` is. Class
  codes go through the layer's own table (and through ESRI's *remapped* codes,
  not its published ones); quantities carry units. Each is stamped with the
  layer's end year, exactly as its legend is.

Two properties it keeps. It is **at the click, never at the point**: `imagery_a`
and `imagery_b` on the saved row describe the point whatever was last clicked,
Wayback's own click read-out already draws that line, and nothing the inspector
says is written anywhere or survives onto the next point. And **a masked pixel
is an answer** — `dwbuilt`, `obtemporal`, `hansen` and the two disagreement
layers are masked differences, so no value at a stable point is the layer saying
*nothing here* and is reported as that rather than as a failure.

It is one Earth Engine round trip per click: both `reduceRegion` calls are
combined server-side into a single dictionary and evaluated once, because the
measured cost here is the request and not the computation (§AL9).

### Nobody signs in — the campaign's service account

The default (`eeAuthMode: 'auto'`). The deployment holds **one** Earth Engine
identity, the Apps Script hands each page a one-hour access token, and a
labeller opens their URL and has the layers. No Cloud project to know, no Earth
Engine registration of their own, no popup — and each of those three is a step
an interpreter can fail at: the registration is a form most of them will not
complete, and a managed browser eats the popup with no error anyone can read.

**The key cannot ship in the page, and that is not a judgement call.** The SDK
refuses it outright — `ee.data.authenticateViaPrivateKey` opens with, verbatim
from `build/ee_api_js.js`:

```js
if ("window" in t) throw Error("Use of private key authentication in the " +
  "browser is insecure. Consider using OAuth, instead.");
```

So the key lives in the Apps Script that already backs the Sheet — in **Script
Properties**, which are per-deployment and never in git — and the browser only
ever sees a token that expires within the hour. Which means this path needs the
Sheet backend deployed even in a campaign that exports its labels by hand.

Once, per deployment:

1. **Make the service account** in the Cloud project that is registered for
   Earth Engine: *IAM & Admin ▸ Service Accounts ▸ Create*. Give it
   **Earth Engine Resource Writer** (`roles/earthengine.writer`) and
   **Service Usage Consumer** (`roles/serviceusage.serviceUsageConsumer`), and
   nothing else — the role is load-bearing, see below. **Writer, not Viewer**:
   the overlays need `earthengine.maps.create` and Viewer does not carry it,
   which cost this deployment a labelling session.
2. **Download a JSON key**: *Keys ▸ Add key ▸ Create new key ▸ JSON*.
3. **Paste it into Script Properties, not into a file.** In the Sheet's Apps
   Script editor: *Project Settings ▸ Script properties ▸ Add script property*,
   name `EE_SERVICE_ACCOUNT_KEY`, value the **whole** downloaded JSON. An
   optional `EE_PROJECT` overrides the project; by default the key's own
   `project_id` is used, which is right whenever the account lives in the
   EE-registered project.
4. **Re-deploy the web app**: *Deploy ▸ Manage deployments ▸ ✏️ ▸ Version: New
   version*. Apps Script serves the version that was deployed, so pasting the
   new `Code.gs` without this leaves `/exec` running the old code — and the
   symptom is not an error, it is the sign-in button still being there.
5. **Check it.** Run `eeTokenSelfTest()` from the editor: it logs the account,
   the project, the token's remaining life (truncated — the editor log is not a
   place to leave a live credential) and what Earth Engine answered, either
   `HTTP 200 -- ready.` or the error verbatim. `curl '<exec-url>'` now also
   reports `"ee_service_account": true`.

Nothing in `config.js` needs changing: `eeAuthMode` is already `'auto'` and
`eeTokenUrl` defaults to `sheetUrl`. Set `'service'` to drop the sign-in button
altogether, `'oauth'` for the old behaviour, `'off'` for neither; `?eeAuth=…`
overrides per link. In `'auto'`, a deployment with no service account — or one
still serving an older `Code.gs` — is left exactly where it was, with the
button, and **no error is shown**: it is not a fault the person reading the
panel can act on.

**What this hands out, and to whom.** A web app deployed *Who has access:
Anyone*, plus a `submitToken` that ships inside `config.js`, means anyone you
gave the app to can mint Earth Engine tokens for your project. That is a real
step up from "can write rows to your sheet", and two things bound it, both of
which you have to actually do:

* **`roles/earthengine.writer` and no more.** Do not reach for Editor or Owner
  to make a permissions error go away. Writer is already wider than this app
  needs — the token every browser is handed can create and delete assets in the
  project — so put the campaign account in a project that holds nothing, or
  give it a **custom role** of `earthengine.computations.create` +
  `earthengine.maps.create` + `serviceusage.services.use`, which is exactly
  what the overlays do and nothing else. Viewer is the tempting middle and it
  does not work: no `earthengine.maps.create`, no overlays.
* **Set `SUBMIT_TOKEN`.** `ee_token` is behind the same check as every other
  action, which raises the bar from *anyone who finds the /exec URL* to *anyone
  you gave the app to* — the same threat model the Sheet writes already accept.

Rotating is deleting the key in the Cloud Console and pasting a new one into
the script property; nothing is cached longer than an hour.

### The sign-in fallback

Used when `eeAuthMode` is `'oauth'`, or when it is `'auto'` and the
deployment has no service account. Everything below is unchanged; it is no
longer the path a labeller normally takes.

**Connecting asks you for one thing: your Cloud project.** It lives behind
*⚙ deployment setup* along with the client id — both are already filled from
`config.js`, and showing them first reads as "this needs setting up", which is
exactly what a labeller must not conclude. `?ee_project_id=…` on the URL
(GeoLibre's spelling, `?eeProject=…` also works) sets it from a link. Once a
browser has connected successfully it **auto-connects** on later visits: the
session re-authorises silently every 50 minutes anyway, so a button that makes
you consent again on every reload buys nothing.

**Setting the deployment up asks for one more, once.** Google will not hand a
browser an access token for a client id it does not recognise from this exact
origin, so the deployment needs an **OAuth client id** of its own. GeoLibre can
ship [a default](https://github.com/opengeos/GeoLibre/blob/main/packages/plugins/src/plugins/earth-engine-auth.ts)
only because that id is registered for GeoLibre's own origins; borrowing it —
or any other — fails with `Error 400: origin_mismatch`. So, once:

1. Open the Earth Engine card and press **Connect Earth Engine**. With no client
   id set it opens *⚙ deployment setup* with the two buttons for the next steps.
2. **make one in the Cloud Console ↗** opens *Credentials ▸ Create credentials ▸
   OAuth client ID* on the project you typed. Type **Web application** — not
   Desktop, which fails with an unhelpful error.
3. **copy origin** puts this page's origin on the clipboard; paste it into
   **Authorised JavaScript origins**. Origins, not URLs: no path, no trailing
   slash. Add every origin the app is served from — `http://localhost:8000` for
   local testing, plus the real host.
4. Paste the client id into the `client id` box. That browser now has it; to
   give it to everyone, put it in `config.js` as `eeClientId`. **`config.js` is
   the copy that survives a redeploy of `label_app.html`.**
5. Each user still needs their own Earth Engine registration; the client id does
   not grant them access.

**None of this applies to the Pages deployment**, which runs
`eeAuthMode: 'service'` and therefore uses no client id at all — that is one of
the reasons it is the right mode for a public static host. This whole section is
the fallback path, for `'auto'` or `'oauth'`.

The checked-in `config.js` is blank; the deployed one comes from the
`LABEL_APP_CONFIG_JS` secret. If you switch that secret to `'auto'`,
`http://localhost:8000` and `http://127.0.0.1:8000` are verified working origins
and **the Pages host is not** — **add it to the client id first**, because a
missing origin is the silent failure described at the foot of this section, not
an error message.

The consent screen currently reads *"Sign in to continue to Earth Engine
Notebook Client - <owner's address>"*, which is the project's **Branding** app
name, not the client's. Worth renaming to something an interpreter recognises:
being asked to hand a stranger's notebook access to your Google account is a
reason to close the window. Renaming it does not affect `ee.Authenticate()` in
Python, which uses Google's own client id rather than one from this project.

**If it does not work**, the button prints a step-by-step trace — client id,
project, libraries loaded, Google sign-in, Earth Engine started — and marks the
step that failed with the error. That distinguishes the four failures that
otherwise look identical from the button: nothing configured, blocked popup,
wrong client-id type or origin, and an account that is not registered for Earth
Engine.

**Four things that make this work, all read off the SDK rather than guessed:**

* **`ee.data.setProject` does not exist.** The Cloud project is the *sixth*
  argument to `ee.initialize(baseurl, tileurl, onOk, onErr, xsrf, project)`.
  Without it the SDK falls back to `earthengine-legacy`, so sign-in reports
  success and every layer then fails — which is why the panel refuses to sign in
  until the project box has something in it, rather than warning and continuing.
* **The scopes are deliberately minimal: `earthengine` only.** The SDK's defaults
  are `earthengine` + `cloud-platform` + full `drive`, and passing extra scopes
  does not replace them — `authenticateViaOauth` takes a *suppressDefaultScopes*
  flag as its last argument and only that switches the defaults off. Dropping
  `cloud-platform` follows [GeoLibre](https://github.com/opengeos/GeoLibre/blob/main/apps/geolibre-desktop/src-tauri/src/earth_engine_oauth.rs),
  for the same reason: it is a broad scope that drags the app into Google's
  restricted-scope verification for capabilities this page never uses. If you
  add Drive export later, `drive.file` is the non-sensitive per-file scope —
  not full `drive`.
* **Both libraries are loaded when the panel is opened, not when the button is
  pressed.** `authenticateViaOauth` ends in `requestAccessToken()`, which opens a
  popup, and a browser only allows that inside a live user gesture. The SDK
  defers that call behind its own async load of Google Identity Services unless
  `window.default_gsi` is set — so the app loads GIS itself, sets that flag, and
  leaves nothing awaited between the click and the popup. The session is
  re-authorised every 50 minutes; a signed-in user does not normally see a
  second consent prompt.
* **An unregistered origin is silent, not an error.** The SDK sets no
  `error_callback` on the GIS token client, so Google reports `origin_mismatch`
  *inside the popup* and nothing reaches the page — indistinguishable from a
  popup the browser blocked. The panel gives up after 90 seconds and names both
  causes; do not read "no answer" as "popup blocked" and stop there.

---

## Calibration batches — do this before anyone labels anything

The standing verdict in the ledger is that `Cropland`/`Nature` label noise sets
the change-F1 ceiling. Without calibration, three people can label a thousand
points to three different standards and the first sign of it is an agreement
number at the end of the round, when it is unfixable.

A calibration batch carries **known answers**, and there are **two stages**, in
this order, both before anyone's first real batch. One mixed set neither teaches
reliably nor measures anything: being told the answer is what makes the legend
stick, and being told the answer is also what makes the score meaningless.

```bash
# 1. teaching: you are told the agreed answer after every call
$G src/build_label_batches.py \
    --candidates <a table that already has agreed transitions> \
    --calibration --stage teach --reference-col transition --prefix calteach

# 2. qualification: blind, told once at the end
$G src/build_label_batches.py \
    --candidates <a different draw from the same table> \
    --calibration --stage qualify --reference-col transition --prefix calqual
```

`--stage` sets `--feedback` for you (`teach` → immediate, `qualify` → end) and
the app says which stage a batch is when it opens.

Draw the reference points from the existing labelled set: what you want to know
is whether a new interpreter reproduces *your* standard, and those plots are the
only statement of it that exists. 25 points is the default — long enough to
separate a systematic legend disagreement from scatter, short enough that people
actually do it.

The app never shows the reference before the call. Either way the end screen
names the boundaries where they differed, and `src/label_rounds.py` reports the
same thing **per expert, with the confusion pairs**, and reports the two stages
apart:

```
calibration (teaching): 40 rows
  e1                    20 / 20  (100%)  (Ann Example)
  e2                    17 / 20  (85%)   (Bo Example)
      Nature -> Nature           -> Cropland -> Nature           3

calibration (qualification): 40 rows
  e1                    19 / 20  (95%)   (Ann Example)
  e2                    16 / 20  (80%)   (Bo Example)
      Nature -> Nature           -> Cropland -> Nature           3
```

**Read the pattern, not the percentage.** `bo` at 85% made the *same* mistake
three times — that is a ten-minute conversation about what counts as fallow. The
same 85% made of scattered singletons is a different problem entirely. And the
reference is one careful reading, not truth: a disagreement is worth a
conversation about the legend as often as it is worth a correction.

Calibration rows are excluded from the yield table, because a reference plot is
an exercise rather than a plot the campaign found.

---

## Closing the round

```bash
G=python   # whichever interpreter has the deps

# pull the round back, read what it bought, and write the exclusion list
$G src/label_rounds.py --url '<exec-url>' \
    --exclude-out data/analysis_results/round1_ids.csv

# cut the next round without repeating any of it
$G src/build_label_batches.py --candidates <next-ranked-table> \
    --channel coverage --exclude-labelled data/analysis_results/round1_ids.csv
```

`label_rounds.py` reports confirmed plots per point per class per channel,
inter-rater agreement (on distinct **experts**, so one person correcting
themselves is a correction and not a second reading), why points came back
unusable counted per cause, seconds per point, and the falsification test §AL states
in advance: an acquisition channel has to return **≥ 2× the equal-area rate** on
the binding class or the campaign should go back to random draws. That test needs
an equal-area arm in the same round — `build_label_batches.py --placeholder`
builds one, and with no `random` rows present the enrichment is reported as
unavailable rather than as passing.
