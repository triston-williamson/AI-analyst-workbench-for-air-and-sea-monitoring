"""Detection runner: Snowflake tracks -> rules + ML -> OPS.ALERTS (+ audit log).

    python -m watchfloor.detect.runner --once            # one pass over the last 24 h
    python -m watchfloor.detect.runner --interval 300    # every 5 minutes
    python -m watchfloor.detect.runner --once --dry-run  # print alerts, write nothing

Idempotency: each run re-scans a sliding window, so the same episode is seen many times. A new
alert whose rule + track overlaps an existing alert's time window *updates* that alert (extends
its window, refreshes score/evidence) instead of creating a duplicate.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from watchfloor.config import get_settings
from watchfloor.detect import ml, rules
from watchfloor.detect.ports import in_port
from watchfloor.detect.rules import Alert, Point
from watchfloor.guardrails.audit import AuditLog
from watchfloor.load.snowflake_conn import connect

log = logging.getLogger("detect")

ALERT_NAMESPACE = uuid.UUID("6f1c2d1e-9a51-4c4e-8f0e-7c3b3c1a9e10")


@dataclass(frozen=True)
class Thresholds:
    """Single place to tune detectors (DESIGN.md: 'Governable')."""

    ais_gap: timedelta = timedelta(hours=2)
    loiter_radius_nm: float = 1.0
    loiter_min: timedelta = timedelta(hours=3)
    rendezvous_distance_nm: float = 0.5
    rendezvous_max_speed_kts: float = 3.0
    rendezvous_min: timedelta = timedelta(minutes=30)
    orbit_min_turn_deg: float = 720.0
    ml_top_k: int = 10


VESSEL_SQL = """
SELECT RECORD_ID, TO_VARCHAR(MMSI) AS TRACK_ID, OBSERVED_AT, LAT, LON, SPEED_KTS, COURSE_DEG
FROM RAW.VESSEL_POSITIONS
WHERE OBSERVED_AT > DATEADD(hour, -%(hours)s, CURRENT_TIMESTAMP())
QUALIFY ROW_NUMBER() OVER (PARTITION BY RECORD_ID ORDER BY LOADED_AT) = 1
"""
AIRCRAFT_SQL = """
SELECT RECORD_ID, ICAO24 AS TRACK_ID, OBSERVED_AT, LAT, LON, SPEED_KTS, COURSE_DEG
FROM RAW.AIRCRAFT_POSITIONS
WHERE OBSERVED_AT > DATEADD(hour, -%(hours)s, CURRENT_TIMESTAMP())
  AND NOT ON_GROUND
QUALIFY ROW_NUMBER() OVER (PARTITION BY RECORD_ID ORDER BY LOADED_AT) = 1
"""
EXISTING_SQL = """
SELECT ALERT_ID, RULE, TRACK_ID, START_AT, END_AT
FROM OPS.ALERTS
WHERE COALESCE(END_AT, DETECTED_AT) > DATEADD(hour, -%(hours)s, CURRENT_TIMESTAMP())
"""
INSERT_SQL = """
INSERT INTO OPS.ALERTS (ALERT_ID, DETECTED_AT, UPDATED_AT, RULE, TRACK_TYPE, TRACK_ID, SCORE,
                        EVIDENCE_IDS, DETAILS, LAT, LON, START_AT, END_AT, STATUS)
SELECT %(alert_id)s, CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), %(rule)s, %(track_type)s,
       %(track_id)s, %(score)s, PARSE_JSON(%(evidence)s)::ARRAY, PARSE_JSON(%(details)s),
       %(lat)s, %(lon)s, %(start_at)s::TIMESTAMP_TZ, %(end_at)s::TIMESTAMP_TZ, 'NEW'
"""
UPDATE_SQL = """
UPDATE OPS.ALERTS SET
  UPDATED_AT = CURRENT_TIMESTAMP(), SCORE = GREATEST(SCORE, %(score)s),
  EVIDENCE_IDS = PARSE_JSON(%(evidence)s)::ARRAY, DETAILS = PARSE_JSON(%(details)s),
  LAT = %(lat)s, LON = %(lon)s,
  START_AT = LEAST(START_AT, %(start_at)s::TIMESTAMP_TZ),
  END_AT = GREATEST(END_AT, %(end_at)s::TIMESTAMP_TZ)
