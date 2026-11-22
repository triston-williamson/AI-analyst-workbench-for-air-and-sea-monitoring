"""Operator console: live map of vessels, aircraft and detector alerts in the AOI.

    python -m watchfloor.dashboard.app      ->  http://127.0.0.1:8050

Phase 4 adds the alert queue and the report approve/reject panel.
"""

from __future__ import annotations

import json
import logging

import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, dcc, html

from watchfloor.config import get_settings
from watchfloor.load.snowflake_conn import connect

log = logging.getLogger("dashboard")

LOOKBACK_MINUTES = 30
ALERT_LOOKBACK_HOURS = 24

# Colour per detector rule (high contrast on the dark basemap).
RULE_COLORS = {
    "AIS_GAP": "#ffd60a",
    "LOITER": "#ff9f1c",
    "RENDEZVOUS": "#ff006e",
    "ORBIT": "#c77dff",
    "ML_ANOMALY": "#2ec4b6",
}

VESSEL_SQL = """
SELECT MMSI, SHIP_NAME, LAT, LON, SPEED_KTS, COURSE_DEG, OBSERVED_AT
FROM CORE.VESSEL_LATEST
WHERE OBSERVED_AT > DATEADD(minute, -%(lookback)s, CURRENT_TIMESTAMP())
"""
AIRCRAFT_SQL = """
SELECT ICAO24, CALLSIGN, ORIGIN_COUNTRY, LAT, LON, BARO_ALTITUDE_M, SPEED_KTS, OBSERVED_AT
FROM CORE.AIRCRAFT_LATEST
WHERE OBSERVED_AT > DATEADD(minute, -%(lookback)s, CURRENT_TIMESTAMP())
  AND NOT ON_GROUND
"""
ALERTS_SQL = """
SELECT ALERT_ID, RULE, TRACK_TYPE, TRACK_ID, SCORE, LAT, LON, START_AT, END_AT,
       TO_JSON(DETAILS) AS DETAILS
FROM OPS.ALERTS
WHERE LAT IS NOT NULL
  AND COALESCE(END_AT, DETECTED_AT) > DATEADD(hour, -%(alert_hours)s, CURRENT_TIMESTAMP())
  AND STATUS <> 'REJECTED'
"""


def describe_alert(rule: str, details_json: str) -> str:
    """One-line human summary for the map hover."""
    d = json.loads(details_json or "{}")
    if rule == "AIS_GAP":
        return f"dark {d.get('gap_hours')} h, moved {d.get('distance_nm')} nm"
    if rule == "LOITER":
        return f"{d.get('duration_hours')} h within {d.get('radius_nm')} nm"
    if rule == "RENDEZVOUS":
        return (
            f"{d.get('vessel_a')} + {d.get('vessel_b')}: {d.get('duration_minutes')} min, "
            f"closest {d.get('min_separation_nm')} nm"
        )
    if rule == "ORBIT":
        return f"{d.get('circles')} circles in {d.get('window_minutes')} min"
    if rule == "ML_ANOMALY":
        drivers = d.get("top_drivers", [])[:2]
        return "unusual " + ", ".join(f"{x['feature']} ({x['robust_z']:+}σ)" for x in drivers)
    return ""


def fetch() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, str | None]:
    try:
        conn = connect()
        try:
            cur = conn.cursor()
            params = {"lookback": LOOKBACK_MINUTES, "alert_hours": ALERT_LOOKBACK_HOURS}
            vessels = cur.execute(VESSEL_SQL, params).fetch_pandas_all()
            aircraft = cur.execute(AIRCRAFT_SQL, params).fetch_pandas_all()
            alerts = cur.execute(ALERTS_SQL, params).fetch_pandas_all()
        finally:
            conn.close()
        return vessels, aircraft, alerts, None
    except Exception as exc:  # noqa: BLE001 - surface any data error on the console
        log.exception("Snowflake query failed")
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), str(exc)


