"""Read-only investigation tools for the analyst agent (AGT-01).

Every tool:
  * only reads (Snowflake SELECTs, the local OFAC copy); nothing can change alert or report state,
  * returns compact JSON whose sensor rows carry their ``record_id`` so the model can cite them,
  * adds every ID it returns to ``allowed_ids`` (the grounding check rejects anything else),
  * is written to the hash-chained audit log with its arguments and result size.

Free-text fields from AIS (ship names, destinations, call signs) are attacker-controllable, so
they are returned inside JSON values and the system prompt tells the model to treat all tool
output as data, never as instructions (AGT-04).
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import timedelta

import pandas as pd
from anthropic import beta_tool

from watchfloor.agent import sanctions
from watchfloor.detect.infrastructure import nearest_asset
from watchfloor.detect.ship_types import label
from watchfloor.guardrails.audit import AuditLog

MAX_POINTS = 60
QueryFn = Callable[[str, dict], pd.DataFrame]

TRACK_SQL = """
SELECT RECORD_ID, OBSERVED_AT, LAT, LON, SPEED_KTS, COURSE_DEG, NAV_STATUS
FROM {table}
WHERE {id_col} = %(track_id)s
  AND OBSERVED_AT BETWEEN %(start)s::TIMESTAMP_TZ AND %(end)s::TIMESTAMP_TZ
QUALIFY ROW_NUMBER() OVER (PARTITION BY RECORD_ID ORDER BY LOADED_AT) = 1
ORDER BY OBSERVED_AT
"""
NEARBY_SQL = """
SELECT TO_VARCHAR(v.MMSI) AS MMSI, v.RECORD_ID, v.OBSERVED_AT, v.LAT, v.LON, v.SPEED_KTS,
       v.NAV_STATUS, COALESCE(i.SHIP_NAME, v.SHIP_NAME) AS NAME, i.SHIP_TYPE, i.LENGTH_M
FROM RAW.VESSEL_POSITIONS v
LEFT JOIN CORE.VESSEL_IDENTITY i ON i.MMSI = v.MMSI
WHERE v.OBSERVED_AT BETWEEN %(start)s::TIMESTAMP_TZ AND %(end)s::TIMESTAMP_TZ
  AND v.LAT BETWEEN %(lat0)s AND %(lat1)s AND v.LON BETWEEN %(lon0)s AND %(lon1)s
QUALIFY ROW_NUMBER() OVER (PARTITION BY v.MMSI ORDER BY v.OBSERVED_AT DESC) = 1
"""
IDENTITY_SQL = """
SELECT RECORD_ID, OBSERVED_AT, SHIP_TYPE, IMO, CALL_SIGN, SHIP_NAME, DESTINATION, LENGTH_M,
       BEAM_M, DRAUGHT_M
