/**
 * RECOVER labelling - Google Sheet backend
 * ========================================
 * Deploy: Extensions > Apps Script in the target Sheet, paste this file, then
 * Deploy > New deployment > Web app, "Execute as: Me", "Who has access:
 * Anyone". Copy the /exec URL into app/config.js as sheetUrl.
 *
 * THIS FILE IS THE SOURCE, NOT THE DEPLOYMENT -- read this before concluding
 * anything about how the campaign is configured. The script that answers /exec
 * is a COPY of this one, pasted into the Sheet's Apps Script editor, and it
 * holds values that are deliberately absent here because this repository is
 * public:
 *
 *   - SUBMIT_TOKEN below is '' HERE and set THERE. A blank token in this file
 *     is the correct committed state and is not evidence of anything.
 *   - the Earth Engine service-account key is in that project's Project
 *     Settings > Script Properties, as EE_SERVICE_ACCOUNT_KEY. It is not in
 *     git, and it cannot be in the page: the SDK throws on a browser-side
 *     private key (see the Earth Engine section below).
 *   - the /exec URL, the token and the expert roster reach the app from
 *     app/config.js, or from the LABEL_APP_CONFIG_JS Actions secret on Pages.
 *
 * So do not diagnose the deployment by reading this file. Ask the deployment,
 * which is the only thing that knows:
 *
 *   curl '<exec-url>?action=ping'
 *     -> {"token_required":true,"ee_service_account":true,...}   both booleans
 *        are answered by the LIVE script, not by anything in this repository
 *
 *   src/check_ee_service.py --url '<exec-url>'
 *     -> the whole Earth Engine path in six steps, exit 0 only if all pass
 *
 * From inside this editor the equivalents are eeTokenSelfTest() and
 * eeMapsSelfTest() at the foot of the Earth Engine section -- run BOTH, they
 * test different IAM permissions.
 *
 * THREE THINGS THIS FILE EXISTS TO GET RIGHT
 * ------------------------------------------
 * 1. CONCURRENT WRITERS. Several interpreters label at once and each save is
 *    its own POST. Two appends racing on the same last row silently overwrite
 *    each other, so every write takes a script lock. This is the whole reason
 *    not to use a bare `appendRow`.
 * 2. UPSERT, NOT APPEND. A labeller who goes back and corrects a point must not
 *    leave two rows with the same id and different answers -- whoever reads the
 *    sheet later has no way to tell which is current. The key is
 *    (campaign, batch_id, point_id, expert_id) and a re-save replaces in place.
 *
 *    THE `expert_id` IS PART OF THE KEY AND IS NOT OPTIONAL. Two people reading
 *    the same point is the campaign's only measurement of the label noise that
 *    caps change-F1 (ACTIVE_LEARNING.md), and a key without the expert makes
 *    the second reading overwrite the first instead of sitting beside it. The
 *    failure is silent: the sheet looks complete and the agreement number is
 *    computed over nothing. `labeller` is kept as the *display name* -- people
 *    rename themselves and type their own name four ways -- and the stable
 *    identifier is `expert_id`, which comes from the roster in config.js.
 * 3. ACKNOWLEDGE BY ID. The client keeps a row "dirty" until this script names
 *    it in `accepted`. A 200 with no id list would let a partial write look
 *    complete and lose labels.
 *
 * The client posts Content-Type: text/plain on purpose -- Apps Script web apps
 * do not answer the CORS preflight that application/json triggers. The body is
 * still JSON and is parsed from e.postData.contents.
 */

var SHEET_NAME = 'labels';
var LOG_NAME = 'activity';

/**
 * Shared secret, optional. A web app deployed with "Who has access: Anyone" is
 * exactly that -- anyone with the URL can write rows to your sheet or read the
 * ones already there. Setting this to a random string and putting the same
 * string in app/config.js as `submitToken` stops accidental and drive-by
 * traffic.
 *
 * It is NOT a secret: it ships inside the page's JavaScript, so anyone you give
 * the app to can read it. It raises the bar from "anyone who finds the URL" to
 * "anyone you gave the app to", which is the actual threat here. Leave empty to
 * disable the check.
 *
 * IT GATES EVERY ACTION, NOT JUST THE SHEET -- `ee_token` included, which hands
 * back a live Earth Engine bearer token for the service account. Only `ping` is
 * open. So "the token is public" and "the service account is reachable" are the
 * same sentence; see the Earth Engine section for what bounds that.
 *
 * BLANK HERE IS DELIBERATE. The deployed copy of this file carries the real
 * string; this repository is public. Read a blank as "not committed", never as
 * "the deployment has no token" -- `?action=ping` answers that with
 * `token_required`.
 */