def build_map(
    vessels: pd.DataFrame, aircraft: pd.DataFrame, alerts: pd.DataFrame | None = None
) -> go.Figure:
    min_lat, min_lon, max_lat, max_lon = get_settings().aoi_bbox
    fig = go.Figure()
    if not vessels.empty:
        fig.add_trace(
            go.Scattermap(
                lat=vessels.LAT,
                lon=vessels.LON,
                mode="markers",
                name=f"Vessels ({len(vessels)})",
                marker={"size": 7, "color": "#1f77b4"},
                text=vessels.SHIP_NAME.fillna("") + " | MMSI " + vessels.MMSI.astype(str),
                hovertemplate="%{text}<extra></extra>",
            )
        )
    if not aircraft.empty:
        fig.add_trace(
            go.Scattermap(
                lat=aircraft.LAT,
                lon=aircraft.LON,
                mode="markers",
                name=f"Aircraft ({len(aircraft)})",
                marker={"size": 8, "color": "#d62728"},
                text=aircraft.CALLSIGN.fillna(aircraft.ICAO24),
                hovertemplate="%{text}<extra></extra>",
            )
        )
    if alerts is not None and not alerts.empty:
        for rule, group in alerts.groupby("RULE"):
            hover = [
                f"<b>{rule}</b> {row.TRACK_TYPE.lower()} {row.TRACK_ID}<br>"
                f"score {row.SCORE:.2f} | {describe_alert(rule, row.DETAILS)}"
                for row in group.itertuples()
            ]
            fig.add_trace(
                go.Scattermap(
                    lat=group.LAT,
                    lon=group.LON,
                    mode="markers",
                    name=f"{rule} ({len(group)})",
                    marker={
                        "size": (10 + 14 * group.SCORE.astype(float)).tolist(),
                        "color": RULE_COLORS.get(rule, "#ffffff"),
                        "opacity": 0.85,
                    },
                    text=hover,
                    hovertemplate="%{text}<extra></extra>",
                )
            )
    fig.update_layout(
        uirevision="keep",  # keep the user's pan/zoom across 30 s refreshes
        map={
            "style": "carto-darkmatter",
            "center": {"lat": (min_lat + max_lat) / 2, "lon": (min_lon + max_lon) / 2},
            "zoom": 4.5,
        },
        margin={"l": 0, "r": 0, "t": 0, "b": 0},
        legend={"x": 0.01, "y": 0.99, "bgcolor": "rgba(0,0,0,0.5)", "font": {"color": "white"}},
        paper_bgcolor="#111",
    )
    return fig


def create_app() -> Dash:
    app = Dash(__name__, title="Watchfloor")
    app.layout = html.Div(
        style={
            "height": "100vh",
            "display": "flex",
            "flexDirection": "column",
            "background": "#111",
            "color": "#eee",
            "fontFamily": "system-ui, sans-serif",
        },
        children=[
            html.Div(
                style={
                    "padding": "8px 16px",
                    "display": "flex",
                    "gap": "24px",
                    "alignItems": "baseline",
                },
                children=[
                    html.H2("WATCHFLOOR", style={"margin": 0, "letterSpacing": "3px"}),
                    html.Span(
                        "Maritime & Air Domain Awareness - UNCLASSIFIED // PUBLIC DATA",
                        style={"opacity": 0.6},
                    ),
                    html.Span(id="status", style={"marginLeft": "auto", "opacity": 0.8}),
                ],
            ),
            dcc.Graph(id="map", style={"flex": 1}, config={"displaylogo": False}),
            dcc.Interval(id="tick", interval=30_000, n_intervals=0),
        ],
    )

    @app.callback(
        Output("map", "figure"), Output("status", "children"), Input("tick", "n_intervals")
    )
    def refresh(_):
        vessels, aircraft, alerts, err = fetch()
        status = (
            f"Data error: {err[:120]}"
            if err
            else (
                f"{len(vessels)} vessels | {len(aircraft)} aircraft (last {LOOKBACK_MINUTES} min)"
                f" | {len(alerts)} alerts (last {ALERT_LOOKBACK_HOURS} h)"
            )
        )
        return build_map(vessels, aircraft, alerts), status

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    create_app().run(debug=False, host="127.0.0.1", port=8050)


if __name__ == "__main__":
    main()
