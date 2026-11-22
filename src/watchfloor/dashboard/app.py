"""Watchfloor operator console.

    python -m watchfloor.dashboard.app      ->  http://127.0.0.1:8050

Layout: classification banners; system-health strip; map (Live / Replay / Traffic density /
Dark events) with vessel-type colours, heading leaders, alert rings and an undersea
infrastructure overlay; alert queue with filters; alert detail with evidence, audit history and
Approve / Reject (written to OPS.ALERTS and the hash-chained audit log).

Callbacks stay thin: queries live in ``data.py`` and drawing in ``figures.py``.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import pandas as pd
from dash import ALL, Dash, Input, Output, State, ctx, dcc, html, no_update

from watchfloor.config import get_settings
from watchfloor.dashboard import data, figures
from watchfloor.guardrails.audit import AuditLog
from watchfloor.logsetup import configure as configure_logging

log = logging.getLogger("dashboard")

BANNER_TEXT = (
    "UNCLASSIFIED // PUBLIC DATA // DEMONSTRATION SYSTEM - NOT AFFILIATED WITH ANY GOVERNMENT"
)
REFRESH_MS = 30_000
REPLAY_STEP_MIN = 10
RULES = list(figures.RULE_LABELS)
DEFAULT_LAYERS = ["vessels", "aircraft", "alerts", "infrastructure"]


# =========================================================================== helpers
def dtg(ts) -> str:
    """Military date-time group, e.g. 221432Z NOV 26."""
    if ts is None or (isinstance(ts, float) and pd.isna(ts)):
        return "-"
    t = pd.Timestamp(ts)
    t = t.tz_convert("UTC") if t.tzinfo else t.tz_localize("UTC")
    return t.strftime("%d%H%MZ %b %y").upper()


def records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> JSON-safe records (timestamps as ISO strings) for dcc.Store."""
    if df is None or df.empty:
        return []
    return json.loads(df.to_json(orient="records", date_format="iso", date_unit="s"))


def frame(recs: list[dict] | None) -> pd.DataFrame:
    return pd.DataFrame(recs or [])


def parse_alerts(df: pd.DataFrame) -> list[dict]:
    recs = records(df)
    for r in recs:
        r["details"] = json.loads(r.pop("DETAILS") or "{}")
        r["evidence"] = json.loads(r.pop("EVIDENCE_IDS") or "[]")
    return recs


def alert_title(a: dict) -> str:
    names = a.get("details", {}).get("vessel_names") or []
    if names:
        return " + ".join(names)
    return f"{a['TRACK_TYPE'].title()} {a['TRACK_ID']}"


def explain(a: dict) -> str:
    """Plain-language reason an analyst can verify against the evidence."""
    d, rule = a.get("details", {}), a["RULE"]
    types = d.get("vessel_types") or []
    kind = f" ({', '.join(types)})" if types and set(types) != {"Unknown"} else ""
    if rule == "AIS_GAP":
        return (
            f"Stopped transmitting AIS for {d.get('gap_hours')} h and reappeared "
            f"{d.get('distance_nm')} nm away (implied {d.get('implied_speed_kts')} kn){kind}. "
            "Vessels must broadcast continuously; long gaps can hide port calls or transfers."
        )
    if rule == "LOITER":
        return (
            f"Stayed within {d.get('radius_nm')} nm for {d.get('duration_hours')} h away from "
            f"any port{kind}. Prolonged loitering over open water can indicate waiting for a "
            "meeting, or activity over undersea infrastructure."
        )
    if rule == "RENDEZVOUS":
        lead = (
            "TANKER PAIR. "
            if d.get("tanker_pair")
            else "TANKER ALONGSIDE CARGO SHIP. "
            if d.get("tanker_alongside_cargo")
            else ""
        )
        if d.get("identity_unknown"):
            kind += " (neither vessel has broadcast its type or size)"
        return (
            f"{lead}Two vessels{kind} stayed within {d.get('min_separation_nm')} nm of each "
            f"other at low speed for {d.get('duration_minutes')} min away from port, the "
            "signature of a ship-to-ship transfer."
        )
    if rule == "ORBIT":
        return (
            f"Aircraft flew {d.get('circles')} {d.get('direction')} circles in "
            f"{d.get('window_minutes')} min. Sustained orbits indicate surveillance, patrol or "
            "a holding pattern."
        )
    if rule == "ML_ANOMALY":
        drivers = ", ".join(
            f"{x['feature'].replace('_', ' ')} {x['robust_z']:+.1f}σ"
            for x in d.get("top_drivers", [])
        )
        pct = d.get("percentile", 1)
        rank = (
            f"the most unusual of {d.get('tracks_compared')} tracks"
            if pct >= 0.999
            else f"in the top {100 * (1 - pct):.1f}% of {d.get('tracks_compared')} tracks"
        )
        return f"Moved unlike other traffic{kind}: {rank}. Main drivers: {drivers}."
    return ""


