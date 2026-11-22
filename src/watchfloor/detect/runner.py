"""Detection runner: Snowflake tracks -> rules + ML -> OPS.ALERTS (+ audit log).

    python -m watchfloor.detect.runner --once            # one pass over the last 24 h
    python -m watchfloor.detect.runner --interval 300    # every 5 minutes
    python -m watchfloor.detect.runner --once --dry-run  # print alerts, write nothing
    python -m watchfloor.detect.runner --retract-rule RENDEZVOUS --reason "..."

Idempotency: each run re-scans a sliding window, so the same episode is seen many times. A new
alert whose rule + track overlaps an existing alert's time window *updates* that alert (extends
its window, refreshes score/evidence) instead of creating a duplicate.

Versioning: every alert records the DETECTOR_VERSION that produced it. When a detector is fixed,
alerts from older versions can be retracted (status RETRACTED, reason kept, nothing deleted).

Storm guard: if one rule would create more than STORM_LIMIT new alerts in a single pass, that is
almost always a detector fault (e.g. an unknown harbour), not dozens of real incidents. The
rule's new alerts are held back and the event is logged and audited instead of flooding the
analyst queue.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pandas as pd
from snowflake.connector.pandas_tools import write_pandas

from watchfloor.config import get_settings
from watchfloor.detect import ml, rules, ship_types
from watchfloor.detect.infrastructure import nearest_asset
from watchfloor.detect.ports import in_port
from watchfloor.detect.rules import Alert, Point
from watchfloor.guardrails.audit import AuditLog
from watchfloor.load.snowflake_conn import connect
from watchfloor.logsetup import configure as configure_logging

log = logging.getLogger("detect")

ALERT_NAMESPACE = uuid.UUID("6f1c2d1e-9a51-4c4e-8f0e-7c3b3c1a9e10")
# v2: moored-status and crowd filters for RENDEZVOUS/LOITER, expanded port list.
# v3: AIS ship type: service craft excluded from LOITER/RENDEZVOUS/ML; tanker pairs boosted.
# v4: RENDEZVOUS limited to ships capable of ship-to-ship transfer (class A, >= 50 m, not
#     passenger) lying alongside (<= 0.15 nm). Tuned on 129 live v3 alerts; see DESIGN.md.
# v5: ML baseline = merchant ships at sea: same vessel pool as v4 RENDEZVOUS, positions in
#     port zones dropped, >= 2 h / 20 points per track, top 5 above an absolute score.
#     Tuned on 43 live v4 alerts; see DESIGN.md.
# v6: LOITER limited to the same vessel pool; anchorage clusters suppressed; loiterers near
#     undersea cables/pipelines always kept and ranked up. Tuned on 163 live alerts.
# v7: SANCTIONS rule: vessels whose IMO or MMSI matches the OFAC SDN list (priority).
DETECTOR_VERSION = "7"
INFRA_NEAR_NM = 1.0
INFRA_BOOST = 0.3
NAV_STATUS = {
    0: "under way",
    1: "at anchor",
    3: "restricted manoeuvrability",
    5: "moored",
    7: "fishing",
    15: "not defined",
}
TANKER_PAIR_BOOST = 0.3
TANKER_CARGO_BOOST = 0.2  # bunkering or cargo transfer alongside a cargo ship
UNKNOWN_IDENTITY_PENALTY = 0.2  # neither vessel has ever said what it is: less to go on
STORM_LIMIT = 50


@dataclass(frozen=True)
class Thresholds:
    """Single place to tune detectors (DESIGN.md: 'Governable')."""

    ais_gap: timedelta = timedelta(hours=2)
    loiter_radius_nm: float = 1.0
    loiter_min: timedelta = timedelta(hours=3)
    loiter_cluster_radius_nm: float = 3.0  # anchorages spread a few nm; 2 nm missed Gothenburg's
    loiter_cluster_min_neighbours: int = 2
    # Ships transferring cargo are moored alongside: antenna-to-antenna well under 0.15 nm.
    # At 0.5 nm (v3) neighbours in an anchorage alerted too.
    rendezvous_distance_nm: float = 0.15
    sts_min_length_m: float = 50.0
    rendezvous_max_speed_kts: float = 3.0
    rendezvous_min: timedelta = timedelta(minutes=30)
    orbit_min_turn_deg: float = 720.0
    ml_top_k: int = 5
    ml_min_percentile: float = 0.99
    ml_min_anomaly_score: float = 0.65
    ml_vessel_min_hours: float = 2.0
    ml_aircraft_min_hours: float = 0.5
    ml_min_points: int = 20


VESSEL_SQL = """
SELECT RECORD_ID, TO_VARCHAR(MMSI) AS TRACK_ID, OBSERVED_AT, LAT, LON, SPEED_KTS, COURSE_DEG,
       NAV_STATUS,
       -- AIS class isn't a column, but the raw message kept on every row says which report
       -- type it was: class B position reports come only from small (non-SOLAS) craft.
       IFF(RAW_PAYLOAD ILIKE '%%StandardClassBPositionReport%%', 'B', 'A') AS AIS_CLASS
