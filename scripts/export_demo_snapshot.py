"""Export a few hours of collected data as a read-only snapshot for the public demo console.

    python scripts/export_demo_snapshot.py --start 2026-11-22T02:45Z --end 2026-11-22T09:00Z

Writes Parquet files, the matching audit-log entries and ``meta.json`` to ``demo_snapshot/``
(git-ignored). Run the console on it with ``DEMO_DIR=demo_snapshot``; see docs/DEMO.md.

What is kept, and what is not:
  * Positions in the window, without raw payloads (the raw message is kept only for the
    records that alerts cite as evidence, so the console can show it).
  * Vessel identity (name, type, IMO, call sign, size, destination) for vessels in the window.
  * Alerts first detected in the window (not retracted), their AI reports and audit entries.
  * Reviewer names are replaced with "analyst"; no secrets or credentials are read.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from watchfloor.config import get_settings  # noqa: E402
from watchfloor.dashboard import data  # noqa: E402

VESSELS_SQL = """
SELECT RECORD_ID, MMSI, SHIP_NAME, LAT, LON, SPEED_KTS, COURSE_DEG, TRUE_HEADING, NAV_STATUS,
       OBSERVED_AT, LOADED_AT
FROM RAW.VESSEL_POSITIONS
WHERE OBSERVED_AT BETWEEN %(start)s::TIMESTAMP_TZ AND %(end)s::TIMESTAMP_TZ
QUALIFY ROW_NUMBER() OVER (PARTITION BY RECORD_ID ORDER BY LOADED_AT) = 1
"""
AIRCRAFT_SQL = """
SELECT RECORD_ID, ICAO24, CALLSIGN, ORIGIN_COUNTRY, LAT, LON, SPEED_KTS, COURSE_DEG,
       BARO_ALTITUDE_M, ON_GROUND, OBSERVED_AT, LOADED_AT
FROM RAW.AIRCRAFT_POSITIONS
WHERE OBSERVED_AT BETWEEN %(start)s::TIMESTAMP_TZ AND %(end)s::TIMESTAMP_TZ
QUALIFY ROW_NUMBER() OVER (PARTITION BY ICAO24, OBSERVED_AT ORDER BY LOADED_AT) = 1
"""
IDENTITY_SQL = """
SELECT i.*
FROM CORE.VESSEL_IDENTITY i
WHERE i.MMSI IN (SELECT DISTINCT MMSI FROM RAW.VESSEL_POSITIONS
                 WHERE OBSERVED_AT BETWEEN %(start)s::TIMESTAMP_TZ AND %(end)s::TIMESTAMP_TZ)
"""
# Alerts first raised during the window. The detector runs every 5 minutes, so allow a few
# minutes past the end for the last pass over the window. Alerts whose episode was extended
# by data collected after the window are left out: their score and evidence describe more
# than the snapshot holds (e.g. an AIS "gap" that spans a pipeline outage).
ALERTS_SQL = """
SELECT ALERT_ID, RULE, TRACK_TYPE, TRACK_ID, SCORE, STATUS, LAT, LON, START_AT, END_AT,
       DETECTED_AT, UPDATED_AT, REVIEWED_BY, REVIEWED_AT, REVIEW_NOTE,
       TO_JSON(DETAILS) AS DETAILS, TO_JSON(EVIDENCE_IDS) AS EVIDENCE_IDS
FROM OPS.ALERTS
WHERE STATUS <> 'RETRACTED'
  AND DETECTED_AT BETWEEN %(start)s::TIMESTAMP_TZ
                      AND DATEADD(minute, 10, %(end)s::TIMESTAMP_TZ)
  AND COALESCE(END_AT, DETECTED_AT) <= DATEADD(minute, 15, %(end)s::TIMESTAMP_TZ)
"""
REPORTS_SQL = """
SELECT ALERT_ID, REPORT_ID, DRAFTED_AT, MODEL, DRAFT_MARKDOWN, GROUNDING_SCORE, GROUNDING_PASSED,
       TO_JSON(UNCITED) AS UNCITED, TO_JSON(UNKNOWN_CITATIONS) AS UNKNOWN_CITATIONS,
       TOOL_CALLS, ATTEMPTS, INPUT_TOKENS, OUTPUT_TOKENS, STOP_REASON