var SUBMIT_TOKEN = '';

// Column order in the sheet. Adding a column here is safe; reordering is not
// (existing rows are addressed by this order). New columns land on the right --
// which is why `expert_id` and `uninterpretable_reason` sit after `received_at`
// rather than next to the fields they belong with.
var COLS = ['campaign', 'batch_id', 'point_id', 'lon', 'lat',
            'class_2018', 'class_2024', 'transition', 'is_change',
            'flags', 'confidence', 'change_year', 'notes',
            'calibration', 'reference', 'channel', 'rank', 'score',
            'labeller', 'labelled_at', 'seconds_on_point',
            'imagery_a', 'imagery_b', 'app_version', 'received_at',
            'expert_id', 'uninterpretable_reason'];

//: 1-based sheet columns for the four key fields and the handful `doGet` reads.
//: Computed once rather than per row: COLS.indexOf inside a loop over 20k rows
//: is the sort of thing that turns a 300 ms GET into a 6 s one.
var COL = {};
for (var _c = 0; _c < COLS.length; _c++) COL[COLS[_c]] = _c;

//: NUL, so a campaign or batch id containing the separator cannot collide two
//: different rows onto one key. A space could, and the key is what stops one
//: expert's save landing on another's row.
var KEY_SEP = '\u0000';

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function sheet_(name, cols) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(name);
  if (!sh) {
    sh = ss.insertSheet(name);
    sh.getRange(1, 1, 1, cols.length).setValues([cols]);
    sh.setFrozenRows(1);
    return sh;
  }
  // COLS grows over time (change_year, calibration, reference, then expert_id
  // and uninterpretable_reason). Widen an existing sheet rather than writing the
  // new values into whatever columns happen to be there -- rows are addressed by
  // position, so a stale header would shift every field by one.
  var width = sh.getLastColumn();
  if (width < cols.length) {
    sh.getRange(1, 1, 1, cols.length).setValues([cols]);
  }
  return sh;
}

/** Reject a request that does not carry the shared token, when one is set. */
function tokenOk_(supplied) {
  return !SUBMIT_TOKEN || String(supplied || '') === SUBMIT_TOKEN;
}

/**
 * The upsert key. FOUR fields, and the fourth is the whole point -- see note 2
 * in the header. tests/test_label_app.py parses this function and fails if any
 * of the four is missing, because the Python mock of this sheet implements the
 * correct contract and therefore cannot catch a regression here.
 */
function keyOf_(r) {
  return [r.campaign || '', r.batch_id || '', r.point_id || '',
          r.expert_id || ''].join(KEY_SEP);
}

// ── the key -> row index ────────────────────────────────────────────────────
// Every save used to re-read the key columns of the whole sheet *inside* the
// script lock, so the cost of a write grew with the campaign and every other
// labeller waited through it. Rows are only ever appended (a correction is an
// in-place update and does not move a key), so the index can be cached and
// extended by the rows added since -- a save on a 20k-row sheet then reads the
// handful of new rows instead of 20k.
var INDEX_CACHE_KEY = 'label-index-v1';
var INDEX_TTL_S = 600;

function loadIndex_(sh) {
  var last = sh.getLastRow();
  var cache = CacheService.getScriptCache();
  var index = null, from = 2;
  try {
    var raw = cache.get(INDEX_CACHE_KEY);
    if (raw) {
      var got = JSON.parse(raw);
      // `upTo > last` means rows were deleted underneath us: throw it away and
      // rebuild rather than hand back row numbers that no longer mean anything.
      if (got && got.upTo && got.upTo <= last && got.index) {
        index = got.index;
        from = got.upTo + 1;
      }
    }
  } catch (err) { index = null; from = 2; }
  if (!index) { index = {}; from = 2; }

  if (last >= from) {
    var n = last - from + 1;
    // Two ranges rather than the whole row: the key columns are 1-3 and
    // expert_id is out on the right, and everything between them is payload.
    var head = sh.getRange(from, 1, n, 3).getValues();
    var who = sh.getRange(from, COL.expert_id + 1, n, 1).getValues();
    for (var i = 0; i < n; i++)
      index[[head[i][0], head[i][1], head[i][2], who[i][0]].join(KEY_SEP)]
        = from + i;
  }
  return index;
}

