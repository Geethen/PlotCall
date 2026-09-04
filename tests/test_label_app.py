"""Browser regression tests for the ways the labelling app lost or blocked work.

All four were live bugs, all four were silent, and none is visible from reading
a diff -- which is why they are pinned here rather than described in a comment:

1. **Unsent rows were dropped when the labeller switched batches.** The dirty
   set was rebuilt per batch, so rows from a half-finished batch became
   unreachable until somebody happened to reopen exactly that batch. A labeller
   who did half of A, moved to B and closed the tab had silently not delivered.
2. **CSV ids were coerced to numbers**, so a `point_id` of `0012` arrived as
   `12`. The join back to the candidate table breaks and nothing complains until
   the *next* round's `--exclude-labelled` excludes nothing.
3. **The app never asked the sheet what it already held.** Two people on one
   batch each did all of it, and a labeller on a second machine saw 0 / 100.
4. **The boot backstop covered a working app.** It tested the loading overlay's
   message text, which `hideLoading()` leaves in place, so any batch without an
   `instructions` field got an overlay dropped back over it fifteen seconds in.
5. **The annotation key did not carry the expert.** `Code.gs` upserted on
   `(campaign, batch_id, point_id)`, so the second reader's row REPLACED the
   first -- deleting the inter-rater agreement measurement, which is the
   campaign's only handle on the label noise that caps change-F1. The failure was
   invisible from Python, because the mock below implements the key the docs
   describe rather than the one `Code.gs` implemented. That is why the tests for
   it read the JavaScript source directly.

The fix for (3) carries its own hazard, pinned here too: a second reader may be
told *that* someone holds a point, and never *what they said*. Showing the first
reading turns the agreement measurement -- the campaign's only handle on the
label noise that caps change-F1 -- into a confirmation measurement.

These need a browser. They skip cleanly where there is not one, so `pytest -q`
stays green on a bare checkout.
"""
from __future__ import annotations

import http.server
import json
import re
import socketserver
import sys
import threading
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api",
                              reason="playwright not installed")

APP_DIR = Path(__file__).parents[1] / "app"
APP_HTML = APP_DIR / "label_app.html"
CODE_GS = APP_DIR / "apps_script" / "Code.gs"

COLS = ["campaign", "batch_id", "point_id", "lon", "lat", "class_2018",
        "class_2024", "transition", "is_change", "flags", "confidence", "notes",
        "channel", "rank", "score", "labeller", "labelled_at",
        "seconds_on_point", "imagery_a", "imagery_b", "app_version",
        "received_at", "expert_id", "uninterpretable_reason"]

#: The answer columns. None of these may appear in what `action=labelled` hands
#: a second reader -- see the module docstring.
ANSWER_COLS = ["transition", "class_2018", "class_2024", "notes", "confidence"]

#: A batch served by the mock rather than read from `app/batches`, so these tests
#: do not break when someone regenerates the demo batch.
BATCH = {
    "campaign": "test-campaign", "batch_id": "t001", "channel": "coverage",
    "instructions": None,
    "points": [{"id": f"t{i:03d}", "lon": 10.0 + i * 0.01, "lat": 60.0,
                "channel": "coverage", "rank": i + 1, "cell_km": 5, "meta": {}}
               for i in range(10)],
}


def _b001_is_baked() -> bool:
    """Whether `app/batches/b001.json` carries the evidence some tests read.

    Two tests below are about the chart and the spectral profile, which render
    from a BAKED batch and cannot be faked by the mock. A fresh clone has only
    the `--placeholder` demo, which has no bake, so they skip rather than fail:
    the thing they guard is not broken, it is absent.
    """
    path = APP_DIR / "batches" / "b001.json"
    if not path.exists():
        return False
    batch = json.loads(path.read_text())
    return bool(batch.get("dense")) and any(
        p.get("evidence", {}).get("t") for p in batch.get("points", []))


needs_baked_b001 = pytest.mark.skipif(
    not _b001_is_baked(),
    reason="app/batches/b001.json carries no baked evidence "
           "(see src/build_batch_evidence.py and src/build_batch_dense.py)")


class Sheet:
    """The Code.gs contract, in memory. Upsert keyed the same way."""

    def __init__(self):
        self.rows: dict[tuple, dict] = {}
        self.fail = False
        #: What `?action=ee_token` answers. None is the honest default: most
        #: deployments have no service account, and that case must leave the
        #: app exactly where it was rather than showing a fault.
        self.ee: dict | None = None

    def put(self, row: dict) -> None:
        row = dict(row)
        if isinstance(row.get("flags"), list):
            row["flags"] = "|".join(row["flags"])
        for column in COLS:
            row.setdefault(column, "")
        # Four fields, and the fourth is `expert_id` -- the stable identifier,
        # never the display name. See test_the_annotation_key_carries_the_expert
        # for why this mock alone cannot protect the contract.
        self.rows[(row["campaign"], row["batch_id"], row["point_id"],
                   row["expert_id"])] = row

    def seed(self, **kwargs) -> None:
        row = {c: "" for c in COLS}
        row.update(kwargs)
        self.put(row)

    def counts_by_batch(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for _, batch, _, _ in self.rows:
            out[batch] = out.get(batch, 0) + 1
        return out

    def rows_for(self, point_id: str) -> list[dict]:
        return [r for r in self.rows.values() if r["point_id"] == point_id]


@pytest.fixture(scope="module")
def server():
    sheet = Sheet()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(APP_DIR), **k)

        def log_message(self, *a):
            pass

        def _png(self, payload):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, obj):
            payload = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            import urllib.parse
            parts = urllib.parse.urlparse(self.path)
            if parts.path == "/batch.json":
                return self._json(BATCH)
            if parts.path == "/cal.json":
                return self._json(CAL_BATCH)
            if parts.path == "/assigned.json":
                return self._json(ASSIGNED_BATCH)
            if parts.path == "/evidence.json":
                return self._json(EVIDENCE_BATCH)
            if parts.path == "/baked.json":
                return self._json(baked_batch(chips=BAKED_CHIPS))
            if parts.path == "/baked-missing.json":
                # the metadata is there and the sprite is not: a bake that was
                # not deployed alongside its batch, which must degrade to live
                # Earth Engine rather than to an empty strip
                return self._json(baked_batch(
                    chips=dict(BAKED_CHIPS, dir="nowhere_chips")))
            if parts.path.startswith("/ev001_chips/"):
                return self._png(SPRITE_PNG)
            if parts.path.startswith("/faketile/"):
                # Earth Engine's tile endpoint failing the way it actually
                # fails: the mapid was minted, and the expression only breaks
                # when a tile is rendered.
                body = json.dumps({"error": {"code": 400, "message":
                    "Image.select: Pattern 'building_presence' did not match "
                    "any bands."}}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            if parts.path.startswith("/solo-"):
                return self._json(solo_batch(parts.path[6:-5]))
            if not parts.path.startswith("/mock"):
                return super().do_GET()
            query = dict(urllib.parse.parse_qsl(parts.query))
            action = query.get("action", "ping")
            if action == "labelled":
                # WHO, never WHAT. The app resolves the display name from the
                # roster in config.js, so not even the name goes over the wire.
                return self._json({"ok": True, "labelled": [
                    {"point_id": r["point_id"], "expert_id": r["expert_id"]}
                    for r in sheet.rows.values()
                    if r["batch_id"] == query.get("batch")]})
            if action == "mine":
                return self._json({"ok": True, "rows": [
                    r for r in sheet.rows.values()
                    if r["batch_id"] == query.get("batch")
                    and r["expert_id"] == query.get("expert")]})
            if action == "ee_token":
                if sheet.ee is None:
                    return self._json({"ok": False, "configured": False,
                                       "error": "no service account configured"})
                return self._json(dict({"ok": True, "configured": True},
                                       **sheet.ee))
            return self._json({"ok": True, "service": "mock",
                               "key": ["campaign", "batch_id", "point_id",
                                       "expert_id"]})

        def do_POST(self):
            if not self.path.startswith("/mock"):
                return self.send_error(404)
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if sheet.fail:
                self.send_response(500)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
                return
            for row in body["rows"]:
                sheet.put(row)
            self._json({"ok": True,
                        "accepted": [r["point_id"] for r in body["rows"]]})

    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}", sheet
    httpd.shutdown()


@pytest.fixture(autouse=True)
def clean_sheet(server):
    """The HTTP server is module-scoped; the SHEET behind it must not be.

    Starting a server per test is slow, so `server` is shared -- and its
    in-memory Sheet was shared with it, accumulating every row every test
    wrote. `pullSheetState` then answers a later test with an earlier test's
    rows, which changes what the app does on open: points get marked as held,
    `advanceIfSettled()` moves off the point under the cursor, and a test that
    presses a class key finds itself somewhere else.

    It surfaced as `test_saving_without_an_identity_is_refused` failing in the
    full run and passing on its own, after four different predecessors and not
    after a fifth -- which is the shape of a leak rather than of a bug in the
    test. Reset the state, keep the server.
    """
    _, sheet = server
    sheet.rows.clear()
    sheet.fail = False
    sheet.ee = None
    yield


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch(args=["--no-sandbox"])
        except Exception as err:                       # no browser installed
            pytest.skip(f"chromium unavailable: {err}")
        yield b
        b.close()


CAL_BATCH = {
    "campaign": "test-campaign", "batch_id": "cal001", "channel": None,
    "calibration": True, "feedback": "immediate", "instructions": None,
    "points": [{"id": f"k{i:03d}", "lon": 10.0 + i * 0.01, "lat": 60.0,
                "cell_km": 5, "meta": {"biome": "boreal"},
                "reference": "Nature -> Cropland" if i < 2 else "Nature -> Nature"}
               for i in range(4)],
}


EMPTY_CONFIG = "window.LABEL_APP_CONFIG = {};"

#: What the suite assumes a deployment looks like, stated here rather than read
#: off whatever `app/config.js` happens to hold.
#:
#: `config.js` is a DEPLOYMENT artefact: it is blank in the repository and the
#: real values are injected from the `LABEL_APP_CONFIG_JS` Actions secret at
#: Pages build time. So "the config the tests run against" is not a thing that
#: exists in the checkout, and any test that inherited it was asserting on an
#: accident. It broke exactly that way: with `experts: []` the identity gate
#: falls back to a free-text box instead of a roster dropdown, so the `a`/`z`
#: keystrokes that `test_saving_without_an_identity_is_refused` presses to prove
#: labelling is REFUSED were typed into that box, and Enter created an expert
#: called "az".
#:
#: Only `experts` matters here -- `sheetUrl`, `campaign` and `manifest` all come
#: from the query string in `open_app`.
FIXTURE_CONFIG = """window.LABEL_APP_CONFIG = {
  campaign: 'recover-habloss',
  manifest: 'batches/index.json',
  experts: [
    { id: 'e1', name: 'expert one' },
    { id: 'e2', name: 'expert two' }
  ],
  pointZoom: 15
};"""


def stub_config(ctx, js=EMPTY_CONFIG):
    """Pin config.js for a test that asserts on a particular configuration.

    The fixture serves `app/` as it stands, so a test that says "unconfigured"
    was really saying "whatever the deployment happens to hold" -- and the day
    a real Earth Engine client id landed in config.js, three of these started
    exercising the configured path instead. The config is part of the fixture,
    so it is stated here rather than inherited. Routed on the CONTEXT so it
    survives a reload.
    """
    # `**/config.js*`, with the star: the app loads it with a `?v=` cache
    # buster, and a glob without one silently stops matching -- which hands
    # every test below the real deployment's config instead of this one.
    ctx.route("**/config.js*", lambda route: route.fulfill(
        status=200, content_type="application/javascript", body=js))


def open_app(browser, base, who=None, fresh=True, batch="/batch.json",
             skip_intro=True, config_js=FIXTURE_CONFIG, extra="",
             hide_loading=True):
    """A page with the mock batch loaded. Does not wait on the map.

    The map needs WebGL and a CDN; the batch, the outbox and the sheet pull do
    not, and boot() is deliberately independent of `map.on('load')` so this works
    on a machine with neither.

    `who` is an EXPERT ID and arrives as `?expert=`, which is both how a real
    per-person bookmark works and the only way to set an identity that the app
    will accept -- the header is a roster dropdown, not a text box, precisely so
    that "Ann", "ann" and "Ann " cannot be three experts.
    """
    ctx = browser.new_context() if fresh else browser.contexts[0]
    if config_js is not None:
        stub_config(ctx, config_js)
    if skip_intro:
        # The first-run overlay is tested on its own; everywhere else it is in
        # the way, exactly as it would be for a returning labeller.
        ctx.add_init_script(
            "try { localStorage.setItem('recover-labels:seen-intro','1'); } catch (e) {}")
    page = ctx.new_page()
    who_qs = f"&expert={who}" if who is not None else ""
    page.goto(f"{base}/label_app.html?sheetUrl=/mock&batch={batch}"
              f"&campaign=test-campaign&manifest=/absent.json{who_qs}{extra}",
              wait_until="load")
    page.wait_for_function("typeof S !== 'undefined' && S.points.length > 0",
                           timeout=30000)
    if skip_intro and hide_loading:
        # NB: this also dismisses the identity gate, which is a real overlay and
        # not part of the first-run brief -- pass hide_loading=False to see it.
        page.evaluate("hideLoading()")
    if who is not None:
        page.wait_for_function(f"Expert.id() === {who!r}", timeout=10000)
        page.wait_for_timeout(600)
    return page, ctx


def label_here(page, first="a", second="z", conf="2"):
    """Make a complete call and save it.

    `conf` is not optional decoration: confidence became REQUIRED to save on
    2026-08-31 (see `saveable()`), so a helper that pressed only the two class
    keys would leave Save disabled and every test built on it would assert
    against a point that was never written. Pass `conf=None` to make a
    deliberately incomplete call.
    """
    page.keyboard.press(first)
    page.keyboard.press(second)
    if conf:
        page.keyboard.press(conf)
    page.keyboard.press("Enter")
    page.wait_for_timeout(200)


# ---------------------------------------------------------------------------
# 1. the outbox outlives the batch
# ---------------------------------------------------------------------------
def test_unsent_rows_survive_a_batch_switch(browser, server):
    base, sheet = server
    page, ctx = open_app(browser, base, who="ann")
    try:
        sheet.fail = True
        label_here(page)
        label_here(page)
        page.wait_for_timeout(2200)
        assert page.evaluate("Outbox.size()") == 2

        # move to a different batch while those two are still unsent
        page.evaluate("""adoptBatch({batch_id:'other', campaign:'test-campaign',
            channel:null, instructions:null,
            points:[{id:'o1',lon:10.7,lat:59.9,meta:{},cell_km:5},
                    {id:'o2',lon:10.8,lat:59.8,meta:{},cell_km:5}]})""")
        page.wait_for_timeout(400)
        label_here(page, "s", "x")
        page.wait_for_timeout(2200)

        held = page.evaluate("Outbox.groups().map(g => g.batch_id + ':' + g.rows.length)")
        assert sorted(held) == ["other:1", "t001:2"], held

        sheet.fail = False
        page.evaluate("hideLoading()")   # the batch-complete modal, if shown
        page.click("#btn-sync")
        page.wait_for_function("Outbox.size() === 0", timeout=20000)
        assert sheet.counts_by_batch() == {"t001": 2, "other": 1}
    finally:
        ctx.close()


def test_the_boot_backstop_does_not_cover_a_working_app(browser, server):
    """The 15 s "still starting" overlay must not fire once a batch has loaded.

    It used to test the overlay's message text, which `hideLoading()` leaves in
    place -- so a batch that loaded normally and carried no `instructions` to
    overwrite that text got an overlay slapped back over a working app fifteen
    seconds in, with the interpreter mid-point.
    """
    base, _ = server
    page, ctx = open_app(browser, base, who="gil")
    try:
        assert page.evaluate("!!S.batch")
        # past the 15 s backstop, with the app deliberately left idle
        page.wait_for_timeout(16000)
        assert page.evaluate(
            "getComputedStyle(document.getElementById('loading')).display"
        ) == "none"
    finally:
        ctx.close()


def test_closing_with_unsent_rows_is_guarded(browser, server):
    """The beforeunload guard reads the outbox, not the current batch."""
    base, sheet = server
    page, ctx = open_app(browser, base, who="bo")
    try:
        sheet.fail = True
        label_here(page)
        page.wait_for_timeout(2200)
        assert page.evaluate("Outbox.size()") >= 1
        assert page.evaluate("!!CFG.sheetUrl")
    finally:
        sheet.fail = False
        ctx.close()


# ---------------------------------------------------------------------------
# 2. ids are strings
# ---------------------------------------------------------------------------
def test_csv_ids_are_not_coerced_to_numbers(browser, server):
    base, _ = server
    page, ctx = open_app(browser, base)
    try:
        points = page.evaluate("""() => {
          const csv = 'point_id,lon,lat,score,rank,stratum\\n'
                    + '0012,10.5,60.1,0.5,1,bare\\n'
                    + '0013,10.6,60.2,,2,steep';
          return parseBatch(csv, 'x.csv').points;
        }""")
        assert [p["id"] for p in points] == ["0012", "0013"]
        # the fields that must stay numeric still are
        assert points[0]["score"] == 0.5
        assert points[0]["rank"] == 1 and points[1]["rank"] == 2
        assert points[0]["lon"] == 10.5
        # a blank score is absent, not a real zero
        assert points[1].get("score") is None
        # unknown columns reach the interpreter as context
        assert points[0]["meta"]["stratum"] == "bare"
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# 3. the app asks the sheet what it already holds
# ---------------------------------------------------------------------------
def test_my_own_rows_come_back_on_a_second_machine(browser, server):
    base, sheet = server
    for i in range(3):
        sheet.seed(campaign="test-campaign", batch_id="t001",
                   point_id=f"t{i:03d}", class_2018="Nature",
                   class_2024="Cropland", transition="Nature -> Cropland",
                   is_change=1, flags="unsure", confidence=2, labeller="cara",
                   expert_id="cara",
                   labelled_at="2026-08-26T09:00:00Z")
    page, ctx = open_app(browser, base, who="cara")   # fresh profile, no storage
    try:
        page.wait_for_function("Object.keys(S.labels).length >= 3", timeout=15000)
        assert page.text_content("#pill-progress").strip() == "3 / 10"
        # resumed past the restored work rather than sitting on top of it
        assert page.text_content("#pt-id") == "t003"
        # and a restored row round-trips into the form
        page.evaluate("goTo(0)")
        page.wait_for_timeout(300)
        state = page.evaluate("({a: cur.c2018, b: cur.c2024, conf: cur.conf,"
                              " flags: [...cur.flags]})")
        assert state == {"a": "Nature", "b": "Cropland", "conf": 2,
                         "flags": ["unsure"]}
        # restored rows are already in the sheet and must not be re-queued
        assert page.evaluate("Outbox.size()") == 0
    finally:
        ctx.close()


