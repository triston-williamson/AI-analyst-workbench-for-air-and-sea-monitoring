"""AIS static data ingestion and ship-type-aware detection.

Message fixtures mirror the exact shape aisstream.io sends (captured from the live feed).
"""

import json
from datetime import timedelta

import pytest
from pydantic import ValidationError

from tests.test_phase2_detectors import OPEN_SEA, T0, drift
from watchfloor.detect import ship_types
from watchfloor.detect.rules import Alert
from watchfloor.detect.runner import Identity, Thresholds, detect, enrich
from watchfloor.ingest.ais_producer import static_to_record
from watchfloor.models import VesselStatic

CLASS_A = {
    "MetaData": {
        "MMSI": 230291000,
        "ShipName": "VOIMA",
        "time_utc": "2026-11-22 03:41:57.296504779 +0000 UTC",
    },
    "MessageType": "ShipStaticData",
    "Message": {
        "ShipStaticData": {
            "CallSign": "OHLW",
            "Destination": "FIHEL@@@@",
            "Dimension": {"A": 25, "B": 59, "C": 12, "D": 8},
            "ImoNumber": 5383158,
            "MaximumStaticDraught": 6.5,
            "Name": "VOIMA",
            "Type": 99,
            "UserID": 230291000,
            "Valid": True,
        }
    },
}
CLASS_B_PART_B = {
    "MetaData": {"MMSI": 261008769, "ShipName": "", "time_utc": "2026-11-22 03:41:57.38 +0000 UTC"},
    "MessageType": "StaticDataReport",
    "Message": {
        "StaticDataReport": {
            "PartNumber": True,
            "ReportA": {"Name": "", "Valid": False},
            "ReportB": {
                "CallSign": "SPG6120",
                "Dimension": {"A": 4, "B": 19, "C": 4, "D": 5},
                "ShipType": 52,
                "Valid": True,
            },
            "UserID": 261008769,
        }
    },
}
CLASS_B_PART_A = {
    "MetaData": {
        "MMSI": 219010038,
        "ShipName": "JAIME LA BONNE VIE",
        "time_utc": "2026-11-22 03:41:58.81 +0000 UTC",
    },
    "MessageType": "StaticDataReport",
    "Message": {
        "StaticDataReport": {
            "PartNumber": False,
            "ReportA": {"Name": "JAIME LA BONNE VIE", "Valid": True},
            "ReportB": {"ShipType": 0, "Valid": False},
        }
    },
}


# ---------------------------------------------------------------- ingestion
def test_class_a_static_parses_and_validates():
    rec = VesselStatic.model_validate(static_to_record(CLASS_A, json.dumps(CLASS_A)))
    assert rec.ship_type == 99 and rec.imo == 5383158 and rec.call_sign == "OHLW"
    assert rec.length_m == 84 and rec.beam_m == 20 and rec.draught_m == 6.5
    assert rec.destination == "FIHEL"  # '@' padding stripped


def test_class_b_part_b_parses_ship_type():
    rec = VesselStatic.model_validate(static_to_record(CLASS_B_PART_B, "{}"))
    assert rec.ship_type == 52 and rec.imo is None and rec.ship_name is None
    assert rec.length_m == 23


def test_class_b_part_a_has_no_type_and_is_skipped():
    assert static_to_record(CLASS_B_PART_A, "{}") is None


def test_impossible_static_values_rejected():
    bad = static_to_record(CLASS_A, "{}")
    bad["length_m"] = 900  # longer than any ship afloat
    with pytest.raises(ValidationError):
        VesselStatic.model_validate(bad)


# ---------------------------------------------------------------- classification
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (52, "Tug"),
        (50, "Pilot vessel"),
        (84, "Tanker"),
        (71, "Cargo"),
        (0, "Unknown"),
        (None, "Unknown"),
        (35, "Military ops"),
    ],
)
def test_labels(code, expected):
    assert ship_types.label(code) == expected


def test_service_set_keeps_dredging_and_military_in_scope():
    assert ship_types.is_service(52) and ship_types.is_service(50) and ship_types.is_service(30)
    assert not ship_types.is_service(33)  # underwater ops: relevant to cable monitoring
    assert not ship_types.is_service(35)
    assert not ship_types.is_service(84)


# ---------------------------------------------------------------- detection
def pair(a, b, speed=1.0):
    return drift(a, OPEN_SEA, hours=1, step_min=5, speed=speed, seed=1) + drift(
        b, (OPEN_SEA[0] + 0.002, OPEN_SEA[1]), hours=1, step_min=5, speed=speed, seed=2
    )


def test_pilot_boats_alongside_are_not_rendezvous():
    pts = pair("219000001", "219000002")
    ids = {"219000001": Identity(50, "PILOT 1"), "219000002": Identity(50, "PILOT 2")}
    assert [a.rule for a in detect(pts, [], Thresholds(), ids)].count("RENDEZVOUS") == 0
    # Same tracks without identity data still alert: type is context, not a requirement.
    assert [a.rule for a in detect(pts, [], Thresholds(), {})].count("RENDEZVOUS") == 1


def test_tanker_pair_is_boosted_and_labelled():
    pts = pair("273000001", "273000002")
    ids = {
        "273000001": Identity(84, "TANKER ONE", 9000001),
        "273000002": Identity(80, "TANKER TWO", 9000002),
    }
    tanker = [a for a in detect(pts, [], Thresholds(), ids) if a.rule == "RENDEZVOUS"][0]
    cargo_ids = {"273000001": Identity(71, "BOX ONE"), "273000002": Identity(70, "BOX TWO")}
    cargo = [a for a in detect(pts, [], Thresholds(), cargo_ids) if a.rule == "RENDEZVOUS"][0]
    assert tanker.details["tanker_pair"] is True
    assert tanker.score > cargo.score
    assert tanker.details["vessel_types"] == ["Tanker", "Tanker"]
    assert tanker.details["imo_numbers"] == [9000001, 9000002]


