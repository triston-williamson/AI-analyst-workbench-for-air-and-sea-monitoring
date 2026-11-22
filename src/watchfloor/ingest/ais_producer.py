"""Stream live AIS from aisstream.io (websocket) and publish to Kafka.

Positions go to ``ais.positions``. Static data (ship type, IMO, call sign, dimensions,
destination) goes to ``ais.static``; detectors use the ship type for context.

aisstream docs: https://aisstream.io/documentation
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

import websockets

from watchfloor.config import TOPIC_AIS, TOPIC_AIS_STATIC, get_settings
from watchfloor.ingest.kafka_utils import Publisher, make_producer
from watchfloor.models import VesselPosition, VesselStatic, make_record_id

log = logging.getLogger("ais")

STREAM_URL = "wss://stream.aisstream.io/v0/stream"
POSITION_TYPES = ["PositionReport", "StandardClassBPositionReport"]
STATIC_TYPES = ["ShipStaticData", "StaticDataReport"]


def parse_ais_time(value: str | None) -> datetime | None:
    """aisstream sends e.g. '2026-11-21 14:03:07.123456789 +0000 UTC'."""
    if not value:
        return None
    stamp = value.replace(" UTC", "").strip()
    date_part, time_part, tz = stamp.split(" ")
    if "." in time_part:  # trim nanoseconds to microseconds
        hms, frac = time_part.split(".")
        time_part = f"{hms}.{frac[:6]}"
    return datetime.fromisoformat(f"{date_part}T{time_part}{tz[:3]}:{tz[3:]}").astimezone(UTC)


def message_to_record(msg: dict, raw: str) -> dict | None:
    mtype = msg.get("MessageType")
    if mtype not in POSITION_TYPES:
        return None
    meta = msg.get("MetaData", {})
    body = msg.get("Message", {}).get(mtype, {})
    return {
        "record_id": make_record_id("aisstream", raw),
        "observed_at": parse_ais_time(meta.get("time_utc")),
        "mmsi": meta.get("MMSI"),
        "ship_name": (meta.get("ShipName") or "").strip() or None,
        "lat": body.get("Latitude", meta.get("latitude")),
        "lon": body.get("Longitude", meta.get("longitude")),
        "speed_kts": body.get("Sog"),
        "course_deg": body.get("Cog"),
        "true_heading": body.get("TrueHeading"),
        "nav_status": body.get("NavigationalStatus"),
        "raw_payload": raw,
    }


def _dims(dim: dict | None) -> tuple[float | None, float | None]:
    """AIS gives distances from the antenna to bow (A), stern (B), port (C), starboard (D)."""
    if not dim:
        return None, None
    length = (dim.get("A") or 0) + (dim.get("B") or 0)
    beam = (dim.get("C") or 0) + (dim.get("D") or 0)
    return (length or None), (beam or None)


def static_to_record(msg: dict, raw: str) -> dict | None:
    """Map a ShipStaticData (class A) or StaticDataReport part B (class B) message.

    Returns None for messages that carry no ship type (e.g. class B part A, name only).
    """
    mtype = msg.get("MessageType")
    meta = msg.get("MetaData", {})
    body = msg.get("Message", {}).get(mtype, {})
    if mtype == "ShipStaticData":
        ship_type, name = body.get("Type"), body.get("Name")
        call_sign, imo = body.get("CallSign"), body.get("ImoNumber")
        destination, draught = body.get("Destination"), body.get("MaximumStaticDraught")
        length, beam = _dims(body.get("Dimension"))
    elif mtype == "StaticDataReport":
        part_b = body.get("ReportB") or {}
        if not part_b.get("Valid"):
            return None
        ship_type, name = part_b.get("ShipType"), meta.get("ShipName")
        call_sign, imo, destination, draught = part_b.get("CallSign"), None, None, None
        length, beam = _dims(part_b.get("Dimension"))
    else:
        return None
    if not ship_type:
        return None
    return {
        "record_id": make_record_id("aisstream", raw),
        "observed_at": parse_ais_time(meta.get("time_utc")),
        "mmsi": meta.get("MMSI"),
        "ship_type": ship_type,
        "imo": imo,
        "call_sign": call_sign,
        "ship_name": name or meta.get("ShipName"),
        "destination": destination,
        "length_m": length,
        "beam_m": beam,
        "draught_m": draught,
        "raw_payload": raw,
    }


async def stream() -> None:
    cfg = get_settings()
    if not cfg.aisstream_api_key:
        raise SystemExit("Set AISSTREAM_API_KEY in .env (free at https://aisstream.io)")
    min_lat, min_lon, max_lat, max_lon = cfg.aoi_bbox
    producer = make_producer()
    pub = Publisher(producer, TOPIC_AIS)
    pub_static = Publisher(producer, TOPIC_AIS_STATIC)
    subscribe = {
        "APIKey": cfg.aisstream_api_key.get_secret_value(),
        "BoundingBoxes": [[[min_lat, min_lon], [max_lat, max_lon]]],
        "FilterMessageTypes": POSITION_TYPES + STATIC_TYPES,
    }
    backoff = 5
    while True:
        try:
            # aisstream's handshake can take well over the library's 10 s default.
            async with websockets.connect(STREAM_URL, open_timeout=60, ping_timeout=60) as ws:
                await ws.send(json.dumps(subscribe))
                log.info("Subscribed to aisstream for bbox %s", cfg.aoi_bbox)
                backoff = 5
                async for raw in ws:
                    raw = raw.decode() if isinstance(raw, bytes) else raw
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if "error" in msg:
                        raise SystemExit(f"aisstream error: {msg['error']}")
                    try:
                        if msg.get("MessageType") in STATIC_TYPES:
                            static = static_to_record(msg, raw)
                            if static:
                                pub_static.publish(VesselStatic, static, key=str(static["mmsi"]))
                            continue
                        record = message_to_record(msg, raw)
                    except (ValueError, TypeError) as exc:
                        log.debug("Unparseable AIS message: %s", exc)
                        continue
                    if record:
                        pub.publish(VesselPosition, record, key=str(record["mmsi"]))
                        if (pub.ok + pub.rejected) % 500 == 0:
                            pub.flush_and_report()
                            pub_static.flush_and_report()
        except (websockets.ConnectionClosed, OSError) as exc:
            log.warning("AIS stream dropped (%s); reconnecting in %ss", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(stream())


if __name__ == "__main__":
    main()