FROM RAW.VESSEL_POSITIONS
WHERE OBSERVED_AT > DATEADD(hour, -%(hours)s, CURRENT_TIMESTAMP())
QUALIFY ROW_NUMBER() OVER (PARTITION BY RECORD_ID ORDER BY LOADED_AT) = 1
"""
AIRCRAFT_SQL = """
SELECT RECORD_ID, ICAO24 AS TRACK_ID, OBSERVED_AT, LAT, LON, SPEED_KTS, COURSE_DEG,
       NULL AS NAV_STATUS, NULL AS AIS_CLASS
FROM RAW.AIRCRAFT_POSITIONS
WHERE OBSERVED_AT > DATEADD(hour, -%(hours)s, CURRENT_TIMESTAMP())
  AND NOT ON_GROUND
QUALIFY ROW_NUMBER() OVER (PARTITION BY RECORD_ID ORDER BY LOADED_AT) = 1
"""
IDENTITY_SQL = """
SELECT TO_VARCHAR(MMSI), SHIP_TYPE, SHIP_NAME, IMO, LENGTH_M
FROM CORE.VESSEL_IDENTITY
"""
# Names from position reports, for vessels that never sent static data (naval vessels often
# don't): the position messages still carry the name.
POSITION_NAMES_SQL = """
SELECT TO_VARCHAR(MMSI), MAX_BY(SHIP_NAME, OBSERVED_AT)
FROM RAW.VESSEL_POSITIONS
WHERE OBSERVED_AT > DATEADD(hour, -24, CURRENT_TIMESTAMP()) AND SHIP_NAME IS NOT NULL
GROUP BY 1
"""
EXISTING_SQL = """
SELECT ALERT_ID, RULE, TRACK_ID, START_AT, END_AT
FROM OPS.ALERTS
WHERE COALESCE(END_AT, DETECTED_AT) > DATEADD(hour, -%(hours)s, CURRENT_TIMESTAMP())
  AND STATUS <> 'RETRACTED'
"""
# Alerts are staged with write_pandas (one bulk load), then applied with two set-based
# statements, instead of one round trip per alert (~0.35 s each).
STAGE_TABLE = "ALERTS_STAGE"  # also hard-coded in the two SQL strings below
APPLY_UPDATES_SQL = """
UPDATE OPS.ALERTS a SET
  UPDATED_AT = CURRENT_TIMESTAMP(), SCORE = GREATEST(a.SCORE, s.SCORE),
  EVIDENCE_IDS = PARSE_JSON(s.EVIDENCE_JSON)::ARRAY, DETAILS = PARSE_JSON(s.DETAILS_JSON),
  LAT = s.LAT, LON = s.LON,
  START_AT = LEAST(a.START_AT, s.START_AT), END_AT = GREATEST(a.END_AT, s.END_AT)
FROM OPS.ALERTS_STAGE s
WHERE a.ALERT_ID = s.ALERT_ID AND NOT s.IS_NEW
"""
APPLY_INSERTS_SQL = """
INSERT INTO OPS.ALERTS (ALERT_ID, DETECTED_AT, UPDATED_AT, RULE, TRACK_TYPE, TRACK_ID, SCORE,
                        EVIDENCE_IDS, DETAILS, LAT, LON, START_AT, END_AT, STATUS)