def test_a_second_reader_is_told_who_not_what(browser, server):
    """The one thing that makes the agreement number mean anything."""
    base, sheet = server
    sheet.seed(campaign="test-campaign", batch_id="t001", point_id="t005",
               class_2018="Artificial", class_2024="Artificial",
               transition="Artificial -> Artificial", is_change=0,
               labeller="dane", expert_id="dane",
               labelled_at="2026-08-26T09:00:00Z")
    page, ctx = open_app(browser, base, who="eve")
    try:
        page.wait_for_function("S.others['t005'] !== undefined", timeout=15000)
        page.evaluate("goTo(5)")
        page.wait_for_timeout(300)
        notice = page.text_content("#pt-others")
        assert "dane" in notice
        assert "Artificial" not in notice, "the first reader's call leaked"
        # nothing anywhere in the page carries their transition either
        assert page.evaluate(
            "!JSON.stringify(S.others).includes('Artificial')")
        # and what came over the wire held only the id
        held = page.evaluate("S.others['t005']")
        assert held == ["dane"], held
    finally:
        ctx.close()


def test_others_points_are_skipped_unless_overlap_is_wanted(browser, server):
    base, sheet = server
    for i in (5, 6, 7):
        sheet.seed(campaign="test-campaign", batch_id="t001",
                   point_id=f"t{i:03d}", transition="Nature -> Nature",
                   is_change=0, labeller="dane", expert_id="dane",
                   labelled_at="2026-08-26T09:00:00Z")
    page, ctx = open_app(browser, base, who="finn")
    try:
        page.wait_for_function("Object.keys(S.others).length >= 3", timeout=15000)
        page.evaluate("goTo(4)")
        page.wait_for_timeout(200)
        page.evaluate("next()")
        page.wait_for_timeout(200)
        assert page.text_content("#pt-id") == "t008"     # 5, 6, 7 skipped

        # The checkbox is a debug override behind the setup disclosure now --
        # overlap is a property of the batch file (T1.1). This batch carries no
        # assignments, so the override is still what governs.
        page.evaluate("document.getElementById('skip-others').checked = false")
        page.dispatch_event("#skip-others", "change")
        page.evaluate("goTo(4)")
        page.wait_for_timeout(200)
        page.evaluate("next()")
        page.wait_for_timeout(200)
        assert page.text_content("#pt-id") == "t005"     # reachable for overlap
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# a name is not optional
# ---------------------------------------------------------------------------
def press_until(page, key, marker, tries=20):
    """Press `key` until `marker` appears, or fail saying it never did.

    A key pressed in the instant focus is being handed back from the identity
    gate is dropped -- the keydown goes to whatever holds focus at dispatch
    time, and `inField()` correctly refuses to read a keystroke aimed at a text
    box. A person presses the key again and thinks nothing of it; a test that
    presses once and sleeps records it as the app being dead, about five runs
    in six. So this presses the way a person does.

    It is a WAIT, not a workaround: the assertion is still that the key
    registers. That the app hands the keyboard back at all is asserted
    separately, and without any retry, by
    `test_the_gate_lets_go_of_the_keyboard_when_it_closes`.
    """
    for _ in range(tries):
        page.keyboard.press(key)
        try:
            page.wait_for_function(
                f"document.querySelector({marker!r}) !== null", timeout=250)
            return
        except sync_api.Error:
            continue
    raise AssertionError(f"{key!r} never registered: {marker} did not appear")


def test_the_gate_lets_go_of_the_keyboard_when_it_closes(browser, server):
    """Pick your name, press `a`, and the app must hear it.

    The gate focused its own control on a **timer** -- `setTimeout(... .focus(),
    50)` -- and the timer outlived the gate. A pick made inside that window left
    it pending, so it fired after the gate had closed and put the cursor back
    into a now-hidden text box, where `inField()` correctly swallows every
    keystroke. The app is keyboard-first and the very next thing anyone does is
    press a class key, so this reads as the app being dead, and whether it
    happens depends on how fast the pick was.

    Pinned separately from the gate's own test because that one failed only
    sometimes, which is the worst way for this to be reported.
    """
    base, _ = server
    page, ctx = open_app(browser, base, batch="/solo-gate.json",
                         hide_loading=False)
    try:
        page.select_option("#intro-who", "__other")
        page.fill("#intro-other", "Hana Ø.")
        page.click("#intro-go")
        # Longer than both pending focus timers (30 ms and 50 ms): the point is
        # that they do not fire into the closed gate, not that we outran them.
        page.wait_for_timeout(300)
        assert page.evaluate(
            "['INPUT','TEXTAREA','SELECT'].indexOf("
            "document.activeElement && document.activeElement.tagName) < 0"), (
            page.evaluate("document.activeElement.id"))
        page.keyboard.press("a")
        page.wait_for_timeout(100)
        assert page.evaluate("S.pick && S.pick['2018']") == "Nature" or \
            page.evaluate(
                "document.querySelector('#ch-2018 .on') !== null"), (
            "a class key pressed right after the gate closed did nothing")
    finally:
        ctx.close()


def test_saving_without_an_identity_is_refused(browser, server):
    """Rows are keyed by expert; "anonymous" collapses everyone into one key.

    With every reader writing as the same person, a second reading overwrites the
    first instead of sitting beside it, and the inter-rater agreement measurement
    silently reports nothing.
    """
    base, _ = server
    # `skip_intro` only suppresses the first-run brief; the identity gate is a
    # separate thing and is not suppressible, which is the point of it.
    page, ctx = open_app(browser, base, batch="/solo-gate.json",
                         hide_loading=False)
    try:
        assert page.evaluate("Expert.cur === null")
        # It is a gate, not an after-the-fact nag. The old app asked on the first
        # SAVE -- so a person could work a point, press Enter, and be asked a
        # question they had no reason to expect, with every keystroke until then
        # having gone into an app that did not know whose workspace it was.
        assert page.is_visible("#intro-go")
        page.keyboard.press("a")
        page.keyboard.press("z")
        page.keyboard.press("2")   # confidence: required to save
        page.keyboard.press("Enter")
        page.wait_for_timeout(400)
        assert page.evaluate("Object.keys(S.labels).length") == 0
        assert page.evaluate("Outbox.size()") == 0

        # saveCurrent refuses on its own too, gate or no gate
        assert page.evaluate("requireIdentity()") is None

        # pick somebody, and labelling proceeds
        page.select_option("#intro-who", "__other")
        page.fill("#intro-other", "  Hana Ø.  ")
        page.click("#intro-go")
        # WAIT FOR THE PRECONDITION, do not sleep at it. The gate takes focus on
        # a timer, so "the overlay is hidden" and "the keyboard is the app's
        # again" are different moments, and a fixed 300 ms sometimes lands
        # between them -- which made this test fail about five runs in six while
        # asserting something else entirely. That the app DOES hand the keyboard
        # back is `test_the_gate_lets_go_of_the_keyboard_when_it_closes`; this
        # test is about the identity, so it waits rather than racing.
        page.wait_for_function(
            "getComputedStyle(document.getElementById('loading')).display"
            " === 'none' && ['INPUT','TEXTAREA','SELECT'].indexOf("
            "document.activeElement && document.activeElement.tagName) < 0",
            timeout=15000)
        press_until(page, "a", "#ch-2018 .on")
        press_until(page, "z", "#ch-2024 .on")
        page.keyboard.press("2")   # confidence: required to save
        page.keyboard.press("Enter")
        page.wait_for_function("Object.keys(S.labels).length === 1", timeout=15000)
        rec = page.evaluate("Object.values(S.labels)[0]")
        assert rec["labeller"] == "Hana Ø."
        # slugged, so "Hana Ø.", "hana ø" and "Hana  Ø" are one expert
        assert rec["expert_id"] == "x-hana-o"
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# the change year, and what two endpoints cannot say
# ---------------------------------------------------------------------------
def test_change_year_is_offered_only_where_there_is_change(browser, server):
    base, _ = server
    page, ctx = open_app(browser, base, who="ida")
    try:
        shown = "getComputedStyle(document.getElementById('sec-year')).display"
        page.keyboard.press("a")
        page.keyboard.press("z")                 # Nature -> Nature, stable
        page.wait_for_timeout(150)
        assert page.evaluate(shown) == "none"

        page.keyboard.press("c")                 # -> Artificial, now a change
        page.wait_for_timeout(150)
        assert page.evaluate(shown) != "none"

        page.click("#years button[data-year='2021']")
        page.keyboard.press("2")   # confidence: required to save
        page.keyboard.press("Enter")
        page.wait_for_timeout(300)
        assert page.evaluate("Object.values(S.labels)[0].change_year") == "2021"
    finally:
        ctx.close()


def test_a_year_answered_then_made_irrelevant_is_dropped(browser, server):
    """Going back to a stable call must not leave a stale year on the record."""
    base, _ = server
    page, ctx = open_app(browser, base, who="ida")
    try:
        page.keyboard.press("a")
        page.keyboard.press("c")                 # change
        page.click("#years button[data-year='2020']")
        page.keyboard.press("z")                 # back to Nature -> Nature
        page.wait_for_timeout(200)
        assert page.evaluate("cur.year") is None
        page.keyboard.press("2")   # confidence: required to save
        page.keyboard.press("Enter")
        page.wait_for_timeout(300)
        assert page.evaluate("Object.values(S.labels)[0].change_year") == ""
    finally:
        ctx.close()


def test_transient_change_opens_the_year_on_a_stable_call(browser, server):
    """Cleared then regrown is `Nature -> Nature` and still worth dating."""
    base, _ = server
    page, ctx = open_app(browser, base, who="ida")
    try:
        page.keyboard.press("a")
        page.keyboard.press("z")                 # stable
        page.keyboard.press("t")                 # change seen, endpoints match
        page.wait_for_timeout(150)
        assert page.evaluate(
            "getComputedStyle(document.getElementById('sec-year')).display") != "none"
        page.click("#years button[data-year='2020']")
        page.keyboard.press("2")   # confidence: required to save
        page.keyboard.press("Enter")
        page.wait_for_timeout(300)
        rec = page.evaluate("Object.values(S.labels)[0]")
        assert rec["transition"] == "Nature -> Nature"
        assert rec["is_change"] == 0
        assert rec["change_year"] == "2020"
        assert "transient_change" in rec["flags"]
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# calibration
# ---------------------------------------------------------------------------
def test_the_reference_answer_is_never_shown_before_the_call(browser, server):
    base, _ = server
    page, ctx = open_app(browser, base, who="jo", batch="/cal.json")
    try:
        assert page.evaluate("S.batch.calibration") is True
        assert page.evaluate("S.points[0].reference") == "Nature -> Cropland"
        # ...and none of the sample's reference answer reaches the panel before
        # an answer is given. The legend itself legitimately uses both class
        # names and the phrase "scale reference", so test the feedback channel
        # and the exact transition rather than those generic words.
        assert "Nature -> Cropland" not in page.text_content("#panel")
        assert page.text_content("#cal-feedback").strip() == ""
        assert "reference" not in page.text_content("#meta-table").lower()
        assert page.evaluate(
            "getComputedStyle(document.getElementById('cal-feedback')).display"
        ) == "none"
    finally:
        ctx.close()


def test_calibration_tells_you_after_each_call_and_scores_at_the_end(browser, server):
    base, _ = server
    page, ctx = open_app(browser, base, who="jo", batch="/cal.json")
    try:
        # k000's reference is Nature -> Cropland; answer Nature -> Nature (wrong)
        page.keyboard.press("a")
        page.keyboard.press("z")
        page.keyboard.press("2")   # confidence: required to save
        page.keyboard.press("Enter")
        page.wait_for_timeout(400)
        note = page.text_content("#cal-feedback")
        assert "k000" in note and "Nature -> Cropland" in note
        assert page.evaluate(
            "document.getElementById('cal-feedback').className") == "miss"

        # k001's reference is Nature -> Cropland; answer it correctly
        page.keyboard.press("a")
        page.keyboard.press("x")
        page.keyboard.press("2")   # confidence: required to save
        page.keyboard.press("Enter")
        page.wait_for_timeout(400)
        assert page.evaluate(
            "document.getElementById('cal-feedback').className") == "hit"

        # finish, and read the report
        for _ in range(2):
            page.keyboard.press("a")
            page.keyboard.press("z")
            page.keyboard.press("2")   # confidence: required to save
            page.keyboard.press("Enter")
            page.wait_for_timeout(400)
        score = page.evaluate("calibrationScore()")
        assert score["n"] == 4 and score["hits"] == 3
        report = page.text_content("#loading")
        assert "3 of 4" in report or "3 of 4" in report.replace("\u2009", " ")
        # the row carries the reference so the sheet can be read per labeller
        rec = page.evaluate("S.labels['k000']")
        assert rec["calibration"] == 1
        assert rec["reference"] == "Nature -> Cropland"
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# the keyboard, and who owns Enter
#
# The app is keyboard-first and the shortcut handler is on `window`. `inField()`
# exempted INPUT/TEXTAREA/SELECT and never named BUTTON, so Enter and Space were
# `preventDefault`ed out from under every button on the page: Tab to *Defer*,
# press Enter, and the point was SAVED. Every button was reachable by Tab and
# none of them was pressable.
#
# The fix has to hold in BOTH directions, which is why there are two tests. The
# first attempt gated on `:focus-visible` -- the textbook answer -- and Chromium
# flips that true the moment a key arrives at the focused element, so a button
# clicked with the MOUSE claimed the next Enter too: clicking a change-year and
# pressing Enter re-toggled the year off instead of saving. Tab is the only
# thing in this app that moves focus onto a control by keyboard, so the modality
# is tracked from Tab and nothing else.
# ---------------------------------------------------------------------------
def test_a_tabbed_button_gets_its_own_enter(browser, server):
    """Tab to Defer, press Enter, and it must DEFER rather than save."""
    base, _ = server
    page, ctx = open_app(browser, base, who="ida")
    try:
        page.keyboard.press("a")
        page.keyboard.press("c")
        # Confidence too, so the call is SAVEABLE. Without it Enter is refused
        # by `saveable()` and this test would pass whether or not the focus
        # logic works -- which is the whole thing it exists to check.
        page.keyboard.press("2")
        page.wait_for_timeout(150)
        # Tab is what arms the modality, and it is also how a keyboard user got
        # here. Focus is then put where the assertion needs it.
        page.keyboard.press("Tab")
        page.focus("#btn-skip")
        page.keyboard.press("Enter")
        page.wait_for_timeout(300)
        assert page.evaluate("Object.keys(S.drafts).length") == 1
        assert page.evaluate("Object.keys(S.labels).length") == 0
    finally:
        ctx.close()


def test_a_clicked_button_does_not_keep_the_next_enter(browser, server):
    """The other half. A mouse user's Enter belongs to the point, not to the
    button their pointer happens to have left the focus on."""
    base, _ = server
    page, ctx = open_app(browser, base, who="ida")
    try:
        page.keyboard.press("a")
        page.keyboard.press("c")
        page.click("#years button[data-year='2021']")
        page.keyboard.press("2")   # confidence: required to save
        page.keyboard.press("Enter")
        page.wait_for_timeout(300)
        assert page.evaluate("Object.keys(S.labels).length") == 1
        assert page.evaluate("Object.values(S.labels)[0].change_year") == "2021"
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# Escape, which was one line doing three jobs badly
#
#     if (e.key === 'Escape') { document.activeElement.blur(); hideLoading(); return; }
#
# It ran BEFORE the lightbox branch, so Esc never reached it -- and the close
# button advertised `title="Esc"`. And `hideLoading()` was unconditional, so it
# also dismissed the identity gate, which puts the app straight back into the
# after-the-fact nag the gate exists to replace.
# ---------------------------------------------------------------------------
def test_escape_does_not_dismiss_the_identity_gate(browser, server):
    base, _ = server
    page, ctx = open_app(browser, base, batch="/solo-gate.json",
                         hide_loading=False)
    try:
        assert page.is_visible("#intro-go")
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
        assert page.is_visible("#intro-go"), "Esc got past the identity gate"
    finally:
        ctx.close()


def test_escape_closes_the_lightbox_and_a_plain_dialog(browser, server):
    base, _ = server
    page, ctx = open_app(browser, base, who="ida")
    try:
        page.evaluate("openLightbox(2019)")
        page.wait_for_timeout(200)
        assert page.evaluate("lightboxOpen()")
        # and the keyboard is put inside it, which is half of what makes it modal
        assert page.evaluate("document.activeElement.id") == "lb-close"
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        assert not page.evaluate("lightboxOpen()")

        # a dismissible dialog still takes Esc -- the gate is the exception
        page.evaluate("explainSync()")
        page.wait_for_timeout(200)
        assert page.is_visible("#loading")
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        assert not page.is_visible("#loading")
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# the dialog dispatcher
#
# `#loading` is seven dialogs, and their buttons were eleven `onclick=`
# attributes. Those need every function they name to be a global, they are
# invisible to tooling, and they are the one thing that makes a `script-src`
# CSP impossible on this page. One delegated listener replaced them.
#
# The trade is that a typo in a `data-act` is SILENT where a typo in `onclick=`
# threw, so the actions are exercised rather than eyeballed.
# ---------------------------------------------------------------------------
def test_every_dialog_action_is_wired(browser, server):
    base, _ = server
    errs = []
    page, ctx = open_app(browser, base, who="wren", batch="/solo-queue.json")
    page.on("pageerror", lambda e: errs.append(str(e)))
    try:
        acts = lambda: page.evaluate(
            "[...document.querySelectorAll('#loading [data-act]')]"
            ".map(b => b.dataset.act).join(',')")

        page.evaluate("explainSync()")
        page.wait_for_timeout(200)
        assert acts() == "resync"
        page.evaluate("hideLoading()")

        # the deferred list: one `goto` row per deferred point, then `close`
        page.keyboard.press("a")
        page.click("#btn-skip")
        page.wait_for_timeout(250)
        for _ in range(6):
            page.keyboard.press("a")
            page.keyboard.press("z")
            page.keyboard.press("2")   # confidence: required to save
            page.keyboard.press("Enter")
            page.wait_for_timeout(200)
        page.wait_for_timeout(400)
        assert "deferred" in page.text_content("#loading-msg").lower()
        assert acts() == "goto,close"
        page.click("#loading button")
        page.wait_for_timeout(300)
        assert page.text_content("#pt-id") == "queue000"
        assert page.evaluate("cur.c2018") == "Nature"

        # every action the dispatcher knows is one some dialog actually emits
        assert set(page.evaluate("Object.keys(LOADING_ACTIONS)")) == {
            "close", "goto", "queue", "resync", "batch"}
        assert not errs, errs
    finally:
        ctx.close()