function saveIndex_(index, upTo) {
  try {
    CacheService.getScriptCache().put(
      INDEX_CACHE_KEY, JSON.stringify({ upTo: upTo, index: index }), INDEX_TTL_S);
  } catch (err) {
    // Over the 100 KB per-value cap on a large campaign. The next request just
    // rebuilds from row 2 -- slower, still correct. Never fail a write for it.
  }
}

function doPost(e) {
  var lock = LockService.getScriptLock();
  try {
    // A labeller's save must not fail because someone else was mid-write.
    // 30 s is far longer than a write takes and far shorter than a person waits.
    lock.waitLock(30000);
  } catch (err) {
    return json_({ ok: false, error: 'busy - could not acquire the write lock' });
  }
  try {
    var body = JSON.parse(e.postData.contents);
    if (!tokenOk_(body.token))
      return json_({ ok: false, error: 'bad or missing token' });
    if (body.action && body.action !== 'submit')
      return json_({ ok: false, error: 'unknown action ' + body.action });

    var rows = body.rows || [];
    if (!rows.length) return json_({ ok: true, accepted: [] });

    var sh = sheet_(SHEET_NAME, COLS);
    var index = loadIndex_(sh);
    var nextRow = sh.getLastRow() + 1;

    var now = new Date().toISOString();
    var appended = [];
    var accepted = [];
    for (var j = 0; j < rows.length; j++) {
      var r = rows[j];
      r.received_at = now;
      if (Object.prototype.toString.call(r.flags) === '[object Array]')
        r.flags = r.flags.join('|');
      var vals = COLS.map(function (c) {
        var v = r[c];
        return (v === undefined || v === null) ? '' : v;
      });
      var key = keyOf_(r);
      var at = index[key];
      if (at) {
        sh.getRange(at, 1, 1, COLS.length).setValues([vals]);
      } else {
        // Claim the row now so two rows in one POST that share a key -- a
        // re-save queued behind its own first save -- land on one row.
        index[key] = nextRow + appended.length;
        appended.push(vals);
      }
      accepted.push(r.point_id);
    }
    if (appended.length)
      sh.getRange(nextRow, 1, appended.length, COLS.length).setValues(appended);
    saveIndex_(index, nextRow + appended.length - 1);
    // The read caches answer from a snapshot of the sheet that this write just
    // invalidated. Cheaper and more honest than trying to patch them.
    dropReadCaches_();

    logActivity_(body, rows.length);
    return json_({ ok: true, accepted: accepted, stored: rows.length });
  } catch (err) {
    return json_({ ok: false, error: String(err) });
  } finally {
    lock.releaseLock();
  }
}

/** Who is working on what, so batch progress is visible without reading data. */
function logActivity_(body, n) {
  try {
    var sh = sheet_(LOG_NAME,
                    ['at', 'expert_id', 'labeller', 'campaign', 'batch_id', 'rows']);
    sh.appendRow([new Date().toISOString(), body.expert_id || '',
                  body.labeller || '', body.campaign || '',
                  body.batch_id || '', n]);
  } catch (err) { /* the log is a convenience; never fail a write for it */ }
}

// ── read caches ─────────────────────────────────────────────────────────────
// `labelled` and `status` are polled -- on every batch open, on every window
// focus, by every interpreter -- and each one used to scan the whole sheet.
// A short TTL is the right trade: the answer is "who holds this point", which
// changes on the minute scale, and a write drops these caches anyway.
var READ_TTL_S = 45;
var READ_KEYS = 'label-read-keys-v1';

function readCache_(name, make) {
  var cache = CacheService.getScriptCache();
  var key = 'label-read-v1:' + name;
  try {
    var hit = cache.get(key);
    if (hit) return hit;
  } catch (err) { /* fall through and compute */ }
  var text = make();
  try {
    cache.put(key, text, READ_TTL_S);
    // Remember what to drop on the next write: CacheService has no "list keys".
    var keys = JSON.parse(cache.get(READ_KEYS) || '[]');
    if (keys.indexOf(key) < 0) {
      keys.push(key);
      cache.put(READ_KEYS, JSON.stringify(keys), 21600);
    }
  } catch (err) { /* an uncached answer is a slow answer, not a wrong one */ }
  return text;
}