SELECT ALERT_ID, CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), RULE, TRACK_TYPE, TRACK_ID, SCORE,
       PARSE_JSON(EVIDENCE_JSON)::ARRAY, PARSE_JSON(DETAILS_JSON), LAT, LON, START_AT, END_AT,
       'NEW'
FROM OPS.ALERTS_STAGE
WHERE IS_NEW
"""
RETRACT_SQL = """
UPDATE OPS.ALERTS
SET STATUS = 'RETRACTED', UPDATED_AT = CURRENT_TIMESTAMP(),
    DETAILS = OBJECT_INSERT(DETAILS, 'retracted_reason', %(reason)s, TRUE)
WHERE RULE = %(rule)s
  AND STATUS = 'NEW'  -- never override a human decision (APPROVED / REJECTED)
  AND COALESCE(DETAILS:detector_version::STRING, '1') <> %(version)s
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
            nav_status=r[7],
            ais_class=r[8],
        )
        for r in rows
    ]


@dataclass(frozen=True)
class Identity:
    ship_type: int | None
    name: str | None = None
    imo: int | None = None
    length_m: float | None = None


def load_identities(cur) -> dict[str, Identity]:
    ids = {
        r[0]: Identity(ship_type=r[1], name=r[2], imo=r[3], length_m=r[4])
        for r in cur.execute(IDENTITY_SQL).fetchall()
    }
    for mmsi, name in cur.execute(POSITION_NAMES_SQL).fetchall():
        if mmsi not in ids and name:
            ids[mmsi] = Identity(ship_type=None, name=name)  # name only: type/size unknown
    return ids


def _knows_what_it_is(ident: Identity | None) -> bool:
    return ident is not None and (ident.ship_type is not None or ident.length_m is not None)


def _type_of(identities: dict[str, Identity], mmsi: str) -> int | None:
    ident = identities.get(mmsi)
    return ident.ship_type if ident else None


def enrich(alert: Alert, identities: dict[str, Identity]) -> Alert:
    """Attach self-reported vessel identity to vessel alerts, and boost tanker-tanker meetings.

    Two tankers stopped side by side at sea is the classic ship-to-ship oil transfer pattern
    used to obscure cargo origin, so it ranks above, say, two cargo ships waiting together.
    """
    if alert.track_type != "VESSEL" or not identities:
        return alert
    mmsis = alert.track_id.split("|")
    types = [_type_of(identities, m) for m in mmsis]
    alert.details["vessel_types"] = [ship_types.label(t) for t in types]
    # One entry per vessel, aligned with vessel_types; fall back to the MMSI when unnamed.
    alert.details["vessel_names"] = [
        (identities[m].name if m in identities and identities[m].name else f"MMSI {m}")
        for m in mmsis
    ]
    lengths = [identities[m].length_m if m in identities else None for m in mmsis]
    if any(lengths):
        alert.details["vessel_lengths_m"] = lengths
    imos = [identities[m].imo for m in mmsis if m in identities and identities[m].imo]
    if imos:
        alert.details["imo_numbers"] = imos
    if alert.rule == "RENDEZVOUS":
        if all(ship_types.is_tanker(t) for t in types):
            alert.details["tanker_pair"] = True
            alert.details["priority"] = True
            alert.score = round(min(1.0, alert.score + TANKER_PAIR_BOOST), 3)
        elif any(ship_types.is_tanker(t) for t in types) and all(
            ship_types.is_cargo_or_tanker(t) for t in types
        ):
            alert.details["tanker_alongside_cargo"] = True
            alert.score = round(min(1.0, alert.score + TANKER_CARGO_BOOST), 3)
        elif not any(_knows_what_it_is(identities.get(m)) for m in mmsis):
            alert.details["identity_unknown"] = True
            alert.score = round(max(0.1, alert.score - UNKNOWN_IDENTITY_PENALTY), 3)
    return alert


def track_classes(points: list[Point]) -> dict[str, str]:
    """Most frequently reported AIS class per track."""
    counts: dict[str, dict[str, int]] = {}
    for p in points:
        if p.ais_class:
            c = counts.setdefault(p.track_id, {})
            c[p.ais_class] = c.get(p.ais_class, 0) + 1
    return {tid: max(c, key=c.get) for tid, c in counts.items()}


