"""The labelling unit: the one Sentinel-2 10 m pixel a point falls in.

WHY THIS IS A MODULE AND NOT A CONSTANT
---------------------------------------
The call an interpreter makes is *majority cover of a 10 m cell*. A square
centred on the point is a different square from a source pixel: it can straddle
four Sentinel-2 pixels and cover no one of them. The application, evidence and
downstream model must therefore use the same snapped footprint.

There is exactly one square that removes the ambiguity, and it is not a choice:
the pixel itself. Sentinel-2 granules use the UTM grid of their MGRS tile, with
10 m pixel edges on multiples of 10 m in that CRS. Snapping the point's UTM
coordinates down to a multiple of 10 therefore identifies the same square in
the imagery, the extracted evidence and compatible model output.

The point survives only as the *address* of that pixel. Nothing should read a
buffer around it again.

THIS DEFINITION IS MIRRORED IN JAVASCRIPT
-----------------------------------------
``s2Cell()`` in ``app/label_app.html`` is this file, in JS, because the app must
draw the cell for a batch it built itself (baked, below) *and* for a file
dropped on the window (computed). ``tests/test_label_cell.py`` runs the JS in
node against this module: same zone, same snap, corners inside a centimetre.
Where a batch carries a baked ``cell`` the app draws that, so the field never
depends on the two agreeing -- the test is what keeps them worth trusting.

WHERE THE GRID IS GENUINELY AMBIGUOUS
-------------------------------------
Sentinel-2 tiles overlap, and in the overlap a point sits in two granules whose
UTM zones can differ; the two pixel grids are then rotated relative to each
other and no square is "the" pixel. The zone rule below is MGRS's own, including
the 32V and Svalbard exceptions, so it agrees with the granule the composite is
dominated by almost everywhere. It is not worth more than that, and a 0.45 m
disagreement at a zone edge is inside what the interpreter can see anyway.
"""
from __future__ import annotations

#: The edge of the labelling cell, in metres. Not a radius.
CELL_M = 10.0


def utm_epsg(lon: float, lat: float) -> int:
    """The EPSG code of the UTM zone MGRS puts this point in."""
    zone = int((lon + 180.0) // 6.0) + 1
    # The two MGRS exceptions: 32V and the Svalbard row.
    if 56.0 <= lat < 64.0 and 3.0 <= lon < 12.0:
        zone = 32
    elif 72.0 <= lat < 84.0:
        if 0.0 <= lon < 9.0:
            zone = 31
        elif 9.0 <= lon < 21.0:
            zone = 33
        elif 21.0 <= lon < 33.0:
            zone = 35
        elif 33.0 <= lon < 42.0:
            zone = 37
    zone = min(max(zone, 1), 60)
    return (32600 if lat >= 0 else 32700) + zone


def cell(lon: float, lat: float, epsg: int | None = None) -> dict:
    """The pixel containing (lon, lat), as a lon/lat ring.

    Returns ``{"epsg", "x0", "y0", "ring"}``. The ring is the four corners
    transformed back one at a time and NOT a lon/lat bounding box: grid
    convergence rotates the square by up to ~3 degrees at a zone edge, which is
    0.45 m over a 10 m cell -- 4.5% of the thing being judged.
    """
    from pyproj import Transformer

    epsg = int(epsg or utm_epsg(lon, lat))
    fwd = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    inv = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    x, y = fwd.transform(lon, lat)
    x0 = (x // CELL_M) * CELL_M
    y0 = (y // CELL_M) * CELL_M
    corners = [(x0, y0), (x0 + CELL_M, y0), (x0 + CELL_M, y0 + CELL_M),
               (x0, y0 + CELL_M)]
    ring = [[round(v, 8) for v in inv.transform(cx, cy)] for cx, cy in corners]
    ring.append(list(ring[0]))
    return {"epsg": epsg, "x0": x0, "y0": y0, "ring": ring}


def cell_geometry(ee, lon: float, lat: float, epsg: int | None = None):
    """The same cell as an ``ee.Geometry``, planar, for painting and reducing."""
    return ee.Geometry.Polygon([cell(lon, lat, epsg)["ring"]], None, False)