def test_a_batch_filename_with_a_quote_in_it_survives(browser, server):
    """The picker row built `onclick="...openBatchFile('FILE')"` -- a JS string
    inside an HTML attribute, two grammars deep. An apostrophe in a filename
    produced a button that did nothing. `data-arg` has one grammar and goes
    through `esc()` like anything else."""
    base, _ = server
    page, ctx = open_app(browser, base, who="wren")
    try:
        page.evaluate("""S.manifest = [{batch_id: "o'brien", file: "o'brien.json",
                                        n: 3, assigned: {}}]""")
        page.evaluate("showBatchPicker('probe')")
        page.wait_for_timeout(200)
        assert page.evaluate(
            "document.querySelector('#loading [data-act=\"batch\"]').dataset.arg"
        ) == "o'brien.json"
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# what is pinned with the call, and what scrolls with the evidence
# ---------------------------------------------------------------------------
def test_the_whole_call_is_pinned_and_only_the_evidence_scrolls(browser, server):
    """The form is one thing and it does not scroll: both dates, both flag
    groups, confidence, the notes and the buttons that end the point. Only the
    evidence -- the profile, the series, the filmstrip, the other people's maps
    -- is below the fold. The imagery flags, confidence, notes and Save used to
    sit past the bottom of an evidence block nine years long."""
    base, _ = server
    page, ctx = open_app(browser, base, who="ida")
    try:
        head = "document.getElementById('panel-head')"
        scroll = "document.getElementById('panel-scroll')"
        for el in ("flags-call", "flags", "sec-conf", "notes", "btn-save",
                   "btn-skip", "sec-uninterp", "transition-out"):
            assert page.evaluate(f"{head}.contains(document.getElementById('{el}'))"), el
        # ...and the evidence is still the only thing that moves
        for el in ("sec-evidence", "ev-chart", "ev-spectral", "model-hint",
                   "sec-year", "sec-meta"):
            assert page.evaluate(f"{scroll}.contains(document.getElementById('{el}'))"), el
        # the foot band is gone rather than merely emptied
        assert page.evaluate("document.getElementById('panel-foot')") is None
        # `unsure` and `flag for review` were pinned beside Save in their own
        # `flags-call` group; they now sit with the other four (AL11.3), because
        # six flags in two places is two lists to learn. The container and its
        # `:empty` rule stay -- what changed is that nothing is assigned to it.
        assert page.evaluate(
            "[...document.querySelectorAll('#flags .flag')]"
            ".map(b => b.dataset.flag)") == [
                "unsure", "uninterpretable", "mixed", "imagery_gap",
                "transient_change", "interesting"]
        assert page.evaluate(
            "document.querySelectorAll('#flags-call .flag').length") == 0
        # Save is reachable without scrolling anything: it is a flex child of
        # the head, below the head's own scroller.
        assert page.evaluate(
            "document.getElementById('panel-head-scroll')"
            ".contains(document.getElementById('btn-save'))") is False
        # every flag lands in exactly one pane, and both panes are still wired
        assert page.evaluate("FORM.flags.length") == page.evaluate("FLAGS.length")
        page.keyboard.press("u")
        page.keyboard.press("r")
        page.wait_for_timeout(200)
        assert page.evaluate("[...cur.flags].sort()") == ["interesting", "unsure"]
        assert page.evaluate(
            "document.querySelector('#flags [data-flag=unsure]')"
            ".getAttribute('aria-pressed')") == "true"
        assert page.evaluate(
            "document.querySelector('#flags [data-flag=interesting]')"
            ".getAttribute('aria-pressed')") == "true"
        page.click("#flags [data-flag=interesting]")
        page.wait_for_timeout(150)
        assert page.evaluate("[...cur.flags]") == ["unsure"]
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# the pixel inspector
# ---------------------------------------------------------------------------
# It reads Earth Engine, which these tests do not have. What they CAN pin is
# everything that is not the request: the arithmetic, the code tables, and the
# rule that a reading of somewhere else never survives onto the next point.
def test_the_inspector_indices_are_the_same_definition_as_the_chips(browser, server):
    """`inspS2Table` computes NDVI/NDMI/NBR itself, from the same six numbers it
    prints. If it grew its own band list the table would disagree with the chart
    and the filmstrip, which is the drift `CHIP_INDEX` exists to prevent."""
    base, _ = server
    page, ctx = open_app(browser, base, who="ida")
    try:
        html = page.evaluate("""() => inspS2Table({
            y2018_B2: 400, y2018_B3: 700, y2018_B4: 1000, y2018_B8: 3000,
            y2018_B11: 2000, y2018_B12: 1200,
            y2024_B2: 500, y2024_B3: 800, y2024_B4: 1200, y2024_B8: 1500,
            y2024_B11: 2400, y2024_B12: 1600 })""")
        # reflectance is the stored 0-10000 divided by 10000, as the spectral
        # profile does it -- not the raw count
        assert "0.300" in html and "0.150" in html      # NIR, both years
        # NDVI = (B8 - B4) / (B8 + B4), which is CHIP_INDEX['NDVI'].bands
        assert "0.500" in html                          # 2018
        assert "0.111" in html                          # 2024
        assert "-0.389" in html                         # the difference
        assert "NDVI" in html and "NDMI" in html and "NBR" in html
        # a year with no cloud-free composite is said, not shown as a zero
        empty = page.evaluate(
            "() => inspS2Table({ y2018_B8: 3000, y2018_B4: 1000 })")
        assert "—" in empty
        assert page.evaluate("() => inspS2Table({})").startswith("<div")
        assert "No cloud-free" in page.evaluate("() => inspS2Table({})")
    finally:
        ctx.close()


def test_the_inspector_decodes_class_codes_and_reports_a_mask_as_an_answer(
        browser, server):
    """A raw `4` under a palette is not a reading. And a masked pixel on a
    masked-difference layer -- dwbuilt, obtemporal, hansen -- is the layer
    saying "nothing here", which must not be reported as a failure."""
    base, _ = server
    page, ctx = open_app(browser, base, who="ida")
    try:
        assert page.evaluate("() => INSP_CODES.dw24(6)") == "built"
        assert page.evaluate("() => INSP_CODES.wc(40)") == "cropland"
        # ESRI remaps its non-contiguous codes onto 0..8 before it paints, and
        # the inspector has to decode the REMAPPED value
        assert page.evaluate("() => INSP_CODES.esri24(3)") == "crops"
        assert page.evaluate("() => INSP_UNITS.hansen(21)") == "cleared in 2021"
        assert "m²" in page.evaluate("() => INSP_UNITS.ghsl(1732)")

        masked = page.evaluate(
            "() => inspOverlayHTML(EE_LAYERS.dwbuilt, { ov_built: null })")
        assert "masked here" in masked and "not a gap" in masked
        # every value carries the layer's end year, exactly as the legend does
        assert "ends 2024" in masked
        named = page.evaluate(
            "() => inspOverlayHTML(EE_LAYERS.dw24, { ov_label: 6 })")
        assert "built" in named and "ends 2024" in named
    finally:
        ctx.close()


def test_the_inspector_reading_does_not_survive_onto_the_next_point(
        browser, server):
    """It is a reading of wherever the interpreter clicked. Left on screen under
    a new point it would be a wrong number in front of somebody making a call --
    the same rule Wayback's click read-out already keeps."""
    base, _ = server
    page, ctx = open_app(browser, base, who="ida")
    try:
        # the checkbox is the only thing that arms the map click
        assert page.evaluate("() => INSP.on") is False
        page.evaluate("() => { document.getElementById('insp-on').checked = true;"
                      "document.getElementById('insp-on')"
                      ".dispatchEvent(new Event('change')); }")
        assert page.evaluate("() => INSP.on") is True
        # with no Earth Engine it says so rather than sitting on a spinner
        page.evaluate("() => inspQuery({ lat: 59.91, lng: 10.75 })")
        out = page.text_content("#insp-out")
        assert "59.91000, 10.75000" in out
        assert "Earth Engine is not connected" in out
        assert page.evaluate(
            "() => getComputedStyle(document.getElementById('insp-out')).display"
        ) != "none"
        # ...and moving on wipes it
        page.evaluate("() => goTo(1)")
        page.wait_for_timeout(200)
        assert page.text_content("#insp-out") == ""
        assert page.evaluate("() => INSP.at") is None
    finally:
        ctx.close()


@needs_baked_b001
def test_chart_dots_rise_monotonically_with_the_plotted_index(browser, server):
    """Higher NDVI is greener. Always, at every point, under every chip scheme.

    Settled three times. AL10 painted the dot on the plotted index's ramp;
    AL11.5 switched it to the chip's false colour so a dot could be matched to
    its filmstrip cell; AL11.8 switched back, with a measurement: over the 99
    stretched points of b001 under SWIR1/NIR/GREEN, NDVI and perceived greenness
    correlate positively at 84 and NEGATIVELY at 15 -- red is SWIR1 there, so
    dry-but-vegetated ground renders brown at a high NDVI. A colour scale that
    reverses at 15% of points is not a scale.

    So this asserts the PROPERTY, not the mechanism: sort the dots by their
    plotted value and the ramp position must not go backwards. A future change
    that re-links the dot to the chip will fail here, which is the point.

    Read from `getComputedStyle`, never `getAttribute('fill')`: in SVG a CSS
    property beats a presentation attribute, and an earlier version of this test
    passed for weeks while every dot on screen was accent blue (AL11.6b).
    """
    base, _ = server
    page, ctx = open_app(browser, base, who="e1", batch="/batches/b001.json")
    try:
        page.wait_for_timeout(1200)
        # (value, ramp position) per dot, straight from the app's own ramp
        rows = lambda: page.evaluate("""(() => {
          const p = S.points[S.i], t = p.evidence.t, yrs = evYears();
          const spec = chipSpec(indexComboForSeries(evSeries));
          return [...document.querySelectorAll('#ev-chart .ev-dot')].map(d => {
            const yr = Number(d.dataset.year), i = yrs.indexOf(yr);
            const v = t[evSeries][i];
            return {v: v, fill: getComputedStyle(d).fill,
                    want: evVisColor(p, t, i, indexComboForSeries(evSeries))};
          }).filter(r => r.v !== null);
        })()""")

        def hexify(css):
            m = re.match(r"rgb\((\d+), ?(\d+), ?(\d+)\)", css)
            return "#" + "".join(f"{int(g):02x}" for g in m.groups()) if m else css

        for scheme in ("SWIR1/NIR/GREEN", "NDVI", "RED/GREEN/BLUE"):
            page.evaluate("s => { chipVis.combo = s; applyChipVis(); }", scheme)
            page.wait_for_timeout(400)
            got = rows()
            assert len(got) >= 5, got
            # the dot is the ramp colour for its own value...
            for r in got:
                assert hexify(r["fill"]) == r["want"], (scheme, r)
            # ...and the ramp is monotonic in the plotted value: sorting by the
            # value must sort the colours along the ramp, not scramble them.
            # "Higher NDVI is greener" is asserted as TWO facts that compose,
            # not as a per-point greenness test. A bare point whose nine years
            # all sit near NDVI 0.1 is legitimately brown throughout -- inside
            # that first ramp segment red rises faster than green, so a
            # G-minus-R measure calls a correct ramp inverted. p0005 and p0010
            # in b001 are exactly that shape, and an earlier draft of this test
            # would have failed on both.
            #
            # Fact 1, above: every dot is `rampColor` evaluated at its OWN
            # value. Fact 2, below: the ramp runs brown -> green. Together they
            # give monotonicity for free, at every point, without a colour
            # heuristic that has to be right about perception.

        # Fact 2: the ramp itself runs brown -> green, so a larger value is a
        # greener dot by construction. Checked on the constant, once.
        lo, hi = (page.evaluate(f"rampColor(INDEX_RAMP, {t})") for t in (0, 1))
        gr = lambda h: int(h[3:5], 16) - int(h[1:3], 16)
        assert gr(lo) < 0 < gr(hi), (
            f"INDEX_RAMP runs the wrong way: t=0 is {lo}, t=1 is {hi}")

        # the CHIP SCHEME must no longer move the dots: they follow the SERIES
        page.evaluate("chipVis.combo = 'SWIR1/NIR/GREEN'; applyChipVis()")
        page.wait_for_timeout(400)
        a = [r["fill"] for r in rows()]
        page.evaluate("chipVis.combo = 'RED/GREEN/BLUE'; applyChipVis()")
        page.wait_for_timeout(400)
        assert [r["fill"] for r in rows()] == a, \
            "changing the chip scheme moved the dots -- they must follow the series"

        # ...but changing the PLOTTED SERIES must
        page.click("#ev-series button[data-series='nbr']")
        page.wait_for_timeout(400)
        assert [r["fill"] for r in rows()] != a
    finally:
        ctx.close()


@needs_baked_b001
def test_the_lightbox_leaves_the_linked_readings_on_screen(browser, server):
    """Stepping years in the lightbox always drove the spectral profile and the
    chart; an `inset: 0` backdrop rendered that invisible."""
    base, _ = server
    ctx = browser.new_context(viewport={"width": 1500, "height": 850})
    stub_config(ctx, FIXTURE_CONFIG)
    ctx.add_init_script(
        "try { localStorage.setItem('recover-labels:seen-intro','1'); } catch (e) {}")
    page = ctx.new_page()
    page.goto(f"{base}/label_app.html?sheetUrl=/mock&batch=/batches/b001.json"
              f"&campaign=test-campaign&manifest=/absent.json&expert=e1",
              wait_until="load")
    page.wait_for_function("typeof S !== 'undefined' && S.points.length > 0",
                           timeout=30000)
    page.evaluate("hideLoading()")
    page.wait_for_timeout(1400)
    try:
        page.evaluate("openLightbox(2019)")
        page.wait_for_timeout(1200)
        assert page.evaluate("lightboxOpen()")
        # both readings fully inside the visible scroller, not under the backdrop
        vis = page.evaluate("""() => {
          const sc = document.getElementById('panel-scroll').getBoundingClientRect();
          const lb = document.getElementById('lightbox').getBoundingClientRect();
          const ok = id => { const r = document.getElementById(id).getBoundingClientRect();
            return r.height > 20 && r.top >= sc.top - 1 && r.bottom <= sc.bottom + 1
                   && r.left >= lb.right - 1; };
          return { chart: ok('ev-chart'), spectral: ok('ev-spectral') }; }""")
        assert vis == {"chart": True, "spectral": True}, vis
        # and stepping a year moves the highlight on the chart
        assert page.evaluate(
            "document.querySelector('#ev-chart .ev-dot.sel').dataset.year") == "2019"
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(600)
        assert page.evaluate(
            "document.querySelector('#ev-chart .ev-dot.sel').dataset.year") == "2020"
        # closing puts the standing explanations back
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        assert not page.evaluate("document.body.classList.contains('lb-open')")
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# chrome
# ---------------------------------------------------------------------------
def test_the_control_stack_does_not_swallow_the_map(browser, server):
    """It used to run the map's full height with every card expanded.

    On a 1366x768 laptop that meant 97%, with the Earth Engine card cut off the
    bottom -- the interpreter looked at the imagery through a letterbox. Two
    things fix it and both are asserted: only the two cards anyone needs start
    open, and one button clears the rest.
    """
    base, _ = server
    page, ctx = open_app(browser, base, who="kim")
    try:
        page.set_viewport_size({"width": 1366, "height": 768})
        page.wait_for_timeout(500)
        measure = """() => {
          const map = document.getElementById('map').getBoundingClientRect();
          const ctrl = document.getElementById('ctrl-right').getBoundingClientRect();
          return {pct: ctrl.height / map.height,
                  area: (ctrl.width * ctrl.height) / (map.width * map.height),
                  overflows: ctrl.scrollHeight > ctrl.clientHeight + 1};
        }"""
        m = page.evaluate(measure)
        # nothing is cut off, which is the bug that actually bit
        assert not m["overflows"], m
        assert m["area"] < 0.20, m

        # only the two cards an interpreter needs start open
        assert page.evaluate(
            "getComputedStyle(document.getElementById('b-layers')).display") == "none"
        assert page.evaluate(
            "getComputedStyle(document.getElementById('b-wayback')).display") != "none"

        # and one button reduces the stack to its headers
        page.click("#btn-cards")
        page.wait_for_timeout(250)
        collapsed = page.evaluate(measure)
        assert collapsed["area"] < 0.10, collapsed
        assert collapsed["area"] < m["area"] / 1.8, (m, collapsed)

        # the choice sticks
        page.click('[data-toggle="b-layers"]')
        page.wait_for_timeout(150)
        assert page.evaluate(
            "localStorage.getItem('recover-labels:card:b-layers')") == "1"
    finally:
        ctx.close()


def test_the_map_info_control_sits_clear_of_the_labelling_panel(browser, server):
    """The floating panel used to cover MapLibre's bottom-right info button."""
    base, _ = server
    page, ctx = open_app(browser, base, who="kim")
    try:
        page.set_viewport_size({"width": 1366, "height": 768})
        page.wait_for_timeout(200)
        position = page.evaluate("""() => {
          // This stand-in makes the test independent of WebGL and the MapLibre
          // CDN while exercising the exact class used by the real control.
          const info = document.createElement('div');
          info.className = 'maplibregl-ctrl-bottom-right';
          info.style.position = 'absolute';
          info.style.width = '32px';
          info.style.height = '32px';
          document.getElementById('map').appendChild(info);
          const ir = info.getBoundingClientRect();
          const pr = document.getElementById('panel').getBoundingClientRect();
          return {infoLeft: ir.left, infoRight: ir.right, panelLeft: pr.left,
                  infoBottom: ir.bottom, viewportWidth: innerWidth,
                  viewportHeight: innerHeight};
        }""")
        assert position["infoLeft"] >= 0, position
        assert position["infoRight"] <= position["panelLeft"] - 4, position
        assert position["infoBottom"] <= position["viewportHeight"], position
    finally:
        ctx.close()


def test_the_evidence_scrollbar_stops_above_the_panel_edge(browser, server):
    """The chart scroller must not disappear into the rounded panel bottom."""
    base, _ = server
    page, ctx = open_app(browser, base, who="kim")
    try:
        page.set_viewport_size({"width": 1366, "height": 768})
        page.wait_for_timeout(200)
        measure = page.evaluate("""() => {
          const scroller = document.getElementById('panel-scroll');
          const filler = document.createElement('div');
          filler.style.height = '1200px';
          scroller.appendChild(filler);
          const sr = scroller.getBoundingClientRect();
          const pr = document.getElementById('panel').getBoundingClientRect();
          const css = getComputedStyle(scroller);
          return {bottomClearance: pr.bottom - sr.bottom,
                  scrolls: scroller.scrollHeight > scroller.clientHeight,
                  gutter: css.scrollbarGutter,
                  marginBottom: css.marginBottom};
        }""")
        assert measure["scrolls"], measure
        assert measure["bottomClearance"] >= 10, measure
        assert "stable" in measure["gutter"], measure
        assert measure["marginBottom"] == "12px", measure
    finally:
        ctx.close()