function dropReadCaches_() {
  try {
    var cache = CacheService.getScriptCache();
    var keys = JSON.parse(cache.get(READ_KEYS) || '[]');
    if (keys.length) cache.removeAll(keys);
    cache.remove(READ_KEYS);
  } catch (err) { /* stale for at most READ_TTL_S; not worth failing a write */ }
}

// ════════════════════════════════════════════════════════════════════════════
// Earth Engine on the campaign's account, so labellers never sign in
// ════════════════════════════════════════════════════════════════════════════
/**
 * WHY A TOKEN ENDPOINT AND NOT A KEY IN THE PAGE
 * ----------------------------------------------
 * The app is static -- a folder on a bucket -- and this script is the only
 * server it has. Earth Engine wants an OAuth token, and the only way to get one
 * without putting a Google sign-in in front of every labeller is a service
 * account: which means a private key, which cannot live in a file the browser
 * downloads.
 *
 * That is not a matter of taste. The Earth Engine SDK refuses it outright --
 * `ee.data.authenticateViaPrivateKey` opens with
 *
 *     if ("window" in t) throw Error("Use of private key authentication in the
 *       browser is insecure. Consider using OAuth, instead.");
 *
 * (read it in build/ee_api_js.js). So the key stays here and the page gets a
 * one-hour access token instead.
 *
 * THE KEY IS NEVER IN THIS FILE. It goes in *Project Settings > Script
 * Properties* under EE_SERVICE_ACCOUNT_KEY, because this file lives in a git
 * repository and a key pasted into it is a key committed. Nothing below reads
 * it from anywhere else.
 *
 * WHAT THIS IS WORTH TO SOMEONE WHO SHOULD NOT HAVE IT
 * ---------------------------------------------------
 * A web app deployed "Anyone", plus a token that ships inside config.js, means
 * anyone you gave the app to can mint Earth Engine tokens for your project.
 * That is a real step up from "can write rows to a sheet", and two things bound
 * it. NOT `roles/earthengine.viewer`, which is what this said and is what the
 * deployment was granted: measured 2026-08-28, viewer carries
 * `earthengine.computations.create` and NOT `earthengine.maps.create`, so the
 * account computes, self-tests clean and is refused every map tile -- every
 * auxiliary overlay in the app comes back empty. The grant is
 * `roles/earthengine.writer` AND `roles/serviceusage.serviceUsageConsumer`.
 * Note what writer widens: this token, which every browser that opens the app
 * is handed, can then create and delete assets in the project. So the campaign
 * account belongs in a project that holds nothing -- or on a custom role of
 * earthengine.computations.create + earthengine.maps.create +
 * serviceusage.services.use, which is the old bound with the overlays working.
 * And set SUBMIT_TOKEN, which this action checks like every other one.
 *
 * WHY IT IS CACHED
 * ----------------
 * Minting is an RSA signature plus a round trip to Google, ~1 s, and every
 * labeller's page asks on load and again each hour. The token is not per-user
 * -- there is one account -- so it is cached script-wide for its own lifetime
 * less a margin, and a room of interpreters costs one mint an hour between them.
 */

//: Script Property names. The key is JSON: the whole service-account file, as
//: downloaded, pasted into the value box.
var EE_KEY_PROP = 'EE_SERVICE_ACCOUNT_KEY';
//: Optional. Defaults to the key's own `project_id`, which is right whenever
//: the service account lives in the project registered for Earth Engine.
var EE_PROJECT_PROP = 'EE_PROJECT';

//: One scope, deliberately. `cloud-platform` would drag every consumer of this
//: token into capabilities the app does not use; see the note in label_app.html.
var EE_SCOPE = 'https://www.googleapis.com/auth/earthengine';
var EE_TOKEN_URI = 'https://oauth2.googleapis.com/token';

var EE_CACHE_KEY = 'ee-access-token-v1';
//: Hand the token back with this much life already deducted, so a page that
//: takes it at the last moment still has a working hour rather than a second.
var EE_SKEW_S = 300;

function eeKey_() {
  var raw = PropertiesService.getScriptProperties().getProperty(EE_KEY_PROP);
  if (!raw) return null;
  var key;
  try { key = JSON.parse(raw); }
  catch (err) { throw new Error(EE_KEY_PROP + ' is not valid JSON'); }
  if (!key.client_email || !key.private_key)
    throw new Error(EE_KEY_PROP + ' has no client_email/private_key -- paste '
                    + 'the whole downloaded JSON key file, not a fragment');
  // A key that came through a form or a shell that ate the escapes arrives with
  // a literal backslash-n instead of a newline, and the signer rejects it with
  // nothing that names the cause.
  key.private_key = String(key.private_key).replace(/\\n/g, '\n');
  return key;
}

