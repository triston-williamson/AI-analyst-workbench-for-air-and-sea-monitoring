from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from watchfloor.ingest.adsb_producer import state_to_record
from watchfloor.ingest.ais_producer import message_to_record, parse_ais_time
from watchfloor.models import AircraftPosition, VesselPosition

NOW = int(datetime.now(UTC).timestamp())

OPENSKY_STATE = [
    "4ca7b5",
    "RYR12AB ",
    "Ireland",
    NOW,
    NOW,
    18.1,
    57.3,
    11277.6,
    False,
    231.5,
    45.2,
    0.0,
    None,
    11582.4,
    "2213",
    False,
    0,
]


def test_opensky_state_maps_and_validates():
    rec = AircraftPosition.model_validate(state_to_record(OPENSKY_STATE))
    assert rec.icao24 == "4ca7b5"
    assert rec.callsign == "RYR12AB"
    assert rec.speed_kts == pytest.approx(450.0, abs=0.5)
    assert len(rec.record_id) == 32


def test_bad_aircraft_latitude_rejected():
    bad = state_to_record(OPENSKY_STATE)
    bad["lat"] = 123.0
    with pytest.raises(ValidationError):
        AircraftPosition.model_validate(bad)


def test_future_timestamp_rejected():
    bad = state_to_record(OPENSKY_STATE)
    bad["observed_at"] = datetime.now(UTC) + timedelta(hours=1)
    with pytest.raises(ValidationError):
        AircraftPosition.model_validate(bad)


def test_ais_time_parsing():
    ts = parse_ais_time("2026-11-21 14:03:07.123456789 +0000 UTC")
    assert ts == datetime(2026, 11, 21, 14, 3, 7, 123456, tzinfo=UTC)


def test_ais_message_maps_and_handles_not_available_sentinels():
    msg = {
        "MessageType": "PositionReport",
        "MetaData": {
            "MMSI": 273456789,
            "ShipName": "EXAMPLE TANKER  ",
            "time_utc": "2026-11-21 14:03:07.0 +0000 UTC",
        },
        "Message": {
            "PositionReport": {
                "Latitude": 59.4,
                "Longitude": 24.7,
                "Sog": 102.3,
                "Cog": 360,
                "TrueHeading": 511,
                "NavigationalStatus": 0,
            }
        },
    }
    rec = VesselPosition.model_validate(message_to_record(msg, "{}"))
    assert rec.ship_name == "EXAMPLE TANKER"
    assert rec.speed_kts is None and rec.course_deg is None and rec.true_heading is None


def test_non_position_ais_message_ignored():
    assert message_to_record({"MessageType": "ShipStaticData"}, "{}") is None