WHERE ALERT_ID = %(alert_id)s
"""


def load_points(cur, sql: str, hours: int) -> list[Point]:
    rows = cur.execute(sql, {"hours": hours}).fetchall()
    return [
        Point(
            record_id=r[0],
            track_id=str(r[1]),
            observed_at=r[2],
            lat=r[3],
            lon=r[4],
            speed_kts=r[5],
            course_deg=r[6],
        )
        for r in rows
    ]


def detect(vessels: list[Point], aircraft: list[Point], t: Thresholds) -> list[Alert]:
    alerts: list[Alert] = []
    alerts += rules.find_ais_gaps(vessels, min_gap=t.ais_gap, in_port=in_port)
    alerts += rules.find_loitering(
        vessels, radius_nm=t.loiter_radius_nm, min_duration=t.loiter_min, in_port=in_port
    )
    alerts += rules.find_rendezvous(
        vessels,
        max_distance_nm=t.rendezvous_distance_nm,
        max_speed_kts=t.rendezvous_max_speed_kts,
        min_duration=t.rendezvous_min,
        in_port=in_port,
    )
    alerts += rules.find_orbits(aircraft, min_turn_deg=t.orbit_min_turn_deg)
    alerts += ml.score_tracks(vessels, "VESSEL", top_k=t.ml_top_k)
    alerts += ml.score_tracks(aircraft, "AIRCRAFT", top_k=t.ml_top_k)
    return alerts


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    if None in (a_start, a_end, b_start, b_end):
        return False
    return a_start <= b_end and b_start <= a_end


def match_existing(alert: Alert, existing: list[tuple]) -> str | None:
    """Return the ALERT_ID of an existing alert for the same episode, if any."""
    for alert_id, rule, track_id, start_at, end_at in existing:
        if rule == alert.rule and track_id == alert.track_id:
            if alert.rule == "ML_ANOMALY" or _overlaps(
                start_at, end_at, alert.start_at, alert.end_at
            ):
                return alert_id
    return None


def params_for(alert: Alert, alert_id: str) -> dict:
    return {
        "alert_id": alert_id,
        "rule": alert.rule,
        "track_type": alert.track_type,
        "track_id": alert.track_id,
        "score": alert.score,
        "evidence": json.dumps(alert.evidence_ids),
        "details": json.dumps(alert.details, default=str),
        "lat": alert.lat,
        "lon": alert.lon,
        "start_at": alert.start_at.isoformat() if alert.start_at else None,
        "end_at": alert.end_at.isoformat() if alert.end_at else None,
    }


def run_once(hours: int, dry_run: bool, thresholds: Thresholds, audit: AuditLog | None) -> dict:
    conn = connect()
    try:
        cur = conn.cursor()
        t0 = time.monotonic()
        vessels = load_points(cur, VESSEL_SQL, hours)
        aircraft = load_points(cur, AIRCRAFT_SQL, hours)
        alerts = detect(vessels, aircraft, thresholds)
        summary = {
            "vessel_points": len(vessels),
            "aircraft_points": len(aircraft),
            "alerts": len(alerts),
            "by_rule": {},
            "created": 0,
            "updated": 0,
        }
        for a in alerts:
            summary["by_rule"][a.rule] = summary["by_rule"].get(a.rule, 0) + 1

        if dry_run:
            for a in sorted(alerts, key=lambda a: -a.score):
                print(
                    f"{a.rule:<11} {a.track_type:<8} {a.track_id:<20} "
                    f"score={a.score:<5} {a.details}"
                )
            return summary

        existing = cur.execute(EXISTING_SQL, {"hours": hours * 2}).fetchall()
        for a in alerts:
            match = match_existing(a, existing)
            if match:
                cur.execute(UPDATE_SQL, params_for(a, match))
                summary["updated"] += 1
                action, alert_id = "alert_updated", match
            else:
                key = f"{a.rule}|{a.track_id}|{a.start_at.isoformat() if a.start_at else ''}"
                alert_id = str(uuid.uuid5(ALERT_NAMESPACE, key))
                cur.execute(INSERT_SQL, params_for(a, alert_id))
                existing.append((alert_id, a.rule, a.track_id, a.start_at, a.end_at))
                summary["created"] += 1
                action = "alert_created"
            if audit and action == "alert_created":
                audit.record(
                    "system:detector",
                    action,
                    alert_id=alert_id,
                    rule=a.rule,
                    track_id=a.track_id,
                    score=a.score,
                    evidence_count=len(a.evidence_ids),
                )
        summary["seconds"] = round(time.monotonic() - t0, 1)
        return summary
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--hours", type=int, default=24, help="lookback window (default 24)")
    parser.add_argument("--once", action="store_true", help="run one pass and exit")
    parser.add_argument("--interval", type=int, default=300, help="seconds between passes")
    parser.add_argument("--dry-run", action="store_true", help="print alerts, write nothing")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    for noisy in ("snowflake", "snowflake.connector", "snowflake.connector._core", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    audit = None if args.dry_run else AuditLog(get_settings().audit_log_path)
    thresholds = Thresholds()
    while True:
        started = datetime.now(UTC)
        summary = run_once(args.hours, args.dry_run, thresholds, audit)
        log.info("pass at %s: %s", started.strftime("%H:%M:%SZ"), summary)
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