def test_the_swipe_knob_does_not_sit_on_the_point(browser, server):
    """The point is dead centre; a knob parked there hides the only marker.

    Needs the swipe up, which needs Wayback, which needs the network -- so this
    builds the divider directly rather than skipping on a firewall.
    """
    base, _ = server
    page, ctx = open_app(browser, base, who="olo")
    try:
        page.set_viewport_size({"width": 1200, "height": 800})
        page.evaluate("""() => {
          const el = document.createElement('div');
          el.id = 'wb-swipe';
          el.innerHTML = '<div class="knob">X</div>';
          el.style.left = (document.getElementById('map').clientWidth / 2) + 'px';
          document.getElementById('map').appendChild(el);
        }""")
        page.wait_for_timeout(200)
        overlap = page.evaluate("""() => {
          const map = document.getElementById('map').getBoundingClientRect();
          const knob = document.querySelector('#wb-swipe .knob').getBoundingClientRect();
          const cx = map.left + map.width / 2, cy = map.top + map.height / 2;
          return knob.left <= cx && cx <= knob.right
              && knob.top <= cy && cy <= knob.bottom;
        }""")
        assert not overlap, "the swipe knob covers the point marker"
    finally:
        ctx.close()


def test_backspace_walks_back_to_the_point_you_just_left(browser, server):
    base, _ = server
    page, ctx = open_app(browser, base, who="lea")
    try:
        first = page.text_content("#pt-id")
        page.keyboard.press("a")
        page.keyboard.press("z")
        page.keyboard.press("2")   # confidence: required to save
        page.keyboard.press("Enter")
        page.wait_for_timeout(300)
        assert page.text_content("#pt-id") != first
        page.keyboard.press("Backspace")
        page.wait_for_timeout(300)
        assert page.text_content("#pt-id") == first
        # the saved answer is back in the form, ready to be corrected
        assert page.evaluate("[cur.c2018, cur.c2024]") == ["Nature", "Nature"]
    finally:
        ctx.close()


def test_the_batch_shape_is_not_shown_while_the_batch_is_being_made(browser, server):
    """A running change/stable tally in the interpreter's eyeline is an anchor.

    "0 change at point 40" is an argument for calling point 41 change, which is
    exactly the anchoring the hidden posterior exists to prevent. The tally is
    still computed -- it is a real check on a batch coming back 0% or 60% change
    -- but it belongs at the end, where it cannot nudge anything.
    """
    base, _ = server
    page, ctx = open_app(browser, base, who="moe")
    try:
        page.keyboard.press("a"); page.keyboard.press("c")   # change
        page.keyboard.press("2"); page.keyboard.press("Enter")
        page.wait_for_timeout(250)
        page.keyboard.press("a"); page.keyboard.press("z")   # stable
        page.keyboard.press("2"); page.keyboard.press("Enter")
        page.wait_for_timeout(250)
        page.keyboard.press("k")                              # unusable
        page.click("#uninterp-reasons button[data-reason='cloud']")
        page.keyboard.press("Enter"); page.wait_for_timeout(250)

        assert page.text_content("#counts").strip() == ""
        assert page.evaluate("batchShape()") == {"change": 1, "stable": 1,
                                                 "unusable": 1}
        # ...and the plain progress read stays
        assert page.text_content("#pill-progress").strip() == "3 / 10"
    finally:
        ctx.close()


def test_the_first_run_overlay_captures_an_identity(browser, server):
    base, _ = server
    page, ctx = open_app(browser, base, skip_intro=False,
                         config_js="window.LABEL_APP_CONFIG = "
                                   "{experts:[{id:'e1',name:'Nils'}]};")
    try:
        page.wait_for_selector("#intro-go", timeout=10000)
        guidance = page.text_content("#loading").lower()
        assert "yellow 10 m label square" in guidance
        assert "majority cover" in guidance
        assert "do not classify the centre coordinate" in guidance
        assert "white 100 m square" in guidance and "scale" in guidance
        assert "dashed 5 km sampling square" in guidance and "context" in guidance
        # it will not let you past without one
        page.click("#intro-go")
        page.wait_for_timeout(200)
        assert page.is_visible("#intro-go")
        page.select_option("#intro-who", "e1")
        page.click("#intro-go")
        page.wait_for_timeout(300)
        assert page.evaluate(
            "getComputedStyle(document.getElementById('loading')).display") == "none"
        assert page.evaluate("Expert.id()") == "e1"
        assert page.evaluate("Expert.name()") == "Nils"
        assert page.input_value("#who") == "e1"
        assert page.evaluate("localStorage.getItem('recover-labels:seen-intro')") == "1"
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# Earth Engine sign-in (optional feature, but it has to fail legibly)
# ---------------------------------------------------------------------------
FAKE_CLIENT = ("123456789012-abcdefghijklmnopqrstuvwxyz012345"
               ".apps.googleusercontent.com")


def test_unconfigured_earth_engine_says_exactly_what_to_do(browser, server):
    base, _ = server
    page, ctx = open_app(browser, base, who="pia", config_js=EMPTY_CONFIG)
    try:
        page.click('[data-toggle="b-ee"]')
        page.wait_for_timeout(200)
        page.click("#ee-signin")
        page.wait_for_timeout(400)
        note = page.text_content("#ee-note")
        assert "client id" in note.lower()
        assert "Web application" in note
        assert "Authorised JavaScript origins" in note
        # the origin it needs is named, not left to the reader
        assert page.evaluate("location.origin") in note
        # and the button is not left disabled
        assert not page.is_disabled("#ee-signin")
    finally:
        ctx.close()


def test_the_sign_in_libraries_load_before_the_click(browser, server):
    """The popup must open inside the user gesture.

    `authenticateViaOauth` ends in `requestAccessToken()`, which opens a window,
    and a browser only allows that inside a live gesture. The SDK defers it
    behind its own async load of Google Identity Services unless
    `window.default_gsi` is set, and the first version additionally awaited a
    340 KB SDK download inside the click handler -- putting the popup outside the
    gesture on any cold or slow load.

    Needs network access to Google's CDNs; skips without it.
    """
    base, _ = server
    ctx = browser.new_context()
    ctx.add_init_script(
        "try { localStorage.setItem('recover-labels:seen-intro','1'); } catch (e) {}")
    page = ctx.new_page()
    try:
        page.goto(f"{base}/label_app.html?eeClientId={FAKE_CLIENT}"
                  f"&eeProject=demo-project&batch=/batch.json"
                  f"&campaign=test-campaign&manifest=/absent.json",
                  wait_until="load")
        page.wait_for_function("typeof S !== 'undefined' && S.points.length > 0",
                               timeout=30000)
        page.evaluate("hideLoading()")
        page.click('[data-toggle="b-ee"]')
        try:
            page.wait_for_function(
                "typeof ee !== 'undefined' && window.default_gsi === true",
                timeout=30000)
        except Exception:
            pytest.skip("no network access to Google's CDNs")
        assert page.evaluate("!!(window.google && google.accounts && "
                             "google.accounts.oauth2)")
        # nothing is awaited between the click and requestAccessToken()
        assert page.evaluate("eePrepared !== null")
    finally:
        ctx.close()


def test_sign_in_stops_when_only_the_project_is_missing(browser, server):
    """The project is the one thing the person signing in has to supply.

    It used to be a warning: without it the SDK falls back to
    `earthengine-legacy`, so Google signs you in, the panel says "signed in",
    and then every layer fails -- the least legible failure this panel can
    produce. `eeSignIn` is called directly so the card stays shut and the
    library prefetch (which needs Google's CDN) never starts.
    """
    base, _ = server
    ctx = browser.new_context()
    ctx.add_init_script(
        "try { localStorage.setItem('recover-labels:seen-intro','1'); } catch (e) {}")
    stub_config(ctx)          # config.js must not supply the project either
    page = ctx.new_page()
    try:
        page.goto(f"{base}/label_app.html?eeClientId={FAKE_CLIENT}"
                  f"&batch=/batch.json&campaign=test-campaign"
                  f"&manifest=/absent.json", wait_until="load")
        page.wait_for_function("typeof S !== 'undefined' && S.points.length > 0",
                               timeout=30000)
        page.evaluate("hideLoading()")
        page.evaluate("eeSignIn()")
        page.wait_for_timeout(300)

        note = page.text_content("#ee-note")
        assert "project" in note.lower()
        # it stopped: no popup was attempted, nothing claims to be signed in
        assert page.evaluate("EE.ready === false")
        assert page.evaluate("eePrepared === null")
        assert page.evaluate(
            "getComputedStyle(document.getElementById('ee-layers')).display"
        ) == "none"
        assert not page.is_disabled("#ee-signin")
    finally:
        ctx.close()


def test_the_project_typed_into_the_panel_outlives_a_reload(browser, server):
    """A labeller cannot edit config.js, so the box has to be the setting.

    Kept in localStorage next to the outbox and the card states: config.js is
    for the deployment, the box is for whoever is sitting there.
    """
    base, _ = server
    page, ctx = open_app(browser, base, config_js=EMPTY_CONFIG)
    try:
        page.click('[data-toggle="b-ee"]')
        # The project and client-id boxes live behind "⚙ deployment setup" now:
        # a client-id field shown first thing reads as "this needs setting up",
        # which is exactly what a labeller must not conclude.
        page.evaluate("document.getElementById('ee-setup-wrap').open = true")
        page.fill("#ee-project", "  my-ee-project  ")
        page.dispatch_event("#ee-project", "change")
        page.wait_for_timeout(200)
        assert page.evaluate("CFG.eeProject") == "my-ee-project"

        page.reload(wait_until="load")
        page.wait_for_function("typeof S !== 'undefined' && S.points.length > 0",
                               timeout=30000)
        assert page.evaluate("CFG.eeProject") == "my-ee-project"
        assert page.input_value("#ee-project") == "my-ee-project"

        # and emptying it takes it back out again
        page.evaluate("document.getElementById('ee-setup-wrap').open = true")
        page.fill("#ee-project", "")
        page.dispatch_event("#ee-project", "change")
        page.reload(wait_until="load")
        page.wait_for_function("typeof S !== 'undefined' && S.points.length > 0",
                               timeout=30000)
        assert page.evaluate("CFG.eeProject") == ""
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# 4b. Earth Engine on the campaign's account
#
# The point of this path is that a labeller never sees a Google popup, so every
# test here asserts something did NOT happen: no click, no Identity Services,
# no client id. The SDK is stubbed rather than downloaded -- these run offline,
# and what is being checked is the four arguments the app hands it, which is
# exactly where the first version of the OAuth path went wrong.
# ---------------------------------------------------------------------------
EE_SCOPE = "https://www.googleapis.com/auth/earthengine"

#: A stand-in for build/ee_api_js.js. `data.setAuthToken` and
#: `data.setAuthTokenRefresher` are real because they are the contract under
#: test; everything else is a chainable no-op so the filmstrip's `ee.Image...`
#: calls do not throw once EE.ready goes true.
EE_STUB = """
window.__ee = { setAuthToken: [], initialize: [], refresher: 0 };
(function () {
  var chain = function () {
    return new Proxy(function () {}, {
      get: function (t, k) { return k === 'then' ? undefined : chain(); },
      apply: function () { return chain(); }
    });
  };
  var data = {
    setAuthToken: function (clientId, type, token, expires, scopes, cb,
                            updateAuthLibrary, suppress) {
      window.__ee.setAuthToken.push({
        clientId: clientId, type: type, token: token, expires: expires,
        scopes: scopes, updateAuthLibrary: updateAuthLibrary,
        suppress: suppress });
      if (updateAuthLibrary === false && cb) cb();
    },
    setAuthTokenRefresher: function (fn) {
      window.__ee.refresher += 1;
      window.__ee_refresher = fn;
    }
  };
  var dataProxy = new Proxy(data, {
    get: function (t, k) { return k in t ? t[k] : chain(); }
  });
  window.ee = new Proxy({}, {
    get: function (t, k) {
      if (k === 'data') return dataProxy;
      if (k === 'initialize')
        return function (a, b, ok, err, xsrf, project) {
          window.__ee.initialize.push(project);
          setTimeout(ok, 0);
        };
      return chain();
    }
  });
})();
"""


def ee_page(browser, base, sheet_url="/mock", extra="", config_js=EMPTY_CONFIG):
    """A page with the Earth Engine SDK stubbed and Google's CDNs blocked.

    The block is the assertion: this path must reach Earth Engine without ever
    touching accounts.google.com, so the route records anything that tries.
    """
    ctx = browser.new_context()
    ctx.add_init_script(
        "try { localStorage.setItem('recover-labels:seen-intro','1'); } catch (e) {}")
    stub_config(ctx, config_js)
    google: list[str] = []

    def _block(route):
        google.append(route.request.url)
        route.abort()

    ctx.route("https://accounts.google.com/**", _block)
    ctx.route("**/ee_api_js.js", lambda route: route.fulfill(
        status=200, content_type="application/javascript", body=EE_STUB))
    page = ctx.new_page()
    page.goto(f"{base}/label_app.html?sheetUrl={sheet_url}&batch=/batch.json"
              f"&campaign=test-campaign&manifest=/absent.json&expert=e1{extra}",
              wait_until="load")
    page.wait_for_function("typeof S !== 'undefined' && S.points.length > 0",
                           timeout=30000)
    page.evaluate("hideLoading()")
    return page, ctx, google


def test_the_campaign_account_connects_with_no_sign_in(browser, server):
    """Nobody clicks anything and the layers are there.

    This is the whole feature: an interpreter with no Cloud project, no Earth
    Engine registration and a browser that eats popups still gets the auxiliary
    layers, because the deployment's service account fetched the token.
    """
    base, sheet = server
    sheet.ee = {"access_token": "ya29.stub-token", "expires_in": 3300,
                "project": "ee-campaign", "scope": EE_SCOPE,
                "client_email": "labels@campaign.iam.gserviceaccount.com"}
    page, ctx, google = ee_page(browser, base)
    try:
        page.wait_for_function("EE.ready === true", timeout=20000)
        assert page.evaluate("EE.mode") == "service"

        call = page.evaluate("window.__ee.setAuthToken[0]")
        assert call["token"] == "ya29.stub-token"
        # updateAuthLibrary false is what makes this work with no sign-in
        # library at all: the SDK's `Aj` (the Identity Services loader) is
        # skipped entirely on `f===!1`.
        assert call["updateAuthLibrary"] is False
        # and the scopes are still the one scope, not the SDK's three
        assert call["suppress"] is True
        assert call["scopes"] == [EE_SCOPE]

        # the project comes from the key, so nobody had to know it
        assert page.evaluate("window.__ee.initialize[0]") == "ee-campaign"
        assert page.evaluate("CFG.eeProject") == "ee-campaign"

        # no Google sign-in was contacted, and no client id was needed
        assert google == [], f"the service path reached Google: {google}"
        assert page.evaluate("CFG.eeClientId") == ""
        assert page.evaluate("eePrepared === null")

        assert page.evaluate(
            "getComputedStyle(document.getElementById('ee-layers')).display"
        ) != "none"
    finally:
        sheet.ee = None
        ctx.close()


def test_service_mode_connects_with_no_gesture_and_never_offers_google(browser, server):
    """`eeAuthMode: 'service'` is what the Pages deployment ships with.

    Two properties, and the second is the one that makes it the right mode for
    a public static host: no labeller is shown a Google popup, and **no OAuth
    client id is used at all** -- so the Pages origin never has to be added to
    a client's Authorised JavaScript origins. An unregistered origin fails
    SILENTLY there: the SDK sets no `error_callback`, Google prints
    `origin_mismatch` inside the popup, and it reads exactly like a popup
    blocker.
    """
    base, sheet = server
    sheet.ee = {"access_token": "ya29.stub-token", "expires_in": 3300,
                "project": "ee-campaign", "scope": EE_SCOPE,
                "client_email": "labels@campaign.iam.gserviceaccount.com"}
    page, ctx, google = ee_page(
        browser, base,
        config_js="window.LABEL_APP_CONFIG = { eeAuthMode: 'service' };")
    try:
        # nobody pressed anything
        page.wait_for_function("EE.ready === true", timeout=20000)
        assert page.evaluate("EE.mode") == "service"
        assert page.evaluate(
            "getComputedStyle(document.getElementById('ee-layers')).display"
        ) != "none"

        # and pressing the button retries the campaign account rather than
        # reaching for a Google account
        page.click('[data-toggle="b-ee"]')
        page.wait_for_timeout(150)
        page.click("#ee-signin")
        page.wait_for_timeout(400)
        assert google == [], f"'service' mode reached Google: {google}"
        assert page.evaluate("CFG.eeClientId") == ""
    finally:
        sheet.ee = None
        ctx.close()


def test_a_deployment_with_no_service_account_is_left_alone(browser, server):
    """`configured:false` is not a fault and must not read as one.

    Most deployments will not have set one up, and an older Code.gs answers
    `ee_token` with its ping. Both have to leave the panel exactly as it was --
    a red step about a token broker is unactionable for the person reading it.
    """
    base, sheet = server
    sheet.ee = None
    page, ctx, _ = ee_page(browser, base)
    try:
        page.wait_for_timeout(1200)
        assert page.evaluate("EE.ready === false")
        assert page.evaluate("EE.mode === null")
        assert page.text_content("#ee-signin").startswith("Connect Earth Engine")
        assert not page.is_disabled("#ee-signin")
        note = page.text_content("#ee-note")
        assert "✗" not in note and "service account" not in note.lower(), note
    finally:
        ctx.close()


def test_the_token_is_refreshed_from_the_deployment(browser, server):
    """The hour boundary is the SDK's problem, and it asks the broker.

    Applying a token schedules `setTimeout(Cj, expires_in*1000*.81)` and `Cj`
    calls whatever `setAuthTokenRefresher` was given -- so the renewal has to
    come back from the Apps Script, not from a popup an hour into a labelling
    session.
    """
    base, sheet = server
    sheet.ee = {"access_token": "first", "expires_in": 3300,
                "project": "ee-campaign"}
    page, ctx, google = ee_page(browser, base)
    try:
        page.wait_for_function("EE.ready === true", timeout=20000)
        assert page.evaluate("window.__ee.refresher") >= 1
        sheet.ee = dict(sheet.ee, access_token="second")
        got = page.evaluate("""() => new Promise(ok =>
            window.__ee_refresher({}, ok))""")
        assert got["access_token"] == "second"
        assert got["token_type"] == "Bearer"
        assert got["expires_in"] == 3300
        assert google == [], f"a refresh reached Google: {google}"
    finally:
        sheet.ee = None
        ctx.close()


def test_pinning_oauth_never_asks_the_deployment(browser, server):
    """`eeAuthMode='oauth'` is the escape hatch and has to be a real one."""
    base, sheet = server
    sheet.ee = {"access_token": "should-not-be-used", "expires_in": 3300,
                "project": "ee-campaign"}
    page, ctx, _ = ee_page(browser, base, extra="&eeAuth=oauth")
    try:
        page.wait_for_timeout(1200)
        assert page.evaluate("EE.ready === false")
        assert page.evaluate("window.__ee === undefined")   # SDK never loaded
    finally:
        sheet.ee = None
        ctx.close()