def sts_capable(
    mmsi: str, identities: dict[str, Identity], classes: dict[str, str], min_length_m: float
) -> bool:
    """Could this vessel take part in a ship-to-ship cargo transfer?

    Needs an ocean-going ship: a class A transponder (mandatory for SOLAS ships; class B is
    small craft), not a ferry, and at least ``min_length_m`` long when the length is known.
    Unknown type/length stays in scope, so a vessel can't evade by not broadcasting it.
    """
    if classes.get(mmsi) == "B":
        return False
    ident = identities.get(mmsi)
    if ident is None:
        return True
    if ship_types.is_passenger(ident.ship_type):
        return False
    return not (ident.length_m and ident.length_m < min_length_m)


def sanctions_lookup(identities: dict[str, Identity], vessels=None):
    """MMSI -> strongest IMO/MMSI sanctions match, or None. Uses the downloaded OFAC list;
    if it hasn't been downloaded the rule simply finds nothing (and says so once)."""
    from watchfloor.agent import sanctions

    listed = sanctions.load() if vessels is None else vessels
    if listed is None:
        if not getattr(sanctions_lookup, "_warned", False):
            log.warning(
                "OFAC list not downloaded: SANCTIONS rule inactive (run scripts/fetch_sanctions.py)"
            )
            sanctions_lookup._warned = True
        return lambda mmsi: None

    def lookup(mmsi: str):
        ident = identities.get(mmsi)
        imo = ident.imo if ident is not None else None
        res = sanctions.screen(imo=imo, mmsi=mmsi, vessels=listed)
        strong = [m for m in res["matches"] if m["strength"] in ("strong", "medium")]
        return strong[0] if strong else None

    return lookup


def annotate_loiter(alert: Alert, track: list[Point]) -> Alert:
    """Add AIS status and nearest seabed infrastructure; rank up loiterers near cables/pipes."""
    stay = [p for p in track if alert.start_at <= p.observed_at <= alert.end_at]
    statuses = [p.nav_status for p in stay if p.nav_status is not None]
    if statuses:
        code = max(set(statuses), key=statuses.count)
        alert.details["nav_status"] = NAV_STATUS.get(code, f"status {code}")
    asset = nearest_asset(alert.lat, alert.lon)
    if asset is not None:
        alert.details["nearest_asset"] = f"{asset.kind}: {asset.name}"
        alert.details["asset_distance_nm"] = asset.distance_nm
        if asset.distance_nm <= INFRA_NEAR_NM:
            alert.details["near_infrastructure"] = True
            alert.details["priority"] = True
            alert.score = round(min(1.0, alert.score + INFRA_BOOST), 3)
    return alert


