"""Distance from a position to the nearest undersea cable or pipeline.

Uses the EMODnet overlay shipped with the console (scripts/fetch_overlays.py). In the 2023-24
Baltic incidents (Balticconnector, Estlink 2) ships dragged anchors across seabed
infrastructure, so a vessel stopped close to a cable or pipeline is ranked up, never filtered.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "dashboard" / "data" / "infrastructure.json"


@dataclass(frozen=True)
class NearestAsset:
    kind: str
    name: str
    distance_nm: float


@lru_cache(maxsize=1)
def _segments() -> tuple[tuple[str, str, float, float, float, float], ...]:
    if not DATA.exists():
        return ()
    feats = json.loads(DATA.read_text(encoding="utf-8"))["features"]
    out = []
    for f in feats:
        for (x1, y1), (x2, y2) in zip(f["coords"], f["coords"][1:], strict=False):
            out.append((f["kind"], f["name"] or "unnamed", x1, y1, x2, y2))
    return tuple(out)


def nearest_asset(
    lat: float, lon: float, search_nm: float = 5.0, segments=None
) -> NearestAsset | None:
    """Nearest cable/pipeline within ``search_nm``, using a local flat-earth projection
    (accurate to well under 1% at these distances)."""
    segs = _segments() if segments is None else segments
    k = math.cos(math.radians(lat))
    pad_lat = search_nm / 60.0
    pad_lon = pad_lat / max(k, 0.2)
    best: NearestAsset | None = None
    for kind, name, x1, y1, x2, y2 in segs:
        if (
            min(y1, y2) - pad_lat > lat
            or max(y1, y2) + pad_lat < lat
            or min(x1, x2) - pad_lon > lon
            or max(x1, x2) + pad_lon < lon
        ):
            continue
        px, py = (lon - x1) * k * 60, (lat - y1) * 60
        dx, dy = (x2 - x1) * k * 60, (y2 - y1) * 60
        seg2 = dx * dx + dy * dy
        t = 0.0 if seg2 == 0 else max(0.0, min(1.0, (px * dx + py * dy) / seg2))
        d = math.hypot(px - t * dx, py - t * dy)
        if d <= search_nm and (best is None or d < best.distance_nm):
            best = NearestAsset(kind, name, round(d, 3))
    return best
