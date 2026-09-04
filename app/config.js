/**
 * Deployment config for label_app.html.
 *
 * This file is separate from the app on purpose: re-deploying label_app.html
 * must never clobber the Sheet URL or the OAuth client id. Edit here, leave the
 * app alone. Every field is optional -- the app runs with all of them empty and
 * you export labels by hand.
 *
 * WHAT THIS FILE IS AND IS NOT. It configures a LOCAL serve. The published
 * Pages app does not read it: .github/workflows/pages.yml OVERWRITES it with
 * the LABEL_APP_CONFIG_JS Actions secret at build time, and a missing secret
 * fails the build rather than publishing an app with no backend. So a field
 * here is not a statement about the deployment -- ask the deployment:
 *
 *     curl '<sheetUrl>?action=ping'
 *       -> "token_required": …, "ee_service_account": …
 *
 * WHAT MAY BE COMMITTED, AND WHY. The line is drawn by one question: does
 * every browser that opens the app already have this value? `sheetUrl` and
 * `submitToken` do -- the page downloads this file -- so committing them
 * publishes nothing that was private, and blanking them hides them from nobody
 * while costing a dragged-in copy of app/ that does not work.
 *
 *   - `sheetUrl` is an Apps Script /exec endpoint whose only powers are the
 *     ones Code.gs gives it: append a label row, read this campaign's rows,
 *     mint a one-hour read-only Earth Engine token. It is not an account and
 *     it holds no credential.
 *   - `submitToken` is anti-spam, not authentication. It stops somebody who
 *     stumbles on the /exec URL from writing junk rows; it draws no line
 *     between your interpreters, and it is not meant to. Rotate it in Script
 *     Properties and here if the sheet ever fills with noise.
 *
 * They are left EMPTY in this template on purpose: a fork that inherited
 * somebody else's /exec URL would quietly file its labels in somebody else's
 * sheet. Fill them in with your own, and commit them if your repo is yours.
 *
 * The thing that IS a secret is the Earth Engine private key, and it is NOT
 * here and cannot be -- see `eeAuthMode` below. It lives in the Apps Script's
 * Script Properties, server-side, and the page is handed a token instead. That
 * asymmetry is the whole design: a value every browser gets may be committed,
 * a value no browser may ever see never touches this file.
 */
window.LABEL_APP_CONFIG = {

  // ── Google Sheet backend ──────────────────────────────────────────────────
  // The /exec URL from Deploy > New deployment > Web app in the Sheet's Apps
  // Script editor (see apps_script/Code.gs). Empty => local-only mode: labels
  // are kept in the browser and exported with the "export" button.
  sheetUrl: '',

  // Matches SUBMIT_TOKEN in apps_script/Code.gs. NOT a secret -- it ships
  // inside this file, which the browser downloads -- but it stops anyone who
  // merely finds the /exec URL from writing rows to your sheet. Anti-spam, not
  // access control: see the header. Leave both sides empty to disable it.
  submitToken: '',

  // ── Earth Engine ──────────────────────────────────────────────────────────
  // How the page gets an Earth Engine token.
  //
  //   'auto'    (default) the deployment's own service account when the Apps
  //             Script has one, and the sign-in button when it does not
  //   'service' the service account only -- the button retries it and never
  //             offers a Google sign-in
  //   'oauth'   the old behaviour: everyone signs in with their own account
  //   'off'     no Earth Engine at all
  //
  // 'auto' and 'service' are what stop a labeller ever seeing a Google popup.
  // The key itself is NOT here and cannot be: see "Earth Engine without a
  // sign-in" in README.md, and the note at the top of the Earth Engine section
  // in label_app.html for why the SDK refuses a browser-side private key. It
  // lives in the Apps Script's Script Properties and this page is handed a
  // one-hour token.
  eeAuthMode: 'auto',

  // Where that token comes from. Empty => the Apps Script already in
  // `sheetUrl`, which is where apps_script/Code.gs serves it. Set this only if
  // you moved the broker somewhere else (a Cloud Function, say).
  eeTokenUrl: '',

  // The Cloud project registered for Earth Engine. With a service account this
  // is filled in from the key and nobody has to know it; it stays here for the
  // sign-in fallback, where it is the one field the person signing in supplies.
  eeProject: '',

  // The OAuth fallback, used when `eeAuthMode` is 'oauth' or when it is 'auto'
  // and the deployment has no service account. An OAuth *Web application*
  // client id from that project, with every origin
  // this app is served from listed under "Authorised JavaScript origins". Made
  // ONCE and then shared by everyone who opens the page; the panel walks you
  // through making it and remembers it per browser, so this field is the way to
  // set it for everybody at once. It cannot be borrowed from another app (a
  // web client validates the origin exactly), which is why there is no default.
  eeClientId: '',

  // ── the expert roster ─────────────────────────────────────────────────────
  // Every label row is keyed by (campaign, batch, point, expert_id), and
  // `expert_id` is what comes from this list. A typed name is not an identity:
  // "Ann", "ann", "Ann " and "Anne" are four experts to a groupby and the
  // failure is silent until the round report. So the header is a dropdown fed
  // from here, `?expert=e1` is a per-person bookmark, and free text is an
  // explicit fallback rather than the default.
  //
  // `id` is permanent and goes in the sheet; `name` is a display label and may
  // be changed at will. Never re-use an id for a different person.
  experts: [
    { id: 'e1', name: 'Interpreter 1' },
    { id: 'e2', name: 'Interpreter 2' }
  ],

  // ── campaign ──────────────────────────────────────────────────────────────
  // The first field of the annotation key, and it must match --campaign in
  // src/build_label_batches.py. Change it when you start a new campaign: rows
  // from two campaigns in one sheet are told apart by this and nothing else.
  campaign: 'plotcall',
  manifest: 'batches/index.json',

  // What the browser tab and the header say. The header is the only place an
  // interpreter can tell two campaigns apart at a glance, so name it.
  title: 'PlotCall · land-cover interpretation',
  heading: 'PlotCall',

  // The campaign's full class definitions, linked from the legend fold and from
  // its summary. The app falls back to its built-in cribsheet when this is
  // empty, so a dragged-in copy of app/ still has the definitions one click
  // from the call. Point it elsewhere for a campaign on a different typology --
  // and if you do, change CLASSES[].hint in the app to match, because the two
  // teaching the same interpreter two legends is the failure the calibration
  // batches exist to catch.
  cribsheetUrl: '',

  // Zoom the map jumps to for each new point. 15 shows roughly the 5 km cell;
  // the interpreter's own zoom is remembered after the first adjustment, so
  // this is a starting scale, not a cage.
  pointZoom: 15
};