def detect(
    vessels: list[Point],
    aircraft: list[Point],
    t: Thresholds,
    identities: dict[str, Identity] | None = None,
) -> list[Alert]:
    identities = identities or {}
    # Harbour/working craft (pilots, tugs, SAR, fishing, pleasure) do loiter and come
    # alongside other ships as their job. They are left out of the behaviour rules and the ML
    # baseline, but kept for AIS_GAP: fishing vessels going dark is itself a known red flag.
    service = {m for m, ident in identities.items() if ship_types.is_service(ident.ship_type)}
    working = [p for p in vessels if p.track_id not in service]

    alerts: list[Alert] = []
    alerts += rules.find_ais_gaps(vessels, min_gap=t.ais_gap, in_port=in_port)
    classes = track_classes(vessels)
    capable = {
        m
        for m in {p.track_id for p in working}
        if sts_capable(m, identities, classes, t.sts_min_length_m)
    }
    # LOITER: merchant ships only (in live data 59 of 163 loiterers were ferries idle at small
    # terminals), anchorage crowds dropped, anything stopped near seabed infrastructure kept.
    by_track = rules.group_tracks([p for p in working if p.track_id in capable])
    loiter = rules.find_loitering(
        [p for p in working if p.track_id in capable],
        radius_nm=t.loiter_radius_nm,
        min_duration=t.loiter_min,
        in_port=in_port,
    )
    for a in loiter:
        annotate_loiter(a, by_track.get(a.track_id, []))

    def near_infra(a: Alert) -> bool:
        return bool(a.details.get("near_infrastructure"))

    # Live data: loiterers that never broadcast type or size were overwhelmingly pilot, rescue
    # and fishing boats (77 -> 28 when dropped), and "engaged in fishing" is normal slow work.
    # Both are kept when near infrastructure; a silent vessel elsewhere is still covered by
    # AIS_GAP and ML.
    loiter = [
        a
        for a in loiter
        if near_infra(a)
        or (
            a.details.get("nav_status") != "fishing"
            and _knows_what_it_is(identities.get(a.track_id))
        )
    ]
    loiter, _ = rules.suppress_anchorage_clusters(
        loiter,
        radius_nm=t.loiter_cluster_radius_nm,
        min_neighbours=t.loiter_cluster_min_neighbours,
        keep=near_infra,
    )
    alerts += loiter
    alerts += rules.find_rendezvous(
        [p for p in working if p.track_id in capable],
        max_distance_nm=t.rendezvous_distance_nm,
        max_speed_kts=t.rendezvous_max_speed_kts,
        min_duration=t.rendezvous_min,
        in_port=in_port,
    )
    alerts, _ = rules.suppress_recurring_spots(alerts)
    alerts += rules.find_sanctioned(vessels, sanctions_lookup(identities))
    alerts += rules.find_orbits(aircraft, min_turn_deg=t.orbit_min_turn_deg)
    # ML baseline: merchant ships at sea. Same transfer-capable pool as RENDEZVOUS (no small
    # craft, ferries or service vessels), and only positions outside port zones: in live runs
    # 23 of 38 ML alerts were ships manoeuvring in harbour, which is normal.
    at_sea = [p for p in working if p.track_id in capable and not in_port(p.lat, p.lon)]
    ml_args = {
        "top_k": t.ml_top_k,
        "min_percentile": t.ml_min_percentile,
        "min_anomaly_score": t.ml_min_anomaly_score,
        "min_points": t.ml_min_points,
    }
    alerts += ml.score_tracks(at_sea, "VESSEL", min_hours=t.ml_vessel_min_hours, **ml_args)
    alerts += ml.score_tracks(aircraft, "AIRCRAFT", min_hours=t.ml_aircraft_min_hours, **ml_args)
    return [enrich(a, identities) for a in alerts]


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


def new_alert_id(alert: Alert) -> str:
    start = alert.start_at.isoformat() if alert.start_at else ""
    key = f"v{DETECTOR_VERSION}|{alert.rule}|{alert.track_id}|{start}"
    return str(uuid.uuid5(ALERT_NAMESPACE, key))


def stage_row(alert: Alert, alert_id: str, is_new: bool) -> dict:
    return {
        "ALERT_ID": alert_id,
        "IS_NEW": is_new,
        "RULE": alert.rule,
        "TRACK_TYPE": alert.track_type,
        "TRACK_ID": alert.track_id,
        "SCORE": alert.score,
        "EVIDENCE_JSON": json.dumps(alert.evidence_ids),
        "DETAILS_JSON": json.dumps(
            {**alert.details, "detector_version": DETECTOR_VERSION}, default=str
        ),
        "LAT": alert.lat,
        "LON": alert.lon,
        "START_AT": alert.start_at,
        "END_AT": alert.end_at,
    }


def plan_writes(
    alerts: list[Alert], existing: list[tuple]
) -> tuple[list[dict], list[tuple[Alert, str]], dict[str, int]]:
    """Decide insert vs update for each alert and apply the storm guard.

    Returns (stage rows, newly created (alert, id) pairs, {rule: held-back count}).
    """
    existing = list(existing)
    rows: list[dict] = []
    created: list[tuple[Alert, str]] = []
    new_by_rule: dict[str, list[tuple[Alert, str]]] = {}
    for a in alerts:
        match = match_existing(a, existing)
        if match:
            rows.append(stage_row(a, match, is_new=False))
            continue
        alert_id = new_alert_id(a)
        existing.append((alert_id, a.rule, a.track_id, a.start_at, a.end_at))
        new_by_rule.setdefault(a.rule, []).append((a, alert_id))

    held: dict[str, int] = {}
    for rule, items in new_by_rule.items():
        if len(items) > STORM_LIMIT:
            # A flood is held back, but priority alerts (near seabed infrastructure, tanker
            # pairs) always get through: noise must not be able to hide them.
            priority = [(a, i) for a, i in items if a.details.get("priority")]
            if len(items) > len(priority):
                held[rule] = len(items) - len(priority)
            items = priority
        for a, alert_id in items:
            rows.append(stage_row(a, alert_id, is_new=True))
            created.append((a, alert_id))
    return rows, created, held


