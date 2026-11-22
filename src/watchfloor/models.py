"""Canonical track-point schemas.

Every external feed is treated as UNTRUSTED input: it must validate against these models
before it reaches Kafka's clean topics or Snowflake. Anything that fails goes to the dead-letter
topic with the reason, so bad data is visible rather than silently dropped.

Each record carries a deterministic ``record_id`` (hash of source + raw payload). The analyst
agent cites these IDs in its reports, which is what makes every claim traceable to source data.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def make_record_id(source: str, raw: dict | str) -> str:
    payload = raw if isinstance(raw, str) else json.dumps(raw, sort_keys=True, default=str)
    return hashlib.sha256(f"{source}|{payload}".encode()).hexdigest()[:32]


class TrackPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    record_id: str
    source: Literal["opensky", "aisstream"]
    observed_at: datetime
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    speed_kts: float | None = Field(default=None, ge=0, le=2000)
    course_deg: float | None = Field(default=None, ge=0, lt=360)
    raw_payload: str

    @field_validator("observed_at")
    @classmethod
    def _not_in_future(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            v = v.replace(tzinfo=UTC)
        # Allow small clock skew; reject obviously bogus timestamps.
        if (v - datetime.now(UTC)).total_seconds() > 300:
            raise ValueError("observed_at is in the future")
        return v


class AircraftPosition(TrackPoint):
    source: Literal["opensky"] = "opensky"
    icao24: str = Field(pattern=r"^[0-9a-f]{6}$")
    callsign: str | None = Field(default=None, max_length=8)
    origin_country: str | None = Field(default=None, max_length=64)
    baro_altitude_m: float | None = Field(default=None, ge=-500, le=25000)
    on_ground: bool = False
    squawk: str | None = Field(default=None, pattern=r"^[0-7]{4}$")


class VesselPosition(TrackPoint):
    source: Literal["aisstream"] = "aisstream"
    mmsi: int = Field(ge=100_000_000, le=999_999_999)
    ship_name: str | None = Field(default=None, max_length=64)
    nav_status: int | None = Field(default=None, ge=0, le=15)
    true_heading: int | None = Field(default=None, ge=0, le=359)

    @field_validator("course_deg", mode="before")
    @classmethod
    def _ais_course_unavailable(cls, v):
        # AIS encodes "not available" as 360.
        return None if v is not None and v >= 360 else v

    @field_validator("speed_kts", mode="before")
    @classmethod
    def _ais_speed_unavailable(cls, v):
        # AIS encodes "not available" as 102.3 knots.
        return None if v is not None and v >= 102.2 else v

    @field_validator("true_heading", mode="before")
    @classmethod
    def _ais_heading_unavailable(cls, v):
        return None if v == 511 else v


class VesselStatic(BaseModel):
    """AIS static/voyage data (message 5, or message 24 part B): who the vessel says it is.

    Self-reported and sometimes wrong; used to give detectors context, not as ground truth.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    record_id: str
    source: Literal["aisstream"] = "aisstream"
    observed_at: datetime
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    mmsi: int = Field(ge=100_000_000, le=999_999_999)
    ship_type: int = Field(ge=1, le=99)
    imo: int | None = Field(default=None, ge=1_000_000, le=9_999_999)
    call_sign: str | None = Field(default=None, max_length=10)
    ship_name: str | None = Field(default=None, max_length=64)
    destination: str | None = Field(default=None, max_length=32)
    length_m: float | None = Field(default=None, gt=0, le=500)
    beam_m: float | None = Field(default=None, gt=0, le=80)
    draught_m: float | None = Field(default=None, gt=0, le=30)
    raw_payload: str

    @field_validator("imo", "length_m", "beam_m", "draught_m", mode="before")
    @classmethod
    def _zero_means_unavailable(cls, v):
        return None if v in (0, 0.0) else v

    @field_validator("call_sign", "ship_name", "destination", mode="before")
    @classmethod
    def _blank_means_unavailable(cls, v):
        if isinstance(v, str):
            v = v.replace("@", "").strip()  # AIS pads text fields with '@'
        return v or None

    @field_validator("observed_at")
    @classmethod
    def _not_in_future(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            v = v.replace(tzinfo=UTC)
        if (v - datetime.now(UTC)).total_seconds() > 300:
            raise ValueError("observed_at is in the future")
        return v
