"""Read/write access for the operator console.

One long-lived Snowflake connection is reused (and re-opened if it drops), so the console isn't
logging in every refresh. All queries are parameterised. Timestamps come back timezone-aware
and are displayed in Zulu (UTC).
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path

import pandas as pd

from watchfloor.config import get_settings
from watchfloor.load.snowflake_conn import connect

log = logging.getLogger("dashboard.data")

_conn = None
_lock = threading.Lock()


def _query(sql: str, params: dict | None = None) -> pd.DataFrame:
    """Run a query on the shared connection; reconnect once if the session has gone stale."""
    global _conn
    with _lock:
        for attempt in (1, 2):
            try:
                if _conn is None or _conn.is_closed():
                    _conn = connect()
                return _conn.cursor().execute(sql, params or {}).fetch_pandas_all()
            except Exception:
                if attempt == 2:
                    raise
                log.warning("Snowflake query failed; reconnecting", exc_info=True)
                try:
                    if _conn is not None:
                        _conn.close()
                finally:
                    _conn = None
    raise RuntimeError("unreachable")


def _execute(sql: str, params: dict) -> int:
    global _conn
    with _lock:
        if _conn is None or _conn.is_closed():
            _conn = connect()
        cur = _conn.cursor()
        cur.execute(sql, params)
        return cur.rowcount or 0


# --------------------------------------------------------------------------- live picture
LIVE_VESSELS_SQL = """
SELECT TO_VARCHAR(v.MMSI) AS ID, COALESCE(i.SHIP_NAME, v.SHIP_NAME) AS NAME, i.SHIP_TYPE,
       v.LAT, v.LON, v.SPEED_KTS, COALESCE(v.COURSE_DEG, v.TRUE_HEADING) AS COURSE,
       v.NAV_STATUS, v.OBSERVED_AT
FROM CORE.VESSEL_LATEST v
LEFT JOIN CORE.VESSEL_IDENTITY i ON i.MMSI = v.MMSI
WHERE v.OBSERVED_AT > DATEADD(minute, -%(minutes)s, CURRENT_TIMESTAMP())
"""
LIVE_AIRCRAFT_SQL = """
SELECT ICAO24 AS ID, COALESCE(CALLSIGN, ICAO24) AS NAME, ORIGIN_COUNTRY, LAT, LON, SPEED_KTS,
       COURSE_DEG AS COURSE, BARO_ALTITUDE_M, OBSERVED_AT
FROM CORE.AIRCRAFT_LATEST
WHERE OBSERVED_AT > DATEADD(minute, -%(minutes)s, CURRENT_TIMESTAMP()) AND NOT ON_GROUND
"""


def live_picture(minutes: int = 30) -> tuple[pd.DataFrame, pd.DataFrame]:
    return (
        _query(LIVE_VESSELS_SQL, {"minutes": minutes}),
        _query(LIVE_AIRCRAFT_SQL, {"minutes": minutes}),
    )


# --------------------------------------------------------------------------- replay
SNAPSHOT_VESSELS_SQL = """
SELECT TO_VARCHAR(v.MMSI) AS ID, COALESCE(i.SHIP_NAME, v.SHIP_NAME) AS NAME, i.SHIP_TYPE,
       v.LAT, v.LON, v.SPEED_KTS, COALESCE(v.COURSE_DEG, v.TRUE_HEADING) AS COURSE,
       v.NAV_STATUS, v.OBSERVED_AT
FROM RAW.VESSEL_POSITIONS v
LEFT JOIN CORE.VESSEL_IDENTITY i ON i.MMSI = v.MMSI
WHERE v.OBSERVED_AT BETWEEN DATEADD(minute, -%(window)s, %(at)s::TIMESTAMP_TZ)
                        AND %(at)s::TIMESTAMP_TZ
QUALIFY ROW_NUMBER() OVER (PARTITION BY v.MMSI ORDER BY v.OBSERVED_AT DESC) = 1
"""
SNAPSHOT_AIRCRAFT_SQL = """
SELECT ICAO24 AS ID, COALESCE(CALLSIGN, ICAO24) AS NAME, ORIGIN_COUNTRY, LAT, LON, SPEED_KTS,
       COURSE_DEG AS COURSE, BARO_ALTITUDE_M, OBSERVED_AT
FROM RAW.AIRCRAFT_POSITIONS
WHERE OBSERVED_AT BETWEEN DATEADD(minute, -%(window)s, %(at)s::TIMESTAMP_TZ)
                      AND %(at)s::TIMESTAMP_TZ
  AND NOT ON_GROUND
