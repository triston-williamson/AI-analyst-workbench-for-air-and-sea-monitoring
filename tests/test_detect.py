from datetime import UTC, datetime, timedelta

import pytest

from watchfloor.detect.geo import haversine_nm
from watchfloor.detect.rules import Point, find_ais_gaps

T0 = datetime(2026, 11, 1, tzinfo=UTC)


def pt(rid, track, minutes, lat, lon):
    return Point(rid, track, T0 + timedelta(minutes=minutes), lat, lon)


def test_haversine_one_degree_latitude_is_60nm():
    assert haversine_nm(0, 0, 1, 0) == pytest.approx(60.0, rel=0.01)


def test_gap_detected_with_evidence():
    pts = [
        pt("a", "111", 0, 59.0, 20.0),
        pt("b", "111", 10, 59.01, 20.0),
        pt("c", "111", 10 + 6 * 60, 59.8, 22.0),
    ]
    alerts = find_ais_gaps(pts, min_gap=timedelta(hours=2))
    assert len(alerts) == 1
    a = alerts[0]
    assert a.rule == "AIS_GAP" and a.track_id == "111"
    assert a.evidence_ids == ["b", "c"]
    assert a.details["gap_hours"] == 6.0


def test_no_gap_for_regular_reporting():
    pts = [pt(str(i), "222", i * 5, 59.0, 20.0) for i in range(50)]
    assert find_ais_gaps(pts) == []


def test_tracks_are_independent_and_unsorted_input_ok():
    pts = [
        pt("y", "B", 300, 58.0, 19.0),
        pt("x", "A", 0, 58.0, 19.0),
        pt("w", "B", 0, 58.0, 19.0),
        pt("z", "A", 30, 58.0, 19.0),
    ]
    alerts = find_ais_gaps(pts, min_gap=timedelta(hours=2))
    assert [a.track_id for a in alerts] == ["B"]