function eeB64Url_(x) {
  var b64 = typeof x === 'string'
    ? Utilities.base64EncodeWebSafe(x, Utilities.Charset.UTF_8)
    : Utilities.base64EncodeWebSafe(x);
  return b64.replace(/=+$/, '');
}

/** Sign a JWT for the service account and swap it for an access token. */
function eeMint_(key) {
  var now = Math.floor(Date.now() / 1000);
  var uri = key.token_uri || EE_TOKEN_URI;
  var unsigned =
    eeB64Url_(JSON.stringify({ alg: 'RS256', typ: 'JWT' })) + '.' +
    eeB64Url_(JSON.stringify({ iss: key.client_email, scope: EE_SCOPE,
                               aud: uri, iat: now, exp: now + 3600 }));
  var assertion = unsigned + '.' +
    eeB64Url_(Utilities.computeRsaSha256Signature(unsigned, key.private_key));

  var res = UrlFetchApp.fetch(uri, {
    method: 'post',
    payload: { grant_type: 'urn:ietf:params:oauth:grant-type:jwt-bearer',
               assertion: assertion },
    muteHttpExceptions: true
  });
  var body;
  try { body = JSON.parse(res.getContentText()); } catch (err) { body = {}; }
  if (res.getResponseCode() !== 200 || !body.access_token)
    // Google's own wording is the actionable part -- "invalid_grant" here
    // almost always means the account's clock, key or IAM, not this code.
    throw new Error('the token endpoint refused the service account: '
      + (body.error_description || body.error || res.getContentText())
        .toString().slice(0, 300));
  return { access_token: body.access_token,
           expires_in: Number(body.expires_in || 3600) };
}

/**
 * A live access token for the service account, cached script-wide.
 * @return {?Object} null when this deployment has no service account set up.
 */
function eeToken_() {
  var key = eeKey_();
  if (!key) return null;

  var cache = CacheService.getScriptCache();
  var hit = cache.get(EE_CACHE_KEY);
  if (hit) {
    try {
      var got = JSON.parse(hit);
      var left = got.expires_at - Math.floor(Date.now() / 1000);
      if (left > EE_SKEW_S)
        return { access_token: got.access_token, expires_in: left - EE_SKEW_S,
                 project: got.project, client_email: got.client_email };
    } catch (err) { /* poisoned entry; mint a new one */ }
  }

  // Several pages loading at once would otherwise each mint. Failing to get the
  // lock is not an error -- it costs one extra mint, never a wrong answer.
  var lock = LockService.getScriptLock();
  var held = false;
  try { held = lock.tryLock(5000); } catch (err) { held = false; }
  try {
    if (held) {
      var second = cache.get(EE_CACHE_KEY);
      if (second) {
        try {
          var again = JSON.parse(second);
          var rest = again.expires_at - Math.floor(Date.now() / 1000);
          if (rest > EE_SKEW_S)
            return { access_token: again.access_token,
                     expires_in: rest - EE_SKEW_S, project: again.project,
                     client_email: again.client_email };
        } catch (err) { /* fall through and mint */ }
      }
    }
    var minted = eeMint_(key);
    var project = PropertiesService.getScriptProperties()
                    .getProperty(EE_PROJECT_PROP) || key.project_id || '';
    var expires_at = Math.floor(Date.now() / 1000) + minted.expires_in;
    var row = { access_token: minted.access_token, expires_at: expires_at,
                project: project, client_email: key.client_email };
    try {
      cache.put(EE_CACHE_KEY, JSON.stringify(row),
                Math.max(60, minted.expires_in - EE_SKEW_S));
    } catch (err) { /* cache full or unavailable; the token is still good */ }
    return { access_token: minted.access_token,
             expires_in: Math.max(60, minted.expires_in - EE_SKEW_S),
             project: project, client_email: key.client_email };
  } finally {
    if (held) lock.releaseLock();
  }
}