FROM RAW.VESSEL_STATIC
WHERE TO_VARCHAR(MMSI) = %(mmsi)s
ORDER BY OBSERVED_AT DESC
LIMIT 1
"""


def _iso(t) -> str:
    return pd.Timestamp(t).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(v, nd=2):
    return None if v is None or pd.isna(v) else round(float(v), nd)


class Investigation:
    """Tools bound to one alert. ``allowed_ids`` accumulates everything the agent may cite."""

    def __init__(self, alert: dict, query: QueryFn, audit: AuditLog | None, sdn=None):
        self.alert = alert
        self.query = query
        self.audit = audit
        self.sdn = sdn  # tuple[SdnVessel] for tests; None -> load the downloaded list
        self.allowed_ids: set[str] = {f"alert:{alert['ALERT_ID']}", *alert.get("evidence", [])}
        self.tool_calls = 0
        self.window_start = pd.Timestamp(alert["START_AT"]).to_pydatetime()
        self.window_end = pd.Timestamp(alert["END_AT"]).to_pydatetime()

    # ------------------------------------------------------------------ helpers
    def _log(self, tool: str, args: dict, n: int) -> None:
        self.tool_calls += 1
        if self.audit:
            self.audit.record(
                "agent:analyst",
                "tool_call",
                alert_id=self.alert["ALERT_ID"],
                tool=tool,
                args=args,
                results=n,
            )

    def _rows(self, df: pd.DataFrame) -> list[dict]:
        if df.empty:
            return []
        if len(df) > MAX_POINTS:  # evenly downsample, always keeping first and last
            idx = sorted({round(i * (len(df) - 1) / (MAX_POINTS - 1)) for i in range(MAX_POINTS)})
            df = df.iloc[idx]
        rows = []
        for r in df.itertuples():
            self.allowed_ids.add(r.RECORD_ID)
            rows.append(
                {
                    "record_id": r.RECORD_ID,
                    "time": _iso(r.OBSERVED_AT),
                    "lat": round(r.LAT, 5),
                    "lon": round(r.LON, 5),
                    "speed_kts": _num(r.SPEED_KTS, 1),
                    "course": _num(r.COURSE_DEG, 0),
                    "nav_status": None if pd.isna(r.NAV_STATUS) else int(r.NAV_STATUS),
                }
            )
        return rows

    # ------------------------------------------------------------------ tool bodies
    def track_history(
        self, track_id: str, hours_before: float = 2.0, hours_after: float = 1.0
    ) -> str:
        hours_before = max(0.0, min(hours_before, 12.0))
        hours_after = max(0.0, min(hours_after, 6.0))
        aircraft = self.alert["TRACK_TYPE"] == "AIRCRAFT"
        sql = TRACK_SQL.format(
            table="RAW.AIRCRAFT_POSITIONS" if aircraft else "RAW.VESSEL_POSITIONS",
            id_col="ICAO24" if aircraft else "TO_VARCHAR(MMSI)",
        )
        if aircraft:
            sql = sql.replace(
                "SPEED_KTS, COURSE_DEG, NAV_STATUS", "SPEED_KTS, COURSE_DEG, NULL AS NAV_STATUS"
            )
        df = self.query(
            sql,
            {
                "track_id": track_id,
                "start": (self.window_start - timedelta(hours=hours_before)).isoformat(),
                "end": (self.window_end + timedelta(hours=hours_after)).isoformat(),
            },
        )
        rows = self._rows(df)
        self._log(
            "get_track_history",
            {"track_id": track_id, "hours_before": hours_before, "hours_after": hours_after},
            len(rows),
        )
        return json.dumps(
            {
                "track_id": track_id,
                "points_total": len(df),
                "points_returned": len(rows),
                "points": rows,
            }
        )

    def vessels_nearby(self, radius_nm: float = 2.0, minutes: int = 60) -> str:
        radius_nm = max(0.1, min(radius_nm, 10.0))
        minutes = max(10, min(minutes, 240))
        lat, lon = self.alert["LAT"], self.alert["LON"]
        mid = self.window_start + (self.window_end - self.window_start) / 2
        dlat = radius_nm / 60
        dlon = dlat / max(0.2, abs(math.cos(math.radians(lat))))
        df = self.query(
            NEARBY_SQL,
            {
                "start": (mid - timedelta(minutes=minutes)).isoformat(),
                "end": (mid + timedelta(minutes=minutes)).isoformat(),
                "lat0": lat - dlat,
                "lat1": lat + dlat,
                "lon0": lon - dlon,
                "lon1": lon + dlon,
            },
        )
        vessels = []
        for r in df.itertuples():
            self.allowed_ids.add(r.RECORD_ID)
            vessels.append(
                {
                    "mmsi": r.MMSI,
                    "name": r.NAME,
                    "type": label(None if pd.isna(r.SHIP_TYPE) else int(r.SHIP_TYPE)),
                    "length_m": _num(r.LENGTH_M, 0),
                    "record_id": r.RECORD_ID,
                    "time": _iso(r.OBSERVED_AT),
                    "speed_kts": _num(r.SPEED_KTS, 1),
                    "lat": round(r.LAT, 5),
                    "lon": round(r.LON, 5),
                }
            )
        self._log("get_vessels_nearby", {"radius_nm": radius_nm, "minutes": minutes}, len(vessels))
        return json.dumps(
            {
                "centre": [lat, lon],
                "radius_nm": radius_nm,
                "window_minutes": minutes,
                "vessels": vessels[:40],
            }
        )

    def vessel_identity(self, mmsi: str) -> str:
        df = self.query(IDENTITY_SQL, {"mmsi": str(mmsi)})
        if df.empty:
            self._log("get_vessel_identity", {"mmsi": mmsi}, 0)
            return json.dumps(
                {
                    "mmsi": mmsi,
                    "static_data": None,
                    "note": "This vessel has never broadcast AIS static data.",
                }
            )
        r = df.iloc[0]
        self.allowed_ids.add(r.RECORD_ID)
        self._log("get_vessel_identity", {"mmsi": mmsi}, 1)
        return json.dumps(
            {
                "mmsi": mmsi,
                "record_id": r.RECORD_ID,
                "reported_at": _iso(r.OBSERVED_AT),
                "ship_type": label(None if pd.isna(r.SHIP_TYPE) else int(r.SHIP_TYPE)),
                "imo": None if pd.isna(r.IMO) else int(r.IMO),
                "call_sign": r.CALL_SIGN,
                "name": r.SHIP_NAME,
                "destination": r.DESTINATION,
                "length_m": _num(r.LENGTH_M, 0),
                "beam_m": _num(r.BEAM_M, 0),
                "draught_m": _num(r.DRAUGHT_M, 1),
                "caveat": "Self-reported over AIS; can be wrong or spoofed.",
            }
        )

    def sanctions_check(self, imo: int = 0, mmsi: str = "", name: str = "") -> str:
        result = sanctions.screen(imo or None, mmsi or None, name or None, vessels=self.sdn)
        for m in result["matches"]:
            self.allowed_ids.add(m["citation"][1:-1])  # "[sdn:123]" -> "sdn:123"
        self._log(
            "check_sanctions", {"imo": imo, "mmsi": mmsi, "name": name}, len(result["matches"])
        )
        return json.dumps(result)

    def infrastructure_nearby(self) -> str:
        asset = nearest_asset(self.alert["LAT"], self.alert["LON"], search_nm=10.0)
        self._log("get_nearest_infrastructure", {}, int(asset is not None))
        if asset is None:
            return json.dumps({"nearest_within_10nm": None})
        return json.dumps(
            {
                "kind": asset.kind,
                "name": asset.name,
                "distance_nm": asset.distance_nm,
                "source": "EMODnet overlay",
                "cite_as": f"[alert:{self.alert['ALERT_ID']}]",
            }
        )

    # ------------------------------------------------------------------ SDK tool wrappers
    def tools(self) -> list:
        inv = self

        @beta_tool
        def get_track_history(
            track_id: str, hours_before: float = 2.0, hours_after: float = 1.0
        ) -> str:
            """Positions of one track around the alert window, oldest first, each with a
            record_id to cite. Downsampled to at most 60 points.

            Args:
                track_id: MMSI (vessel) or ICAO24 (aircraft). For a two-vessel alert, call once
                    per vessel.
                hours_before: Hours before the alert window to include (max 12).
                hours_after: Hours after the alert window to include (max 6).
            """
            return inv.track_history(track_id, hours_before, hours_after)

        @beta_tool
        def get_vessels_nearby(radius_nm: float = 2.0, minutes: int = 60) -> str:
            """Other vessels near the alert position around the middle of the alert window:
            latest position per vessel with identity and a record_id to cite.

            Args:
                radius_nm: Search radius in nautical miles (max 10).
                minutes: Minutes either side of the window midpoint (max 240).
            """
            return inv.vessels_nearby(radius_nm, minutes)

        @beta_tool
        def get_vessel_identity(mmsi: str) -> str:
            """The vessel's latest self-reported AIS static data: type, IMO, call sign,
            dimensions, destination, with a record_id to cite.

            Args:
                mmsi: The vessel's 9-digit MMSI.
            """
            return inv.vessel_identity(mmsi)

        @beta_tool
        def check_sanctions(imo: int = 0, mmsi: str = "", name: str = "") -> str:
            """Screen a vessel against the US Treasury OFAC SDN list. Matches carry an
            [sdn:...] citation and a strength (IMO strong, MMSI medium, name-only weak).

            Args:
                imo: 7-digit IMO number, or 0 if unknown.
                mmsi: 9-digit MMSI, or empty.
                name: Vessel name, or empty.
            """
            return inv.sanctions_check(imo, mmsi, name)

        @beta_tool
        def get_nearest_infrastructure() -> str:
            """Nearest undersea cable or pipeline to the alert position (within 10 nm)."""
            return inv.infrastructure_nearby()

        return [
            get_track_history,
            get_vessels_nearby,
            get_vessel_identity,
            check_sanctions,
            get_nearest_infrastructure,
        ]
