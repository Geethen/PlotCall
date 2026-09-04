"""The chip display ramp, and the two places it is written down.

The bake renders a sprite through `combo_bounds()` in Python; the app paints the
tint under that sprite, and every dot on the annual chart, through
`comboBounds()` in JavaScript. They are one contract, and a disagreement is
invisible in exactly the way this project keeps getting caught by: the picture
still appears, it is just a different picture from the colour behind it.

So this does not re-implement the JavaScript in Python -- §AL8's lesson is that
a Python double polices a contract the JavaScript may not have signed. It runs
the app's own function, extracted from `label_app.html`, in node.

Skips cleanly where there is no node, so `pytest -q` stays green on a bare
checkout.
"""
from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
APP = ROOT / "app" / "label_app.html"
sys.path.insert(0, str(ROOT / "src"))

import build_batch_chips as C          # noqa: E402
import build_batch_dense as D          # noqa: E402

NODE = shutil.which("node") or shutil.which("nodejs")


def _js_const(name: str) -> str:
    """The literal a top-level `const NAME = ...;` is assigned."""
    m = re.search(rf"^const {re.escape(name)} = (.+?);\s*$",
                  APP.read_text(), re.M)
    assert m, f"{name} not found in label_app.html"
    return m.group(1)


def _js_block(start: str) -> str:
    """Source from `start` to the first line that is just `}` or `};`.

    The app indents everything inside a top-level declaration, so a closing
    brace at column 0 ends it. `};` as well as `}`: an object literal closes
    with the semicolon, and matching only `}` ran straight past CHIP_RGB into
    the end of the next function.
    """
    text = APP.read_text()
    i = text.index(start)
    m = re.compile(r"^\};?$", re.M).search(text, i)
    assert m, f"no closing brace for {start!r}"
    return text[i:m.end()]


def test_the_bake_version_the_app_looks_for_is_the_one_the_baker_writes():
    """A mismatch is not cosmetic: chip1 sprites were rendered through the
    fixed global bounds, and painting a chip2 tint under one shows two
    different exposures of the same pixel."""
    assert _js_const("CHIP_BAKE_VERSION") == f"'{C.CHIP_BAKE_VERSION}'"
    assert _js_const("DENSE_BAKE_VERSION") == f"'{D.DENSE_BAKE_VERSION}'"


def test_the_batch_keys_the_bakers_write_survive_parsebatch():
    """`parseBatch` is an ALLOW-LIST.

    A batch-level key it does not name is dropped without a word, and the
    feature reverts to its fallback -- live Earth Engine, or nothing. Caught
    once already on `chips`; `dense` is the same shape of mistake.
    """
    src = _js_block("function parseBatch(text, name) {")
    for key in ("chips", "dense", "evidence_schema", "evidence_version"):
        assert f"{key}: obj.{key}" in src, f"parseBatch drops batch['{key}']"


def test_a_rebake_busts_the_cache():
    """A re-bake writes new bytes to the SAME path.

    Without a stamp on the URL, every browser and CDN that already fetched a
    sprite keeps serving the old one -- and from a fresh profile everything
    looks correct, so the report comes back as "it still looks the same" with
    nothing to see on this side.
    """
    src = APP.read_text()
    assert "'?v=' + b.built" in src, "the sprite URL carries no bake stamp"
    for mod in (C, D):
        assert '"built": int(time.time())' in (
            Path(mod.__file__).read_text()), f"{mod.__name__} writes no stamp"


def test_the_fallback_says_which_condition_it_failed():
    """Four ways a bake is silently not used, and a strip of flat tints looks
    identical for all of them -- and identical to no bake at all."""
    src = _js_block("function chipBakeMiss(p) {")
    for cond in ("version", "S.batchUrl", "combos", "width_m"):
        assert cond in src, f"chipBakeMiss does not name {cond}"
    assert "chipBakeMiss(p)" in _js_block("function liveChips(p, years, gen, why) {")