def test_fishing_vessel_going_dark_still_alerts():
    pts = drift("230000009", OPEN_SEA, hours=1)
    later = drift("230000009", (OPEN_SEA[0] + 0.5, OPEN_SEA[1]), hours=0.2)
    later = [
        type(p)(
            p.record_id, p.track_id, p.observed_at + timedelta(hours=5), p.lat, p.lon, p.speed_kts
        )
        for p in later
    ]
    alerts = detect(pts + later, [], Thresholds(), {"230000009": Identity(30, "TRAWLER")})
    gap = [a for a in alerts if a.rule == "AIS_GAP"]
    assert len(gap) == 1 and gap[0].details["vessel_types"] == ["Fishing"]


def test_names_and_types_stay_aligned_when_one_vessel_unknown():
    a = Alert("RENDEZVOUS", "VESSEL", "111111111|222222222", 0.5, [], start_at=T0, end_at=T0)
    enrich(a, {"222222222": Identity(52, "TUG")})
    assert a.details["vessel_names"] == ["MMSI 111111111", "TUG"]
    assert a.details["vessel_types"] == ["Unknown", "Tug"]


# ---------------------------------------------------------------- v4 STS tuning
# Tuned on 129 live v3 RENDEZVOUS alerts: 87 involved a vessel under 30 m, 43 a class B
# transponder, and most large pairs were ferries or ships anchored ~0.4 nm apart.
from watchfloor.detect.rules import Point, suppress_recurring_spots  # noqa: E402
from watchfloor.detect.runner import sts_capable, track_classes  # noqa: E402


def with_class(pts, cls):
    return [
        Point(
            p.record_id,
            p.track_id,
            p.observed_at,
            p.lat,
            p.lon,
            p.speed_kts,
            p.course_deg,
            p.nav_status,
            cls,
        )
        for p in pts
    ]


@pytest.mark.parametrize(
    ("ident", "cls", "expected"),
    [
        (Identity(80, "TANKER", length_m=180), "A", True),
        (None, "A", True),  # unknown identity stays in scope: can't evade by staying silent
        (Identity(80, "TANKER", length_m=180), "B", False),  # class B = small craft
        (Identity(37, "YACHT", length_m=12), "A", False),  # too small
        (Identity(60, "FERRY", length_m=150), "A", False),  # ferries berth side by side
        (Identity(79, "COASTER", length_m=None), "A", True),
    ],
)
def test_sts_capable(ident, cls, expected):
    ids = {"1": ident} if ident else {}
    assert sts_capable("1", ids, {"1": cls}, 50.0) is expected


def test_track_class_is_majority_vote():
    pts = with_class(drift("1", OPEN_SEA, hours=1), "A")
    pts[0] = with_class([pts[0]], "B")[0]
    assert track_classes(pts) == {"1": "A"}


def test_small_craft_alongside_are_not_rendezvous():
    pts = with_class(pair("265000001", "265000002"), "B")
    assert [a.rule for a in detect(pts, [], Thresholds(), {})].count("RENDEZVOUS") == 0


def test_ships_anchored_apart_are_not_rendezvous():
    a = drift("273000001", OPEN_SEA, hours=1, step_min=5, speed=0.5, seed=1)
    b = drift(
        "273000002", (OPEN_SEA[0] + 0.006, OPEN_SEA[1]), hours=1, step_min=5, speed=0.5, seed=2
    )  # ~0.36 nm apart: anchorage neighbours (alerted under v3's 0.5 nm)
    assert [x.rule for x in detect(a + b, [], Thresholds(), {})].count("RENDEZVOUS") == 0


def test_recurring_spot_with_rotating_vessels_is_suppressed():
    def rdv(a, b, lat=57.0):
        return Alert(
            "RENDEZVOUS", "VESSEL", f"{a}|{b}", 0.5, [], lat=lat, lon=19.0, start_at=T0, end_at=T0
        )

    berth = [rdv("1", "2"), rdv("2", "3"), rdv("3", "4")]  # four ships rotate through
    sts = [rdv("8", "9", lat=58.0)]  # one pair elsewhere
    kept, dropped = suppress_recurring_spots(berth + sts)
    assert dropped == 3 and [a.track_id for a in kept] == ["8|9"]


def test_tanker_alongside_cargo_outranks_unidentified_pair():
    pts = pair("273000001", "273000002")
    known = {
        "273000001": Identity(80, "TANKER", length_m=80),
        "273000002": Identity(70, "CARGO", length_m=108),
    }
    tc = [a for a in detect(pts, [], Thresholds(), known) if a.rule == "RENDEZVOUS"][0]
    # Identity table populated (as in production), but neither of these two vessels is in it.
    others = {"999999999": Identity(70, "SOMEONE ELSE")}
    unk = [a for a in detect(pts, [], Thresholds(), others) if a.rule == "RENDEZVOUS"][0]
    assert tc.details["tanker_alongside_cargo"] and unk.details["identity_unknown"]
    assert tc.score > unk.score


def test_retraction_never_overrides_a_human_decision():
    """Regression: the v4 retraction flipped an analyst-REJECTED alert to RETRACTED."""
    from watchfloor.detect.runner import RETRACT_SQL

    assert "STATUS = 'NEW'" in RETRACT_SQL
