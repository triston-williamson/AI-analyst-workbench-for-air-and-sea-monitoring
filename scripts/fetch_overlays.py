"""Download undersea infrastructure from EMODnet Human Activities and write a compact overlay.

    python scripts/fetch_overlays.py

Source: EMODnet Human Activities WFS (https://emodnet.ec.europa.eu/en/human-activities),
public EU open data, no fees or access constraints. Attribution is shown on the map.

The raw layers are large (the German power-cable layer alone is ~20 MB of survey-grade
geometry), so lines are simplified (Douglas-Peucker, ~100 m tolerance) and coordinates
rounded. The result is committed at src/watchfloor/dashboard/data/infrastructure.json so the
dashboard needs no network access to draw it.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from watchfloor.config import get_settings  # noqa: E402

WFS = "https://ows.emodnet-humanactivities.eu/wfs"
OUT = Path(__file__).resolve().parents[1] / "src/watchfloor/dashboard/data/infrastructure.json"
TOLERANCE_DEG = 0.001  # ~100 m

# layer -> (kind, name property, keep(feature props) -> bool)
LAYERS = {
    "pipelines": (
        "Pipeline",
        "name",
        lambda p: (
            p.get("status") in ("Active", "Not in use", "n/a", None)
            and p.get("medium") not in ("Sewage", "Water", "Geothermal Heating")
        ),
    ),
    "bshcontiscables": ("Telecom cable", "name_", lambda p: True),
    "pcablesbshcontis": ("Power cable", "name_", lambda p: True),
    "pcablesnve": ("Power cable", "navn", lambda p: True),
}


def _perp_dist(p, a, b) -> float:
    (x, y), (x1, y1), (x2, y2) = p, a, b
    dx, dy = x2 - x1, y2 - y1
    if dx == dy == 0:
        return math.hypot(x - x1, y - y1)
    t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - (x1 + t * dx), y - (y1 + t * dy))


def simplify(points: list[list[float]], tol: float = TOLERANCE_DEG) -> list[list[float]]:
    """Iterative Douglas-Peucker line simplification."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        s, e = stack.pop()
        best, idx = 0.0, None
        for i in range(s + 1, e):
            d = _perp_dist(points[i], points[s], points[e])
            if d > best:
                best, idx = d, i
        if idx is not None and best > tol:
            keep[idx] = True
            stack += [(s, idx), (idx, e)]
    return [p for p, k in zip(points, keep, strict=True) if k]


def lines_of(geometry: dict) -> list[list[list[float]]]:
    if geometry["type"] == "LineString":
        return [geometry["coordinates"]]
    if geometry["type"] == "MultiLineString":
        return geometry["coordinates"]
    return []


def main() -> None:
    min_lat, min_lon, max_lat, max_lon = get_settings().aoi_bbox
    bbox = f"{min_lat},{min_lon},{max_lat},{max_lon},urn:ogc:def:crs:EPSG::4326"
    features = []
    with httpx.Client(timeout=180) as http:
        for layer, (kind, name_key, keep) in LAYERS.items():
            resp = http.get(
                WFS,
                params={
                    "service": "WFS",
                    "version": "2.0.0",
                    "request": "GetFeature",
                    "typeNames": f"emodnet:{layer}",
                    "outputFormat": "application/json",
                    "srsName": "EPSG:4326",
                    "bbox": bbox,
                },
            )
            resp.raise_for_status()
            kept = 0
            for f in resp.json()["features"]:
                props = f.get("properties") or {}
                if not f.get("geometry") or not keep(props):
                    continue
                for line in lines_of(f["geometry"]):
                    # GeoServer's GeoJSON output is lon,lat (GeoJSON order), even though the
                    # WFS 2.0 bbox parameter above is lat,lon. Stored as [lon, lat].
                    pts = [[round(c[0], 4), round(c[1], 4)] for c in line]
                    pts = simplify(pts)
                    if len(pts) >= 2:
                        features.append(
                            {
                                "kind": kind,
                                "name": (props.get(name_key) or "").strip(),
                                "coords": pts,
                            }
                        )
                        kept += 1
            print(f"{layer:<18} {kind:<14} {kept} line(s)")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "attribution": "Infrastructure: EMODnet Human Activities (EU open data)",
        "bbox": [min_lat, min_lon, max_lat, max_lon],
        "features": features,
    }
    OUT.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {OUT.name}: {len(features)} lines, {OUT.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