# ---------------------------------------------------------------------------
# The broker half of the same contract, read off Code.gs. A Python double
# cannot police a contract the Apps Script disagrees with -- section 5 below
# is the standing example of that costing a round of labels.
# ---------------------------------------------------------------------------
def test_the_private_key_is_never_in_the_source():
    """`Code.gs` is in the repository; a key pasted into it is a key committed.

    The only way this file may reach a private key is through Script
    Properties, which are per-deployment and not in git.
    """
    text = CODE_GS.read_text()
    assert "-----BEGIN" not in text, "a private key has been pasted into Code.gs"
    src = _gs_function("eeKey_")
    assert "PropertiesService.getScriptProperties()" in src
    assert "EE_KEY_PROP" in src


def test_the_token_endpoint_is_behind_the_token_and_before_the_sheet():
    """Two ways to get this wrong, both silent.

    Answering `ee_token` without the token check hands Earth Engine access to
    anyone who finds the /exec URL. Answering it *after* `doGet` opens the
    sheet means an empty sheet returns the row-listing shortcut instead of a
    token, so a brand-new campaign is the one that cannot connect.
    """
    src = _gs_function("doGet")
    check = src.index("tokenOk_")
    token = src.index("action === 'ee_token'")
    sheet = src.index("sheet_(SHEET_NAME")
    assert check < token < sheet, (
        "ee_token must sit after the token check and before the sheet is "
        f"opened (positions: token check {check}, ee_token {token}, "
        f"sheet {sheet})")


def test_the_minted_token_carries_one_scope():
    """The same rule as the browser side: `earthengine`, not cloud-platform.

    A token minted with cloud-platform is a token that can do everything the
    service account can do in the project, handed to every browser that opens
    the page.
    """
    text = CODE_GS.read_text()
    assert ("var EE_SCOPE = 'https://www.googleapis.com/auth/earthengine';"
            in text)
    # comments stripped: the file explains at length why cloud-platform is not
    # here, and the explanation must not be what satisfies the test
    code = re.sub(r"//.*", "", re.sub(r"/\*.*?\*/", "", text, flags=re.S))
    assert "cloud-platform" not in code
    assert "scope: EE_SCOPE" in _gs_function("eeMint_")

