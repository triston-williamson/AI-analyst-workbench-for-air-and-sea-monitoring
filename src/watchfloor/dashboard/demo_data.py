"""Read-only stand-in for ``data.py``, backed by a snapshot on disk instead of Snowflake.

The public demo console runs on a few hours of real collected data exported with
``scripts/export_demo_snapshot.py``. ``DemoData`` answers the same calls as ``data.py`` with
the same columns, treating the end of the snapshot as "now". It holds no credentials and
cannot write: review, report drafting and any SQL are refused.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

FILES = (
    "vessel_positions",
    "aircraft_positions",
    "vessel_identity",
    "alerts",
    "reports",
    "evidence",
)


class ReadOnlyDemo(PermissionError):
    """Raised for anything that would write or reach a database in the public demo."""


class DemoData:
    def __init__(self, path: str | Path):
        root = Path(path)
        self.meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
        frames = {name: pd.read_parquet(root / f"{name}.parquet") for name in FILES}
        self._vessels = frames["vessel_positions"]
        self._aircraft = frames["aircraft_positions"]
        self._identity = frames["vessel_identity"]
        self._alerts = frames["alerts"]
        self._reports = frames["reports"]
        self._evidence = frames["evidence"]
        audit = root / "audit.jsonl"
        self._audit = (
            [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines() if line]
            if audit.exists()
            else []
        )
        self.start = pd.Timestamp(self.meta["start"])
        self.end = pd.Timestamp(self.meta["end"])

    # ------------------------------------------------------------------- clock and writes
    def now(self) -> datetime:
        """The snapshot's "now": its last observation."""
        return self.end.to_pydatetime()

    def run_query(self, sql: str, params: dict | None = None) -> pd.DataFrame:
        raise ReadOnlyDemo("The public demo has no database connection.")

    def execute(self, sql: str, params: dict) -> int:
        raise ReadOnlyDemo("The public demo is read-only.")

    def set_review(self, alert_id: str, status: str, reviewer: str, note: str | None) -> int:
        raise ReadOnlyDemo("Review is disabled in the public demo.")

    # ------------------------------------------------------------------- helpers
    def _since(self, df: pd.DataFrame, delta: timedelta, col: str = "OBSERVED_AT") -> pd.DataFrame:
        return df[(df[col] > self.end - delta) & (df[col] <= self.end)]

    def _vessel_rows(self, df: pd.DataFrame) -> pd.DataFrame:
        """Latest report per vessel, shaped like LIVE_VESSELS_SQL."""
        latest = df.sort_values("OBSERVED_AT").drop_duplicates("MMSI", keep="last")
        ident = self._identity[["MMSI", "SHIP_NAME", "SHIP_TYPE"]].rename(
            columns={"SHIP_NAME": "ID_NAME"}
        )
        m = latest.merge(ident, on="MMSI", how="left")
        return pd.DataFrame(
            {
                "ID": m["MMSI"].astype(str),
                "NAME": m["ID_NAME"].fillna(m["SHIP_NAME"]),
                "SHIP_TYPE": m["SHIP_TYPE"],
                "LAT": m["LAT"],
                "LON": m["LON"],
                "SPEED_KTS": m["SPEED_KTS"],
                "COURSE": m["COURSE_DEG"].fillna(m["TRUE_HEADING"]),
                "NAV_STATUS": m["NAV_STATUS"],
                "OBSERVED_AT": m["OBSERVED_AT"],
            }
        )

    def _aircraft_rows(self, df: pd.DataFrame) -> pd.DataFrame:
        """Latest report per aircraft that is airborne, shaped like LIVE_AIRCRAFT_SQL."""
        latest = df.sort_values("OBSERVED_AT").drop_duplicates("ICAO24", keep="last")
        latest = latest[~latest["ON_GROUND"].astype(bool)]
        return pd.DataFrame(
            {
                "ID": latest["ICAO24"],
                "NAME": latest["CALLSIGN"].fillna(latest["ICAO24"]),
                "ORIGIN_COUNTRY": latest["ORIGIN_COUNTRY"],
                "LAT": latest["LAT"],
                "LON": latest["LON"],
                "SPEED_KTS": latest["SPEED_KTS"],
                "COURSE": latest["COURSE_DEG"],
                "BARO_ALTITUDE_M": latest["BARO_ALTITUDE_M"],
                "OBSERVED_AT": latest["OBSERVED_AT"],
            }
        ).reset_index(drop=True)

    # ------------------------------------------------------------------- live picture and replay
    def live_picture(self, minutes: int = 30) -> tuple[pd.DataFrame, pd.DataFrame]:
        delta = timedelta(minutes=minutes)
        return (
            self._vessel_rows(self._since(self._vessels, delta)),
            self._aircraft_rows(self._since(self._aircraft, delta)),
        )

    def replay_bounds(self, hours: int = 12) -> tuple[datetime | None, datetime | None]:
        rows = self._since(self._vessels, timedelta(hours=hours))
        if rows.empty:
            return None, None
        return rows["OBSERVED_AT"].min().to_pydatetime(), rows["OBSERVED_AT"].max().to_pydatetime()

    def snapshot(self, at: datetime, window_minutes: int = 10) -> tuple[pd.DataFrame, pd.DataFrame]:
        at = pd.Timestamp(at)
        lo = at - timedelta(minutes=window_minutes)

        def window(df: pd.DataFrame) -> pd.DataFrame:
            return df[(df["OBSERVED_AT"] >= lo) & (df["OBSERVED_AT"] <= at)]

        return self._vessel_rows(window(self._vessels)), self._aircraft_rows(window(self._aircraft))

    # ------------------------------------------------------------------- heatmaps
    def traffic_density(self, hours: int = 24) -> pd.DataFrame:
        rows = self._since(self._vessels, timedelta(hours=hours))
        grid = pd.DataFrame({"LAT": rows["LAT"].round(2), "LON": rows["LON"].round(2)})
        return grid.groupby(["LAT", "LON"]).size().rename("N").reset_index()

    def dark_events(self, hours: int = 72) -> pd.DataFrame:
        a = self._recent_alerts(hours)
        a = a[(a["RULE"] == "AIS_GAP") & a["LAT"].notna()]
        gap = a["DETAILS"].map(lambda d: (json.loads(d) if d else {}).get("gap_hours"))
        return pd.DataFrame(
            {
                "LAT": a["LAT"],
                "LON": a["LON"],
                "SCORE": a["SCORE"],
                "TRACK_ID": a["TRACK_ID"],
                "GAP_HOURS": pd.to_numeric(gap, errors="coerce"),
            }
        ).reset_index(drop=True)

    # ------------------------------------------------------------------- alerts
    def _recent_alerts(self, hours: int) -> pd.DataFrame:
        a = self._alerts
        when = a["END_AT"].fillna(a["DETECTED_AT"])
        return a[(a["STATUS"] != "RETRACTED") & (when > self.end - timedelta(hours=hours))]

    def alerts(self, hours: int = 72) -> pd.DataFrame:
        return self._recent_alerts(hours).reset_index(drop=True)

    # ------------------------------------------------------------------- tracks and evidence
    def track(
        self, track_type: str, ids: list[str], start: datetime, end: datetime
    ) -> pd.DataFrame:
        lo, hi = pd.Timestamp(start), pd.Timestamp(end)
        if track_type == "AIRCRAFT":
            df = self._aircraft[self._aircraft["ICAO24"].isin(ids)]
            out = pd.DataFrame({"ID": df["ICAO24"]})
            nav = None
        else:
            df = self._vessels[self._vessels["MMSI"].astype(str).isin([str(i) for i in ids])]
            out = pd.DataFrame({"ID": df["MMSI"].astype(str)})
            nav = df["NAV_STATUS"]
        out["OBSERVED_AT"] = df["OBSERVED_AT"]
        out["LAT"], out["LON"] = df["LAT"], df["LON"]
        out["SPEED_KTS"], out["COURSE"] = df["SPEED_KTS"], df["COURSE_DEG"]
        out["NAV_STATUS"] = nav
        out = out[(out["OBSERVED_AT"] >= lo) & (out["OBSERVED_AT"] <= hi)]
        return out.sort_values(["ID", "OBSERVED_AT"]).reset_index(drop=True)

    def evidence(self, record_ids: list[str]) -> pd.DataFrame:
        if not record_ids:
            return pd.DataFrame()
        ev = self._evidence[self._evidence["RECORD_ID"].isin(record_ids)]
        return ev.sort_values("OBSERVED_AT").reset_index(drop=True)

    # ------------------------------------------------------------------- AI reports
    def latest_report(self, alert_id: str) -> dict | None:
        r = self._reports[self._reports["ALERT_ID"] == alert_id]
        if r.empty:
            return None
        row = r.sort_values("DRAFTED_AT").iloc[-1].drop(labels=["ALERT_ID"]).to_dict()
        for k in ("UNCITED", "UNKNOWN_CITATIONS"):
            row[k] = json.loads(row[k]) if isinstance(row.get(k), str) and row[k] else []
        return row

    def all_reports(self, limit: int = 100) -> pd.DataFrame:
        latest = self._reports.sort_values("DRAFTED_AT").drop_duplicates("ALERT_ID", keep="last")
        cols = ["ALERT_ID", "RULE", "TRACK_TYPE", "TRACK_ID", "SCORE", "STATUS", "START_AT",
                "END_AT", "DETAILS"]
        m = latest.merge(self._alerts[cols], on="ALERT_ID", how="left")
        keep = ["ALERT_ID", "DRAFTED_AT", "MODEL", "DRAFT_MARKDOWN", "GROUNDING_SCORE",
                "GROUNDING_PASSED", *cols[1:]]
        return m[keep].sort_values("DRAFTED_AT", ascending=False).head(limit).reset_index(drop=True)

    def reported_alert_ids(self) -> list[str]:
        return self._reports["ALERT_ID"].drop_duplicates().tolist()

    # ------------------------------------------------------------------- health and audit
    def health(self) -> dict:
        """The health strip as it read at the end of the snapshot."""
        # Reference = the last observation. Load times can't be: rows replayed from Kafka after
        # a restart carry load times hours after the window.
        v10 = self._since(self._vessels, timedelta(minutes=10))
        a10 = self._since(self._aircraft, timedelta(minutes=10))
        v5 = self._since(self._vessels, timedelta(minutes=5))
        a5 = self._since(self._aircraft, timedelta(minutes=5))
        lag = (v5["LOADED_AT"] - v5["OBSERVED_AT"]).dt.total_seconds()
        latency = lag[lag < 3600].median()  # leave out late replays
        made = self._alerts.loc[self._alerts["DETECTED_AT"] <= self.end, "DETECTED_AT"]
        detector_age = (self.end - made.max()).total_seconds() if len(made) else None
        return {
            "VESSELS": float(v10["MMSI"].nunique()),
            "AIRCRAFT": float(a10.loc[~a10["ON_GROUND"].astype(bool), "ICAO24"].nunique()),
            "ROWS_5MIN": float(len(v5) + len(a5)),
            "LATENCY_S": None if pd.isna(latency) else float(latency),
            "FRESHNESS_S": 0.0,
            "REJECTED_1H": float(self.meta.get("rejected_last_hour") or 0),
            "OPEN_ALERTS": float((self._alerts["STATUS"] == "NEW").sum()),
            "DETECTOR_AGE_S": detector_age,
        }

    def audit_history(self, alert_id: str, path: str | None = None) -> list[dict]:
        return [e for e in self._audit if e.get("details", {}).get("alert_id") == alert_id]
