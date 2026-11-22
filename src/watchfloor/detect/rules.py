"""Rule-based detectors.

Every detector returns ``Alert`` objects that carry the RECORD_IDs that triggered them, so the
analyst agent (and a human reviewer) can trace each alert back to raw sensor observations.

    AIS_GAP     vessel stops transmitting ("goes dark") and later reappears        DET-01
    LOITER      vessel stays inside a small radius for a long time, away from port  DET-02
    RENDEZVOUS  two vessels close together at low speed, away from port            DET-03
                (pattern associated with ship-to-ship transfers)
    ORBIT       aircraft accumulates >= 720 deg of turn in one direction            DET-04

Rules come first because analysts must be able to see *why* something was flagged. ML scoring
(see ``ml.py``) ranks tracks on top of these; it does not replace them.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from watchfloor.detect.geo import haversine_nm, initial_bearing_deg, signed_turn_deg

PortFilter = Callable[[float, float], bool]


def _never_in_port(lat: float, lon: float) -> bool:
    return False


@dataclass(frozen=True)
class Point:
    record_id: str
    track_id: str
    observed_at: datetime
    lat: float
    lon: float
    speed_kts: float | None = None
    course_deg: float | None = None


@dataclass
class Alert:
    rule: str
    track_type: str
    track_id: str
    score: float
    evidence_ids: list[str]
    details: dict = field(default_factory=dict)
    lat: float | None = None
    lon: float | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None


def group_tracks(points: Iterable[Point]) -> dict[str, list[Point]]:
    """Group points by track and sort each track by time."""
    by_track: dict[str, list[Point]] = defaultdict(list)
    for p in points:
        by_track[p.track_id].append(p)
    for pts in by_track.values():
        pts.sort(key=lambda p: p.observed_at)
    return by_track


def sample_evidence(pts: list[Point], k: int = 5) -> list[str]:
    """First, last and evenly spaced points in between: enough for a reviewer to retrace."""
    if len(pts) <= k:
        return [p.record_id for p in pts]
    idx = sorted({round(i * (len(pts) - 1) / (k - 1)) for i in range(k)})
    return [pts[i].record_id for i in idx]


def _centroid(pts: list[Point]) -> tuple[float, float]:
    return sum(p.lat for p in pts) / len(pts), sum(p.lon for p in pts) / len(pts)


# --------------------------------------------------------------------------- DET-01 AIS gap
def find_ais_gaps(
    points: Iterable[Point],
    min_gap: timedelta = timedelta(hours=2),
    min_distance_nm: float = 0.0,
    in_port: PortFilter = _never_in_port,
) -> list[Alert]:
    """Flag transmission gaps for each vessel.

    A gap is two consecutive reports from the same MMSI more than ``min_gap`` apart. The score
    grows with gap length and with displacement, because a vessel that went dark and reappeared
    far away is more interesting than one that went quiet in one spot. Gaps that start and end
    in port are ignored (ships often power down AIS alongside).
    """
    alerts: list[Alert] = []
    for track_id, pts in group_tracks(points).items():
        for prev, cur in zip(pts, pts[1:], strict=False):
            gap = cur.observed_at - prev.observed_at
            if gap < min_gap:
                continue
            if in_port(prev.lat, prev.lon) and in_port(cur.lat, cur.lon):
                continue
            dist = haversine_nm(prev.lat, prev.lon, cur.lat, cur.lon)
            if dist < min_distance_nm:
                continue
            hours = gap.total_seconds() / 3600
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
                        "implied_speed_kts": round(dist / hours, 1) if hours else 0.0,
                        "dark_from": prev.observed_at.isoformat(),
                        "dark_until": cur.observed_at.isoformat(),
                    },
                    lat=prev.lat,
                    lon=prev.lon,
                    start_at=prev.observed_at,
                    end_at=cur.observed_at,
                )
            )
    return alerts


# --------------------------------------------------------------------------- DET-02 loitering
def find_loitering(
    points: Iterable[Point],
    radius_nm: float = 1.0,
    min_duration: timedelta = timedelta(hours=3),
    max_report_gap: timedelta = timedelta(hours=1),
    in_port: PortFilter = _never_in_port,
    track_type: str = "VESSEL",
) -> list[Alert]:
    """Flag tracks that stay within ``radius_nm`` of where they started for ``min_duration``.

    Two-pointer scan per track: from each anchor point, extend forward while every point stays
    inside the circle and reports keep arriving (a long silence is an AIS_GAP, not loitering).
    Once a stay qualifies, scanning resumes after it, so one stay produces one alert.
    """
    alerts: list[Alert] = []
    for track_id, pts in group_tracks(points).items():
        n, i = len(pts), 0
        while i < n:
            anchor = pts[i]
            if in_port(anchor.lat, anchor.lon):
                i += 1
                continue
            j = i
            while (
                j + 1 < n
                and pts[j + 1].observed_at - pts[j].observed_at <= max_report_gap
                and haversine_nm(anchor.lat, anchor.lon, pts[j + 1].lat, pts[j + 1].lon)
                <= radius_nm
            ):
                j += 1
            duration = pts[j].observed_at - anchor.observed_at
            stay = pts[i : j + 1]
            clat, clon = _centroid(stay)
            if duration >= min_duration and not in_port(clat, clon):
                ratio = duration / min_duration
                speeds = [p.speed_kts for p in stay if p.speed_kts is not None]
                alerts.append(
                    Alert(
                        rule="LOITER",
                        track_type=track_type,
                        track_id=track_id,
                        score=round(min(1.0, 0.5 + 0.5 * (ratio - 1) / 3), 3),
                        evidence_ids=sample_evidence(stay),
                        details={
                            "duration_hours": round(duration.total_seconds() / 3600, 2),
                            "radius_nm": radius_nm,
                            "reports": len(stay),
                            "mean_speed_kts": round(sum(speeds) / len(speeds), 2)
                            if speeds
                            else None,
                        },
                        lat=clat,
                        lon=clon,
                        start_at=anchor.observed_at,
                        end_at=pts[j].observed_at,
                    )
                )
                i = j + 1
            else:
                i += 1
    return alerts


# --------------------------------------------------------------------------- DET-03 rendezvous
def find_rendezvous(
    points: Iterable[Point],
    max_distance_nm: float = 0.5,
    max_speed_kts: float = 3.0,
    min_duration: timedelta = timedelta(minutes=30),
    bin_size: timedelta = timedelta(minutes=10),
    in_port: PortFilter = _never_in_port,
) -> list[Alert]:
    """Flag pairs of vessels that stay close together at low speed, away from port.

    1. Bucket time into ``bin_size`` slots and keep each vessel's last slow, out-of-port report
       per slot.
    2. Within a slot, find close pairs with a spatial grid (cells ~ max_distance wide), so we
       only compare neighbours instead of every pair of ships.
    3. A pair that is close in consecutive slots (one missed slot tolerated) spanning at least
       ``min_duration`` is a rendezvous.
    """
    bin_s = bin_size.total_seconds()
    slots: dict[int, dict[str, Point]] = defaultdict(dict)
    for p in points:
        if p.speed_kts is None or p.speed_kts > max_speed_kts or in_port(p.lat, p.lon):
            continue
        b = int(p.observed_at.timestamp() // bin_s)
        prev = slots[b].get(p.track_id)
        if prev is None or p.observed_at > prev.observed_at:
            slots[b][p.track_id] = p

    cell_lat = max_distance_nm / 60.0
    hits: dict[tuple[str, str], list[tuple[int, Point, Point]]] = defaultdict(list)
    for b, latest in slots.items():
        grid: dict[tuple[int, int], list[Point]] = defaultdict(list)
        for p in latest.values():
            cell_lon = cell_lat / max(math.cos(math.radians(p.lat)), 0.2)
            grid[(math.floor(p.lat / cell_lat), math.floor(p.lon / cell_lon))].append(p)
        for (cy, cx), members in grid.items():
            for p in members:
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        for q in grid.get((cy + dy, cx + dx), ()):
                            if q.track_id <= p.track_id:
                                continue  # each unordered pair once
                            if haversine_nm(p.lat, p.lon, q.lat, q.lon) <= max_distance_nm:
                                hits[(p.track_id, q.track_id)].append((b, p, q))

    alerts: list[Alert] = []
    for (a, b_id), events in hits.items():
        events.sort(key=lambda e: e[0])
        run = [events[0]]
        for ev in [*events[1:], None]:
            if ev is not None and ev[0] - run[-1][0] <= 2:  # tolerate one missed slot
                run.append(ev)
                continue
            span = timedelta(seconds=(run[-1][0] - run[0][0] + 1) * bin_s)
            if span >= min_duration:
                first, last = run[0], run[-1]
                dists = [haversine_nm(p.lat, p.lon, q.lat, q.lon) for _, p, q in run]
                pts_a = [p for _, p, _ in run]
                pts_b = [q for _, _, q in run]
                clat, clon = _centroid(pts_a + pts_b)
                alerts.append(
                    Alert(
                        rule="RENDEZVOUS",
                        track_type="VESSEL",
                        track_id=f"{a}|{b_id}",
                        score=round(min(1.0, 0.5 + 0.5 * (span / min_duration - 1) / 4), 3),
                        evidence_ids=sample_evidence(pts_a, 3) + sample_evidence(pts_b, 3),
                        details={
                            "vessel_a": a,
                            "vessel_b": b_id,
                            "duration_minutes": round(span.total_seconds() / 60),
                            "min_separation_nm": round(min(dists), 3),
                            "mean_separation_nm": round(sum(dists) / len(dists), 3),
                        },
                        lat=clat,
                        lon=clon,
                        start_at=min(first[1].observed_at, first[2].observed_at),
                        end_at=max(last[1].observed_at, last[2].observed_at),
                    )
                )
            if ev is not None:
                run = [ev]
    return alerts


# --------------------------------------------------------------------------- DET-04 orbits
def find_orbits(
    points: Iterable[Point],
    min_turn_deg: float = 720.0,
    max_window: timedelta = timedelta(minutes=45),
    max_report_gap: timedelta = timedelta(minutes=5),
    min_step_nm: float = 0.05,
) -> list[Alert]:
    """Flag aircraft that keep turning the same way: two or more full circles.

    Headings come from the reported track angle, or from consecutive positions when it is
    missing. Signed turn is summed over a sliding window, so S-turns cancel out and only
    sustained circling (orbits, racetracks, holding patterns) accumulates. Holding patterns near
    airports are expected benign hits; the analyst agent is told to consider them.
    """
    alerts: list[Alert] = []
    for track_id, pts in group_tracks(points).items():
        headings: list[float | None] = []
        for k, p in enumerate(pts):
            if p.course_deg is not None:
                headings.append(p.course_deg)
            elif k and haversine_nm(pts[k - 1].lat, pts[k - 1].lon, p.lat, p.lon) >= min_step_nm:
                headings.append(initial_bearing_deg(pts[k - 1].lat, pts[k - 1].lon, p.lat, p.lon))
            else:
                headings.append(None)

        # turn[k] = signed heading change between point k-1 and k (0 if unknown or a gap).
        turn = [0.0] * len(pts)
        for k in range(1, len(pts)):
            gap = pts[k].observed_at - pts[k - 1].observed_at
            if headings[k] is not None and headings[k - 1] is not None and gap <= max_report_gap:
                turn[k] = signed_turn_deg(headings[k - 1], headings[k])

        i, total = 0, 0.0
        for j in range(1, len(pts)):
            if pts[j].observed_at - pts[j - 1].observed_at > max_report_gap:
                i, total = j, 0.0  # broken track: restart the window
                continue
            total += turn[j]
            while pts[j].observed_at - pts[i].observed_at > max_window:
                i += 1
                total -= turn[i]
            if abs(total) >= min_turn_deg:
                window = pts[i : j + 1]
                clat, clon = _centroid(window)
                circles = abs(total) / 360
                alerts.append(
                    Alert(
                        rule="ORBIT",
                        track_type="AIRCRAFT",
                        track_id=track_id,
                        score=round(min(1.0, 0.4 + 0.15 * circles), 3),
                        evidence_ids=sample_evidence(window),
                        details={
                            "total_turn_deg": round(total),
                            "circles": round(circles, 1),
                            "direction": "clockwise" if total > 0 else "counter-clockwise",
                            "window_minutes": round(
                                (pts[j].observed_at - pts[i].observed_at).total_seconds() / 60
                            ),
                        },
                        lat=clat,
                        lon=clon,
                        start_at=pts[i].observed_at,
                        end_at=pts[j].observed_at,
                    )
                )
                i, total = j, 0.0  # one alert per orbit episode; continue after it
    return alerts
