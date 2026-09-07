# PlotCall operator guide

This guide covers configuration and routine operation of the static application in this directory. See the [repository README](../README.md) for scientific scope, limitations and the full workflow.

## Local demo

From the repository root:

```bash
python -m pip install -e .
python src/build_label_batches.py --placeholder
python -m http.server 8000 --directory app
```

Open <http://localhost:8000/label_app.html>.

The default configuration has no submission endpoint. Labels remain in browser storage until the reader exports them. This is suitable for trying the interface, but a study should configure and test a backend or establish a controlled export procedure.

Screenshots of the interface are in the [repository README](../README.md#what-it-looks-like).

Do not open `label_app.html` directly as a `file://` URL. A local HTTP server is required for loading the manifest, batches and sidecars.

## Files served to the browser

```text
label_app.html       application and default interpretation protocol
config.js            deployment-specific browser configuration
batches/index.json   batch manifest
batches/*.json       batch definitions
batches/*_chips/     optional image-filmstrip sidecars
batches/*_dense/     optional dense time-series sidecars
```

Everything in this directory is public when deployed. Do not place private keys, credentials or confidential candidate data here.

## Configure the application

Edit `config.js`:

| Field | Meaning |
| --- | --- |
| `sheetUrl` | Apps Script `/exec` endpoint. Empty means local-only storage. |
| `submitToken` | Anti-spam value expected by the backend. It is visible in the browser and is not authentication. |
| `experts` | Stable reader IDs and display names. |
| `campaign` | Namespace stored with each interpretation. It must match the batch builder. |
| `manifest` | Batch-manifest path, normally `batches/index.json`. |
| `title`, `heading` | Deployment name shown in the browser and page header. |
| `cribsheetUrl` | Link to the full class protocol. |
| `pointZoom` | Initial map zoom at a new point. |
| `eeAuthMode` | Earth Engine mode: `auto`, `service`, `oauth` or `off`. |
| `eeTokenUrl` | Optional token broker; otherwise `sheetUrl` is used. |
| `eeProject`, `eeClientId` | Project and web-client settings for OAuth. |

Use permanent expert IDs. Display names can change, but reusing an ID for another person makes independent rows indistinguishable.

The default classes and endpoint years are implemented in `label_app.html`; they are not deployment fields in `config.js`. A different protocol requires coordinated changes to the interface, class descriptions, calibration batches, evidence builders, output consumers and tests.

## Google Sheets backend

The optional backend performs locked upserts into a Sheet. Its record key is:

```text
(campaign, batch_id, point_id, expert_id)
```

This prevents a corrected interpretation from creating an ambiguous duplicate and keeps independent readers in separate rows.

Set it up as follows:

1. Create a Google Sheet.
2. Open **Extensions → Apps Script**.
3. Replace the editor contents with `apps_script/Code.gs`.
4. Set `SUBMIT_TOKEN` in the deployed copy if an anti-spam value is wanted.
5. Choose **Deploy → New deployment → Web app**.
6. Run as the owner and choose the access policy required by the study.
7. Copy the resulting `/exec` URL to `sheetUrl` in `config.js`.
8. Put the same anti-spam value in `submitToken`.

Check the live deployment:

```bash
curl "YOUR_EXEC_URL?action=ping"
```

The response reports whether a token is required and whether the optional Earth Engine service account is configured. A blank value in the repository does not describe a separately deployed Apps Script copy.

### Security properties

- The web app is an application endpoint, not user authentication.
- `submitToken` is delivered to every reader's browser. It can reduce accidental writes but cannot identify or authorise individual readers.
- Anyone allowed to call the read action may be able to retrieve study labels, depending on deployment settings.
- Treat the Sheet URL as operational configuration, not as a secret.
- Never put a service-account private key in `config.js`, `label_app.html`, `Code.gs` or Git.

Assess access requirements for the data being collected. The supplied backend is appropriate only when its access model matches those requirements.

## Earth Engine

PlotCall can show live Earth Engine layers or use precomputed evidence.

### Service-account mode

To avoid a reader sign-in, store the complete service-account JSON as the Apps Script property `EE_SERVICE_ACCOUNT_KEY`. The optional property `EE_PROJECT` overrides the key's project ID. Set `eeAuthMode: 'service'` or `'auto'` in `config.js`.

The backend mints a short-lived access token; the private key remains server-side. The bearer token is still available to application users, so use a dedicated project, the smallest workable IAM role, quotas and monitoring. The comments in `Code.gs` describe the required Earth Engine permissions and self-tests.

### OAuth mode

Set `eeAuthMode: 'oauth'`, `eeProject` and an OAuth Web application client ID in `eeClientId`. Register every exact deployment origin. Each reader signs in with an account that has Earth Engine access.

### Offline or baked mode

Set `eeAuthMode: 'off'` when live layers are not part of the protocol. Baked evidence remains available if its sidecars were deployed. The core labelling and export functions do not require Earth Engine.

## Build and assign batches

Create batches from a CSV, Parquet or GeoJSON candidate table:

```bash
python src/build_label_batches.py \
  --candidates candidates.csv \
  --campaign example \
  --channel coverage \
  --batch-size 100 \
  --experts e1,e2 \
  --double-frac 0.05
```

Required coordinate columns are longitude and latitude. The reader accepts common names such as `lon`/`lat`. Optional provenance and ordering fields include `id`, `channel`, `rank`, `score` and `cell_km`.

`--experts` assigns primary readers round-robin. `--double-frac` selects a reproducible shared subset. Agreement is estimable only from independently read overlap; it should not be created after inspecting outcomes.

The builder appends or replaces entries in `batches/index.json`. Use a new campaign or batch prefix when identifier reuse would be ambiguous, and archive the exact batch files used in a session.

To omit previously returned points:

```bash
python src/build_label_batches.py \
  --candidates next_candidates.csv \
  --exclude-labelled pooled_labels.csv
```

## Calibration

Calibration points carry an agreed transition in a reference column:

```bash
python src/build_label_batches.py \
  --candidates calibration.csv \
  --calibration \
  --reference-col transition \
  --stage teach \
  --feedback immediate
```

Use a teaching stage for immediate explanations and a separate qualification stage for measurement:

```bash
python src/build_label_batches.py \
  --candidates qualification.csv \
  --calibration \
  --reference-col transition \
  --stage qualify \
  --feedback end
```

Reference labels should be produced by a documented process. They are not made correct merely by being placed in a calibration file.

## Bake evidence

Install the optional dependencies and authenticate the build environment:

```bash
python -m pip install -e ".[bake]"
earthengine authenticate
```

Available builders are:

```bash
python src/build_batch_evidence.py --batch app/batches/b001.json
python src/build_batch_chips.py --batch app/batches/b001.json
python src/build_batch_dense.py --batch app/batches/b001.json
```

- `build_batch_evidence.py` adds compact summary values and annual context.
- `build_batch_chips.py` creates static Sentinel-2 filmstrips.
- `build_batch_dense.py` creates dense index-series sidecars.

Use `--dry-run` on the chip and dense builders to inspect planned work. Rebuilding may change derived evidence when upstream collections or processing code change. Record the builder version, source assets, dates and parameters with the interpretation round.

When a batch declares a sidecar, deploy its directory with the JSON file. The Pages workflow checks for missing and incompatible sidecars.

## GitHub Pages

The workflow in `.github/workflows/pages.yml` publishes `app/` after changes on `main`.

There are two configuration paths:

- commit `app/config.js`; or
- set the repository secret `LABEL_APP_CONFIG_JS` to the complete JavaScript file.

When the secret is present, it replaces the committed file for that deployment. When absent, the committed file is used. A blank `sheetUrl` produces a workflow warning and a local-only application; it does not fail the build.

Before a session, test the actual Pages URL in a clean browser profile:

1. the expected campaign title and expert roster appear;
2. the correct batch opens;
3. the yellow cell is positioned correctly;
4. both endpoint calls can be completed;
5. a row reaches the intended Sheet, or manual export works;
6. a failed submission remains in the outbox;
7. an exported file restores progress without replacing unfinished points;
8. required evidence and attribution load.

## Reader operation

For each point, the reader should:

1. inspect the exact yellow cell at the first endpoint;
2. select the majority class;
3. inspect the same cell at the second endpoint;
4. select the majority class;
5. record confidence, applicable flags and notes;
6. save and continue.

External products are supporting evidence, not authoritative answers. The initial call is preserved separately when a reader later changes it after opening model-derived context.

`cannot interpret` means no defensible endpoint classification can be made. `unsure` means a best call is possible but evidence is weak or ambiguous. Keep those outcomes distinct during analysis.

## Export, recovery and collection

Browser storage is namespaced by campaign, reader and batch. The internal storage prefix is retained for compatibility with existing saved drafts.

The export contains completed label rows, not the batch definition. Dropping an export back into the application merges saved answers with the currently loaded batch by point ID; unfinished points remain available.

Pool local exports or download from the backend:

```bash
python src/label_rounds.py --csv exports/*.csv --campaign example
python src/label_rounds.py --url "YOUR_EXEC_URL" --campaign example
```

Add `--exclude-out interpreted_ids.txt` to produce an exclusion list for the next batch build. Use `--binding "Class A -> Class B"` only for a transition selected before inspecting the round; it adds a comparison with the random control channel.

## Troubleshooting

- **The app says it is unconfigured:** check that `config.js` loads, contains
  valid JavaScript and assigns `window.LABEL_APP_CONFIG`.

- **Labels do not reach the Sheet:** call `?action=ping`, compare `sheetUrl` and
  `submitToken` with the deployed Apps Script copy, inspect the outbox indicator,
  and export before clearing browser data.

- **Live evidence does not draw:** check `eeAuthMode`, project registration,
  exact OAuth origin or token-broker response. Core labels can still be saved
  without live Earth Engine.

- **A filmstrip or dense series falls back to live processing:** confirm the
  sidecar directory is deployed and that its version matches the constants in
  the app and builder.

- **The wrong reader queue appears:** confirm the URL's `expert` value matches a
  stable ID in `config.js` and that the batch's `required_readers` values use
  the same IDs.

## Pre-session record

Archive at least:

- Git commit or release identifier;
- deployed `config.js`, excluding any separately managed credentials;
- class protocol and calibration references;
- batch manifest and batch files;
- evidence schemas and sidecars;
- source dataset versions and processing parameters;
- sampling and overlap rules;
- backend ping result and a successful test submission;
- planned disagreement and exclusion rules.

This record is necessary to interpret the resulting labels as measurements produced by a defined procedure.