# ---------------------------------------------------------------------------
# 5. the annotation key carries the expert
#
# These read the JavaScript source. The mock Sheet above implements the CORRECT
# contract -- the one the docs describe -- so it passed happily for as long as
# `Code.gs` implemented a different one. A Python double of a system cannot
# catch the system disagreeing with its own documentation; only the source can.
# ---------------------------------------------------------------------------
def _gs_function(name: str) -> str:
    """The source of one top-level function in Code.gs, braces matched."""
    text = CODE_GS.read_text()
    start = text.index(f"function {name}(")
    depth, i = 0, text.index("{", start)
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[start:j + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def _gs_block(needle: str) -> str:
    """The braced block introduced by the line containing `needle`."""
    text = CODE_GS.read_text()
    start = text.index(needle)
    depth, i = 0, text.index("{", start)
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[start:j + 1]
    raise AssertionError(f"unbalanced braces after {needle!r}")


def test_the_annotation_key_carries_the_expert():
    """`keyOf_` must compose all four fields.

    With three, expert B's save lands on expert A's row and the second reading is
    deleted on arrival. ACTIVE_LEARNING.md states the key as
    (campaign, batch_id, point_id, labeller) and warns in as many words that "a
    dedupe on (batch, point) alone would silently delete the measurement".
    """
    src = _gs_function("keyOf_")
    for field in ("campaign", "batch_id", "point_id", "expert_id"):
        assert f"r.{field}" in src, (
            f"keyOf_ does not compose {field}; the key is "
            f"(campaign, batch_id, point_id, expert_id)\n{src}")


def test_expert_id_is_a_column_and_the_index_reads_it():
    """A key field that is not a column cannot be read back to build the index."""
    text = CODE_GS.read_text()
    assert "'expert_id'" in text.split("var COLS")[1].split("]")[0]
    # ...and the row index that doPost upserts through has to read that column,
    # not just the first three.
    assert "COL.expert_id" in _gs_function("loadIndex_")


def test_labelled_never_returns_an_answer():
    """`action=labelled` says WHO holds a point and nothing about WHAT.

    Showing the first reading to the second reader turns the agreement
    measurement -- the campaign's only handle on the label noise that caps
    change-F1 -- into a confirmation measurement.
    """
    block = _gs_block("if (action === 'labelled')")
    for column in ANSWER_COLS:
        assert column not in block, (
            f"the `labelled` endpoint mentions {column!r}; it must return "
            f"{{point_id, expert_id}} and nothing else\n{block}")


def test_labelled_over_the_wire_carries_no_answer(browser, server):
    """The same rule, checked against what the app actually receives."""
    base, sheet = server
    sheet.seed(campaign="test-campaign", batch_id="t001", point_id="t009",
               class_2018="Cropland", class_2024="Nature",
               transition="Cropland -> Nature", is_change=1, notes="obvious",
               confidence=3, labeller="ravi", expert_id="ravi",
               labelled_at="2026-08-26T09:00:00Z")
    page, ctx = open_app(browser, base, who="sam")
    try:
        page.wait_for_function("S.others['t009'] !== undefined", timeout=15000)
        body = page.evaluate(
            "sheetGet({action:'labelled', batch:'t001'})")
        blob = json.dumps(body)
        for column in ("Cropland -> Nature", "obvious"):
            assert column not in blob, blob
        for entry in body["labelled"]:
            assert set(entry) == {"point_id", "expert_id"}, entry
    finally:
        ctx.close()


def test_two_experts_on_one_point_produce_two_rows(browser, server):
    """The bug this whole section exists for, end to end.

    Two experts, one batch, one point, both save. Two rows survive; each sees
    only their own through `mine`; and re-saving one does not touch the other.
    """
    base, sheet = server
    first, ctx1 = open_app(browser, base, who="ann", batch="/solo-two.json")
    try:
        first.keyboard.press("a")
        first.keyboard.press("c")            # Nature -> Artificial
        first.keyboard.press("2")
        first.keyboard.press("Enter")
        first.wait_for_function("Outbox.size() === 0", timeout=20000)
    finally:
        ctx1.close()

    second, ctx2 = open_app(browser, base, who="bo", batch="/solo-two.json")
    try:
        # bo reads the same point, independently, and disagrees
        second.evaluate("$('skip-others').checked = false")
        second.evaluate("goTo(0)")
        second.wait_for_timeout(200)
        second.keyboard.press("a")
        second.keyboard.press("z")           # Nature -> Nature
        second.keyboard.press("2")
        second.keyboard.press("Enter")
        second.wait_for_function("Outbox.size() === 0", timeout=20000)

        rows = sheet.rows_for("two000")
        assert len(rows) == 2, rows
        assert {r["expert_id"] for r in rows} == {"ann", "bo"}
        assert {r["transition"] for r in rows} == {"Nature -> Artificial",
                                                   "Nature -> Nature"}

        # bo corrects their own call; ann's row is untouched
        second.evaluate("goTo(0)")
        second.wait_for_timeout(200)
        second.keyboard.press("x")           # -> Cropland
        # 3, not 2: the reopened point already carries the confidence it was
        # saved with, and `setConf` TOGGLES -- pressing the same key again
        # clears it, and `saveable()` then refuses the correction.
        second.keyboard.press("3")
        second.keyboard.press("Enter")
        second.wait_for_function("Outbox.size() === 0", timeout=20000)
        rows = sheet.rows_for("two000")
        assert len(rows) == 2, rows
        by = {r["expert_id"]: r["transition"] for r in rows}
        assert by["ann"] == "Nature -> Artificial"
        assert by["bo"] == "Nature -> Cropland"

        # and `mine` hands each of them only their own
        got = second.evaluate(
            "sheetGet({action:'mine', batch:'two', expert:'bo'})")
        assert [r["point_id"] for r in got["rows"]] == ["two000"]
        assert got["rows"][0]["transition"] == "Nature -> Cropland"
    finally:
        ctx2.close()


def test_each_expert_gets_their_own_local_workspace(browser, server):
    """Switching identity is a workspace switch, not a rename.

    A shared localStorage namespace would let expert B open a batch, see A's
    answers already in the form, and re-save them as their own -- a confirmation
    measurement wearing an agreement measurement's clothes.
    """
    base, sheet = server
    sheet.fail = True                       # keep everything local
    page, ctx = open_app(browser, base, who="ann", batch="/solo-work.json")
    try:
        page.keyboard.press("a")
        page.keyboard.press("c")
        page.keyboard.press("2")
        page.keyboard.press("Enter")
        page.wait_for_timeout(300)
        assert page.evaluate("Object.keys(S.labels).length") == 1

        page.evaluate("switchExpert('bo')")
        page.wait_for_timeout(300)
        assert page.evaluate("Expert.id()") == "bo"
        # bo's workspace is empty, and ann's call is not in the form
        assert page.evaluate("Object.keys(S.labels).length") == 0
        assert page.evaluate("[cur.c2018, cur.c2024]") == [None, None]

        # ann's unsent row is still owed to the sheet, under ann
        groups = page.evaluate(
            "Outbox.groups().map(g => g.expert_id + ':' + g.rows.length)")
        assert groups == ["ann:1"], groups

        page.evaluate("switchExpert('ann')")
        page.wait_for_timeout(300)
        assert page.evaluate("Object.keys(S.labels).length") == 1
    finally:
        sheet.fail = False
        ctx.close()


# ---------------------------------------------------------------------------
# 6. cannot-interpret is not a one-way door
# ---------------------------------------------------------------------------
def test_cannot_interpret_restores_the_call_it_cleared(browser, server):
    """`K` sits between `M` and `G`; a mis-key used to wipe a finished call."""
    base, _ = server
    page, ctx = open_app(browser, base, who="tam")
    try:
        page.keyboard.press("a")
        page.keyboard.press("c")                       # Nature -> Artificial
        page.keyboard.press("k")                       # mis-key
        page.wait_for_timeout(150)
        assert page.evaluate("[cur.c2018, cur.c2024]") == [None, None]
        page.keyboard.press("k")                       # and back
        page.wait_for_timeout(150)
        assert page.evaluate("[cur.c2018, cur.c2024]") == ["Nature", "Artificial"]
    finally:
        ctx.close()


def test_cannot_interpret_needs_a_reason(browser, server):
    """These rows never reach the training set, so a countable cause is the only
    thing they can still buy."""
    base, _ = server
    page, ctx = open_app(browser, base, who="tam")
    try:
        page.keyboard.press("k")
        page.wait_for_timeout(150)
        assert page.evaluate(
            "getComputedStyle(document.getElementById('sec-uninterp')).display"
        ) != "none"
        assert page.is_disabled("#btn-save")
        page.keyboard.press("Enter")
        page.wait_for_timeout(250)
        assert page.evaluate("Object.keys(S.labels).length") == 0

        page.click("#uninterp-reasons button[data-reason='cloud']")
        page.wait_for_timeout(100)
        assert not page.is_disabled("#btn-save")
        page.keyboard.press("Enter")
        page.wait_for_timeout(300)
        rec = page.evaluate("Object.values(S.labels)[0]")
        assert rec["uninterpretable_reason"] == "cloud"
        assert rec["transition"] == ""
    finally:
        ctx.close()


def test_confidence_and_change_year_are_dropped_on_an_unusable_row(browser, server):
    base, _ = server
    page, ctx = open_app(browser, base, who="tam")
    try:
        page.keyboard.press("a")
        page.keyboard.press("c")                       # a change
        page.click("#years button[data-year='2021']")
        page.keyboard.press("3")                       # high confidence
        page.keyboard.press("k")                       # ...and then, unusable
        page.wait_for_timeout(150)
        assert page.evaluate("cur.conf") is None
        assert page.evaluate("cur.year") is None
        page.click("#uninterp-reasons button[data-reason='no_imagery']")
        page.keyboard.press("Enter")
        page.wait_for_timeout(300)
        rec = page.evaluate("Object.values(S.labels)[0]")
        assert rec["confidence"] == "" and rec["change_year"] == ""
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# 7. Defer keeps the draft; the arrow keys do too
# ---------------------------------------------------------------------------
def test_defer_keeps_a_partial_call(browser, server):
    """Skip used to discard a half-made call and a typed note, silently."""
    base, _ = server
    page, ctx = open_app(browser, base, who="uma", batch="/solo-defer.json")
    try:
        first = page.text_content("#pt-id")
        page.keyboard.press("a")                       # 2018 only
        page.fill("#notes", "come back to this, cloud on the 2024 side")
        page.evaluate("document.activeElement.blur()")
        page.click("#btn-skip")
        page.wait_for_timeout(400)
        assert page.text_content("#pt-id") != first

        page.evaluate("goTo(0)")
        page.wait_for_timeout(300)
        assert page.text_content("#pt-id") == first
        assert page.evaluate("cur.c2018") == "Nature"
        assert "cloud on the 2024 side" in page.input_value("#notes")
        # nothing was counted as done
        assert page.evaluate("Object.keys(S.labels).length") == 0
        assert page.evaluate("Outbox.size()") == 0
    finally:
        ctx.close()


def test_a_batch_is_not_complete_while_a_point_is_deferred(browser, server):
    base, _ = server
    page, ctx = open_app(browser, base, who="wren", batch="/solo-queue.json")
    try:
        page.keyboard.press("a")
        page.click("#btn-skip")                        # queue000 deferred
        page.wait_for_timeout(300)
        # a deferred point leaves the Enter path -- it is "come back to this",
        # not "show it to me again in four seconds"
        assert page.evaluate("outstanding(S.points[0])") is False
        for _ in range(5):
            page.keyboard.press("a")
            page.keyboard.press("z")
            page.keyboard.press("2")
            page.keyboard.press("Enter")
            page.wait_for_timeout(250)
        page.wait_for_timeout(500)
        report = page.text_content("#loading")
        assert "deferred" in report.lower(), report
        assert "queue000" in report
        # and it is reachable from there, with what was typed still in it
        page.click("#loading button")
        page.wait_for_timeout(300)
        assert page.text_content("#pt-id") == "queue000"
        assert page.evaluate("cur.c2018") == "Nature"
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# 8. blinding
# ---------------------------------------------------------------------------
def test_confidence_is_required_before_a_call_can_be_saved(browser, server):
    """Both classes are not enough: `saveable()` also wants a confidence.

    It was optional until 2026-08-31 and therefore mostly absent, which cost the
    campaign the one thing that separates "the legend is ambiguous here" from
    "the imagery is" when two experts disagree. Asserted through the KEYBOARD,
    because the whole reason this is affordable is that 1/2/3 were already bound
    and merely untaught -- a mandatory field that needs the mouse is a different
    and much worse change.
    """
    base, _ = server
    page, ctx = open_app(browser, base, who="rae")
    try:
        page.keyboard.press("a")
        page.keyboard.press("z")
        page.wait_for_timeout(150)
        assert page.evaluate("saveable()") is False
        assert page.evaluate("document.getElementById('btn-save').disabled") is True
        assert "onfidence" in page.text_content("#btn-save")

        page.keyboard.press("Enter")            # must NOT save
        page.wait_for_timeout(250)
        assert page.evaluate("Object.keys(S.labels).length") == 0

        page.keyboard.press("2")
        page.wait_for_timeout(150)
        assert page.evaluate("saveable()") is True
        page.keyboard.press("Enter")
        page.wait_for_timeout(300)
        assert page.evaluate("Object.keys(S.labels).length") == 1
        assert page.evaluate("Object.values(S.labels)[0].confidence") == 2
    finally:
        ctx.close()


def test_cannot_interpret_does_not_ask_for_confidence(browser, server):
    """The other side of it. That row carries no call, `saveCurrent` blanks the
    field on the way out, and "how sure are you that you cannot tell" is a
    question with no use for the answer."""
    base, _ = server
    page, ctx = open_app(browser, base, who="rae")
    try:
        page.keyboard.press("k")
        page.click("#uninterp-reasons button[data-reason='cloud']")
        page.wait_for_timeout(150)
        assert page.evaluate("saveable()") is True
        page.keyboard.press("Enter")
        page.wait_for_timeout(300)
        assert page.evaluate("Object.keys(S.labels).length") == 1
        assert page.evaluate("Object.values(S.labels)[0].confidence") == ""
    finally:
        ctx.close()


def test_progress_counts_this_experts_readings_not_the_whole_batch(browser, server):
    """A batch that splits 100 points between two experts owes each of them ~52,
    and a finished batch used to read "52 / 100" -- which is not a slow labeller,
    it is a wrong denominator."""
    base, _ = server
    page, ctx = open_app(browser, base, who="ann", batch="/solo-two.json")
    try:
        total = page.evaluate("myWorkload()")
        n_points = page.evaluate("S.points.length")
        assert total <= n_points
        assert page.text_content("#pill-progress").strip() == f"0 / {total}"
        label_here(page)
        assert page.text_content("#pill-progress").strip() == f"1 / {total}"
    finally:
        ctx.close()


def test_dropping_an_export_restores_it_instead_of_replacing_the_batch(browser, server):
    """The bug this is about ATE WORK, and it ate it at the worst moment.

    `exportBatch` writes the LABELLED rows and only those. Every dropped file
    went to `adoptBatch`, so re-dropping your own export replaced the batch's
    points with the handful you had finished: the count read `n / n`, the strip
    lost every outstanding point, and there was no way back except re-opening
    the batch. Exactly backwards for a file whose purpose is "carry on where I
    stopped" -- and it is the file you reach for when sync has already failed,
    so the state it destroyed was the state with no copy on the server.
    """
    base, _ = server
    page, ctx = open_app(browser, base, who="ann", batch="/solo-work.json")
    try:
        n_points = page.evaluate("S.points.length")
        assert n_points >= 3, "need an unfinished batch for this to mean anything"
        label_here(page)
        label_here(page)
        assert page.evaluate("Object.keys(S.labels).length") == 2

        csv = page.evaluate("toCSV(Object.values(S.labels))")
        assert page.evaluate("csv => looksLikeLabels(parseCSV(csv))", csv) is True

        # a fresh browser: the work exists only in the file
        page.evaluate("S.labels = {}; LS.save(S.batch.batch_id, {});"
                      "Outbox._cache = {}; Outbox._flush();"
                      "buildStrip(); updateProgress();")
        assert page.evaluate("Object.keys(S.labels).length") == 0

        page.evaluate("csv => importLabels(parseCSV(csv))", csv)
        page.wait_for_timeout(300)

        # the batch is intact and the work is back
        assert page.evaluate("S.points.length") == n_points, \
            "the export replaced the batch instead of restoring into it"
        assert page.evaluate("Object.keys(S.labels).length") == 2
        assert page.evaluate("Outbox.size()") == 2, "restored rows must re-sync"
        assert page.text_content("#pill-progress").strip().startswith("2 / ")
    finally:
        ctx.close()


def test_a_batch_file_is_still_adopted_rather_than_restored(browser, server):
    """The other half of the branch. The two files look alike and mean opposite
    things, so `looksLikeLabels` has to say no to a batch -- a batch CSV carries
    points and no answers."""
    base, _ = server
    page, ctx = open_app(browser, base, who="ann", batch="/solo-work.json")
    try:
        assert page.evaluate(
            "looksLikeLabels(parseCSV('id,lon,lat\\np0,10,60\\np1,11,61\\n'))"
        ) is False
        # and a labels export missing the answer columns is not a labels file
        assert page.evaluate(
            "looksLikeLabels(parseCSV('point_id,lon,lat\\np0,10,60\\n'))"
        ) is False
    finally:
        ctx.close()


def test_a_restored_row_is_filed_under_the_expert_who_made_it(browser, server):
    """`LS.key` is per-expert. Filing a colleague's readings under whoever
    happened to drop the file would both hide them from their owner and corrupt
    the agreement measurement -- so they go to the outbox (which groups by
    expert) and NOT into this expert's workspace."""
    base, _ = server
    page, ctx = open_app(browser, base, who="ann", batch="/solo-work.json")
    try:
        label_here(page)
        csv = page.evaluate("toCSV(Object.values(S.labels))")
        foreign = csv.replace(",ann,", ",bo,")
        assert foreign != csv, "fixture did not contain the expert id"
        page.evaluate("S.labels = {}; LS.save(S.batch.batch_id, {});"
                      "Outbox._cache = {}; Outbox._flush();")
        res = page.evaluate("csv => importLabels(parseCSV(csv))", foreign)
        assert res["foreign"] == 1, res
        assert page.evaluate("Object.keys(S.labels).length") == 0, \
            "bo's reading must not appear in ann's workspace"
        assert page.evaluate("Outbox.size()") == 1, "but it must still sync"
        assert page.evaluate(
            "Object.values(Outbox.all())[0].expert_id") == "bo"
    finally:
        ctx.close()


def test_rank_score_and_channel_are_hidden_until_the_point_is_saved(browser, server):
    """"rank 1, uncertainty" tells the interpreter the model finds this hard.

    Descriptive context stays visible -- §AL-T's coverage gap is why these points
    are in the batch and the interpreter should be able to see when they are in
    it. What is hidden is why the acquisition function picked this one.
    """
    base, _ = server
    page, ctx = open_app(browser, base, who="vic", batch="/solo-blind.json")
    try:
        assert page.evaluate(
            "getComputedStyle(document.getElementById('pt-channel')).display"
        ) == "none"
        assert page.evaluate(
            "getComputedStyle(document.getElementById('meta-why')).display"
        ) == "none"
        panel = page.text_content("#panel-scroll")
        assert "acquisition score" not in panel
        assert "rank in batch" not in panel

        page.keyboard.press("a"); page.keyboard.press("z")
        page.keyboard.press("2"); page.keyboard.press("Enter")
        page.wait_for_timeout(300)
        page.evaluate("goTo(0)")
        page.wait_for_timeout(300)
        assert page.evaluate(
            "getComputedStyle(document.getElementById('pt-channel')).display"
        ) != "none"
        assert "rank in batch" in page.text_content("#meta-why")
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# 9. the two queues come from the batch file
# ---------------------------------------------------------------------------
def solo_batch(name: str) -> dict:
    """A batch nobody else touches, served from `/solo-<name>.json`.

    The mock Sheet is module-scoped and rows accumulate across tests, so a test
    that seeds or saves into a shared batch changes which point a *later* test
    opens on -- and a test that then asserts on "the first point" is asserting on
    whatever the previous test left behind. Two of the tests below chased that
    for a while. One batch id per test removes the question.
    """
    return {
        "campaign": "test-campaign", "batch_id": name, "channel": "coverage",
        "instructions": None,
        "points": [{"id": f"{name}{i:03d}", "lon": 11.0 + i * 0.01, "lat": 59.0,
                    "channel": "coverage", "rank": i + 1, "score": 0.5,
                    "cell_km": 5, "meta": {"stratum": "bare"}}
                   for i in range(6)],
    }

ASSIGNED_BATCH = {
    "campaign": "test-campaign", "batch_id": "a001", "channel": "coverage",
    "instructions": None, "experts": ["ann", "bo"],
    "points": [
        {"id": "a000", "lon": 10.0, "lat": 60.0, "cell_km": 5, "meta": {},
         "primary_expert": "ann", "required_readers": ["ann"]},
        {"id": "a001", "lon": 10.1, "lat": 60.0, "cell_km": 5, "meta": {},
         "primary_expert": "bo", "required_readers": ["bo"]},
        {"id": "a002", "lon": 10.2, "lat": 60.0, "cell_km": 5, "meta": {},
         "primary_expert": "ann", "required_readers": ["ann", "bo"]},
    ],
}


#: A batch with baked evidence. The whole labelling loop -- including this -- has
#: to render and work with Earth Engine never signed in: hosting is static, and
#: sign-in is on the critical path for imagery only.
EV_YEARS = list(range(2017, 2026))
#: The declared annual NDVI. `bands.B8` is derived from it below so the
#: fixture's series and its bands agree -- see the note there.
EV_NDVI = [0.71, 0.70, 0.68, 0.66, 0.30, 0.28, 0.29, 0.27, 0.26]

EVIDENCE_BATCH = {
    "campaign": "test-campaign", "batch_id": "ev001", "channel": "coverage",
    "instructions": None, "evidence_version": "ev1",
    "evidence_schema": {
        "version": "ev1",
        "rows": [
            {"key": "dw_2018", "label": "Dynamic World 2018",
             "dataset": "GOOGLE/DYNAMICWORLD/V1", "end": 2018, "res": "10 m"},
            {"key": "dw_2024", "label": "Dynamic World 2024",
             "dataset": "GOOGLE/DYNAMICWORLD/V1", "end": 2024, "res": "10 m"},
            {"key": "wc_2021", "label": "WorldCover 2021",
             "dataset": "ESA/WorldCover/v200", "end": 2021, "res": "10 m"},
        ],
        "timeline": {"years": EV_YEARS, "series": ["ndvi", "ndmi", "nbr"],
                     "bands": ["B2", "B3", "B4", "B8", "B11", "B12"],
                     "season": {"name": "northern (Jun-Sep)", "start_month": 6,
                                "months": 4, "year_offset": 0}},
    },
    "points": [{
        "id": f"ev{i:03d}", "lon": 10.0 + i * 0.01, "lat": 60.0, "cell_km": 5,
        "meta": {"stratum": "bare"},
        "evidence": {
            "v": {"dw_2018": "trees", "dw_2024": "crops",
                  "wc_2021": "tree cover"},
            "t": {"ndvi": EV_NDVI,
                  "ndmi": [0.2] * 9, "nbr": [0.5] * 9,
                  "n": [12] * 9,
                  # B4/B8 are DERIVED FROM `EV_NDVI` rather than set flat. Every
                  # band used to carry the same [1000..1800], so B8 == B4, the
                  # real NDVI was 0 in all nine years and every chart dot came
                  # out the same brown -- while the `ndvi` list above said the
                  # series ran 0.71 -> 0.26. A fixture whose declared series and
                  # declared bands disagree cannot test a colour computed from
                  # the bands.
                  "bands": dict({b: [1000 + j * 100 for j in range(9)]
                                 for b in ("B2", "B3", "B11", "B12")},
                                B4=[1000] * 9,
                                B8=[round(1000 * (1 + v) / (1 - v))
                                    for v in EV_NDVI])},
        },
    } for i in range(3)],
}


#: A baked filmstrip: one sprite per point, `len(years)` cells wide, sliced in
#: the browser. Built by `src/build_batch_chips.py`, which is the static-hosting
#: equivalent of the DIST-ALERT inspector's `warm_ts_cache.py` -- the point of
#: both is that a cold chip costs what it costs, so somebody pays before the
#: interpreter looks. The pixels do not matter here; the geometry does.
def _sprite_png(cells=9, cell=8):
    import io
    try:
        from PIL import Image
    except ImportError:                                  # pragma: no cover
        #: `allow_module_level` because the ONLY caller is the module-level
        #: `SPRITE_PNG` below. A skip raised during collection is a collection
        #: ERROR, not a skip: it takes the whole file down, and the CI guard
        #: that fails a run of nothing but skips does not fire on an error.
        #: That is how this stayed green locally -- the geo env has pillow --
        #: and failed on the first push, where the runner did not.
        pytest.skip("PIL unavailable", allow_module_level=True)
    im = Image.new("RGB", (cell * cells, cell))
    for i in range(cells):                # a different colour per year cell
        for x in range(cell * i, cell * (i + 1)):
            for y in range(cell):
                im.putpixel((x, y), (i * 25, 255 - i * 25, 128))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


SPRITE_PNG = _sprite_png()

#: The per-point display ramp `build_batch_chips.py` measures. Deliberately far
#: from the global 0-3500 so a test can tell which one the app used: the tint
#: under an unarrived chip and the live request both have to be drawn through
#: the POINT's bounds, or the colour and the image are two exposures of one
#: pixel. See `tests/test_chip_ramp.py` for the Python/JavaScript agreement.
def _js_const(name: str) -> str:
    """The literal a top-level `const NAME = ...;` is assigned in the app."""
    m = re.search(rf"^const {re.escape(name)} = (.+?);\s*$",
                  APP_HTML.read_text(), re.M)
    assert m, f"{name} not found in label_app.html"
    return m.group(1)


BAKED_STRETCH = {"ev000": {b: [200.0, 1400.0]
                           for b in ("B2", "B3", "B4", "B8", "B11", "B12")}}
BAKED_CHIPS = {
    # Read from the app rather than pinned: a fixture that names a version the
    # app has moved past tests the FALLBACK, silently, for every test that uses
    # it -- and the fallback is the thing these tests exist to keep off.
    "version": _js_const("CHIP_BAKE_VERSION").strip("'"),
    "dir": "ev001_chips", "years": EV_YEARS,
    "cell": 176, "width_m": 640, "cap": 12, "format": "png",
    "combos": ["SWIR1/NIR/GREEN"],
    "stretch": BAKED_STRETCH, "stretch_pct": [2, 98], "stretch_width_m": 640,
    "built": 1787900000,
}


def baked_batch(chips):
    import copy
    batch = copy.deepcopy(EVIDENCE_BATCH)
    batch["batch_id"] = "ev001"
    batch["chips"] = chips
    return batch


def test_a_baked_filmstrip_needs_no_earth_engine(browser, server):
    """The fastest chip is the one Earth Engine was asked for last week.

    With a bake next to the batch, opening a point costs ONE static file and no
    Earth Engine at all -- so the strip is imagery, not colour swatches, with
    nobody signed in.
    """
    base, _ = server
    page, ctx = open_app(browser, base, who="zed", batch="/baked.json")
    try:
        page.wait_for_function("S.points.length === 3", timeout=15000)
        assert page.evaluate("EE.ready") is False
        page.wait_for_function(
            "document.querySelectorAll('#ev-strip .chip.baked').length === 9",
            timeout=15000)

        # one file for nine years, and every cell points at that same file
        urls = page.evaluate(
            "[...new Set([...document.querySelectorAll('#ev-strip .chip')]"
            ".map(c => c.style.backgroundImage))]")
        assert len(urls) == 1 and "ev001_chips/swir1_nir_green/ev000.png" in urls[0]
        # ...carrying the bake stamp. A re-bake writes NEW pixels to the SAME
        # path, so without this every browser and CDN that already fetched a
        # sprite keeps serving the old one -- which looks exactly like the
        # re-bake not having worked, and is invisible from a fresh profile.
        assert "?v=1787900000" in urls[0], urls[0]

        # ...sliced by position, evenly, first cell to last
        pos = page.evaluate(
            "[...document.querySelectorAll('#ev-strip .chip')]"
            ".map(c => parseFloat(c.style.backgroundPosition))")
        assert pos == [0, 12.5, 25, 37.5, 50, 62.5, 75, 87.5, 100]
        assert page.evaluate(
            "[...document.querySelectorAll('#ev-strip .chip')]"
            ".every(c => c.style.backgroundSize === '900% 100%')")

        # and the "you need Earth Engine" note is NOT shown, because the chips
        # are on screen -- it would be saying the opposite of what is true
        assert page.text_content("#ev-strip-note").strip() == ""
    finally:
        ctx.close()


def test_a_bake_that_is_not_there_falls_back_to_the_live_path(browser, server):
    """A bake can be stale, partial, or simply not deployed with the batch.

    Every one of those has to land on the live Earth Engine path, never on an
    empty strip: the bake is a latency optimisation and is never the only way
    to see a chip.
    """
    base, _ = server
    page, ctx = open_app(browser, base, who="zed", batch="/baked-missing.json")
    try:
        page.wait_for_function("S.points.length === 3", timeout=15000)
        # the sprite 404s, so nothing is baked...
        page.wait_for_function(
            "document.getElementById('ev-strip-note').textContent"
            ".includes('Earth Engine')", timeout=15000)
        assert page.evaluate(
            "document.querySelectorAll('#ev-strip .chip.baked').length") == 0
        # ...and the strip is still nine cells of baked colour, not nothing
        assert page.evaluate(
            "document.querySelectorAll('#ev-strip .chip.tinted').length") == 9
    finally:
        ctx.close()


def test_a_bake_for_another_scheme_or_width_is_not_used(browser, server):
    """The bake is a picture of a 640 m box under one vis scheme.

    An interpreter who switches to NDMI, or asks for 1280 m, is asking a
    different question -- not for the same picture again. Serving the baked one
    there would be showing them the answer to the question they just left.
    """
    base, _ = server
    page, ctx = open_app(browser, base, who="zed", batch="/baked.json")
    try:
        page.wait_for_function("S.points.length === 3", timeout=15000)
        page.wait_for_function(
            "document.querySelectorAll('#ev-strip .chip.baked').length === 9",
            timeout=15000)
        assert page.evaluate("!!chipSpriteUrl(S.points[S.i])")

        page.evaluate("chipVis.combo = 'NDMI'")
        assert page.evaluate("chipSpriteUrl(S.points[S.i])") is None
        page.evaluate("chipVis.combo = 'SWIR1/NIR/GREEN'; chipVis.w = 1280")
        assert page.evaluate("chipSpriteUrl(S.points[S.i])") is None
    finally:
        ctx.close()


def test_a_permission_denial_is_never_reported_as_outside_coverage(browser, server):
    """The two things Earth Engine's silence can mean need opposite reactions.

    "Outside coverage" is a statement about the ground. A 403 is a statement
    about the deployment -- it is every point, for every labeller, and no
    reading of this one. Handing an interpreter a configuration fault dressed
    as a coverage boundary is the exact failure the `cover` field and the
    "never blank" rule exist to prevent, arriving through the other door.

    Found in production: a brand-new service account has NO roles, so the
    campaign account minted a token, computed fine, and was refused every map
    tile with `earthengine.maps.create denied`.
    """
    base, _ = server
    page, ctx = open_app(browser, base, who="zed", batch="/evidence.json")
    try:
        page.wait_for_function("S.points.length === 3", timeout=15000)
        if not page.evaluate("typeof map !== 'undefined' && !!map"):
            pytest.skip("no WebGL in this browser")
        page.evaluate("""(() => {
          EE.ready = true;
          const key = Object.keys(EE_LAYERS)[0];
          EE_LAYERS[key].build = () => ({
            image: { getMap: (vis, cb) => cb(null,
              "Permission 'earthengine.maps.create' denied on resource "
              + "'projects/ee-gsingh' (or it may not exist).") }, vis: {} });
          eeSelectLayer(key);
        })()""")
        page.wait_for_function(
            "document.getElementById('ee-layer-note').textContent.length > 0",
            timeout=20000)
        note = page.text_content("#ee-layer-note")
        assert "outside coverage" not in note.lower(), note
        assert "deployment problem" in note.lower(), note
        assert "earthengine.viewer" in note, note

        # ...and the genuinely empty case still says exactly that
        page.evaluate("""(() => {
          const key = Object.keys(EE_LAYERS)[1];
          EE_LAYERS[key].build = () => ({
            image: { getMap: (vis, cb) => cb(null, 'no results') }, vis: {} });
          eeSelectLayer(key);
        })()""")
        page.wait_for_function(
            "document.getElementById('ee-layer-note').textContent"
            ".toLowerCase().indexOf('outside coverage') >= 0", timeout=20000)
    finally:
        ctx.close()


def test_the_deployment_self_test_probes_maps_not_only_compute():
    """`earthengine.maps.create` is a SEPARATE IAM permission from
    `earthengine.computations.create`.

    The compute probe's own comment claimed "every single thing it does,
    getMapId included, is a COMPUTATION" -- and on that reasoning a service
    account with no Earth Engine role self-tested clean and had every overlay
    in the app come back empty. The maps probe is the other half, and it has to
    carry `X-Goog-User-Project` for the same reason the compute one does.
    """
    src = _gs_function("eeMapsSelfTest")
    assert "/maps'" in src, "the probe does not call maps.create"
    assert "X-Goog-User-Project" in src
    assert "Image.constant" in src, (
        "maps.create will not take a constant -- the probe must be an IMAGE")
    # It must name the role that actually mints tiles. Viewer alone was tried
    # in production and the denial persisted, so the log has to say what to do
    # NEXT rather than repeat the grant that did not work.
    assert "roles/earthengine.writer" in src
    assert "get-iam-policy" in src, (
        "the log must show how to read what the account actually holds ON THIS "
        "PROJECT -- a grant on the wrong project looks identical from the app")


def test_an_earth_engine_overlay_that_draws_nothing_says_so(browser, server):
    """`getMapId` validates the request; the expression breaks at TILE time.

    A band name that does not match, a filter that leaves the collection
    empty, a memory limit, an expired token -- all of those mint a mapid
    successfully and then fail per tile, which MapLibre reports on
    `map.on('error')` and which this app sent to `console.warn` and nowhere
    else. The mint succeeded, the note was cleared, and the map stayed exactly
    as it was: "I select a layer and nothing appears", with nothing on screen
    to go on.

    Reading Earth Engine's own message out of the tile response is the
    difference between "something is wrong" and knowing which band.
    """
    base, _ = server
    page, ctx = open_app(browser, base, who="zed", batch="/evidence.json")
    try:
        page.wait_for_function("S.points.length === 3", timeout=15000)
        if not page.evaluate("typeof map !== 'undefined' && !!map"):
            pytest.skip("no WebGL in this browser")
        page.wait_for_function("map.isStyleLoaded && map.isStyleLoaded()",
                               timeout=20000)
        page.evaluate("""(() => {
          EE.ready = true;
          const key = Object.keys(EE_LAYERS)[0];
          EE_LAYERS[key].build = () => ({
            image: { getMap: (vis, cb) => cb({ urlFormat: location.origin
              + '/faketile/{z}/{x}/{y}.png' }) }, vis: {} });
          eeSelectLayer(key);
        })()""")
        page.wait_for_function(
            "document.getElementById('ee-layer-note').textContent.length > 0",
            timeout=20000)
        note = page.text_content("#ee-layer-note")
        assert "400" in note
        # ...and it must not read as an absence of change, which is the same
        # rule the "outside coverage" wording exists for one step earlier
        assert "nothing here" in note
        # Earth Engine's own reason, fetched from the tile the map could not get
        page.wait_for_function(
            "document.getElementById('ee-layer-note').textContent"
            ".indexOf('building_presence') >= 0", timeout=20000)
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# the class definitions, against the campaign cribsheet
# ---------------------------------------------------------------------------
def test_the_legend_teaches_the_cribsheets_load_bearing_carve_outs(browser, server):
    """The classes are LUCAS as the campaign cribsheet states them, and three of
    its rules decide most of the arguable points. The app was silent on two and
    contradicted a third.

    * **Ploughing, not grass.** Cropland includes temporary grassland inside a
      rotation and EXCLUDES permanent pasture, which is Nature. This is the
      boundary the ledger says caps change-F1 and the legend did not state it.
    * **Bare ground is Nature unless it is being worked.** Sand and rock at a
      mine or a development are Artificial. This is AL-T's error in one
      sentence -- the largest on the map, and one the model is confident about.
    * **A feature is Artificial for what it is, not what covers it** -- a
      grassed car park is Artificial, a park inside a city is not. The old hint
      said "urban green inside the built fabric", which is the opposite and is
      not in the typology.
    """
    base, _ = server
    page, ctx = open_app(browser, base, who="zed")
    try:
        page.evaluate("document.querySelector('details.legend').open = true")
        body = page.text_content("#legend-body").lower()
        assert "permanent pasture" in body and "rotation" in body
        assert "rough grazing" in body
        assert "mine" in body or "quarry" in body
        assert "what it is, not what covers it" in body
        assert "urban green" not in body
        # A felling is whichever class was growing: B84 makes oil palm, rubber,
        # coffee, tea, cocoa and coconut Cropland, while a stand grown for
        # timber is in no B class and falls to Nature. Stated as one rule --
        # "logging is Nature -> Nature" -- it is wrong half the time, and it
        # hides the transition the campaign exists to count.
        assert "whichever class was growing" in body
        assert "oil palm" in body and "timber plantation" in body
        assert "nature \u2192 cropland" in body
        # ...and a clearance with no class change still has only the flag
        assert "change seen, endpoints match" in body
        # The map marks three different spatial units. Only the 10 m square is
        # labelled; the point, scale square and sampling square are not targets.
        assert "yellow 10 m label square" in body
        assert "majority cover" in body
        assert "not the centre coordinate" in body
        assert "white 100 m square is a scale reference only" in body
        assert "dashed 5 km sampling square provides context only" in body
        assert "call the point" not in body

        hints = page.evaluate("() => CLASSES.map(c => c.key + ': ' + c.hint)")
        nature, crop, art = [h.lower() for h in hints]
        assert "permanent pasture" in nature and "sand" in nature
        assert "rotation" in crop and "orchards" in crop
        # "plantations" alone cannot be read: it is Cropland or Nature by what
        # is planted, so the hints name the side each one falls on
        assert "oil palm" in crop and "rubber" in crop
        assert "timber plantation" in nature
        assert "greenhouses" in art and "3 m" in art
        assert "urban green" not in art
    finally:
        ctx.close()


def test_the_full_definitions_are_one_click_from_the_call(browser, server):
    """A definition nobody can find is a definition nobody reads, and the legend
    fold is closed by default -- so the link is on the summary as well as inside
    it. Overridable per deployment, with a default so a dragged-in copy of app/
    still has it."""
    base, _ = server
    page, ctx = open_app(browser, base, who="zed")
    try:
        href = page.get_attribute("#legend-crib", "href")
        assert href and href.startswith("https://"), href
        assert page.get_attribute("#legend-crib", "target") == "_blank"
        assert "noopener" in (page.get_attribute("#legend-crib", "rel") or "")
        page.evaluate("document.querySelector('details.legend').open = true")
        assert page.evaluate(
            "() => [...document.querySelectorAll('#legend-body a')]"
            ".some(a => a.href === CRIBSHEET_URL)")
        assert page.evaluate("CRIBSHEET_URL") == href
    finally:
        ctx.close()


def test_a_deployment_can_point_the_cribsheet_somewhere_else(browser, server):
    base, _ = server
    cfg = FIXTURE_CONFIG.replace(
        "window.LABEL_APP_CONFIG = {",
        "window.LABEL_APP_CONFIG = {\n  cribsheetUrl: 'https://example.org/crib',")
    page, ctx = open_app(browser, base, who="zed", config_js=cfg)
    try:
        assert page.evaluate("CRIBSHEET_URL") == "https://example.org/crib"
        assert page.get_attribute("#legend-crib", "href") == "https://example.org/crib"
    finally:
        ctx.close()


def test_a_campaign_account_deployment_never_asks_anyone_to_sign_in(
        browser, server):
    """The campaign runs on one service account precisely so that no labeller
    ever sees a Google prompt. The strip note asked anyway.

    It takes a few seconds for that account to come up, and for those seconds
    the first thing on screen said the pictures "need Earth Engine" with a
    Connect button under it -- an instruction to sign in to a deployment
    designed so that nobody does, at the exact moment a new labeller is deciding
    whether the app is working.
    """
    base, _ = server
    page, ctx = open_app(browser, base, who="zed", batch="/baked.json")
    try:
        page.wait_for_function("S.points.length === 3", timeout=15000)
        assert page.evaluate("EE.ready") is False
        # this deployment has a token broker, so a sign-in is the wrong ask
        assert page.evaluate("eeServiceWanted()") is True
        page.evaluate("chipVis.combo = 'NDVI'; applyChipVis();")
        page.wait_for_timeout(400)
        note = page.text_content("#ev-strip-note")
        assert page.evaluate("!document.getElementById('chip-connect')"), note
        assert "sign" not in note.lower(), note
        assert "campaign account" in note, note

        # ...and where a personal sign-in IS what the deployment runs on, the
        # button is still the right thing to offer
        page.evaluate("CFG.eeAuthMode = 'oauth'; applyChipVis();")
        page.wait_for_timeout(400)
        assert page.evaluate("!!document.getElementById('chip-connect')")
    finally:
        ctx.close()


def test_the_strip_note_clears_itself_when_the_account_comes_up(browser, server):
    """The note is written before Earth Engine connects and nothing else redraws
    the strip until the next point, so it sat there telling a connected app that
    its pictures were still loading."""
    base, _ = server
    page, ctx = open_app(browser, base, who="zed", batch="/baked.json")
    try:
        page.wait_for_function("S.points.length === 3", timeout=15000)
        page.evaluate("chipVis.combo = 'NDVI'; applyChipVis();")
        page.wait_for_timeout(400)
        assert page.text_content("#ev-strip-note").strip() != ""
        # eeOnConnected is what every connect path ends in
        page.evaluate("""() => {
          EE.ready = true;
          const orig = window.eeShowLayer, o2 = window.prefetchChips;
          window.eeShowLayer = () => {}; window.prefetchChips = () => {};
          try { eeOnConnected('service'); }
          finally { window.eeShowLayer = orig; window.prefetchChips = o2; } }""")
        page.wait_for_timeout(400)
        note = page.text_content("#ev-strip-note")
        assert "still loading" not in note, note
        assert "campaign account" not in note, note
    finally:
        ctx.close()


def test_wayback_is_a_basemap_and_stays_under_the_overlays(browser, server):
    """The z-order used to change by itself as the interpreter worked.

    `addLayer(l, beforeId)` puts a layer immediately BELOW `beforeId`, i.e.
    immediately ABOVE everything already there, and Wayback and the Earth Engine
    raster both inserted before `labels`. So whichever was touched last won the
    top slot, and both orders happen in ordinary use: pick an overlay, then turn
    the archive on, and Wayback covers the overlay; step to the next point,
    which re-mints the overlay per point, and a 70%-opaque class raster covers
    the whole cell and the imagery with it. "I selected a GEE layer and the
    Wayback image is not appearing", having changed nothing but the point.

    Wayback is a BASEMAP -- it is the imagery the label is made from, not an
    overlay that happens to be imagery -- so it has a slot of its own, declared
    in the style rather than reached for at insertion time.
    """
    base, _ = server
    page, ctx = open_app(browser, base, who="zed", batch="/evidence.json")
    try:
        page.wait_for_function("S.points.length === 3", timeout=15000)
        if not page.evaluate("typeof map !== 'undefined' && !!map"):
            pytest.skip("no WebGL in this browser")
        page.wait_for_function("map.isStyleLoaded && map.isStyleLoaded()",
                               timeout=20000)
        order = "() => map.getStyle().layers.map(l => l.id)"
        ids = page.evaluate(order)
        # the slot exists, above every basemap and below the place labels
        assert "imagery-slot" in ids, ids
        assert ids.index("base-esri") < ids.index("imagery-slot") < ids.index("labels")

        # the archive goes in through its own anchor, exactly as applyRelease does
        add_wb = """() => { map.addSource('wayback-src', { type: 'raster',
              tiles: [location.origin + '/faketile/{z}/{x}/{y}.png'],
              tileSize: 256 });
            map.addLayer({ id: 'wayback-layer', type: 'raster',
              source: 'wayback-src' }, ANCHOR_IMAGERY); }"""
        add_ee = """() => { EE.ready = true;
            const key = Object.keys(EE_LAYERS)[0];
            EE_LAYERS[key].build = () => ({
              image: { getMap: (vis, cb) => cb({ urlFormat: location.origin
                + '/faketile/{z}/{x}/{y}.png' }) }, vis: {} });
            eeSelectLayer(key); }"""

        # EE FIRST, then the archive -- the order that was reported
        page.evaluate(add_ee)
        page.wait_for_function("!!map.getLayer('ee-active')", timeout=20000)
        page.evaluate(add_wb)
        ids = page.evaluate(order)
        assert ids.index("wayback-layer") < ids.index("ee-active"), ids

        # ...and re-minting the overlay, which is what stepping to a point does,
        # no longer flips them back
        page.evaluate("eeShowLayer(S.points[1])")
        page.wait_for_function("!!map.getLayer('ee-active')", timeout=20000)
        ids = page.evaluate(order)
        assert ids.index("wayback-layer") < ids.index("ee-active"), ids
        # everything the interpreter must see is still above both
        # `cell` above all: it is the unit being called, and it replaced the
        # centre dot (`here-dot`), which said "judge this point" in the only
        # language a map has.
        for v in ("footprint", "scalebox", "cell", "here-halo"):
            assert ids.index("ee-active") < ids.index(v), (v, ids)
    finally:
        ctx.close()


def test_the_two_layer_anchors_are_used_the_right_way_round():
    """The invariant, at the two call sites, so a future insertion cannot put an
    overlay under the archive by picking the wrong constant."""
    src = APP_HTML.read_text()
    wb = src[src.index("id: WB_LAYER"):]
    assert "ANCHOR_IMAGERY" in wb[:400] and "ANCHOR_OVERLAY" not in wb[:400]
    ee = src[src.index("id: eeLayerId(), type: 'raster'"):]
    assert "ANCHOR_OVERLAY" in ee[:400]


def test_a_bake_that_is_not_used_says_why_and_offers_the_fix(browser, server):
    """Four ways a bake is silently not used, and all four look identical: a
    strip of flat tints, exactly like no bake at all.

    The one that actually bites is a stale `chipVis.w` in one person's
    localStorage — it disables every sprite in the batch, for that person only,
    and survives a reload. So the strip names the condition and offers the
    width back.

    Also pins the reason the first version of this did nothing: the note wired
    the button and then did `innerHTML +=` for the Earth Engine line, which
    re-parses the subtree and discards the listener. The button rendered
    perfectly and was dead.
    """
    base, _ = server
    page, ctx = open_app(browser, base, who="zed", batch="/baked.json")
    try:
        page.wait_for_function("S.points.length === 3", timeout=15000)
        page.wait_for_function(
            "document.querySelectorAll('#ev-strip .chip.baked').length === 9",
            timeout=15000)
        assert page.text_content("#ev-strip-note").strip() == ""

        # `oauth` so the second note -- the sign-in one -- is offered at all;
        # a service deployment does not ask, which is its own test below.
        page.evaluate("CFG.eeAuthMode = 'oauth'; chipVis.w = 1280;"
                      "applyChipVis();")
        page.wait_for_function(
            "document.querySelectorAll('#ev-strip .chip.baked').length === 0",
            timeout=15000)
        note = page.text_content("#ev-strip-note")
        assert "640" in note and "1280" in note, note
        # ...and both buttons in that note work, which they do not if the
        # second one was appended with `+=`
        assert page.evaluate("!!document.getElementById('chip-connect')")
        page.click("#chip-usebake")
        page.wait_for_function(
            "document.querySelectorAll('#ev-strip .chip.baked').length === 9",
            timeout=15000)
        assert page.evaluate("chipVis.w") == 640
        assert page.text_content("#ev-strip-note").strip() == ""
    finally:
        ctx.close()


def test_a_remembered_width_does_not_survive_into_the_next_batch(browser, server):
    """`chipVis.w` is persisted and the sprite path needs it to equal the bake's
    width EXACTLY, so one nudge of the width slider disables every baked chip in
    every batch, for that browser, across reloads -- and every point from then
    on opens as nine flat tints. The note under the strip says so and offers the
    width back, and a person who moved that slider three points ago has stopped
    reading it.

    Width is a view preference; the bake is the instrument. Opening a batch that
    has one snaps to it. Moving the slider mid-batch is still allowed and still
    drops to live Earth Engine -- what it no longer does is follow the
    interpreter into the next batch."""
    base, _ = server
    ctx = browser.new_context()
    stub_config(ctx, FIXTURE_CONFIG)
    ctx.add_init_script(
        "try { localStorage.setItem('recover-labels:seen-intro','1');"
        "localStorage.setItem('recover-labels:chips',"
        "JSON.stringify({combo:'SWIR1/NIR/GREEN', w: 1280})); } catch (e) {}")
    page = ctx.new_page()
    page.goto(f"{base}/label_app.html?sheetUrl=/mock&batch=/baked.json"
              f"&campaign=test-campaign&manifest=/absent.json&expert=zed",
              wait_until="load")
    try:
        page.wait_for_function("S.points.length === 3", timeout=15000)
        page.evaluate("hideLoading()")
        # the stale width is gone, the control agrees, and the sprites are used
        assert page.evaluate("chipVis.w") == 640
        assert page.evaluate(
            "document.getElementById('chip-w').value") == "640"
        page.wait_for_function(
            "document.querySelectorAll('#ev-strip .chip.baked').length === 9",
            timeout=15000)
        assert page.text_content("#ev-strip-note").strip() == ""
        # ...and it was written back, so the next reload starts here too
        assert '"w":640' in page.evaluate(
            "localStorage.getItem('recover-labels:chips')").replace(" ", "")
        # moving it mid-batch is still the interpreter's call
        page.evaluate("chipVis.w = 800; applyChipVis();")
        page.wait_for_function(
            "document.querySelectorAll('#ev-strip .chip.baked').length === 0",
            timeout=15000)
        assert "800" in page.text_content("#ev-strip-note")
    finally:
        ctx.close()


def test_an_index_scheme_is_reported_as_live_by_design_not_as_a_failure(
        browser, server):
    """Index schemes are NEVER baked, on purpose: one normalised difference
    through a ramp is cheap and does not clip. Reported down the same channel as
    a stale bake it reads as a fault, and the interpreter goes looking for the
    broken thing instead of waiting the few seconds it costs."""
    base, _ = server
    page, ctx = open_app(browser, base, who="zed", batch="/baked.json")
    try:
        page.wait_for_function("S.points.length === 3", timeout=15000)
        page.wait_for_function(
            "document.querySelectorAll('#ev-strip .chip.baked').length === 9",
            timeout=15000)
        page.evaluate("chipVis.combo = 'NDVI'; applyChipVis();")
        page.wait_for_timeout(400)
        note = page.text_content("#ev-strip-note")
        assert "generated when requested" in note, note
        assert "not being used" not in note, note
        # and it is marked as information rather than as a fault
        assert page.evaluate(
            "!!document.querySelector('#ev-strip-note .chip-off.calm')")
        # a real bake miss still reads as one
        page.evaluate("chipVis.combo = 'SWIR1/NIR/GREEN'; chipVis.w = 1280;"
                      "applyChipVis();")
        page.wait_for_timeout(400)
        miss = page.text_content("#ev-strip-note")
        assert "not being used" in miss and "640" in miss and "1280" in miss
    finally:
        ctx.close()


def test_a_strip_that_is_flat_on_purpose_says_so(browser, server):
    """The last way a chip is one colour, and the only one that is CORRECT.

    The three channels share one ramp -- `lo` the darkest of their floors, `hi`
    the brightest of their ceilings -- because a per-band ramp moves hue and a
    narrow one manufactures change, both tested-negative in AL10. Where the
    three bands sit at very different levels (bright bare ground: SWIR1 ~6600,
    NIR ~4900, GREEN ~2800) that ramp is ~4000 DN wide against a per-band spread
    of ~140, so nine years bake as nine identical orange squares. It is the
    right picture of a uniform desert and it is indistinguishable on screen from
    the bake having failed."""
    base, _ = server
    page, ctx = open_app(browser, base, who="zed", batch="/baked.json")
    try:
        page.wait_for_function("S.points.length === 3", timeout=15000)
        page.wait_for_function(
            "document.querySelectorAll('#ev-strip .chip.baked').length === 9",
            timeout=15000)
        # the fixture point has all six bands on one 200-1400 ramp: normal
        assert page.evaluate("chipFlatNote(S.points[0])") is None
        assert page.text_content("#ev-strip-note").strip() == ""

        # now the desert: three bands, far apart, each ~140 DN wide
        page.evaluate("""() => {
          S.batch.chips.stretch['ev000'] = {
            B2: [2600, 2740], B3: [2738, 2874], B4: [3100, 3240],
            B8: [4811, 4951], B11: [6591, 6711], B12: [6800, 6940] };
          renderChips(S.points[0]); }""")
        page.wait_for_timeout(300)
        note = page.text_content("#ev-strip-note")
        assert "the ground is" in note, note
        # the ratio, said as a percentage rather than in digital numbers --
        # 140 of a 3973-wide ramp is 4%, and "DN" is not a word outside remote
        # sensing
        assert "4% of the range" in note, note
        assert page.evaluate(
            "!!document.querySelector('#ev-strip-note .chip-off.calm')")
        # it is a reading of the ramp, so an index scheme -- which has no shared
        # ramp to be crushed by -- is not flagged
        page.evaluate("chipVis.combo = 'NDVI';")
        assert page.evaluate("chipFlatNote(S.points[0])") is None
    finally:
        ctx.close()


def test_the_tint_is_drawn_through_the_points_own_ramp(browser, server):
    """The colour under an unarrived chip has to be the SAME exposure as the
    chip that lands on top of it.

    The fixed 0-3500 bounds are one ramp for a global draw, and they saturate:
    on the first bake of b001, 55 of 900 year-cells were more than half
    saturated, 38 more than half floored, and a quarter of the filmstrip was
    one flat colour. `build_batch_chips.py` now measures a ramp per point and
    writes it into the batch, and BOTH sides have to read it -- the sprite is
    rendered through it in Python, the tint and the live request through it in
    JavaScript. A tint mixed through the global bounds under a sprite baked
    through the point's is two exposures of one pixel, and looks like nothing
    at all going wrong.
    """
    base, _ = server
    page, ctx = open_app(browser, base, who="zed", batch="/evidence.json")
    try:
        page.wait_for_function("S.points.length === 3", timeout=15000)
        # No bake on this batch, so the global ramp: band 1000 of 0-3500.
        assert page.evaluate("chipStretch(S.points[0])") is None
        assert page.evaluate("comboBounds(S.points[0], 'SWIR1/NIR/GREEN').max") == 3500
        plain = page.evaluate("evVisColor(S.points[0], S.points[0].evidence.t, 0)")

        # ...and with the bake, the point's own: band 1000 of 200-1400, which
        # is more than twice as bright.
        page.evaluate("c => { S.batch.chips = c; }", BAKED_CHIPS)
        page.evaluate("S.points[0].id = 'ev000'")
        assert page.evaluate(
            "comboBounds(S.points[0], 'SWIR1/NIR/GREEN').max") == 1400
        toned = page.evaluate("evVisColor(S.points[0], S.points[0].evidence.t, 0)")
        assert toned != plain
        assert int(toned[1:3], 16) > int(plain[1:3], 16) + 60, (toned, plain)

        # A point the bake could not measure -- no clear pixel in any year --
        # keeps the global ramp rather than inventing one.
        page.evaluate("S.points[1].id = 'not-measured'")
        assert page.evaluate("chipStretch(S.points[1])") is None
        assert page.evaluate(
            "comboBounds(S.points[1], 'SWIR1/NIR/GREEN').max") == 3500
    finally:
        ctx.close()


def test_a_baked_batch_is_warmed_whole_not_in_a_window(browser, server):
    """Warming N points ahead is a live-Earth-Engine compromise, not a design.

    Each live point costs 5-40 s of quota, so you buy only what the interpreter
    is about to reach. A baked sprite is one static GET of ~24 KB, and a window
    never covers stepping BACKWARDS, a point revisited after a `?` flag, or the
    second expert opening the same batch cold.
    """
    base, _ = server
    page, ctx = open_app(browser, base, who="zed", batch="/baked.json")
    try:
        page.wait_for_function("S.points.length === 3", timeout=15000)
        page.wait_for_function(
            "document.querySelectorAll('#ev-strip .chip.baked').length === 9",
            timeout=15000)
        # every point in the batch, not the two after this one
        page.wait_for_function(
            "S.points.every(p => prefetchedChips.has("
            "p.id + '|' + chipVis.combo + '|' + chipVis.w))", timeout=15000)
    finally:
        ctx.close()


def test_the_evidence_renders_with_earth_engine_never_signed_in(browser, server):
    """Point values and the timeline are BAKED, and that is the whole design.

    Hosting is static and Earth Engine sign-in is on the critical path for
    imagery only. Losing the chips is acceptable; losing the app is not.
    """
    base, _ = server
    page, ctx = open_app(browser, base, who="zed", batch="/evidence.json")
    try:
        page.wait_for_function("S.points.length === 3", timeout=15000)
        assert page.evaluate("EE.ready") is False

        values = page.text_content("#ev-values")
        assert "Dynamic World 2018" in values and "trees" in values
        assert "WorldCover 2021" in values and "tree cover" in values
        # every dataset shows its end year -- the rule that caught Hansen v1_11
        assert "2018" in values and "2021" in values
        # ...and a dataset that cannot see the far end of the label window is
        # marked as such rather than quietly answering for it. Exactly the two
        # rows that end before 2024, and no others: this is the rule that
        # caught Hansen v1_11 answering a 2024 question with a 2023 band.
        assert page.evaluate(
            "[...document.querySelectorAll('#ev-values .ev-when.stale')]"
            ".map(e => e.textContent.split(' ')[0])") == ["'18", "'21"]
        # the row that CAN see 2024 is not marked
        assert page.evaluate(
            "[...document.querySelectorAll('#ev-values .ev-when')]"
            ".filter(e => !e.classList.contains('stale'))"
            ".map(e => e.textContent.split(' ')[0])") == ["'24"]

        # the timeline drew, with a point per year
        assert page.evaluate(
            "document.querySelectorAll('#ev-chart .ev-dot').length") == 9
        assert page.evaluate("evSeries") == "ndvi"   # not NBR: wrong legend

        # ...and every one of those dots carries the year's colour under the
        # current vis scheme, mixed from the BAKED bands. This is the half of
        # "what did this year look like" that does not wait on Earth Engine.
        # Read the COMPUTED fill. This assertion used to read the `fill`
        # attribute and passed for weeks while every dot on screen was accent
        # blue: in SVG a CSS property beats a presentation attribute, so the
        # stylesheet was repainting a correct attribute (AL11.6b). The colour is
        # now an inline style, which does win.
        assert page.evaluate(
            "[...document.querySelectorAll('#ev-chart .ev-dot')]"
            ".every(e => /^rgb\\(/.test(getComputedStyle(e).fill))")
        accent = page.evaluate(
            "getComputedStyle(document.documentElement)"
            ".getPropertyValue('--accent').trim()")
        fills = page.evaluate(
            "[...document.querySelectorAll('#ev-chart .ev-dot')]"
            ".map(e => getComputedStyle(e).fill)")
        assert len(set(fills)) > 1, (
            f"every dot the same colour ({fills[0]}) -- the stylesheet is "
            f"painting over the computed colour again; accent is {accent}")
        # the filmstrip is there too, tinted the same way, with the note about
        # the missing connection BESIDE it rather than in place of it
        assert page.evaluate(
            "document.querySelectorAll('#ev-strip .chip.tinted').length") == 9

        # the spectral profile reads the same payload, no further call, and is
        # OPEN: it is the instrument for the Cropland / Nature boundary the
        # ledger says caps change-F1, and it used to be behind a fold.
        assert page.evaluate(
            "document.querySelectorAll('#ev-spectral .spec-line').length") == 9

        # MEASUREMENT FIRST, OPINIONS LAST, and collapsed. What is being
        # labelled is training data for a 10 m model, so the reading that
        # decides the call is the one made at 10 m from the sensor. Twenty-three
        # rows of other people's confident classifications at the TOP of the
        # panel is an anchor: the interpreter spends their attention agreeing
        # with Dynamic World instead of looking at the point.
        order = page.evaluate(
            "['ev-spectral', 'ev-chart', 'ev-values'].map(id => "
            "[...document.querySelectorAll('#sec-evidence *')]"
            ".indexOf(document.getElementById(id)))")
        assert order == sorted(order) and -1 not in order, (
            f"evidence panel is out of order: {order}")
        assert page.evaluate(
            "document.getElementById('ev-values-fold').open") is False

        # ...and the filmstrip says what is missing rather than looking broken.
        # The note is BESIDE the coloured strip, not in place of it: replacing
        # the strip threw away a real reading of the same nine years in order
        # to report a missing connection. Asserted on the note ELEMENT rather
        # than on the words "Earth Engine": on a campaign-account deployment the
        # right sentence never names it, because there is nothing the labeller
        # is being asked to do about it.
        assert page.text_content("#ev-strip-note").strip() != ""
        assert page.evaluate(
            "document.getElementById('ev-film')"
            ".contains(document.getElementById('ev-strip-note'))")
        assert page.evaluate(
            "document.querySelectorAll('#ev-strip .chip').length") == 9
    finally:
        ctx.close()


def test_selecting_a_year_is_the_sync_hub(browser, server):
    """One year, one meaning, everywhere: chart, spectra, filmstrip, Wayback
    release and the `When did it change?` answer."""
    base, _ = server
    page, ctx = open_app(browser, base, who="zed", batch="/evidence.json")
    try:
        page.wait_for_function("S.points.length === 3", timeout=15000)
        page.keyboard.press("a")
        page.keyboard.press("c")                       # a change, so the year opens
        page.wait_for_timeout(150)
        assert page.evaluate(
            "getComputedStyle(document.getElementById('sec-year')).display") != "none"

        page.evaluate("selectEvYear(2021)")
        page.wait_for_timeout(200)
        assert page.evaluate("evSelYear") == 2021
        assert page.evaluate("cur.year") == "2021"
        assert page.evaluate(
            "document.querySelectorAll('#ev-chart .ev-dot.sel').length") == 1
    finally:
        ctx.close()


def test_the_recorded_imagery_describes_the_point_not_the_last_click(browser, server):
    """`imagery_a` / `imagery_b` are the provenance of the CALL.

    They used to follow whatever the interpreter last clicked, so somebody
    comparing a neighbouring field recorded that field's capture dates as the
    provenance of their own call -- defeating the exact purpose those two
    columns exist for.
    """
    base, _ = server
    page, ctx = open_app(browser, base, who="yaz", batch="/solo-click.json")
    try:
        page.wait_for_function("wb.pt !== null", timeout=15000)
        at_point = page.evaluate("({lng: wb.pt.lng, lat: wb.pt.lat})")
        assert at_point == {"lng": page.evaluate("S.points[S.i].lon"),
                            "lat": page.evaluate("S.points[S.i].lat")}
        # a click somewhere else moves the inspection point and nothing else
        page.evaluate("wb.clickPt = {lng: 99.0, lat: 1.0}")
        after = page.evaluate("({lng: wb.pt.lng, lat: wb.pt.lat})")
        assert after == at_point
        # ...and moving to the next point clears the inspection read-out
        page.evaluate("goTo(1)")
        page.wait_for_timeout(200)
        assert page.evaluate("wb.clickPt") is None
        assert page.evaluate("wb.pt.lng") == page.evaluate("S.points[1].lon")
    finally:
        ctx.close()


def test_the_queues_are_a_property_of_the_batch_file(browser, server):
    """Correct with no network at all.

    Two experts opening a batch offline used to both see every point as
    available, and the ~5% deliberate overlap depended on somebody remembering a
    checkbox in the right direction.
    """
    base, _ = server
    page, ctx = open_app(browser, base, who="bo", batch="/assigned.json")
    try:
        page.wait_for_function("S.points.length === 3", timeout=15000)
        # bo owns a001; a002 is bo's second reading; a000 is not bo's at all
        assert page.evaluate("S.points.map(p => queueOf(p))") == \
            [None, "mine", "second"]
        assert page.evaluate("queueCounts()") == \
            {"mine": 1, "second": 1, "unassigned": 0}
        assert page.evaluate("S.points.filter(p => outstanding(p)).map(p => p.id)") \
            == ["a001"]
        page.evaluate("setQueue('second')")
        page.wait_for_timeout(200)
        assert page.evaluate("S.points.filter(p => outstanding(p)).map(p => p.id)") \
            == ["a002"]
        assert "Independent overlap sample" in page.text_content("#second-banner")
    finally:
        ctx.close()


def test_an_expert_the_batch_does_not_name_is_told_so(browser, server):
    """Rather than shown "0 / 0" and then "batch complete — 0 of 3 labelled",
    which reads as a claim about the batch instead of about the link."""
    base, _ = server
    page, ctx = open_app(browser, base, who="cass", batch="/assigned.json")
    try:
        page.wait_for_function("S.points.length === 3", timeout=15000)
        assert page.evaluate("queueCounts()") == \
            {"mine": 0, "second": 0, "unassigned": 0}
        assert "assigned to you" in page.text_content("#queue-note").lower()
        page.evaluate("announceDone()")
        page.wait_for_timeout(200)
        report = page.text_content("#loading")
        assert "assigned to you" in report.lower()
        assert "complete" not in report.lower()
    finally:
        ctx.close()


# ── the view is addressable, and the unit is a pixel ────────────────────────
# Two experts settle a disagreement by looking at the same thing. Until the
# link carried the point and the scheme, the most this app could hand over was
# a batch -- and the thing being disagreed about was drawn as a square that was
# not any pixel of the imagery it was drawn on.

def test_a_link_opens_the_point_and_the_view_it_names(browser, server):
    base, _ = server
    page, ctx = open_app(browser, base,
                         extra="&point=t004&scheme=NIR/RED/GREEN&w=320")
    try:
        assert page.evaluate("S.points[S.i].id") == "t004"
        assert page.evaluate("chipVis.combo") == "NIR/RED/GREEN"
        assert page.evaluate("chipVis.w") == 320
        # ...and the rest of the link survives the rewrite, or the next reload
        # of the URL the interpreter is now looking at loads no batch at all.
        url = page.evaluate("location.search")
        assert "batch=" in url and "campaign=test-campaign" in url
    finally:
        ctx.close()


def test_the_link_follows_the_interpreter(browser, server):
    """Stepping to a point rewrites the URL, so the link to send is always the
    one in the address bar."""
    base, _ = server
    page, ctx = open_app(browser, base)
    try:
        page.evaluate("goTo(6)")
        assert "point=t006" in page.evaluate("location.search")
        page.evaluate("chipVis.combo = 'RED/GREEN/BLUE'; applyChipVis()")
        assert "scheme=RED%2FGREEN%2FBLUE" in page.evaluate("location.search")
    finally:
        ctx.close()


def test_a_link_to_a_point_this_batch_does_not_have_still_opens(browser, server):
    """A stale link is a link to the wrong round, not a reason to show nothing:
    it resumes where the interpreter was, and the URL corrects itself."""
    base, _ = server
    page, ctx = open_app(browser, base, extra="&point=not-in-this-batch")
    try:
        assert page.evaluate("S.points.length") > 0
        assert "point=t000" in page.evaluate("location.search")
    finally:
        ctx.close()


def test_the_square_drawn_is_the_sentinel_pixel(browser, server):
    """`s2Cell` in the page against `label_cell.cell` in Python — the same
    check `tests/test_label_cell.py` runs in node, but through the app's own
    `pointCell`, which is what actually reaches the map."""
    sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
    py_cell = pytest.importorskip("label_cell")
    base, _ = server
    page, ctx = open_app(browser, base)
    try:
        got = page.evaluate("(() => { const p = S.points[0];"
                            " return { ring: pointCell(p).ring,"
                            "          epsg: pointCell(p).epsg,"
                            "          lon: p.lon, lat: p.lat }; })()")
        want = py_cell.cell(got["lon"], got["lat"])
        assert got["epsg"] == want["epsg"]
        for a, b in zip(got["ring"], want["ring"]):
            assert abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) < 1e-6
        # The property that is the whole point: the point is INSIDE the square
        # and is not its centre.
        xs = [c[0] for c in got["ring"]]
        ys = [c[1] for c in got["ring"]]
        assert min(xs) <= got["lon"] <= max(xs)
        assert min(ys) <= got["lat"] <= max(ys)
        assert abs((min(xs) + max(xs)) / 2 - got["lon"]) > 1e-9
    finally:
        ctx.close()


