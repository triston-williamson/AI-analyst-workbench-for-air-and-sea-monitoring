"""Public demo: the snapshot-backed data layer and the read-only console (no Snowflake needed)."""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from watchfloor.dashboard import app as console
from watchfloor.dashboard import data as live_data
from watchfloor.dashboard.demo_data import DemoData, ReadOnlyDemo

END = datetime(2026, 11, 22, 8, 54, tzinfo=UTC)


def ts(minutes_before_end: float) -> pd.Timestamp:
    return pd.Timestamp(END - timedelta(minutes=minutes_before_end))


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    """A tiny snapshot in the exporter's format."""
    vessels = pd.DataFrame(
        {
            "RECORD_ID": ["v1", "v2", "v3", "v4"],
            "MMSI": [111, 111, 222, 333],
            "SHIP_NAME": ["OLD NAME", "OLD NAME", None, "FAR"],
            "LAT": [55.0, 55.1, 56.0, 57.0],
            "LON": [12.0, 12.1, 13.0, 14.0],
            "SPEED_KTS": [10.0, 11.0, 0.1, 5.0],
            "COURSE_DEG": [180.0, None, 90.0, 0.0],
            "TRUE_HEADING": [181.0, 182.0, 91.0, 1.0],
            "NAV_STATUS": [0.0, 0.0, 1.0, 0.0],
            "OBSERVED_AT": [ts(20), ts(2), ts(4), ts(200)],
            "LOADED_AT": [ts(19.8), ts(1.8), ts(3.8), ts(-600)],  # v4: replayed hours later
        }
    )
    aircraft = pd.DataFrame(
        {
            "RECORD_ID": ["a1", "a2", "a3"],
            "ICAO24": ["4ab", "4ab", "gnd"],
            "CALLSIGN": [None, "SAS123", "TAXI1"],
            "ORIGIN_COUNTRY": ["Sweden", "Sweden", "Finland"],
            "LAT": [58.0, 58.2, 60.3],
            "LON": [17.0, 17.3, 24.9],
            "SPEED_KTS": [300.0, 310.0, 10.0],
            "COURSE_DEG": [45.0, 50.0, 0.0],
            "BARO_ALTITUDE_M": [9000.0, 9100.0, 0.0],
            "ON_GROUND": [False, False, True],
            "OBSERVED_AT": [ts(9), ts(1), ts(1)],
            "LOADED_AT": [ts(8.9), ts(0.9), ts(0.9)],
        }
    )
    identity = pd.DataFrame(
        {"MMSI": [111, 222], "SHIP_TYPE": [80, 70], "SHIP_NAME": ["TANKER ONE", "CARGO TWO"]}
    )
    alerts = pd.DataFrame(
        {
            "ALERT_ID": ["al-gap", "al-sts", "al-old"],
            "RULE": ["AIS_GAP", "SANCTIONS", "LOITER"],
            "TRACK_TYPE": ["VESSEL", "VESSEL", "VESSEL"],
            "TRACK_ID": ["111", "222", "333"],
            "SCORE": [0.7, 0.95, 0.5],
            "STATUS": ["NEW", "NEW", "RETRACTED"],
            "LAT": [55.1, 56.0, 57.0],
            "LON": [12.1, 13.0, 14.0],
            "START_AT": [ts(30), ts(40), ts(300)],
            "END_AT": [ts(2), ts(4), ts(200)],
            "DETECTED_AT": [ts(1), ts(3), ts(199)],
            "UPDATED_AT": [ts(1), ts(-500), ts(199)],  # al-sts touched again after the window
            "REVIEWED_BY": [None, None, None],
            "REVIEWED_AT": pd.to_datetime([None, None, None], utc=True),
            "REVIEW_NOTE": [None, None, None],
            "DETAILS": [json.dumps({"gap_hours": 2.5}), json.dumps({"why": "listed"}), "{}"],
            "EVIDENCE_IDS": [json.dumps(["v1", "v2"]), json.dumps(["v3"]), "[]"],
        }
    )
    reports = pd.DataFrame(
        {
            "ALERT_ID": ["al-sts", "al-sts"],
            "REPORT_ID": ["r-old", "r-new"],
            "DRAFTED_AT": [ts(30), ts(10)],
            "MODEL": ["claude-opus-5-5", "claude-opus-5-5"],
            "DRAFT_MARKDOWN": ["## Summary\nold", "## Summary\nnew [rec:v3]"],
            "GROUNDING_SCORE": [1.0, 1.0],
            "GROUNDING_PASSED": [True, True],
            "UNCITED": ["[]", json.dumps(["x"])],
            "UNKNOWN_CITATIONS": ["[]", "[]"],
            "TOOL_CALLS": [3, 4],
            "ATTEMPTS": [1, 1],
            "INPUT_TOKENS": [1000, 1200],
            "OUTPUT_TOKENS": [300, 320],
            "STOP_REASON": ["end_turn", "end_turn"],
        }
    )
    evidence = pd.DataFrame(
        {
            "KIND": ["VESSEL", "VESSEL"],
            "RECORD_ID": ["v2", "v1"],
            "TRACK_ID": ["111", "111"],
            "OBSERVED_AT": [ts(2), ts(20)],
            "LAT": [55.1, 55.0],
            "LON": [12.1, 12.0],
            "SPEED_KTS": [11.0, 10.0],
            "COURSE_DEG": [None, 180.0],
            "NAV_STATUS": [0.0, 0.0],
            "RAW_PAYLOAD": ['{"MessageType":"PositionReport"}'] * 2,
        }
    )
    for name, df in {
        "vessel_positions": vessels,
        "aircraft_positions": aircraft,
        "vessel_identity": identity,
        "alerts": alerts,
        "reports": reports,
        "evidence": evidence,
    }.items():
        df.to_parquet(tmp_path / f"{name}.parquet", index=False)
    audit = [
        {"ts": "2026-11-22T08:53:00+00:00", "actor": "system:detector",
         "action": "alert_created", "details": {"alert_id": "al-sts"}},
        {"ts": "2026-11-22T08:50:00+00:00", "actor": "system:detector",
         "action": "alert_created", "details": {"alert_id": "al-gap"}},
    ]
    (tmp_path / "audit.jsonl").write_text("\n".join(json.dumps(e) for e in audit) + "\n")
    meta = {"start": ts(200).isoformat(), "end": END.isoformat(), "rejected_last_hour": 7}
    (tmp_path / "meta.json").write_text(json.dumps(meta))
    return tmp_path


