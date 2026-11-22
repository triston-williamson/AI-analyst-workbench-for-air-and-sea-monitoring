"""Operator console: drawing helpers and app logic (no Snowflake needed)."""

import json
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from watchfloor.dashboard import figures
from watchfloor.dashboard.app import (
    alert_title,
    detail_key,
    dtg,
    explain,
    health_tiles,
    records,
    trail_window,
)
from watchfloor.dashboard.data import audit_history
from watchfloor.guardrails.audit import AuditLog

T0 = datetime(2026, 11, 22, 3, 4, 3, 500000, tzinfo=UTC)


# ---------------------------------------------------------------- categories & geometry
@pytest.mark.parametrize(
    ("code", "cat"),
    [
        (84, "Tanker"),
        (71, "Cargo"),
        (60, "Passenger"),
        (52, "Working craft"),
        (30, "Fishing"),
        (37, "Pleasure / sailing"),
        (35, "Military / law enf."),
        (None, "Other / unknown"),
        (float("nan"), "Other / unknown"),
        (99, "Other / unknown"),
    ],
)
def test_vessel_category(code, cat):
    assert figures.vessel_category(code) == cat
    assert cat in figures.CATEGORY_COLORS


def test_leader_points_along_course_and_shrinks_with_zoom():
    lat2, lon2 = figures.leader_end(56.0, 18.0, course=0, speed_kts=15, zoom=5)
    assert lat2 > 56.0 and lon2 == pytest.approx(18.0)
    lat3, lon3 = figures.leader_end(56.0, 18.0, course=90, speed_kts=15, zoom=5)
    assert lon3 > 18.0 and lat3 == pytest.approx(56.0)
    near = figures.leader_end(56.0, 18.0, 0, 15, zoom=10)[0] - 56.0
    far = figures.leader_end(56.0, 18.0, 0, 15, zoom=5)[0] - 56.0
    assert near < far / 10


def test_stationary_vessels_get_no_leader():
    df = pd.DataFrame(
        {"LAT": [56.0, 56.1], "LON": [18.0, 18.1], "SPEED_KTS": [0.0, 12.0], "COURSE": [90.0, 90.0]}
    )
    lats, _ = figures.leaders(df, zoom=6)
    assert len(lats) == 3  # one moving vessel: start, end, break


def test_zoom_for_extent_tighter_for_smaller_area():
    wide = figures.zoom_for_extent([54.0, 60.0], [10.0, 28.0])
    tight = figures.zoom_for_extent([56.0, 56.02], [18.0, 18.03])
    assert tight > wide


def test_circle_is_closed():
    lats, lons = figures.circle(56.0, 18.0, 1.0)
    assert lats[0] == pytest.approx(lats[-1]) and lons[0] == pytest.approx(lons[-1])


# ---------------------------------------------------------------- traces
def _track(rows):
    return pd.DataFrame(rows, columns=["ID", "OBSERVED_AT", "LAT", "LON", "SPEED_KTS"])


def test_gap_trail_draws_both_legs_dotted_gap_and_labels():
    """Regression: track times arrive at whole-second precision; the 'after' leg vanished
    when the gap end had fractional seconds."""
    dark_from = T0
    dark_until = T0 + timedelta(hours=2, seconds=0.5)
    track = _track(
        [
            ("1", (T0 - timedelta(minutes=10)).isoformat(), 56.95, 11.75, 16.0),
            ("1", T0.replace(microsecond=0).isoformat(), 56.92, 11.77, 16.0),
            ("1", dark_until.replace(microsecond=0).isoformat(), 56.36, 12.13, 16.0),
            ("1", (dark_until + timedelta(minutes=5)).isoformat(), 56.33, 12.15, 16.0),
        ]
    )
    alert = {
        "RULE": "AIS_GAP",
        "details": {
            "dark_from": dark_from.isoformat(),
            "dark_until": dark_until.isoformat(),
            "gap_hours": 2.0,
        },
    }
    names = [t.name for t in figures.trail_traces(alert, track)]
    assert "Track before dark" in names and "Track after" in names
    assert "Dark (no AIS)" in names
    texts = [t.text[0] for t in figures.trail_traces(alert, track) if t.mode == "markers+text"]
    assert any(s.startswith("Went dark") for s in texts)
    assert any(s.startswith("Reappeared") for s in texts)


def test_rendezvous_trail_draws_each_vessel():
    track = _track(
        [
            ("A", T0.isoformat(), 56.0, 18.0, 1.0),
            ("A", (T0 + timedelta(minutes=10)).isoformat(), 56.0, 18.0, 1.0),
            ("B", T0.isoformat(), 56.002, 18.0, 1.0),
            ("B", (T0 + timedelta(minutes=10)).isoformat(), 56.002, 18.0, 1.0),
        ]
    )
    alert = {
        "RULE": "RENDEZVOUS",
        "LAT": 56.001,
        "LON": 18.0,
        "details": {"vessel_names": ["ALPHA", "BRAVO"]},
    }
    names = [t.name for t in figures.trail_traces(alert, track)]
    assert "ALPHA" in names and "BRAVO" in names


