"""Rule-based detectors. Each returns Alert objects that carry the RECORD_IDs that triggered them,
so the analyst agent (and a human reviewer) can trace every alert back to raw observations.

Implemented:   AIS_GAP     - vessel stops transmitting ("goes dark") then reappears
Planned:       LOITER      - vessel/aircraft stays within a small radius for a long time
               RENDEZVOUS  - two vessels close and slow away from port (ship-to-ship transfer?)
               ORBIT       - aircraft flying repeated circles (cumulative heading change >= 720 deg)
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from watchfloor.detect.geo import haversine_nm


@dataclass(frozen=True)
class Point:
    record_id: str
    track_id: str
    observed_at: datetime
    lat: float
    lon: float
    speed_kts: float | None = None


@dataclass
class Alert:
    rule: str
    track_type: str
    track_id: str
    score: float
    evidence_ids: list[str]
    details: dict = field(default_factory=dict)


def find_ais_gaps(
    points: Iterable[Point],
    min_gap: timedelta = timedelta(hours=2),
    min_distance_nm: float = 0.0,
) -> list[Alert]:
    """Flag transmission gaps for each vessel.

    A gap is two consecutive reports from the same MMSI more than ``min_gap`` apart. The score
    grows with gap length and with how implausible the jump is (distance vs. time), because a
    vessel that went dark and reappeared far away is more interesting than one sitting in port.
    """
    by_track: dict[str, list[Point]] = {}
    for p in points:
        by_track.setdefault(p.track_id, []).append(p)

    alerts: list[Alert] = []
    for track_id, pts in by_track.items():
        pts.sort(key=lambda p: p.observed_at)
        for prev, cur in zip(pts, pts[1:], strict=False):
            gap = cur.observed_at - prev.observed_at
            if gap < min_gap:
                continue
            dist = haversine_nm(prev.lat, prev.lon, cur.lat, cur.lon)
            if dist < min_distance_nm:
                continue
            hours = gap.total_seconds() / 3600
            implied_kts = dist / hours if hours else 0.0
            score = min(1.0, hours / 24) * 0.6 + min(1.0, dist / 100) * 0.4
            alerts.append(
                Alert(
                    rule="AIS_GAP",
                    track_type="VESSEL",
                    track_id=track_id,
                    score=round(score, 3),
                    evidence_ids=[prev.record_id, cur.record_id],
                    details={
                        "gap_hours": round(hours, 2),
                        "distance_nm": round(dist, 1),
                        "implied_speed_kts": round(implied_kts, 1),
                        "dark_from": prev.observed_at.isoformat(),
                        "dark_until": cur.observed_at.isoformat(),
                    },
                )
            )
    return alerts


def find_loitering(points: Iterable[Point], **kwargs) -> list[Alert]:
    raise NotImplementedError("Phase 2: see docs/DESIGN.md, requirement DET-02")


def find_rendezvous(points: Iterable[Point], **kwargs) -> list[Alert]:
    raise NotImplementedError("Phase 2: see docs/DESIGN.md, requirement DET-03")


def find_orbits(points: Iterable[Point], **kwargs) -> list[Alert]:
    raise NotImplementedError("Phase 2: see docs/DESIGN.md, requirement DET-04")