def trail_window(a: dict) -> tuple[datetime, datetime]:
    start = pd.Timestamp(a["START_AT"]).to_pydatetime()
    end = pd.Timestamp(a["END_AT"]).to_pydatetime()
    before, after = {
        "AIS_GAP": (timedelta(hours=2), timedelta(hours=1)),
        "RENDEZVOUS": (timedelta(hours=1), timedelta(hours=1)),
        "LOITER": (timedelta(minutes=30), timedelta(minutes=30)),
        "ORBIT": (timedelta(minutes=10), timedelta(minutes=10)),
    }.get(a["RULE"], (timedelta(0), timedelta(0)))
    return start - before, end + after


def _grade(v, ok, warn) -> str:
    if v is None:
        return "warn"
    return "ok" if v <= ok else "warn" if v <= warn else "bad"


def health_tiles(h: dict | None, err: str | None = None) -> list:
    def tile(label, value, status="", title=None):
        return html.Div(
            [html.Div(label, className="k"), html.Div(value, className="v")],
            className=f"tile {status}",
            title=title,
        )

    if err or h is None:
        return [tile("Data", "ERROR", "bad", err)]
    lat, fresh, det = h.get("LATENCY_S"), h.get("FRESHNESS_S"), h.get("DETECTOR_AGE_S")
    rate = (h.get("ROWS_5MIN") or 0) / 5
    rejected = int(h.get("REJECTED_1H") or 0)
    return [
        tile("Vessels (10m)", f"{int(h.get('VESSELS') or 0):,}", "ok"),
        tile("Aircraft (10m)", f"{int(h.get('AIRCRAFT') or 0):,}", "ok"),
        tile("Msgs / min", f"{rate:,.0f}", "ok" if rate > 0 else "bad"),
        tile(
            "Sensor→DB",
            f"{lat:.0f}s" if lat is not None else "-",
            _grade(lat, 120, 600),
            "Median seconds from a vessel's broadcast to it landing in Snowflake",
        ),
        tile(
            "Last load", f"{fresh:.0f}s ago" if fresh is not None else "-", _grade(fresh, 60, 300)
        ),
        tile(
            "Rejected (1h)",
            f"{rejected:,}",
            _grade(rejected, 50, 500),
            "Records that failed validation and went to the dead-letter queue",
        ),
        tile("Open alerts", f"{int(h.get('OPEN_ALERTS') or 0):,}"),
        tile(
            "Detector",
            f"{det / 60:.0f}m ago" if det is not None else "-",
            _grade(det, 600, 1800),
            "Time since the detector last updated an alert",
        ),
    ]


# =========================================================================== layout
def control_group(title: str, component) -> html.Div:
    return html.Div(
        [html.Span(title, className="group-title"), component],
        style={"display": "flex", "alignItems": "center"},
    )