@pytest.fixture
def demo(snapshot: Path) -> DemoData:
    return DemoData(snapshot)


# ---------------------------------------------------------------- same calls as data.py
def test_demo_answers_every_call_the_console_makes():
    public = {
        name
        for name in dir(live_data)
        if not name.startswith("_") and callable(getattr(live_data, name))
        and getattr(getattr(live_data, name), "__module__", "") == live_data.__name__
    }
    missing = {n for n in public if not hasattr(DemoData, n)} - {"execute", "run_query"}
    assert not missing


def test_clock_is_the_end_of_the_snapshot(demo):
    assert demo.now() == END


def test_live_picture_latest_per_vessel_with_identity(demo):
    vessels, aircraft = demo.live_picture(minutes=30)
    assert list(vessels.columns) == [
        "ID", "NAME", "SHIP_TYPE", "LAT", "LON", "SPEED_KTS", "COURSE", "NAV_STATUS", "OBSERVED_AT"
    ]
    v = vessels.set_index("ID")
    assert set(v.index) == {"111", "222"}  # 333 was last seen 200 min ago
    assert v.loc["111", "LAT"] == 55.1  # latest report wins
    assert v.loc["111", "NAME"] == "TANKER ONE"  # identity name over the position report's
    assert v.loc["111", "COURSE"] == 182.0  # no course over ground: true heading
    assert list(aircraft.ID) == ["4ab"]  # on the ground: left out
    assert aircraft.iloc[0]["NAME"] == "SAS123"


def test_replay_snapshot_and_bounds(demo):
    t0, t1 = demo.replay_bounds(hours=12)
    assert t0 == ts(200) and t1 == ts(2)
    vessels, aircraft = demo.snapshot(END - timedelta(minutes=15), window_minutes=10)
    assert list(vessels.ID) == ["111"] and vessels.iloc[0]["LAT"] == 55.0
    assert aircraft.empty  # its reports are 9 and 1 minutes before the end, outside the window


def test_heatmaps(demo):
    density = demo.traffic_density(hours=24)
    assert density["N"].sum() == 4
    dark = demo.dark_events()
    assert list(dark.TRACK_ID) == ["111"] and dark.iloc[0]["GAP_HOURS"] == 2.5


def test_alerts_leave_out_retracted(demo):
    assert set(demo.alerts().ALERT_ID) == {"al-gap", "al-sts"}


def test_track_and_evidence(demo):
    tr = demo.track("VESSEL", ["111"], END - timedelta(hours=1), END)
    assert list(tr.LAT) == [55.0, 55.1] and tr.ID.iloc[0] == "111"
    assert list(demo.track("AIRCRAFT", ["4ab"], END - timedelta(hours=1), END).LAT) == [58.0, 58.2]
    ev = demo.evidence(["v1", "v2"])
    assert list(ev.RECORD_ID) == ["v1", "v2"]  # oldest first
    assert demo.evidence([]).empty