def test_alert_and_traffic_traces_carry_click_targets():
    alerts = pd.DataFrame(
        [
            {
                "ALERT_ID": "a1",
                "RULE": "LOITER",
                "TRACK_TYPE": "VESSEL",
                "TRACK_ID": "1",
                "SCORE": 0.7,
                "LAT": 56.0,
                "LON": 18.0,
            }
        ]
    )
    core = [t for t in figures.alert_traces(alerts) if t.customdata is not None]
    assert list(core[0].customdata[0]) == ["alert", "a1"]
    vessels = pd.DataFrame(
        [
            {
                "ID": "230000001",
                "NAME": "X",
                "SHIP_TYPE": 84,
                "LAT": 56.0,
                "LON": 18.0,
                "SPEED_KTS": 12.0,
                "COURSE": 45.0,
            }
        ]
    )
    dots = [
        t for t in figures.traffic_traces(vessels, pd.DataFrame(), 6) if t.customdata is not None
    ]
    assert list(dots[0].customdata[0]) == ["vessel", "230000001"]
    assert dots[0].name.startswith("Tanker")


def test_infrastructure_overlay_ships_with_package():
    traces = figures.infrastructure_traces()
    assert {t.name for t in traces} >= {"Pipeline"}
    assert "EMODnet" in figures.infrastructure_attribution()


# ---------------------------------------------------------------- app logic
def test_dtg_is_zulu_military_format():
    assert dtg("2026-11-22T14:32:00Z") == "221432Z NOV 26"
    assert dtg(None) == "-"


@pytest.mark.parametrize("rule", list(figures.RULE_LABELS))
def test_every_rule_has_a_plain_language_explanation(rule):
    details = {
        "gap_hours": 2,
        "distance_nm": 30,
        "implied_speed_kts": 15,
        "radius_nm": 1,
        "duration_hours": 3,
        "min_separation_nm": 0.1,
        "duration_minutes": 40,
        "circles": 2.5,
        "direction": "clockwise",
        "window_minutes": 20,
        "tracks_compared": 700,
        "percentile": 0.99,
        "top_drivers": [{"feature": "turn_rate", "robust_z": 9.1}],
    }
    text = explain({"RULE": rule, "details": details})
    assert len(text) > 40


def test_tanker_pair_called_out_first():
    a = {"RULE": "RENDEZVOUS", "details": {"tanker_pair": True, "vessel_types": ["Tanker"] * 2}}
    assert explain(a).startswith("TANKER PAIR")


def test_alert_title_prefers_vessel_names():
    assert (
        alert_title(
            {"TRACK_TYPE": "VESSEL", "TRACK_ID": "1|2", "details": {"vessel_names": ["A", "B"]}}
        )
        == "A + B"
    )
    assert alert_title({"TRACK_TYPE": "AIRCRAFT", "TRACK_ID": "abc123", "details": {}}) == (
        "Aircraft abc123"
    )


def test_detail_key_changes_only_on_review():
    """Regression: the 30 s refresh re-rendered the open panel and wiped a typed note."""
    a = {"ALERT_ID": "x", "STATUS": "NEW", "REVIEWED_AT": None, "SCORE": 0.5}
    same_after_refresh = dict(a, SCORE=0.6)
    reviewed = dict(a, STATUS="REJECTED", REVIEWED_AT="2026-11-22T05:17:00Z")
    assert detail_key(a) == detail_key(same_after_refresh)
    assert detail_key(a) != detail_key(reviewed)


def test_trail_window_pads_by_rule():
    a = {"RULE": "AIS_GAP", "START_AT": "2026-11-22T03:00:00Z", "END_AT": "2026-11-22T05:00:00Z"}
    start, end = trail_window(a)
    assert end - start == timedelta(hours=5)


def test_health_tiles_grade_latency():
    tiles = health_tiles(
        {
            "VESSELS": 1900,
            "AIRCRAFT": 30,
            "ROWS_5MIN": 9000,
            "LATENCY_S": 721,
            "FRESHNESS_S": 5,
            "REJECTED_1H": 10,
            "OPEN_ALERTS": 3,
            "DETECTOR_AGE_S": 60,
        }
    )
    latency = next(t for t in tiles if t.children[0].children == "Sensor→DB")
    assert "bad" in latency.className
    assert "bad" in health_tiles(None, "boom")[0].className


def test_records_are_json_safe():
    df = pd.DataFrame({"T": pd.to_datetime(["2026-11-22T04:59:26.123Z"], utc=True), "X": [1]})
    out = records(df)
    json.dumps(out)
    assert out[0]["T"].startswith("2026-11-22T04:59:26")


def test_audit_history_filters_by_alert(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record("system:detector", "alert_created", alert_id="a1")
    log.record("system:detector", "alert_created", alert_id="a2")
    log.record("human:analyst", "reject", alert_id="a1", note="pilot station")
    hist = audit_history("a1", str(path))
    assert [e["action"] for e in hist] == ["alert_created", "reject"]
    assert hist[1]["details"]["note"] == "pilot station"


def test_citations_are_compacted_for_display_only():
    from watchfloor.dashboard.app import compact_citations

    text = "Held station [rec:3d5af079c5fda3a6460fea090fff27bc][alert:e5f7456c-0065-541c]."
    out = compact_citations(text)
    assert "`rec:3d5af079`" in out and "`alert:e5f7456c`" in out
    assert "3d5af079c5fda3a6" not in out