def build_layout() -> html.Div:
    mode = dcc.RadioItems(
        id="mode",
        value="live",
        inline=True,
        options=[
            {"label": "Live", "value": "live"},
            {"label": "Replay", "value": "replay"},
            {"label": "Traffic density", "value": "density"},
            {"label": "Dark events", "value": "dark"},
        ],
    )
    layers = dcc.Checklist(
        id="layers",
        value=DEFAULT_LAYERS,
        inline=True,
        options=[
            {"label": "Vessels", "value": "vessels"},
            {"label": "Aircraft", "value": "aircraft"},
            {"label": "Alerts", "value": "alerts"},
            {"label": "Cables & pipelines", "value": "infrastructure"},
        ],
    )
    attribution = (
        "Basemap © OpenStreetMap, CARTO · AIS aisstream.io · ADS-B OpenSky Network · "
        + figures.infrastructure_attribution()
    )
    map_column = html.Div(
        className="mapcol",
        children=[
            html.Div(
                className="controls",
                children=[control_group("View", mode), control_group("Layers", layers)],
            ),
            html.Div(
                id="replay-row",
                className="replay",
                style={"display": "none"},
                children=[
                    html.Button("▶ Play", id="play", className="btn play"),
                    html.Div(
                        dcc.Slider(
                            id="replay-slider",
                            min=0,
                            max=1,
                            step=1,
                            value=1,
                            marks=None,
                            updatemode="mouseup",
                        ),
                        style={"flex": 1},
                    ),
                    html.Span(id="replay-label", className="replay-label"),
                ],
            ),
            html.Div(
                className="mapwrap",
                children=[
                    dcc.Graph(
                        id="map",
                        style={"height": "100%"},
                        config={"displaylogo": False, "scrollZoom": True},
                    ),
                    html.Div(attribution, className="attribution"),
                ],
            ),
        ],
    )
    sidebar = html.Div(
        className="side",
        children=[
            html.Div(
                [
                    html.H2("ALERT QUEUE", id="queue-title"),
                    html.Button(
                        "Clear selection",
                        id="clear-selection",
                        className="btn ghost",
                        style={"margin": "6px 12px 0 0", "padding": "3px 8px"},
                    ),
                ],
                style={"display": "flex", "justifyContent": "space-between"},
            ),
            html.Div(
                className="filters",
                children=[
                    dcc.Checklist(
                        id="rule-filter",
                        value=RULES,
                        inline=True,
                        options=[{"label": figures.RULE_LABELS[r], "value": r} for r in RULES],
                    ),
                    dcc.RadioItems(
                        id="status-filter",
                        value="NEW",
                        inline=True,
                        options=[
                            {"label": "Open", "value": "NEW"},
                            {"label": "Approved", "value": "APPROVED"},
                            {"label": "Rejected", "value": "REJECTED"},
                            {"label": "All", "value": "ALL"},
                        ],
                    ),
                ],
            ),
            html.Div(id="queue", className="queue"),
            html.Div(id="detail", className="detail"),
        ],
    )
    return html.Div(
        className="console",
        children=[
            html.Div(BANNER_TEXT, className="banner"),
            html.Div(
                className="header",
                children=[
                    html.H1("WATCHFLOOR"),
                    html.Span("Maritime & Air Domain Awareness · Baltic Sea", className="sub"),
                    html.Span(id="clock", className="clock"),
                ],
            ),
            html.Div(id="health", className="health"),
            html.Div(className="body", children=[map_column, sidebar]),
            html.Div(BANNER_TEXT, className="banner"),
            dcc.Store(id="live-data"),
            dcc.Store(id="alerts-data"),
            dcc.Store(id="selected-alert"),
            dcc.Store(id="selected-track"),
            dcc.Store(id="trail-data"),
            dcc.Store(id="detail-key"),
            dcc.Store(id="zoom", data=figures.DEFAULT_ZOOM),
            dcc.Store(id="review-tick", data={"n": 0, "msg": ""}),
            dcc.Store(id="replay-bounds"),
            dcc.Store(id="replay-data"),
            dcc.Store(id="heat-data"),
            dcc.Interval(id="tick", interval=REFRESH_MS, n_intervals=0),
            dcc.Interval(id="play-tick", interval=1500, disabled=True),
        ],
    )


