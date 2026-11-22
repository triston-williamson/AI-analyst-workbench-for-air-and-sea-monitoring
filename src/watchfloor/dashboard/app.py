"""Operator console: live map of vessels and aircraft in the AOI.

    python -m watchfloor.dashboard.app      ->  http://127.0.0.1:8050

Phase 4 adds the alert queue and the report approve/reject panel.
"""

from __future__ import annotations

import logging

import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, dcc, html

from watchfloor.config import get_settings
from watchfloor.load.snowflake_conn import connect

log = logging.getLogger("dashboard")

LOOKBACK_MINUTES = 30

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


def fetch() -> tuple[pd.DataFrame, pd.DataFrame, str | None]:
    try:
        conn = connect()
        try:
            cur = conn.cursor()
            params = {"lookback": LOOKBACK_MINUTES}
            vessels = cur.execute(VESSEL_SQL, params).fetch_pandas_all()
            aircraft = cur.execute(AIRCRAFT_SQL, params).fetch_pandas_all()
        finally:
            conn.close()
        return vessels, aircraft, None
    except Exception as exc:  # noqa: BLE001 - surface any data error on the console
        log.exception("Snowflake query failed")
        return pd.DataFrame(), pd.DataFrame(), str(exc)


def build_map(vessels: pd.DataFrame, aircraft: pd.DataFrame) -> go.Figure:
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
    fig.update_layout(
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
        vessels, aircraft, err = fetch()
        status = (
            f"Data error: {err[:120]}"
            if err
            else (
                f"{len(vessels)} vessels | {len(aircraft)} aircraft | last {LOOKBACK_MINUTES} min"
            )
        )
        return build_map(vessels, aircraft), status

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    create_app().run(debug=False, host="127.0.0.1", port=8050)


if __name__ == "__main__":
    main()
