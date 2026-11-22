"""Synthetic scenarios for the Phase 2 detectors: true hits, look-alikes that must NOT fire,
and port suppression."""

import math
import random
from datetime import UTC, datetime, timedelta

import pytest

from watchfloor.detect import ml
from watchfloor.detect.geo import initial_bearing_deg, signed_turn_deg
from watchfloor.detect.ports import in_port
from watchfloor.detect.rules import Point, find_loitering, find_orbits, find_rendezvous
from watchfloor.detect.runner import Thresholds, detect, match_existing

T0 = datetime(2026, 11, 1, tzinfo=UTC)
OPEN_SEA = (56.80, 19.40)  # central Baltic, far from any port
GDANSK = (54.40, 18.67)
_ids = iter(range(10**9))


def P(track, minutes, lat, lon, speed=None, course=None):
    return Point(f"r{next(_ids)}", track, T0 + timedelta(minutes=minutes), lat, lon, speed, course)


def drift(track, start, hours, step_min=10, jitter_deg=0.002, speed=0.5, seed=1):
    rnd = random.Random(seed)
    lat, lon = start
    return [
        P(
            track,
            m,
            lat + rnd.uniform(-jitter_deg, jitter_deg),
            lon + rnd.uniform(-jitter_deg, jitter_deg),
            speed,
        )
        for m in range(0, int(hours * 60) + 1, step_min)
    ]


def transit(track, start, hours, kts=12.0, heading=90.0, step_min=10, minute0=0):
    lat, lon = start
    out = []
    for m in range(0, int(hours * 60) + 1, step_min):
        d = kts * m / 60 / 60  # degrees travelled (1 deg ~ 60 nm)
        out.append(
            P(
                track,
                minute0 + m,
                lat + d * math.cos(math.radians(heading)),
                lon + d * math.sin(math.radians(heading)) / math.cos(math.radians(lat)),
                kts,
                heading,
            )
        )
    return out


def circle(
    track, centre, radius_nm, period_min, minutes, step_s=30, clockwise=True, with_course=False
):
    lat0, lon0 = centre
    out = []
    for s in range(0, minutes * 60 + 1, step_s):
        ang = 2 * math.pi * s / (period_min * 60) * (1 if clockwise else -1)
        lat = lat0 + radius_nm / 60 * math.cos(ang)
        lon = lon0 + radius_nm / 60 * math.sin(ang) / math.cos(math.radians(lat0))
        course = (math.degrees(ang) + (90 if clockwise else -90)) % 360 if with_course else None
        out.append(
            Point(f"r{next(_ids)}", track, T0 + timedelta(seconds=s), lat, lon, 180.0, course)
        )
    return out


# ---------------------------------------------------------------- geometry helpers
def test_signed_turn_wraps_across_north():
    assert signed_turn_deg(350, 10) == 20
    assert signed_turn_deg(10, 350) == -20
    assert signed_turn_deg(0, 180) == 180


def test_bearing_east_is_90():
    assert initial_bearing_deg(0, 0, 0, 1) == pytest.approx(90)


def test_port_zones():
    assert in_port(*GDANSK)
    assert not in_port(*OPEN_SEA)


# ---------------------------------------------------------------- DET-02 loitering
def test_loitering_vessel_at_sea_is_flagged():
    alerts = find_loitering(drift("111111111", OPEN_SEA, hours=4), in_port=in_port)
    assert len(alerts) == 1
    a = alerts[0]
    assert a.rule == "LOITER" and a.details["duration_hours"] == pytest.approx(4.0)
    assert len(a.evidence_ids) == 5 and a.lat == pytest.approx(OPEN_SEA[0], abs=0.01)


def test_transiting_vessel_is_not_loitering():
    assert find_loitering(transit("222222222", OPEN_SEA, hours=6), in_port=in_port) == []


def test_short_stop_is_not_loitering():
    assert find_loitering(drift("333333333", OPEN_SEA, hours=2), in_port=in_port) == []


def test_loitering_in_port_is_suppressed():
    pts = drift("444444444", GDANSK, hours=8)
    assert find_loitering(pts) != []  # without the filter it would fire...
    assert find_loitering(pts, in_port=in_port) == []  # ...the port filter suppresses it


# ---------------------------------------------------------------- DET-03 rendezvous
def test_two_slow_vessels_side_by_side_is_rendezvous():
    a = drift("555555555", OPEN_SEA, hours=1, step_min=5, speed=1.0, seed=1)
    b = drift(
        "666666666", (OPEN_SEA[0] + 0.002, OPEN_SEA[1]), hours=1, step_min=5, speed=1.2, seed=2
    )
    alerts = find_rendezvous(a + b, in_port=in_port)
    assert len(alerts) == 1
    r = alerts[0]
    assert r.track_id == "555555555|666666666"
    assert r.details["duration_minutes"] >= 30
    assert set(r.evidence_ids) & {p.record_id for p in a}
    assert set(r.evidence_ids) & {p.record_id for p in b}