QUALIFY ROW_NUMBER() OVER (PARTITION BY ICAO24 ORDER BY OBSERVED_AT DESC) = 1
"""
BOUNDS_SQL = """
SELECT MIN(OBSERVED_AT) AS T0, MAX(OBSERVED_AT) AS T1
FROM RAW.VESSEL_POSITIONS
WHERE OBSERVED_AT > DATEADD(hour, -%(hours)s, CURRENT_TIMESTAMP())
"""


def replay_bounds(hours: int = 12) -> tuple[datetime | None, datetime | None]:
    row = _query(BOUNDS_SQL, {"hours": hours}).iloc[0]
    if pd.isna(row.T0):
        return None, None
    return pd.Timestamp(row.T0).to_pydatetime(), pd.Timestamp(row.T1).to_pydatetime()


def snapshot(at: datetime, window_minutes: int = 10) -> tuple[pd.DataFrame, pd.DataFrame]:
    params = {"at": at.isoformat(), "window": window_minutes}
    return _query(SNAPSHOT_VESSELS_SQL, params), _query(SNAPSHOT_AIRCRAFT_SQL, params)


# --------------------------------------------------------------------------- heatmaps
DENSITY_SQL = """
SELECT ROUND(LAT, 2) AS LAT, ROUND(LON, 2) AS LON, COUNT(*) AS N
FROM RAW.VESSEL_POSITIONS
WHERE OBSERVED_AT > DATEADD(hour, -%(hours)s, CURRENT_TIMESTAMP())
GROUP BY 1, 2
"""
DARK_SQL = """
SELECT LAT, LON, SCORE, TRACK_ID, DETAILS:gap_hours::FLOAT AS GAP_HOURS
FROM OPS.ALERTS
WHERE RULE = 'AIS_GAP' AND STATUS <> 'RETRACTED' AND LAT IS NOT NULL
  AND COALESCE(END_AT, DETECTED_AT) > DATEADD(hour, -%(hours)s, CURRENT_TIMESTAMP())
"""


def traffic_density(hours: int = 24) -> pd.DataFrame:
    return _query(DENSITY_SQL, {"hours": hours})


def dark_events(hours: int = 72) -> pd.DataFrame:
    return _query(DARK_SQL, {"hours": hours})


# --------------------------------------------------------------------------- alerts
ALERTS_SQL = """
SELECT ALERT_ID, RULE, TRACK_TYPE, TRACK_ID, SCORE, STATUS, LAT, LON, START_AT, END_AT,
       DETECTED_AT, UPDATED_AT, REVIEWED_BY, REVIEWED_AT, REVIEW_NOTE,
       TO_JSON(DETAILS) AS DETAILS, TO_JSON(EVIDENCE_IDS) AS EVIDENCE_IDS
FROM OPS.ALERTS
WHERE STATUS <> 'RETRACTED'
  AND COALESCE(END_AT, DETECTED_AT) > DATEADD(hour, -%(hours)s, CURRENT_TIMESTAMP())
"""
REVIEW_SQL = """
UPDATE OPS.ALERTS
SET STATUS = %(status)s, REVIEWED_BY = %(reviewer)s, REVIEWED_AT = CURRENT_TIMESTAMP(),
    REVIEW_NOTE = %(note)s
WHERE ALERT_ID = %(alert_id)s AND STATUS <> 'RETRACTED'
"""


def alerts(hours: int = 72) -> pd.DataFrame:
    return _query(ALERTS_SQL, {"hours": hours})


def set_review(alert_id: str, status: str, reviewer: str, note: str | None) -> int:
    if status not in ("APPROVED", "REJECTED", "NEW"):
        raise ValueError(f"invalid review status {status!r}")
    return _execute(
        REVIEW_SQL,
        {"alert_id": alert_id, "status": status, "reviewer": reviewer, "note": note or None},
    )


# --------------------------------------------------------------------------- tracks & evidence
VESSEL_TRACK_SQL = """
SELECT TO_VARCHAR(MMSI) AS ID, OBSERVED_AT, LAT, LON, SPEED_KTS, COURSE_DEG AS COURSE, NAV_STATUS
FROM RAW.VESSEL_POSITIONS
WHERE TO_VARCHAR(MMSI) IN (SELECT VALUE::STRING FROM TABLE(FLATTEN(PARSE_JSON(%(ids)s))))
  AND OBSERVED_AT BETWEEN %(start)s::TIMESTAMP_TZ AND %(end)s::TIMESTAMP_TZ
QUALIFY ROW_NUMBER() OVER (PARTITION BY RECORD_ID ORDER BY LOADED_AT) = 1
ORDER BY ID, OBSERVED_AT
"""
AIRCRAFT_TRACK_SQL = """
SELECT ICAO24 AS ID, OBSERVED_AT, LAT, LON, SPEED_KTS, COURSE_DEG AS COURSE, NULL AS NAV_STATUS
FROM RAW.AIRCRAFT_POSITIONS
WHERE ICAO24 IN (SELECT VALUE::STRING FROM TABLE(FLATTEN(PARSE_JSON(%(ids)s))))
  AND OBSERVED_AT BETWEEN %(start)s::TIMESTAMP_TZ AND %(end)s::TIMESTAMP_TZ