/**
 * GET ?action=ee_token -> a one-hour Earth Engine access token, or a plain
 * "not configured" that the app reads as "use the sign-in button instead".
 *
 * `configured:false` is a deliberate non-error: a deployment that has not set a
 * service account up is not broken, and the app must fall back silently rather
 * than put a red step in front of a labeller who cannot act on it.
 */
function eeTokenResponse_() {
  var token;
  try { token = eeToken_(); }
  catch (err) {
    return json_({ ok: false, configured: true,
                   error: String(err && err.message || err) });
  }
  if (!token)
    return json_({ ok: false, configured: false,
                   error: 'no service account configured' });
  return json_({ ok: true, configured: true,
                 access_token: token.access_token,
                 expires_in: token.expires_in,
                 project: token.project,
                 client_email: token.client_email,
                 scope: EE_SCOPE });
}

/**
 * Run this from the Apps Script editor after pasting the key. It prints what
 * the app would get -- with the token itself truncated, because the editor's
 * log is not a place to leave a live credential.
 */
function eeTokenSelfTest() {
  var key = eeKey_();
  if (!key) {
    Logger.log('No ' + EE_KEY_PROP + ' in Script Properties. Project Settings '
               + '> Script Properties > Add script property, and paste the '
               + 'whole downloaded service-account JSON as the value.');
    return;
  }
  Logger.log('service account: ' + key.client_email);
  var token = eeToken_();
  Logger.log('project: ' + token.project);
  Logger.log('expires_in: ' + token.expires_in + ' s');
  Logger.log('token: ' + token.access_token.slice(0, 12) + '...('
             + token.access_token.length + ' chars)');
  // `value:compute`, not a listing. The app never reads this project's assets;
  // reduceRegion and getThumbId are COMPUTATIONS, so the probe has to be one
  // too or it passes on an account that cannot do the job it has. It is also
  // the cheapest possible one: the constant 1, evaluated.
  //
  // THIS PROBE IS NOT SUFFICIENT ON ITS OWN, and the comment that used to be
  // here said it was -- "every single thing it does, getMapId included, is a
  // COMPUTATION". That is false in the only place it matters:
  // `earthengine.maps.create` is a SEPARATE IAM permission from
  // `earthengine.computations.create`. A service account can pass this probe
  // and be refused every map tile, which is exactly what happened -- the
  // deployment self-tested clean and every overlay in the app came back empty.
  // The maps probe below is the other half.
  var probe = UrlFetchApp.fetch(
    'https://earthengine.googleapis.com/v1/projects/' + token.project
      + '/value:compute',
    { method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify(
        { expression: { values: { '0': { constantValue: 1 } }, result: '0' } }),
      headers: { Authorization: 'Bearer ' + token.access_token,
                 // THE HEADER THAT MAKES THIS TEST MEAN ANYTHING. The browser
                 // SDK sets X-Goog-User-Project from `ee.initialize`'s project
                 // argument, and it is what triggers the *serviceusage* check
                 // -- a service account with the Earth Engine role but not
                 // Service Usage Consumer answers 200 to a plain REST call and
                 // 403 to every call the app makes. Without this line the
                 // self-test passes on exactly the deployment that is broken.
                 'X-Goog-User-Project': token.project },
      muteHttpExceptions: true });
  var code = probe.getResponseCode();
  if (code === 200) {
    Logger.log('Earth Engine computed 1 -- ready. Labellers will not be asked '
               + 'to sign in.');
    return;
  }
  Logger.log('Earth Engine answered HTTP ' + code + ': '
             + probe.getContentText().slice(0, 400));
  if (code === 401)
    Logger.log('401 is the token itself. Check the key in ' + EE_KEY_PROP + '.');
  if (code === 403)
    Logger.log('403 is IAM, not the key. Read the message above: '
               + '"required permission to use project" is the SERVICE USAGE '
               + 'role, not the Earth Engine one. Grant ' + key.client_email
               + ' both roles/serviceusage.serviceUsageConsumer and '
               + 'roles/earthengine.writer on project ' + token.project
               + ' -- WRITER, not viewer: viewer was measured to grant '
               + 'earthengine.computations.create and NOT '
               + 'earthengine.maps.create, so a viewer account passes this '
               + 'probe and draws no overlays'
               + ' at https://console.cloud.google.com/iam-admin/iam?project='
               + token.project + ' -- a new grant takes a minute or two to '
               + 'propagate.');
}