def test_vessels_passing_at_speed_are_not_rendezvous():
    a = transit("555555555", OPEN_SEA, hours=1, kts=12, heading=90, step_min=5)
    b = transit(
        "666666666", (OPEN_SEA[0] + 0.002, OPEN_SEA[1]), hours=1, kts=12, heading=90, step_min=5
    )
    assert find_rendezvous(a + b, in_port=in_port) == []


def test_slow_vessels_far_apart_are_not_rendezvous():
    a = drift("555555555", OPEN_SEA, hours=1, step_min=5)
    b = drift("666666666", (OPEN_SEA[0] + 0.2, OPEN_SEA[1]), hours=1, step_min=5)
    assert find_rendezvous(a + b, in_port=in_port) == []


def test_brief_encounter_is_not_rendezvous():
    a = drift("555555555", OPEN_SEA, hours=0.25, step_min=5)
    b = drift("666666666", OPEN_SEA, hours=0.25, step_min=5, seed=3)
    assert find_rendezvous(a + b, in_port=in_port) == []


def test_rendezvous_in_port_is_suppressed():
    a = drift("555555555", GDANSK, hours=1, step_min=5)
    b = drift("666666666", GDANSK, hours=1, step_min=5, seed=2)
    assert find_rendezvous(a + b, in_port=in_port) == []


# ---------------------------------------------------------------- DET-04 orbits
@pytest.mark.parametrize("with_course", [True, False])
def test_aircraft_orbit_is_flagged(with_course):
    pts = circle("abc123", OPEN_SEA, radius_nm=3, period_min=4, minutes=12, with_course=with_course)
    alerts = find_orbits(pts)
    assert len(alerts) >= 1
    a = alerts[0]
    assert a.rule == "ORBIT" and a.details["circles"] >= 2
    assert a.details["direction"] == "clockwise"


def test_counter_clockwise_orbit_direction():
    pts = circle("abc124", OPEN_SEA, 3, 4, 12, clockwise=False, with_course=True)
    assert find_orbits(pts)[0].details["direction"] == "counter-clockwise"


def test_straight_flight_is_not_orbit():
    pts = transit("abc125", OPEN_SEA, hours=0.5, kts=450, step_min=1)
    assert find_orbits(pts) == []


def test_s_turns_cancel_out():
    pts, heading = [], 90.0
    for k in range(60):  # weave left/right 60 deg every minute for an hour
        heading = 60.0 if k % 2 else 120.0
        pts.append(P("abc126", k, 56.0 + k * 0.01, 19.0, 300, heading))
    assert find_orbits(pts) == []


def test_single_circle_is_not_enough():
    pts = circle("abc127", OPEN_SEA, 3, period_min=4, minutes=5, with_course=True)
    assert find_orbits(pts) == []


# ---------------------------------------------------------------- DET-05 ML
def test_isolation_forest_ranks_the_odd_track_first():
    rnd = random.Random(7)
    pts = []
    for k in range(40):  # 40 ordinary ships in transit
        start = (55.5 + rnd.uniform(0, 3), 15 + rnd.uniform(0, 8))
        pts += transit(
            f"9000000{k:02d}",
            start,
            hours=2,
            kts=rnd.uniform(10, 14),
            heading=rnd.uniform(0, 360),
            step_min=6,
        )
    odd = circle("999999999", OPEN_SEA, radius_nm=0.4, period_min=20, minutes=120, step_s=360)
    odd = [Point(p.record_id, p.track_id, p.observed_at, p.lat, p.lon, 2.0, None) for p in odd]
    alerts = ml.score_tracks(pts + odd, "VESSEL", top_k=3)
    assert alerts and alerts[0].track_id == "999999999"
    assert alerts[0].details["top_drivers"]  # explanation present
    assert alerts[0].score >= 0.98


def test_ml_needs_enough_tracks():
    pts = transit("900000001", OPEN_SEA, hours=2)
    assert ml.score_tracks(pts, "VESSEL") == []


# ---------------------------------------------------------------- runner glue
def test_detect_combines_all_rules():
    vessels = drift("111111111", OPEN_SEA, hours=4)
    a = drift("555555555", (57.5, 20.5), hours=1, step_min=5, speed=1.0, seed=1)
    b = drift("666666666", (57.502, 20.5), hours=1, step_min=5, speed=1.0, seed=2)
    aircraft = circle("abc123", OPEN_SEA, 3, 4, 12, with_course=True)
    rules = {a.rule for a in detect(vessels + a + b, aircraft, Thresholds())}
    assert {"LOITER", "RENDEZVOUS", "ORBIT"} <= rules


def test_overlapping_episode_updates_instead_of_duplicating():
    alert = find_loitering(drift("111111111", OPEN_SEA, hours=4))[0]
    existing = [("id-1", "LOITER", "111111111", alert.start_at + timedelta(hours=1), alert.end_at)]
    assert match_existing(alert, existing) == "id-1"
    later = [
        (
            "id-2",
            "LOITER",
            "111111111",
            alert.end_at + timedelta(hours=5),
            alert.end_at + timedelta(hours=9),
        )
    ]
    assert match_existing(alert, later) is None