def test_the_min_span_guard_is_the_same_number_on_both_sides():
    assert float(_js_const("STRETCH_MIN_SPAN")) == C.STRETCH_MIN_SPAN


def test_the_app_and_the_baker_agree_on_the_combos_they_can_bake():
    """A scheme the app thinks is baked and the baker never renders is a 404
    per point, which degrades to live Earth Engine -- silently, and only for
    whoever picked that scheme."""
    rgb = _js_block("const CHIP_RGB = {")
    for combo in C.COMBOS:
        assert f"'{combo}'" in rgb, f"{combo} is bakeable but not in CHIP_RGB"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_two_ramps_are_the_same_ramp():
    """`combo_bounds` (Python, bakes the sprite) against `comboBounds`
    (JavaScript, paints the tint and drives the live request), on random
    stretch tables including the degenerate ones the guard exists for."""
    src = "\n".join([
        _js_block("const CHIP_RGB = {"),
        _js_block("const CHIP_INDEX = {"),
        "const STRETCH_MIN_SPAN = " + _js_const("STRETCH_MIN_SPAN") + ";",
        "function chipIsIndex(c) { return c in CHIP_INDEX; }",
        _js_block("function chipSpec(combo) {"),
        # The app reads the table off S.batch; the test hands it in directly.
        "let STRETCH = null;",
        "function chipStretch() { return STRETCH; }",
        _js_block("function comboBounds(p, combo) {"),
        "const cases = JSON.parse(process.argv[2]);",
        "const out = cases.map(c => { STRETCH = c.stretch;",
        "  const b = comboBounds({}, c.combo);",
        "  return [b.min, b.max]; });",
        "console.log(JSON.stringify(out));",
    ])

    rng = random.Random(11)
    cases = []
    for _ in range(300):
        combo = rng.choice(sorted(C.COMBOS))
        if rng.random() < 0.15:
            stretch = None                                  # no bake
        else:
            stretch = {}
            for band in C.S2_BANDS:
                if rng.random() < 0.08:
                    stretch[band] = None                    # no clear pixel
                else:
                    lo = rng.uniform(0, 6000)
                    stretch[band] = [round(lo, 1),
                                     round(lo + rng.choice([2, 40, 300, 3000]), 1)]
        cases.append({"combo": combo, "stretch": stretch})

    script = Path(__file__).parent / "_ramp.mjs"
    script.write_text(src)
    try:
        got = json.loads(subprocess.run(
            [NODE, str(script), json.dumps(cases)],
            capture_output=True, text=True, check=True).stdout)
    finally:
        script.unlink()

    for case, js in zip(cases, got):
        _, lo, hi = C.combo_bounds(case["combo"], case["stretch"])
        assert [lo, hi] == pytest.approx(js, abs=1e-6), (
            f"{case['combo']} with {case['stretch']}: python {(lo, hi)} "
            f"vs javascript {tuple(js)}")


def test_the_dense_recipe_is_the_same_recipe():
    """`series_for` is `denseFetchLive` in Python. The sidecar it bakes is
    served to half the batch and the live call to the other half, so a drift
    here puts two different measurements on one chart."""
    live = _js_block("function denseFetchLive(p) {")
    assert f"'CLOUDY_PIXEL_PERCENTAGE', {D.CLOUDY_MAX}" in live
    assert f"lt({D.CLDPRB_MAX})" in live
    # The labelling cell is the PIXEL, and the read that returns it is
    # `reduceRegion` over the point at `SCALE_M` -- no geometry to get wrong,
    # on either side. A buffer reappearing here is the drift.
    assert f"reduceRegion(ee.Reducer.mean(), pt, {D.SCALE_M})" in live
    assert "buffer(" not in live
    assert f", {D.SCALE_M})" in live
    for band in D.BANDS:
        assert f"'{band}'" in live
    assert "MSK_CLDPRB" in live and "s2cloudless" not in live.lower(), (
        "the dense series must not use the s2cloudless join -- it made a "
        "first attempt take 103 s for 269 scenes")