/**
 * The other half of the self-test: can this account create MAP TILES?
 *
 * `earthengine.maps.create` is a separate IAM permission from
 * `earthengine.computations.create`, so an account can pass
 * `eeTokenSelfTest()` and still have every auxiliary overlay in the app come
 * back empty -- which is how this was found, from a labeller rather than from
 * a test. Run both.
 *
 * The expression is `ee.Image(1)`, serialised exactly as the client library
 * serialises it, which is the cheapest thing that is an IMAGE rather than a
 * number: `maps.create` will not take a constant.
 */
function eeMapsSelfTest() {
  var key = eeKey_();
  if (!key) { Logger.log('No ' + EE_KEY_PROP + ' in Script Properties.'); return; }
  var token = eeToken_();
  var probe = UrlFetchApp.fetch(
    'https://earthengine.googleapis.com/v1/projects/' + token.project + '/maps',
    { method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify({
        expression: { result: '0', values: { '0': { functionInvocationValue: {
          functionName: 'Image.constant',
          arguments: { value: { constantValue: 1 } } } } } },
        // REQUIRED, and leaving it out fails in the shape of the thing this
        // test looks for. `maps.create` answers HTTP 400 "Missing or
        // unrecognized image file format: IMAGE_FILE_FORMAT_UNSPECIFIED"
        // however complete the account's IAM is -- so without this line the
        // probe can never go green, and a deployment that has just been fixed
        // still reports that it cannot draw. The client sends it too:
        // `convert_to_image_file_format(None)` is exactly this value.
        fileFormat: 'AUTO_JPEG_PNG' }),
      // Same header, same reason as above: it is what makes the serviceusage
      // check fire, and without it this passes on the broken deployment too.
      headers: { Authorization: 'Bearer ' + token.access_token,
                 'X-Goog-User-Project': token.project },
      muteHttpExceptions: true });
  var code = probe.getResponseCode();
  if (code === 200) {
    Logger.log('Earth Engine minted a map -- the auxiliary overlays will draw.');
    return;
  }
  Logger.log('Earth Engine answered HTTP ' + code + ' to maps.create: '
             + probe.getContentText().slice(0, 400));
  if (code === 400) {
    Logger.log('A 400 is this REQUEST, not the account -- it says nothing '
               + 'about what the service account may do. Do not grant roles on '
               + 'the strength of it.');
    return;
  }
  Logger.log('If that says "Permission \'earthengine.maps.create\' denied", the '
             + 'account can compute and cannot draw. Grant ' + key.client_email
             + ' an Earth Engine role on project ' + token.project
             + ' at https://console.cloud.google.com/iam-admin/iam?project='
             + token.project + '. A brand-new service account has NO roles at '
             + 'all, so that is the usual state and not a corruption -- but if '
             + 'Earth Engine Resource VIEWER is already granted and this still '
             + 'fails, grant WRITER (roles/earthengine.writer): maps.create is '
             + 'a separate permission from computations.create and viewer does '
             + 'not necessarily carry it. Check what the account actually holds '
             + 'ON THIS PROJECT with:  gcloud projects get-iam-policy '
             + token.project + ' --flatten="bindings[].members" --filter='
             + '"bindings.members:' + key.client_email + '" '
             + '--format="table(bindings.role)"');
}

/**
 * GET ?action=ping                       -> health check, and what this build is
 * GET ?action=status                     -> per-batch counts, for a dashboard
 * GET ?action=labelled&batch=b1          -> WHO has each point, not WHAT they said
 * GET ?action=mine&batch=b1&expert=e1    -> e1's own rows, in full
 * GET ?action=export                     -> every row as CSV, for label_rounds.py
 * GET ?action=ee_token                   -> an Earth Engine token for the app
 *
 * `export` is what closes the campaign loop: the next round's candidates are cut
 * with the returned ids excluded, and the yield per channel is read off these
 * rows. Without it a round is write-only.
 *
 * WHY `labelled` AND `mine` ARE TWO DIFFERENT ENDPOINTS
 * ----------------------------------------------------
 * The app needs two unrelated things from the sheet, and giving it one endpoint
 * that answers both would break the campaign's only measurement of label noise.
 *
 *   `mine`     restores an expert's own work when they open the batch on
 *              another machine. Their own calls cannot anchor them.
 *   `labelled` says only *that* someone else holds a point, and which expert. It
 *              must NOT return their call: when two people deliberately read the
 *              same point, showing the first reading to the second destroys the
 *              independence that makes the agreement number mean anything.
 *
 * So `labelled` returns {point_id, expert_id} and nothing else -- not even the
 * display name, which the app resolves from the roster in config.js. Do not
 * "helpfully" add the transition to it; tests/test_label_app.py reads this
 * function's source and fails if any answer column appears inside it.
 */