def test_a_baked_cell_is_drawn_rather_than_recomputed(browser, server):
    """The bake is the authority: the builders reduce over it, and a page that
    quietly preferred its own arithmetic would draw a square nothing measured."""
    base, _ = server
    page, ctx = open_app(browser, base)
    try:
        ring = [[10.0, 60.0], [10.001, 60.0], [10.001, 60.001],
                [10.0, 60.001], [10.0, 60.0]]
        got = page.evaluate(
            "(r) => pointCell({ lon: 10.5, lat: 60.5,"
            "                   cell: { epsg: 32632, ring: r } }).ring", ring)
        assert got == ring
    finally:
        ctx.close()


# ── the light / dark chrome ─────────────────────────────────────────────────
# Light is the default and dark is a toggle. The failure mode this pair guards
# is not "the toggle does nothing" -- that is visible in a second -- but the
# quiet one: a rule that hard-codes a colour has no dark reading, so a single
# panel stays white inside an otherwise dark app and nobody notices until
# somebody labels at night. The stylesheet's own comment claims every rule is
# written against the tokens; these assert the claim.

#: Tokens the dark block deliberately does NOT restate, and why. The three
#: class swatches are MAP colours -- they match the raster, the chips and the
#: legend, and a swatch that means one thing on the imagery and another in the
#: panel is worse than a swatch that is slightly dark for its ground.
#: `--glass-blur` is not a colour at all: it is the frost itself, and both
#: themes are the same sheet of glass over the same map.
INHERITED_BY_DARK = {"--nature", "--cropland", "--artificial", "--brand-ramp",
                     "--glass-blur"}


