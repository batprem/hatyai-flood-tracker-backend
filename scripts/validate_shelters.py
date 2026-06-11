"""Validate the Hat Yai shelter dataset against the U-Tapao basin polygon.

Checks, for every feature in ``data/shelters_hatyai.geojson``:

* the geometry is a Point with lon/lat in plausible ranges,
* required provenance properties are present and non-empty,
* the point falls inside the basin polygon in ``data/basin_utapao.geojson``
  (points outside the basin are reported with their distance so an operator
  can decide whether an ``accuracy_note`` flag is acceptable).

Run from the backend repo root:

    uv run python scripts/validate_shelters.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from shapely.geometry import Point, shape

REQUIRED_PROPS = (
    "name_th",
    "name_en",
    "type",
    "source",
    "source_url",
    "license",
    "retrieved_date",
)
ALLOWED_TYPES = {
    "school",
    "university",
    "temple",
    "mosque",
    "community_center",
    "other",
}
DEG_TO_KM = 111.0


def main() -> int:
    """Validate shelter features and report basin containment.

    Returns:
        Process exit code: 0 when every check passes, 1 otherwise.
    """
    root = Path(__file__).resolve().parent.parent
    basin_gj = json.loads((root / "data" / "basin_utapao.geojson").read_text())
    shelters_gj = json.loads((root / "data" / "shelters_hatyai.geojson").read_text())

    basin = shape(basin_gj["features"][0]["geometry"])
    features = shelters_gj.get("features", [])
    failures = 0

    print(f"shelters: {len(features)} features")
    for feat in features:
        props = feat.get("properties", {})
        name = props.get("name_en", "<unnamed>")

        missing = [k for k in REQUIRED_PROPS if not props.get(k)]
        if missing:
            failures += 1
            print(f"FAIL {name}: missing properties {missing}")

        if props.get("type") not in ALLOWED_TYPES:
            failures += 1
            print(f"FAIL {name}: type {props.get('type')!r} not in {sorted(ALLOWED_TYPES)}")

        geom = feat.get("geometry", {})
        if geom.get("type") != "Point":
            failures += 1
            print(f"FAIL {name}: geometry type {geom.get('type')!r}, expected Point")
            continue
        lon, lat = geom["coordinates"][:2]
        if not (99.0 <= lon <= 102.0 and 5.5 <= lat <= 8.5):
            failures += 1
            print(f"FAIL {name}: coordinates ({lon}, {lat}) outside Songkhla region")
            continue

        point = Point(lon, lat)
        if basin.contains(point):
            dist_km = basin.exterior.distance(point) * DEG_TO_KM
            print(f"OK   {name}: inside basin ({dist_km:.2f} km from boundary)")
        else:
            failures += 1
            dist_km = point.distance(basin) * DEG_TO_KM
            print(f"FAIL {name}: OUTSIDE basin by {dist_km:.2f} km")

    if len(features) < 5:
        failures += 1
        print(f"FAIL dataset: only {len(features)} shelters, minimum is 5")

    print(f"result: {'PASS' if failures == 0 else f'FAIL ({failures} problems)'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