function doGet(e) {
  var action = (e && e.parameter && e.parameter.action) || 'ping';
  // The health check stays open so `curl <exec-url>` still confirms a
  // deployment; everything that returns data does not. The app calls this on
  // boot before it claims to be connected to anything.
  if (action === 'ping')
    return json_({ ok: true, service: 'recover-labelling',
                   key: ['campaign', 'batch_id', 'point_id', 'expert_id'],
                   token_required: !!SUBMIT_TOKEN,
                   ee_service_account: !!PropertiesService.getScriptProperties()
                     .getProperty(EE_KEY_PROP) });
  if (!tokenOk_(e.parameter.token))
    return json_({ ok: false, error: 'bad or missing token' });

  // Before the sheet is opened: this action never touches it, and the empty
  // -sheet shortcut below would otherwise answer it with a row listing.
  if (action === 'ee_token') return eeTokenResponse_();

  var sh = sheet_(SHEET_NAME, COLS);
  var last = sh.getLastRow();
  if (last < 2) return json_({ ok: true, batches: {}, labelled: [], rows: [] });
  var n = last - 1;

  if (action === 'labelled') {
    var want = e.parameter.batch || '';
    // Three columns, not twenty-five: this is the most frequently polled action
    // in the app and it needs the batch, the point and who holds it.
    var text = readCache_('labelled:' + last + ':' + want, function () {
      var ids = sh.getRange(2, COL.batch_id + 1, n, 2).getValues();   // batch, point
      var who = sh.getRange(2, COL.expert_id + 1, n, 1).getValues();
      var held = [];
      for (var i = 0; i < n; i++)
        if (!want || ids[i][0] === want)
          held.push({ point_id: ids[i][1], expert_id: who[i][0] });
      return JSON.stringify({ ok: true, labelled: held });
    });
    return ContentService.createTextOutput(text)
      .setMimeType(ContentService.MimeType.JSON);
  }

  var data = sh.getRange(2, 1, n, COLS.length).getValues();

  if (action === 'mine') {
    // On expert_id, never on the display name: "Ann", "ann" and "Ann " are one
    // person and three strings, and a name filter silently returns 0 / 100.
    var who = String(e.parameter.expert || '');
    var batch = e.parameter.batch;
    var mine = [];
    for (var q = 0; q < n; q++) {
      if (batch && data[q][COL.batch_id] !== batch) continue;
      if (String(data[q][COL.expert_id]) !== who) continue;
      var rec = {};
      for (var c = 0; c < COLS.length; c++) rec[COLS[c]] = data[q][c];
      mine.push(rec);
    }
    return json_({ ok: true, rows: mine });
  }

  if (action === 'export') {
    var csv = [COLS.join(',')];
    for (var m = 0; m < n; m++) {
      var cells = [];
      for (var k2 = 0; k2 < COLS.length; k2++) {
        var v = data[m][k2];
        v = (v === null || v === undefined) ? '' : String(v);
        cells.push(/[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v);
      }
      csv.push(cells.join(','));
    }
    return ContentService.createTextOutput(csv.join('\n'))
      .setMimeType(ContentService.MimeType.CSV);
  }

  if (action === 'status') {
    var statusText = readCache_('status:' + last, function () {
      var out = {};
      for (var k = 0; k < n; k++) {
        var b = data[k][COL.batch_id] || '(none)';
        if (!out[b]) out[b] = { n: 0, change: 0, experts: {}, transitions: {} };
        out[b].n++;
        if (data[k][COL.is_change] === 1 || data[k][COL.is_change] === '1')
          out[b].change++;
        var who2 = data[k][COL.expert_id] || data[k][COL.labeller];
        out[b].experts[who2] = (out[b].experts[who2] || 0) + 1;
        var t = data[k][COL.transition] || '(not interpretable)';
        out[b].transitions[t] = (out[b].transitions[t] || 0) + 1;
      }
      return JSON.stringify({ ok: true, batches: out });
    });
    return ContentService.createTextOutput(statusText)
      .setMimeType(ContentService.MimeType.JSON);
  }
  return json_({ ok: false, error: 'unknown action ' + action });
}