FROM OPS.REPORTS
WHERE ALERT_ID IN (SELECT VALUE::STRING FROM TABLE(FLATTEN(PARSE_JSON(%(ids)s))))
"""
REJECTED_SQL = """
SELECT COUNT(*) AS N FROM OPS.REJECTED_RECORDS
WHERE REJECTED_AT BETWEEN DATEADD(hour, -1, %(end)s::TIMESTAMP_TZ) AND %(end)s::TIMESTAMP_TZ
"""


def to_utc(df: pd.DataFrame) -> pd.DataFrame:
    """Timestamps to UTC and Snowflake NUMBER (Decimal) to float, so Parquet stores them cleanly."""
    for col in df.columns:
        s = df[col]
        if pd.api.types.is_datetime64_any_dtype(s):
            df[col] = pd.to_datetime(s, utc=True)
        elif s.dtype == object and s.dropna().map(lambda v: type(v).__name__ == "Decimal").any():
            df[col] = pd.to_numeric(s)
    return df


def audit_entries(alert_ids: set[str], path: str | None = None) -> list[dict]:
    current = Path(path or get_settings().audit_log_path)
    files = sorted(current.parent.glob(f"{current.stem}-*{current.suffix}")) + [current]
    out = []
    for p in files:
        if not p.exists():
            continue
        with p.open(encoding="utf-8") as f:
            for line in f:
                entry = json.loads(line)
                details = entry.get("details") or {}
                if details.get("alert_id") not in alert_ids:
                    continue
                if str(entry.get("actor", "")).startswith("human:"):
                    entry["actor"] = "human:analyst"
                # Keep only what the console shows; drop hashes and anything else.
                out.append(
                    {
                        "ts": entry["ts"],
                        "actor": entry["actor"],
                        "action": entry["action"],
                        "details": {k: details[k] for k in ("alert_id", "note") if k in details},
                    }
                )
    return sorted(out, key=lambda e: e["ts"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", required=True, help="UTC start, e.g. 2026-11-22T02:45Z")
    ap.add_argument("--end", required=True, help="UTC end, e.g. 2026-11-22T09:00Z")
    ap.add_argument("--out", default="demo_snapshot")
    args = ap.parse_args()
    start = pd.Timestamp(args.start).tz_convert(UTC) if pd.Timestamp(args.start).tzinfo else (
        pd.Timestamp(args.start).tz_localize(UTC))
    end = pd.Timestamp(args.end).tz_convert(UTC) if pd.Timestamp(args.end).tzinfo else (
        pd.Timestamp(args.end).tz_localize(UTC))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    data.execute("ALTER SESSION SET TIMEZONE = 'UTC'", {})
    window = {"start": start.isoformat(), "end": end.isoformat()}

    vessels = to_utc(data.run_query(VESSELS_SQL, window))
    aircraft = to_utc(data.run_query(AIRCRAFT_SQL, window))
    identity = to_utc(data.run_query(IDENTITY_SQL, window))
    alerts = to_utc(data.run_query(ALERTS_SQL, window))
    alerts["REVIEWED_BY"] = alerts["REVIEWED_BY"].where(alerts["REVIEWED_BY"].isna(), "analyst")
    ids = alerts["ALERT_ID"].tolist()
    reports = to_utc(data.run_query(REPORTS_SQL, {"ids": json.dumps(ids)}))

    evidence_ids = sorted({r for e in alerts["EVIDENCE_IDS"].dropna() for r in json.loads(e)})
    evidence = to_utc(data.evidence(evidence_ids))
    rejected = int(data.run_query(REJECTED_SQL, window).iloc[0]["N"])

    files = {
        "vessel_positions": vessels,
        "aircraft_positions": aircraft,
        "vessel_identity": identity,
        "alerts": alerts,
        "reports": reports,
        "evidence": evidence,
    }
    for name, df in files.items():
        df.to_parquet(out / f"{name}.parquet", index=False)
    audit = audit_entries(set(ids))
    with (out / "audit.jsonl").open("w", encoding="utf-8") as f:
        for e in audit:
            f.write(json.dumps(e, default=str) + "\n")

    meta = {
        "start": vessels["OBSERVED_AT"].min().isoformat(),
        "end": vessels["OBSERVED_AT"].max().isoformat(),
        "requested": window,
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rejected_last_hour": rejected,
        "counts": {name: len(df) for name, df in files.items()} | {"audit": len(audit)},
        "sources": "AIS: aisstream.io · ADS-B: OpenSky Network · OFAC SDN list · EMODnet",
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    size = sum(p.stat().st_size for p in out.iterdir()) / 1e6
    print(json.dumps(meta["counts"]), f"{size:.1f} MB ->", out)


if __name__ == "__main__":
    main()