QUALIFY ROW_NUMBER() OVER (PARTITION BY RECORD_ID ORDER BY LOADED_AT) = 1
ORDER BY ID, OBSERVED_AT
"""
EVIDENCE_SQL = """
SELECT * FROM (
  SELECT 'VESSEL' AS KIND, RECORD_ID, TO_VARCHAR(MMSI) AS TRACK_ID, OBSERVED_AT, LAT, LON,
         SPEED_KTS, COURSE_DEG, NAV_STATUS, RAW_PAYLOAD
  FROM RAW.VESSEL_POSITIONS
  WHERE RECORD_ID IN (SELECT VALUE::STRING FROM TABLE(FLATTEN(PARSE_JSON(%(ids)s))))
  UNION ALL
  SELECT 'AIRCRAFT', RECORD_ID, ICAO24, OBSERVED_AT, LAT, LON, SPEED_KTS, COURSE_DEG, NULL,
         RAW_PAYLOAD
  FROM RAW.AIRCRAFT_POSITIONS
  WHERE RECORD_ID IN (SELECT VALUE::STRING FROM TABLE(FLATTEN(PARSE_JSON(%(ids)s))))
)
QUALIFY ROW_NUMBER() OVER (PARTITION BY RECORD_ID ORDER BY OBSERVED_AT) = 1
ORDER BY OBSERVED_AT
"""


def track(track_type: str, ids: list[str], start: datetime, end: datetime) -> pd.DataFrame:
    sql = AIRCRAFT_TRACK_SQL if track_type == "AIRCRAFT" else VESSEL_TRACK_SQL
    return _query(sql, {"ids": json.dumps(ids), "start": start.isoformat(), "end": end.isoformat()})


def evidence(record_ids: list[str]) -> pd.DataFrame:
    if not record_ids:
        return pd.DataFrame()
    return _query(EVIDENCE_SQL, {"ids": json.dumps(record_ids)})


# --------------------------------------------------------------------------- system health
HEALTH_SQL = """
SELECT
  (SELECT COUNT(DISTINCT MMSI) FROM RAW.VESSEL_POSITIONS
    WHERE OBSERVED_AT > DATEADD(minute, -10, CURRENT_TIMESTAMP())) AS VESSELS,
  (SELECT COUNT(DISTINCT ICAO24) FROM RAW.AIRCRAFT_POSITIONS
    WHERE OBSERVED_AT > DATEADD(minute, -10, CURRENT_TIMESTAMP()) AND NOT ON_GROUND) AS AIRCRAFT,
  (SELECT COUNT(*) FROM RAW.VESSEL_POSITIONS
    WHERE LOADED_AT > DATEADD(minute, -5, CURRENT_TIMESTAMP())) +
  (SELECT COUNT(*) FROM RAW.AIRCRAFT_POSITIONS
    WHERE LOADED_AT > DATEADD(minute, -5, CURRENT_TIMESTAMP())) AS ROWS_5MIN,
  (SELECT MEDIAN(DATEDIFF(second, OBSERVED_AT, LOADED_AT)) FROM RAW.VESSEL_POSITIONS
    WHERE LOADED_AT > DATEADD(minute, -5, CURRENT_TIMESTAMP())) AS LATENCY_S,
  (SELECT DATEDIFF(second, MAX(LOADED_AT), CURRENT_TIMESTAMP()) FROM RAW.VESSEL_POSITIONS)
    AS FRESHNESS_S,
  (SELECT COUNT(*) FROM OPS.REJECTED_RECORDS
    WHERE REJECTED_AT > DATEADD(hour, -1, CURRENT_TIMESTAMP())) AS REJECTED_1H,
  (SELECT COUNT(*) FROM OPS.ALERTS WHERE STATUS = 'NEW') AS OPEN_ALERTS,
  (SELECT DATEDIFF(second, MAX(UPDATED_AT), CURRENT_TIMESTAMP()) FROM OPS.ALERTS)
    AS DETECTOR_AGE_S
"""


def health() -> dict:
    row = _query(HEALTH_SQL).iloc[0].to_dict()
    # Snowflake returns NUMBER as Decimal; the console only needs floats.
    return {k: (None if pd.isna(v) else float(v)) for k, v in row.items()}


# --------------------------------------------------------------------------- audit history
def audit_history(alert_id: str, path: str | None = None) -> list[dict]:
    """Audit-log entries that mention this alert, oldest first."""
    p = Path(path or get_settings().audit_log_path)
    if not p.exists():
        return []
    out = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            if alert_id not in line:
                continue
            entry = json.loads(line)
            if entry.get("details", {}).get("alert_id") == alert_id:
                out.append(entry)
    return out