def test_reports(demo):
    rep = demo.latest_report("al-sts")
    assert rep["REPORT_ID"] == "r-new"
    assert rep["UNCITED"] == ["x"] and rep["UNKNOWN_CITATIONS"] == []
    assert demo.latest_report("al-gap") is None
    allr = demo.all_reports()
    assert len(allr) == 1 and allr.iloc[0]["RULE"] == "SANCTIONS"
    assert demo.reported_alert_ids() == ["al-sts"]


def test_health_reads_as_at_the_end_of_the_snapshot(demo):
    h = demo.health()
    assert h["VESSELS"] == 2 and h["AIRCRAFT"] == 1
    assert h["ROWS_5MIN"] == 4  # v2, v3, a2, a3
    assert h["LATENCY_S"] == pytest.approx(12)  # late replays are left out
    assert h["REJECTED_1H"] == 7 and h["OPEN_ALERTS"] == 2
    assert h["DETECTOR_AGE_S"] == pytest.approx(60)  # by detection time, not later updates


def test_audit_history_oldest_first(demo):
    assert [e["action"] for e in demo.audit_history("al-gap")] == ["alert_created"]
    assert demo.audit_history("nope") == []


def test_demo_cannot_write_or_query(demo):
    with pytest.raises(ReadOnlyDemo):
        demo.set_review("al-sts", "APPROVED", "someone", None)
    with pytest.raises(ReadOnlyDemo):
        demo.execute("DELETE FROM OPS.ALERTS", {})
    with pytest.raises(ReadOnlyDemo):
        demo.run_query("SELECT 1")


# ---------------------------------------------------------------- read-only console
def find(component, cid):
    """Depth-first search of a Dash component tree for an id."""
    if getattr(component, "id", None) == cid:
        return component
    children = getattr(component, "children", None)
    if isinstance(children, list | tuple):
        for c in children:
            hit = find(c, cid)
            if hit is not None:
                return hit
    elif children is not None and not isinstance(children, str | int | float):
        return find(children, cid)
    return None


@pytest.fixture
def demo_console(demo, monkeypatch):
    monkeypatch.setattr(console, "DEMO", True)
    monkeypatch.setattr(console, "data", demo)
    return {a["ALERT_ID"]: a for a in console.parse_alerts(demo.alerts())}


def test_demo_console_disables_review_and_drafting(demo_console):
    panel = console.html.Div(console.alert_detail(demo_console["al-sts"], None))
    for cid in ("approve", "reject", "reopen", "review-note"):
        assert find(panel, cid).disabled is True
    assert find(panel, "draft-report").style == {"display": "none"}
    text = json.dumps(panel.to_plotly_json(), default=str)
    assert console.DEMO_OFF in text
    assert "new [rec:v3]" in text or "new" in text  # the stored report still shows


def test_demo_console_explains_missing_report(demo_console):
    section = console.report_section(demo_console["al-gap"])
    text = json.dumps(section.to_plotly_json(), default=str)
    assert "drafting is turned off" in text


def test_live_console_keeps_review_enabled(demo, monkeypatch):
    monkeypatch.setattr(console, "data", demo)  # data source only; DEMO stays False
    monkeypatch.setattr(console, "agent_configured", lambda: False)
    a = console.parse_alerts(demo.alerts())[0]
    panel = console.html.Div(console.alert_detail(a, None))
    assert not find(panel, "approve").disabled
    assert find(panel, "draft-report").style == {}


# ---------------------------------------------------------------- exporter
def load_exporter():
    path = Path(__file__).resolve().parents[1] / "scripts" / "export_demo_snapshot.py"
    spec = importlib.util.spec_from_file_location("export_demo_snapshot", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_exporter_keeps_only_matching_audit_entries_and_hides_reviewer(tmp_path):
    log = tmp_path / "audit.jsonl"
    entries = [
        {"ts": "2", "actor": "human:Jane Doe", "action": "approve", "hash": "abc",
         "details": {"alert_id": "a1", "note": "ok", "track_id": "111"}},
        {"ts": "1", "actor": "system:detector", "action": "alert_created",
         "details": {"alert_id": "a1"}},
        {"ts": "3", "actor": "system:detector", "action": "alert_created",
         "details": {"alert_id": "other"}},
    ]
    log.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    out = load_exporter().audit_entries({"a1"}, str(log))
    assert [e["ts"] for e in out] == ["1", "2"]
    assert out[1] == {"ts": "2", "actor": "human:analyst", "action": "approve",
                      "details": {"alert_id": "a1", "note": "ok"}}