def apply_writes(conn, rows: list[dict]) -> None:
    if not rows:
        return
    df = pd.DataFrame(rows)
    for col in ("START_AT", "END_AT"):
        df[col] = pd.to_datetime(df[col], utc=True, format="ISO8601")
    ok, *_ = write_pandas(
        conn,
        df,
        table_name=STAGE_TABLE,
        schema="OPS",
        auto_create_table=True,
        overwrite=True,
        table_type="temporary",
        use_logical_type=True,
        quote_identifiers=False,
    )
    if not ok:
        raise RuntimeError("staging alerts failed")
    cur = conn.cursor()
    cur.execute(APPLY_UPDATES_SQL)
    cur.execute(APPLY_INSERTS_SQL)


def run_once(hours: int, dry_run: bool, thresholds: Thresholds, audit: AuditLog | None) -> dict:
    conn = connect()
    try:
        cur = conn.cursor()
        t0 = time.monotonic()
        vessels = load_points(cur, VESSEL_SQL, hours)
        aircraft = load_points(cur, AIRCRAFT_SQL, hours)
        identities = load_identities(cur)
        alerts = detect(vessels, aircraft, thresholds, identities)
        summary = {
            "vessel_points": len(vessels),
            "vessels_with_type": sum(1 for m in {p.track_id for p in vessels} if m in identities),
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
        rows, created, held = plan_writes(alerts, existing)
        apply_writes(conn, rows)
        summary["created"] = len(created)
        summary["updated"] = len(rows) - len(created)
        if held:
            summary["held_back"] = held
        for rule, count in held.items():
            log.warning(
                "STORM GUARD: %s would create %d new alerts in one pass (limit %d); held back. "
                "Likely a detector fault: investigate before raising the limit.",
                rule,
                count,
                STORM_LIMIT,
            )
            if audit:
                audit.record("system:detector", "storm_guard_held", rule=rule, count=count)
        if audit:
            for a, alert_id in created:
                audit.record(
                    "system:detector",
                    "alert_created",
                    alert_id=alert_id,
                    rule=a.rule,
                    track_id=a.track_id,
                    score=a.score,
                    evidence_count=len(a.evidence_ids),
                    detector_version=DETECTOR_VERSION,
                )
        summary["seconds"] = round(time.monotonic() - t0, 1)
        return summary
    finally:
        conn.close()


def retract(rule: str, reason: str, audit: AuditLog | None) -> int:
    """Mark a rule's alerts from older detector versions as RETRACTED (reversible; no delete)."""
    if not reason:
        raise SystemExit("--reason is required so reviewers know why alerts were retracted")
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(RETRACT_SQL, {"rule": rule, "reason": reason, "version": DETECTOR_VERSION})
        count = cur.rowcount or 0
    finally:
        conn.close()
    log.info("retracted %d %s alerts from detector versions != %s", count, rule, DETECTOR_VERSION)
    if audit:
        audit.record(
            "system:maintenance",
            "alerts_retracted",
            rule=rule,
            count=count,
            reason=reason,
            current_detector_version=DETECTOR_VERSION,
        )
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--hours", type=int, default=24, help="lookback window (default 24)")
    parser.add_argument("--once", action="store_true", help="run one pass and exit")
    parser.add_argument("--interval", type=int, default=300, help="seconds between passes")
    parser.add_argument("--dry-run", action="store_true", help="print alerts, write nothing")
    parser.add_argument(
        "--retract-rule",
        metavar="RULE",
        help="retract this rule's alerts from older detector versions, then exit",
    )
    parser.add_argument("--reason", default="", help="reason recorded with --retract-rule")
    args = parser.parse_args()

    configure_logging("runner")
    audit = None if args.dry_run else AuditLog(get_settings().audit_log_path)
    if args.retract_rule:
        retract(args.retract_rule, args.reason, audit)
        return
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