def test_the_bake_versions_agree_across_the_two_languages():
    """The builder stamps a version and the app pins one. A bump applied to
    only one of them is silent in the worst way: every sprite and sidecar in
    the campaign falls back to live Earth Engine, which with nobody signed in
    is a strip of flat colour swatches -- the exact appearance of a bake that
    was never run. The deploy workflow checks the same pair, but this is the
    one that fails in a second rather than at deploy time."""
    for name, module in (("CHIP_BAKE_VERSION", C), ("DENSE_BAKE_VERSION", D)):
        assert _js_const(name).strip("'") == getattr(module, name), name


def _stub_batch(root: Path, *, version: str, schemes: tuple[str, ...]) -> Path:
    """A batch with sprites already on disk for each of `schemes`."""
    points = [{"id": f"p{i:04d}", "lon": 10.0 + i, "lat": 60.0} for i in range(3)]
    batch = {
        "campaign": "t", "batch_id": "b999", "points": points,
        "evidence_schema": {"timeline": {"years": [2018, 2024]}},
        "chips": {"version": version, "dir": "b999_chips",
                  "years": [2018, 2024], "cell": C.CELL, "width_m": 640.0,
                  "combos": sorted(schemes), "format": "webp"},
    }
    for scheme in schemes:
        d = root / "b999_chips" / C.slug(scheme)
        d.mkdir(parents=True)
        for p in points:
            (d / f"{p['id']}.webp").write_bytes(b"stale")
    path = root / "b999.json"
    path.write_text(json.dumps(batch, indent=1))
    return path


def test_a_version_bump_rebakes_every_scheme_not_just_the_first(tmp_path,
                                                               monkeypatch,
                                                               capsys):
    """The four schemes are baked in ONE process, and the batch is carried
    between them so scheme two reads scheme one's cached ramp. That sharing is
    what made this silent: scheme one's returned meta stamps
    `batch["chips"]["version"]` with the new version, so schemes two, three and
    four re-read it and decide their old sprites are current.

    The batch then ships stamped `chip3` with three quarters of its sprites
    drawn by `chip2` -- and the app serves them, because the stamp is the only
    thing it can check. It is worse than no bake at all: a fallback is visible
    (`chipBakeMiss`), this shows the interpreter the previous footprint with
    nothing anywhere saying so. Caught on the chip2 -> chip3 re-bake of b001,
    which had put the old red ring next to the new map cell for three of the
    four schemes.

    `--dry-run` cannot see this. It returns before the meta that carries the
    stamp forward exists, so every scheme reads the on-disk version and the bug
    is invisible -- which is why Earth Engine is stubbed here instead.
    """
    schemes = ("SWIR1/NIR/GREEN", "NIR/RED/GREEN", "RED/GREEN/BLUE")
    path = _stub_batch(tmp_path, version="chip_old", schemes=schemes)

    drawn = []

    def fake_bake_one(ee, point, years, combo, width_m, cell, out, fmt,
                      quality, stretch):
        drawn.append((C.slug(combo), point["id"]))
        out.write_bytes(b"fresh")
        return out, 5

    monkeypatch.setattr(C, "_ee", lambda: object())
    monkeypatch.setattr(C, "stretches", lambda *a, **k: {})
    monkeypatch.setattr(C, "bake_one", fake_bake_one)
    monkeypatch.setattr(sys, "argv",
                        ["build_batch_chips.py", "--batch", str(path),
                         "--combo", *schemes])
    C.main()

    out = capsys.readouterr().out
    assert capsys and out.count("-- re-baking all") == len(schemes), out
    for scheme in schemes:
        n = sum(1 for slug, _ in drawn if slug == C.slug(scheme))
        assert n == 3, f"{scheme} re-baked {n}/3 points on a version bump\n{out}"
    assert json.loads(path.read_text())["chips"]["version"] == C.CHIP_BAKE_VERSION