def _theme_blocks():
    src = APP_HTML.read_text()
    style = src[src.index("<style>"):src.index("</style>")]
    style = re.sub(r"/\*.*?\*/", "", style, flags=re.S)

    def tokens(head):
        body = style[style.index(head) + len(head):]
        return set(re.findall(r"(--[a-z0-9-]+)\s*:", body[:body.index("}")]))

    return tokens(":root {"), tokens('html[data-theme="dark"] {')


def test_every_token_has_a_dark_reading():
    light, dark = _theme_blocks()
    assert dark - light == set(), (
        f"the dark block invents tokens the light block never defines: "
        f"{sorted(dark - light)} -- light is the default, so these are "
        f"undefined until somebody turns dark on")
    missing = light - dark - INHERITED_BY_DARK
    assert missing == set(), (
        f"{sorted(missing)} are defined only for the light theme. Either give "
        f"each a dark value or add it to INHERITED_BY_DARK with the reason it "
        f"is the same colour in both.")


def test_the_toggle_round_trips_and_survives_a_reload(browser, server):
    base, _ = server
    page, ctx = open_app(browser, base, who="e1")
    try:
        html = page.locator("html")
        ground = lambda: page.evaluate(
            "getComputedStyle(document.body).backgroundColor")
        light_ground = ground()
        assert html.get_attribute("data-theme") is None      # light is bare :root

        page.click("#btn-theme")
        assert html.get_attribute("data-theme") == "dark"
        dark_ground = ground()
        assert dark_ground != light_ground, (
            f"the attribute flipped and nothing repainted ({dark_ground})")
        assert page.evaluate(
            "localStorage.getItem('recover-labels:theme')") == "dark"

        # ...and it is applied in the head, before the first paint: a theme
        # restored in initChrome() is a white flash on every load, on the
        # imagery, which is the thing being looked at.
        page.reload(wait_until="load")
        assert page.locator("html").get_attribute("data-theme") == "dark"
        assert page.evaluate("document.readyState") in ("interactive", "complete")

        page.click("#btn-theme")
        assert page.locator("html").get_attribute("data-theme") is None
        assert page.evaluate(
            "localStorage.getItem('recover-labels:theme')") == "light"
    finally:
        ctx.close()