# =========================================================================== callbacks
def register(app: Dash) -> None:  # noqa: C901 - one place to read every interaction
    @app.callback(
        Output("live-data", "data"),
        Output("alerts-data", "data"),
        Output("health", "children"),
        Output("clock", "children"),
        Input("tick", "n_intervals"),
        Input("review-tick", "data"),
    )
    def refresh(_n, _review):
        clock = dtg(datetime.now(UTC))
        try:
            vessels, aircraft = data.live_picture()
            alerts = data.alerts()
            h = data.health()
        except Exception as exc:  # noqa: BLE001 - surface the failure on the console
            log.exception("refresh failed")
            return no_update, no_update, health_tiles(None, str(exc)), clock
        live = {"vessels": records(vessels), "aircraft": records(aircraft)}
        return live, parse_alerts(alerts), health_tiles(h), clock

    @app.callback(
        Output("queue", "children"),
        Output("queue-title", "children"),
        Input("alerts-data", "data"),
        Input("rule-filter", "value"),
        Input("status-filter", "value"),
        Input("selected-alert", "data"),
    )
    def render_queue(alerts, rules, status, selected):
        alerts = alerts or []
        shown = [
            a
            for a in alerts
            if a["RULE"] in (rules or []) and (status == "ALL" or a["STATUS"] == status)
        ]
        shown.sort(key=lambda a: (-a["SCORE"], a["ALERT_ID"]))
        open_n = sum(1 for a in alerts if a["STATUS"] == "NEW")
        title = f"ALERT QUEUE · {len(shown)} shown · {open_n} open"
        if not shown:
            return html.Div("No alerts match these filters.", className="empty"), title
        cards = []
        for a in shown[:200]:
            color = figures.RULE_COLORS.get(a["RULE"], "#fff")
            cards.append(
                html.Div(
                    id={"type": "alert-card", "id": a["ALERT_ID"]},
                    n_clicks=0,
                    className="card selected" if a["ALERT_ID"] == selected else "card",
                    style={"borderLeftColor": color},
                    children=[
                        html.Div(
                            [
                                html.Span(
                                    figures.RULE_LABELS.get(a["RULE"], a["RULE"]),
                                    style={"color": color},
                                ),
                                html.Span(
                                    [
                                        f"{a['SCORE']:.2f} ",
                                        html.Span(a["STATUS"], className=f"badge {a['STATUS']}"),
                                    ]
                                ),
                            ],
                            className="t",
                        ),
                        html.Div(alert_title(a), className="who"),
                        html.Div(f"{dtg(a['START_AT'])} → {dtg(a['END_AT'])}", className="meta"),
                    ],
                )
            )
        return cards, title

    @app.callback(
        Output("selected-alert", "data"),
        Output("selected-track", "data"),
        Input({"type": "alert-card", "id": ALL}, "n_clicks"),
        Input("map", "clickData"),
        Input("clear-selection", "n_clicks"),
        prevent_initial_call=True,
    )
    def select(_cards, click, _clear):
        trig = ctx.triggered_id
        value = ctx.triggered[0]["value"] if ctx.triggered else None
        if trig == "clear-selection":
            return (None, None) if value else (no_update, no_update)
        if isinstance(trig, dict) and trig.get("type") == "alert-card":
            return (trig["id"], None) if value else (no_update, no_update)
        if trig == "map" and click:
            custom = click["points"][0].get("customdata")
            if not custom:
                return no_update, no_update
            kind, ident = custom
            if kind == "alert":
                return ident, None
            return None, {"type": "AIRCRAFT" if kind == "aircraft" else "VESSEL", "id": ident}
        return no_update, no_update

    @app.callback(
        Output("trail-data", "data"),
        Output("zoom", "data", allow_duplicate=True),
        Input("selected-alert", "data"),
        Input("selected-track", "data"),
        State("alerts-data", "data"),
        prevent_initial_call=True,
    )
    def load_trail(alert_id, track_sel, alerts):
        a = None
        try:
            if alert_id:
                a = next((x for x in alerts or [] if x["ALERT_ID"] == alert_id), None)
                if a is None:
                    return None, no_update
                start, end = trail_window(a)
                tr = data.track(a["TRACK_TYPE"], a["TRACK_ID"].split("|"), start, end)
                key = f"alert:{alert_id}"
            elif track_sel:
                end = datetime.now(UTC)
                tr = data.track(track_sel["type"], [track_sel["id"]], end - timedelta(hours=2), end)
                key = f"track:{track_sel['id']}"
            else:
                return None, figures.DEFAULT_ZOOM
        except Exception:  # noqa: BLE001
            log.exception("trail query failed")
            return None, no_update
        lats = tr.LAT.tolist() if not tr.empty else []
        lons = tr.LON.tolist() if not tr.empty else []
        if not lats and a and a.get("LAT") is not None:
            lats, lons = [a["LAT"]], [a["LON"]]
        fit = None
        if lats:
            fit = {
                "center": {"lat": (min(lats) + max(lats)) / 2, "lon": (min(lons) + max(lons)) / 2},
                "zoom": figures.zoom_for_extent(lats, lons),
            }
        # Size heading leaders for the zoom the map is about to jump to.
        return {"key": key, "alert": a, "track": records(tr), "fit": fit}, (
            fit["zoom"] if fit else no_update
        )

    @app.callback(
        Output("zoom", "data"),
        Input("map", "relayoutData"),
        State("zoom", "data"),
        prevent_initial_call=True,
    )
    def track_zoom(relayout, current):
        z = (relayout or {}).get("map.zoom")
        if z is None or abs(z - (current or 0)) < 0.4:
            return no_update
        return z

    # ---------------------------------------------------------------- replay
    @app.callback(
        Output("replay-row", "style"),
        Output("replay-bounds", "data"),
        Output("replay-slider", "max"),
        Output("replay-slider", "value"),
        Output("replay-slider", "marks"),
        Input("mode", "value"),
    )
    def enter_replay(mode):
        if mode != "replay":
            return {"display": "none"}, no_update, no_update, no_update, no_update
        try:
            t0, t1 = data.replay_bounds()
        except Exception:  # noqa: BLE001
            log.exception("replay bounds failed")
            t0 = t1 = None
        if t0 is None:
            return {}, None, 1, 1, {}
        steps = max(1, int((t1 - t0).total_seconds() // (REPLAY_STEP_MIN * 60)))
        every = max(1, steps // 6)
        marks = {
            i: {
                "label": (t0 + timedelta(minutes=REPLAY_STEP_MIN * i))
                .astimezone(UTC)
                .strftime("%H%MZ"),
                "style": {"color": "#7d8a99"},
            }
            for i in range(0, steps + 1, every)
        }
        return {}, {"t0": t0.isoformat(), "steps": steps}, steps, steps, marks

    @app.callback(
        Output("play-tick", "disabled"),
        Output("play", "children"),
        Input("play", "n_clicks"),
        State("play-tick", "disabled"),
        prevent_initial_call=True,
    )
    def toggle_play(_n, disabled):
        start = bool(disabled)
        return (not start), ("❚❚ Pause" if start else "▶ Play")

    @app.callback(
        Output("replay-slider", "value", allow_duplicate=True),
        Input("play-tick", "n_intervals"),
        State("replay-slider", "value"),
        State("replay-slider", "max"),
        prevent_initial_call=True,
    )
    def advance(_n, value, maximum):
        return 0 if (value or 0) >= (maximum or 0) else (value or 0) + 1

    @app.callback(
        Output("replay-data", "data"),
        Output("replay-label", "children"),
        Input("replay-slider", "value"),
        State("replay-bounds", "data"),
        State("mode", "value"),
    )
    def load_snapshot(step, bounds, mode):
        if mode != "replay" or not bounds:
            return no_update, ""
        at = datetime.fromisoformat(bounds["t0"]) + timedelta(minutes=REPLAY_STEP_MIN * (step or 0))
        try:
            vessels, aircraft = data.snapshot(at)
        except Exception:  # noqa: BLE001
            log.exception("snapshot failed")
            return no_update, "query failed"
        return {"vessels": records(vessels), "aircraft": records(aircraft)}, dtg(at)

    @app.callback(Output("heat-data", "data"), Input("mode", "value"))
    def load_heat(mode):
        try:
            if mode == "density":
                return {"density": records(data.traffic_density())}
            if mode == "dark":
                return {"dark": records(data.dark_events())}
        except Exception:  # noqa: BLE001
            log.exception("heatmap query failed")
        return no_update

    # ---------------------------------------------------------------- map
    @app.callback(
        Output("map", "figure"),
        Input("live-data", "data"),
        Input("alerts-data", "data"),
        Input("trail-data", "data"),
        Input("replay-data", "data"),
        Input("heat-data", "data"),
        Input("mode", "value"),
        Input("layers", "value"),
        Input("zoom", "data"),
        State("selected-alert", "data"),
    )
    def draw(live, alerts, trail, replay, heat, mode, layers, zoom, selected):
        layers = set(layers or [])
        zoom = zoom or figures.DEFAULT_ZOOM
        traces: list = []
        if "infrastructure" in layers:
            traces += figures.infrastructure_traces()

        if mode in ("density", "dark"):
            heat = heat or {}
            if mode == "density":
                traces += figures.density_traces(frame(heat.get("density")))
            else:
                traces += figures.density_traces(None, frame(heat.get("dark")))
        else:
            picture = (replay if mode == "replay" else live) or {}
            vessels = frame(picture.get("vessels")) if "vessels" in layers else pd.DataFrame()
            aircraft = frame(picture.get("aircraft")) if "aircraft" in layers else pd.DataFrame()
            traces += figures.traffic_traces(vessels, aircraft, zoom)
            if "alerts" in layers and mode == "live":
                shown = frame([a for a in alerts or [] if a["STATUS"] != "REJECTED"])
                traces += figures.alert_traces(shown, selected)

        if trail and mode in ("live", "replay"):
            traces += figures.trail_traces(trail.get("alert"), frame(trail.get("track")))

        min_lat, min_lon, max_lat, max_lon = get_settings().aoi_bbox
        center = {"lat": (min_lat + max_lat) / 2, "lon": (min_lon + max_lon) / 2}
        view_zoom, rev = figures.DEFAULT_ZOOM, "base"
        if trail and trail.get("fit"):
            center, view_zoom, rev = trail["fit"]["center"], trail["fit"]["zoom"], trail["key"]
        return figures.figure(traces, center, view_zoom, rev)

    # ---------------------------------------------------------------- detail & review
    @app.callback(
        Output("detail", "children"),
        Output("detail-key", "data"),
        Input("selected-alert", "data"),
        Input("selected-track", "data"),
        Input("alerts-data", "data"),
        State("review-tick", "data"),
        State("detail-key", "data"),
    )
    def render_detail(alert_id, track_sel, alerts, review, shown_key):
        if track_sel and not alert_id:
            key = f"track:{track_sel['id']}"
            if key == shown_key:
                return no_update, no_update
            return [
                html.H3(f"{track_sel['type'].title()} {track_sel['id']}"),
                html.Div("Showing the last 2 hours of this track on the map.", className="why"),
            ], key
        a = next((x for x in alerts or [] if x["ALERT_ID"] == alert_id), None)
        if a is None:
            return html.Div(
                "Select an alert from the queue, or click an alert ring on the map.",
                className="empty",
            ), None
        # The 30 s refresh must not re-render an open panel (it would wipe a half-typed
        # note). Only re-render when the selection or the alert's review state changes.
        key = detail_key(a)
        if key == shown_key:
            return no_update, no_update
        return alert_detail(a, review), key

    @app.callback(
        Output("review-tick", "data"),
        Input("approve", "n_clicks"),
        Input("reject", "n_clicks"),
        Input("reopen", "n_clicks"),
        State("review-note", "value"),
        State("selected-alert", "data"),
        State("alerts-data", "data"),
        State("review-tick", "data"),
        prevent_initial_call=True,
    )
    def review(_a, _r, _o, note, alert_id, alerts, tick):
        if not alert_id or not (ctx.triggered and ctx.triggered[0]["value"]):
            return no_update
        status = {"approve": "APPROVED", "reject": "REJECTED", "reopen": "NEW"}[ctx.triggered_id]
        a = next((x for x in alerts or [] if x["ALERT_ID"] == alert_id), {})
        reviewer = get_settings().analyst_name
        data.set_review(alert_id, status, reviewer, note)
        AuditLog(get_settings().audit_log_path).record(
            f"human:{reviewer}",
            {"APPROVED": "approve", "REJECTED": "reject", "NEW": "reopen"}[status],
            alert_id=alert_id,
            rule=a.get("RULE"),
            track_id=a.get("TRACK_ID"),
            note=note or None,
        )
        n = (tick or {}).get("n", 0) + 1
        return {
            "n": n,
            "alert_id": alert_id,
            "msg": f"{status.title()} by {reviewer}; recorded in the audit log.",
        }


def detail_key(a: dict) -> str:
    return f"{a['ALERT_ID']}|{a['STATUS']}|{a.get('REVIEWED_AT')}"


def alert_detail(a: dict, review: dict | None) -> list:
    d = a.get("details", {})
    color = figures.RULE_COLORS.get(a["RULE"], "#fff")
    try:
        ev = data.evidence(a.get("evidence") or [])
    except Exception:  # noqa: BLE001
        log.exception("evidence query failed")
        ev = pd.DataFrame()
    history = data.audit_history(a["ALERT_ID"])

    identity = []
    types = d.get("vessel_types") or []
    for i, name in enumerate(d.get("vessel_names") or []):
        identity += [
            html.Div("Vessel", className="k"),
            html.Div(f"{name} · {types[i] if i < len(types) else 'Unknown'}", className="v"),
        ]
    if d.get("imo_numbers"):
        identity += [
            html.Div("IMO", className="k"),
            html.Div(", ".join(map(str, d["imo_numbers"])), className="v"),
        ]

    rows = []
    for r in ev.itertuples() if not ev.empty else []:
        rows.append(
            html.Tr(
                [
                    html.Td(r.RECORD_ID[:8]),
                    html.Td(f"{pd.Timestamp(r.OBSERVED_AT).tz_convert('UTC'):%H:%M:%SZ}"),
                    html.Td(f"{r.LAT:.4f}"),
                    html.Td(f"{r.LON:.4f}"),
                    html.Td(f"{r.SPEED_KTS:.1f}" if pd.notna(r.SPEED_KTS) else "-"),
                    html.Td(f"{r.COURSE_DEG:.0f}" if pd.notna(r.COURSE_DEG) else "-"),
                ]
            )
        )
    raw = ev.RAW_PAYLOAD.iloc[0] if not ev.empty else None
    try:
        raw = json.dumps(json.loads(raw), indent=1) if raw else None
    except (TypeError, ValueError):
        pass

    decided = a["STATUS"] in ("APPROVED", "REJECTED")
    position = f"{a['LAT']:.4f}N {a['LON']:.4f}E" if a.get("LAT") is not None else "-"
    reviewed = (
        f"{a['STATUS'].title()} by {a['REVIEWED_BY']} at {dtg(a['REVIEWED_AT'])}"
        if decided and a.get("REVIEWED_BY")
        else (review or {}).get("msg", "")
        if (review or {}).get("alert_id") == a["ALERT_ID"]
        else ""
    )
    return [
        html.Div(
            [
                html.H3(figures.RULE_LABELS.get(a["RULE"], a["RULE"]), style={"color": color}),
                html.Span(a["STATUS"], className=f"badge {a['STATUS']}"),
            ],
            style={"display": "flex", "justifyContent": "space-between", "alignItems": "center"},
        ),
        html.Div(alert_title(a), style={"fontWeight": 600}),
        html.Div(explain(a), className="why"),
        html.Div(
            className="kv",
            children=[
                html.Div("Score", className="k"),
                html.Div(f"{a['SCORE']:.2f}", className="v"),
                html.Div("Window", className="k"),
                html.Div(f"{dtg(a['START_AT'])} → {dtg(a['END_AT'])}", className="v"),
                html.Div("Position", className="k"),
                html.Div(position, className="v"),
                html.Div("Track", className="k"),
                html.Div(f"{a['TRACK_TYPE']} {a['TRACK_ID']}", className="v"),
                html.Div("Detector", className="k"),
                html.Div(f"v{d.get('detector_version', '?')}", className="v"),
                *identity,
            ],
        ),
        html.H4(f"Evidence ({len(rows)} records cited)"),
        html.Table(
            className="evidence",
            children=[
                html.Tr([html.Th(h) for h in ("record", "time", "lat", "lon", "kn", "cog")]),
                *rows,
            ],
        )
        if rows
        else html.Div("Evidence records not found.", className="empty"),
        html.Details([html.Summary("Raw sensor payload (first record)"), html.Pre(raw)])
        if raw
        else None,
        html.H4("Audit history"),
        html.Div(
            className="audit",
            children=[
                html.Div(
                    f"{dtg(e['ts'])}  {e['actor']}  {e['action']}"
                    + (f'  "{e["details"]["note"]}"' if e["details"].get("note") else "")
                )
                for e in history
            ]
            or [html.Div("No entries.")],
        ),
        html.H4("Analyst decision"),
        dcc.Textarea(
            id="review-note",
            placeholder="Note (optional): why approve or reject?",
            value=a.get("REVIEW_NOTE") or "",
            style={"width": "100%", "height": "52px"},
        ),
        html.Div(
            className="decision",
            children=[
                html.Button("✔ Approve", id="approve", className="btn approve"),
                html.Button("✖ Reject", id="reject", className="btn reject"),
                html.Button(
                    "Reopen",
                    id="reopen",
                    className="btn ghost",
                    style={} if decided else {"display": "none"},
                ),
            ],
        ),
        html.Div(reviewed, className="msg"),
        html.H4("AI analyst report"),
        html.Div(
            "Phase 3: an LLM analyst will draft an assessment here, with every claim cited to the "
            "evidence records above and checked by the grounding guardrail before it reaches you.",
            className="agent",
        ),
    ]


def create_app() -> Dash:
    app = Dash(__name__, title="Watchfloor", suppress_callback_exceptions=True)
    app.layout = build_layout()
    register(app)
    return app


def main() -> None:
    configure_logging("dashboard")
    create_app().run(debug=False, host="127.0.0.1", port=8050)


if __name__ == "__main__":
    main()
