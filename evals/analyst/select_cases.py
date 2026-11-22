"""Select and freeze eval cases for the analyst agent, and print the raw evidence per case.

    python evals/analyst/select_cases.py            # writes evals/analyst/cases.jsonl (unlabelled)

Cases are real alerts, stratified by rule, frozen as JSON snapshots: the detector keeps
updating live alerts (windows extend, details change), so the eval must not read them live.
The tools still query RAW sensor tables, which are append-only, so a frozen window returns the
same records on every rerun. The evidence printed here is computed from raw data, independent
of any model output, to support human labelling.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from watchfloor.dashboard import data  # noqa: E402
from watchfloor.dashboard.app import parse_alerts  # noqa: E402
from watchfloor.detect.infrastructure import nearest_asset  # noqa: E402

OUT = Path(__file__).parent / "cases.jsonl"
PER_RULE = {"SANCTIONS": 1, "RENDEZVOUS": 6, "LOITER": 6, "AIS_GAP": 6, "ML_ANOMALY": 5}

TRACK_SQL = """
SELECT COUNT(*) N, MIN(SPEED_KTS) SMIN, MEDIAN(SPEED_KTS) SMED, MAX(SPEED_KTS) SMAX,
       LISTAGG(DISTINCT NAV_STATUS, ',') STATUSES
FROM RAW.VESSEL_POSITIONS
WHERE TO_VARCHAR(MMSI) = %(id)s
  AND OBSERVED_AT BETWEEN %(s)s::TIMESTAMP_TZ AND %(e)s::TIMESTAMP_TZ
"""
IDENT_SQL = """
SELECT SHIP_NAME, SHIP_TYPE, LENGTH_M, IMO, DESTINATION
FROM CORE.VESSEL_IDENTITY WHERE TO_VARCHAR(MMSI) = %(id)s
"""


def pick(alerts: list[dict]) -> list[dict]:
    chosen = []
    for rule, n in PER_RULE.items():
        pool = sorted(
            (a for a in alerts if a["RULE"] == rule), key=lambda a: (-a["SCORE"], a["ALERT_ID"])
        )
        if rule == "LOITER":  # all near-infrastructure loiterers, then the highest others
            near = [a for a in pool if a["details"].get("near_infrastructure")]
            rest = [a for a in pool if a not in near]
            pool = near + rest
        if rule == "AIS_GAP":  # spread across gap lengths rather than only the top scores
            pool = sorted(pool, key=lambda a: a["details"].get("gap_hours", 0))
            step = max(1, len(pool) // n)
            pool = pool[::step]
        chosen += pool[:n]
    return chosen


def evidence(a: dict) -> dict:
    ev = {"rule": a["RULE"], "score": a["SCORE"], "details": a["details"]}
    if a["TRACK_TYPE"] == "VESSEL":
        ev["vessels"] = []
        for mmsi in a["TRACK_ID"].split("|"):
            t = data.run_query(TRACK_SQL, {"id": mmsi, "s": a["START_AT"], "e": a["END_AT"]})
            i = data.run_query(IDENT_SQL, {"id": mmsi})
            ev["vessels"].append(
                {
                    "mmsi": mmsi,
                    "identity": None
                    if i.empty
                    else {
                        k.lower(): (None if v != v else v) for k, v in i.iloc[0].to_dict().items()
                    },
                    "track": {
                        k.lower(): (None if v != v else (float(v) if k != "STATUSES" else v))
                        for k, v in t.iloc[0].to_dict().items()
                    },
                }
            )
    if a.get("LAT") is not None:
        asset = nearest_asset(a["LAT"], a["LON"], search_nm=10)
        ev["nearest_asset"] = None if asset is None else f"{asset.name} {asset.distance_nm} nm"
    return ev


def main() -> None:
    alerts = [a for a in parse_alerts(data.alerts(hours=24 * 7)) if a["STATUS"] != "RETRACTED"]
    cases = []
    for k, a in enumerate(pick(alerts), start=1):
        snapshot = {
            key: a[key]
            for key in (
                "ALERT_ID",
                "RULE",
                "TRACK_TYPE",
                "TRACK_ID",
                "SCORE",
                "LAT",
                "LON",
                "START_AT",
                "END_AT",
                "details",
                "evidence",
            )
        }
        cases.append(
            {
                "id": f"{a['RULE'].lower()}-{k:02d}",
                "alert": snapshot,
                "evidence": evidence(a),
                "label": None,
                "label_rationale": None,
            }
        )
    with OUT.open("w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, default=str) + "\n")
    for c in cases:
        print(json.dumps({"id": c["id"], **c["evidence"]}, default=str))
    print(f"wrote {len(cases)} cases -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
