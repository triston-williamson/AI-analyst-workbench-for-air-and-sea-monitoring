"""Map rendering for the operator console. Pure functions: DataFrames in, Plotly objects out.

Display conventions borrowed from maritime/air surveillance consoles:
  * Tracks are coloured by vessel category and drawn with a **heading leader**: a short line
    in the direction of travel whose length grows with speed. Stationary tracks have no
    leader, so "stopped" is visible at a glance.
  * Alerts are drawn as glowing rings (halo + core) in a rule colour, on top of traffic.
  * The selected alert's **evidence trail** explains the detection: the gap a vessel went dark
    across is dotted, the two tracks of a rendezvous converge, an orbit shows its circles.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

from watchfloor.detect import ship_types
from watchfloor.detect.geo import haversine_nm

BASE_STYLE = "carto-darkmatter"
DEFAULT_ZOOM = 4.8

# ---------------------------------------------------------------- vessel categories
CATEGORY_COLORS = {
    "Tanker": "#ef6f6c",
    "Cargo": "#7bc47f",
    "Passenger": "#5fa8d3",
    "Working craft": "#f2c14e",
    "Fishing": "#4fd1c5",
    "Pleasure / sailing": "#b39ddb",
    "Military / law enf.": "#e0e0e0",
    "Other / unknown": "#7f8c99",
}
AIRCRAFT_COLOR = "#ff8fab"

RULE_COLORS = {
    "AIS_GAP": "#ffd60a",
    "LOITER": "#ff9f1c",
    "RENDEZVOUS": "#ff006e",
    "ORBIT": "#c77dff",
    "ML_ANOMALY": "#2ec4b6",
    "SANCTIONS": "#e63946",
}
RULE_LABELS = {
    "AIS_GAP": "Went dark",
    "LOITER": "Loitering",
    "RENDEZVOUS": "Ship meeting",
    "ORBIT": "Circling",
    "ML_ANOMALY": "ML anomaly",
    "SANCTIONS": "Sanctioned vessel",
}
INFRA_COLORS = {"Pipeline": "#c08552", "Telecom cable": "#48cae4", "Power cable": "#ffd166"}


def vessel_category(ship_type) -> str:
    if ship_type is None or (isinstance(ship_type, float) and math.isnan(ship_type)):
        return "Other / unknown"
    t = int(ship_type)
    if ship_types.is_tanker(t):
        return "Tanker"
    if 70 <= t <= 79:
        return "Cargo"
    if 60 <= t <= 69 or 40 <= t <= 49:
        return "Passenger"
    if t == 30:
        return "Fishing"
    if t in (36, 37):
        return "Pleasure / sailing"
    if t in (35, 55):
        return "Military / law enf."
    if t in (31, 32, 33, 34, 50, 51, 52, 53, 54, 58):
        return "Working craft"
    return "Other / unknown"


# ---------------------------------------------------------------- geometry helpers
def leader_length_deg(zoom: float) -> float:
    """Leader length in degrees for a ~15 kt track, scaled so it stays a few px on screen."""
    return 0.06 * 2 ** (DEFAULT_ZOOM - zoom)


def leader_end(lat: float, lon: float, course: float, speed_kts: float, zoom: float):
    """End point of a heading leader. Length scales with speed (capped) and zoom."""
    length = leader_length_deg(zoom) * min(max(speed_kts, 2.0), 30.0) / 15.0
    rad = math.radians(course)
    return (
        lat + length * math.cos(rad),
        lon + length * math.sin(rad) / max(math.cos(math.radians(lat)), 0.2),
    )


def leaders(df: pd.DataFrame, zoom: float, min_speed: float = 0.5) -> tuple[list, list]:
    """Line coordinates (with None breaks) for every moving track that has a course."""
    lats, lons = [], []
    if df.empty:
        return lats, lons
    moving = df[(df.SPEED_KTS.fillna(0) >= min_speed) & df.COURSE.notna()]
    for r in moving.itertuples():
        lat2, lon2 = leader_end(r.LAT, r.LON, float(r.COURSE), float(r.SPEED_KTS), zoom)
        lats += [r.LAT, lat2, None]
        lons += [r.LON, lon2, None]
    return lats, lons


def interpolate(lat1, lon1, lat2, lon2, n: int = 24) -> tuple[list[float], list[float]]:
    """Evenly spaced points between two positions, used to draw a dotted 'dark' segment."""
    return (
        [lat1 + (lat2 - lat1) * i / n for i in range(1, n)],
        [lon1 + (lon2 - lon1) * i / n for i in range(1, n)],
    )


def circle(lat: float, lon: float, radius_nm: float, n: int = 64) -> tuple[list, list]:
    dlat = radius_nm / 60.0
    dlon = dlat / max(math.cos(math.radians(lat)), 0.2)
    pts = [
        (lat + dlat * math.cos(2 * math.pi * i / n), lon + dlon * math.sin(2 * math.pi * i / n))
        for i in range(n + 1)
    ]
    return [p[0] for p in pts], [p[1] for p in pts]


def zoom_for_extent(lats: list[float], lons: list[float]) -> float:
    """Rough web-map zoom that fits the given points."""
    if not lats:
        return DEFAULT_ZOOM
    span_nm = max(
        haversine_nm(min(lats), min(lons), max(lats), max(lons)),
        2.0,
    )
    return max(3.0, min(13.0, math.log2(21600 / span_nm) - 0.6))


# ---------------------------------------------------------------- traffic layers
def traffic_traces(vessels: pd.DataFrame, aircraft: pd.DataFrame, zoom: float) -> list:
    traces = []
    if not vessels.empty:
        v = vessels.copy()
        v["CATEGORY"] = v.SHIP_TYPE.map(vessel_category)
        for cat, color in CATEGORY_COLORS.items():
            g = v[v.CATEGORY == cat]
            if g.empty:
                continue
            llat, llon = leaders(g, zoom)
            if llat:
                traces.append(
                    go.Scattermap(
                        lat=llat,
                        lon=llon,
                        mode="lines",
                        line={"width": 1.4, "color": color},
                        hoverinfo="skip",
                        showlegend=False,
                        legendgroup=cat,
                    )
                )
            hover = [
                f"<b>{(n or 'MMSI ' + i)}</b><br>{cat} | MMSI {i}<br>"
                f"{(s if pd.notna(s) else 0):.1f} kn" + (f" | {c:.0f}°" if pd.notna(c) else "")
                for n, i, s, c in zip(g.NAME, g.ID, g.SPEED_KTS, g.COURSE, strict=False)
            ]
            traces.append(
                go.Scattermap(
                    lat=g.LAT,
                    lon=g.LON,
                    mode="markers",
                    name=f"{cat} ({len(g)})",
                    legendgroup=cat,
                    marker={"size": 6, "color": color},
                    text=hover,
                    hovertemplate="%{text}<extra></extra>",
                    customdata=[["vessel", i] for i in g.ID],
                )
            )
    if not aircraft.empty:
        llat, llon = leaders(aircraft, zoom * 0.98, min_speed=30)
        if llat:
            traces.append(
                go.Scattermap(
                    lat=llat,
                    lon=llon,
                    mode="lines",
                    line={"width": 1.4, "color": AIRCRAFT_COLOR},
                    hoverinfo="skip",
                    showlegend=False,
                    legendgroup="aircraft",
                )
            )
        hover = [
            f"<b>{n}</b><br>Aircraft | {c or ''}<br>"
            + (f"FL{a * 3.281 / 100:.0f}" if pd.notna(a) else "")
            for n, c, a in zip(
                aircraft.NAME, aircraft.ORIGIN_COUNTRY, aircraft.BARO_ALTITUDE_M, strict=False
            )
        ]
        traces.append(
            go.Scattermap(
                lat=aircraft.LAT,
                lon=aircraft.LON,
                mode="markers",
                name=f"Aircraft ({len(aircraft)})",
                legendgroup="aircraft",
                marker={"size": 7, "color": AIRCRAFT_COLOR},
                text=hover,
                hovertemplate="%{text}<extra></extra>",
                customdata=[["aircraft", i] for i in aircraft.ID],
            )
        )
    return traces


def alert_traces(alerts: pd.DataFrame, selected: str | None = None) -> list:
    traces = []
    if alerts.empty:
        return traces
    shown = alerts[alerts.LAT.notna()]
    for rule, g in shown.groupby("RULE"):
        color = RULE_COLORS.get(rule, "#ffffff")
        size = (14 + 16 * g.SCORE.astype(float)).tolist()
        hover = [
            f"<b>{RULE_LABELS.get(rule, rule)}</b> score {s:.2f}<br>{t} {tid}<br>"
            "<i>click for details</i>"
            for s, t, tid in zip(g.SCORE, g.TRACK_TYPE.str.lower(), g.TRACK_ID, strict=False)
        ]
        custom = [["alert", a] for a in g.ALERT_ID]
        traces.append(  # halo
            go.Scattermap(
                lat=g.LAT,
                lon=g.LON,
                mode="markers",
                marker={"size": size, "color": color, "opacity": 0.28},
                hoverinfo="skip",
                showlegend=False,
                legendgroup=f"alert-{rule}",
            )
        )
        traces.append(  # core
            go.Scattermap(
                lat=g.LAT,
                lon=g.LON,
                mode="markers",
                name=f"⚠ {RULE_LABELS.get(rule, rule)} ({len(g)})",
                legendgroup=f"alert-{rule}",
                marker={"size": 8, "color": color},
                text=hover,
                hovertemplate="%{text}<extra></extra>",
                customdata=custom,
            )
        )
    if selected is not None:
        sel = shown[shown.ALERT_ID == selected]
        if not sel.empty:
            traces.append(
                go.Scattermap(
                    lat=sel.LAT,
                    lon=sel.LON,
                    mode="markers",
                    showlegend=False,
                    marker={"size": 34, "color": "#ffffff", "opacity": 0.18},
                    hoverinfo="skip",
                )
            )
    return traces


@lru_cache(maxsize=1)
def _infrastructure() -> dict:
    path = Path(__file__).parent / "data" / "infrastructure.json"
    if not path.exists():
        return {"features": [], "attribution": ""}
    return json.loads(path.read_text(encoding="utf-8"))


def infrastructure_traces() -> list:
    traces = []
    by_kind: dict[str, tuple[list, list, list]] = {}
    for f in _infrastructure()["features"]:
        lats, lons, names = by_kind.setdefault(f["kind"], ([], [], []))
        for lon, lat in f["coords"]:
            lats.append(lat)
            lons.append(lon)
            names.append(f"{f['kind']}: {f['name'] or 'unnamed'}")
        lats.append(None)
        lons.append(None)
        names.append(None)
    for kind, (lats, lons, names) in by_kind.items():
        traces.append(
            go.Scattermap(
                lat=lats,
                lon=lons,
                mode="lines",
                name=kind,
                line={"width": 1.6, "color": INFRA_COLORS.get(kind, "#999")},
                opacity=0.75,
                text=names,
                hovertemplate="%{text}<extra></extra>",
                legendgroup="infra",
            )
        )
    return traces


def infrastructure_attribution() -> str:
    return _infrastructure().get("attribution", "")


# ---------------------------------------------------------------- evidence trails
def _line(df: pd.DataFrame, color: str, name: str, width: float = 3.0) -> go.Scattermap:
    return go.Scattermap(
        lat=df.LAT,
        lon=df.LON,
        mode="lines+markers",
        name=name,
        line={"width": width, "color": color},
        marker={"size": 4, "color": color},
        text=[
            f"{name}<br>{pd.Timestamp(t):%H:%MZ} | {s if pd.notna(s) else 0:.1f} kn"
            for t, s in zip(df.OBSERVED_AT, df.SPEED_KTS, strict=False)
        ],
        hovertemplate="%{text}<extra></extra>",
        legendgroup="trail",
    )


def _label(lat, lon, text: str, color: str) -> go.Scattermap:
    return go.Scattermap(
        lat=[lat],
        lon=[lon],
        mode="markers+text",
        text=[text],
        textposition="top right",
        textfont={"color": color, "size": 13},
        marker={"size": 13, "color": color},
        hovertemplate=f"{text}<extra></extra>",
        showlegend=False,
    )


def trail_traces(alert: dict | None, track: pd.DataFrame) -> list:
    """Evidence trail for the selected alert (or a plain track for a clicked vessel)."""
    if track is None or track.empty:
        return []
    track = track.sort_values("OBSERVED_AT")
    rule = (alert or {}).get("RULE")
    details = (alert or {}).get("details", {})
    color = RULE_COLORS.get(rule, "#ffffff")
    traces: list = []

    if rule == "AIS_GAP":
        # Track times reach the browser at whole-second precision, so compare at that
        # precision too; otherwise the reappearance point (e.g. 05:15:34 vs 05:15:34.5)
        # falls on the wrong side of the gap and the "after" leg disappears.
        t0 = pd.Timestamp(details.get("dark_from")).floor("s")
        t1 = pd.Timestamp(details.get("dark_until")).floor("s")
        times = pd.to_datetime(track.OBSERVED_AT, utc=True, format="ISO8601")
        before = track[times <= t0]
        after = track[times >= t1]
        if not before.empty:
            traces.append(_line(before, "#ffffff", "Track before dark"))
        if not after.empty:
            traces.append(_line(after, "#ffffff", "Track after"))
        if not before.empty and not after.empty:
            a, b = before.iloc[-1], after.iloc[0]
            dlat, dlon = interpolate(a.LAT, a.LON, b.LAT, b.LON)
            traces.append(
                go.Scattermap(
                    lat=dlat,
                    lon=dlon,
                    mode="markers",
                    name="Dark (no AIS)",
                    marker={"size": 5, "color": color},
                    hovertemplate=f"No AIS for {details.get('gap_hours')} h<extra></extra>",
                )
            )
            traces.append(
                _label(a.LAT, a.LON, f"Went dark {pd.Timestamp(a.OBSERVED_AT):%H%MZ}", color)
            )
            traces.append(
                _label(b.LAT, b.LON, f"Reappeared {pd.Timestamp(b.OBSERVED_AT):%H%MZ}", color)
            )
        return traces

    if rule == "RENDEZVOUS":
        palette = ["#ff006e", "#3a86ff"]
        names = details.get("vessel_names") or []
        for k, (tid, g) in enumerate(track.groupby("ID")):
            label = names[k] if k < len(names) else f"MMSI {tid}"
            traces.append(_line(g, palette[k % 2], label))
        if alert and alert.get("LAT") is not None:
            traces.append(_label(alert["LAT"], alert["LON"], "Meeting point", color))
        return traces

    if rule == "LOITER" and alert and alert.get("LAT") is not None:
        clat, clon = circle(alert["LAT"], alert["LON"], details.get("radius_nm", 1.0))
        traces.append(
            go.Scattermap(
                lat=clat,
                lon=clon,
                mode="lines",
                name="Loiter radius",
                line={"width": 2, "color": color},
                hoverinfo="skip",
            )
        )
        traces.append(_line(track, "#ffffff", "Track", width=2))
        return traces

    # ORBIT, ML_ANOMALY, or a clicked track with no alert.
    traces.append(_line(track, color if rule else "#ffffff", "Track"))
    first, last = track.iloc[0], track.iloc[-1]
    traces.append(_label(first.LAT, first.LON, f"{pd.Timestamp(first.OBSERVED_AT):%H%MZ}", color))
    traces.append(_label(last.LAT, last.LON, f"{pd.Timestamp(last.OBSERVED_AT):%H%MZ}", color))
    return traces


# ---------------------------------------------------------------- heatmaps
def density_traces(density: pd.DataFrame, dark: pd.DataFrame | None = None) -> list:
    traces = []
    if density is not None and not density.empty:
        traces.append(
            go.Densitymap(
                lat=density.LAT,
                lon=density.LON,
                z=density.N.map(lambda n: math.log1p(n)),
                radius=6,
                colorscale="Viridis",
                showscale=False,
                opacity=0.8,
                name="Traffic density",
                hoverinfo="skip",
            )
        )
    if dark is not None and not dark.empty:
        traces.append(
            go.Densitymap(
                lat=dark.LAT,
                lon=dark.LON,
                z=dark.SCORE.astype(float),
                radius=22,
                colorscale="Hot",
                showscale=False,
                opacity=0.85,
                name="Dark events",
                hoverinfo="skip",
            )
        )
        traces.append(
            go.Scattermap(
                lat=dark.LAT,
                lon=dark.LON,
                mode="markers",
                name=f"Went dark ({len(dark)})",
                marker={"size": 6, "color": RULE_COLORS["AIS_GAP"]},
                text=[
                    f"MMSI {t}: dark {g:.1f} h"
                    for t, g in zip(dark.TRACK_ID, dark.GAP_HOURS, strict=False)
                ],
                hovertemplate="%{text}<extra></extra>",
            )
        )
    return traces


# ---------------------------------------------------------------- figure
def figure(traces: list, center: dict, zoom: float, uirevision: str) -> go.Figure:
    fig = go.Figure(traces)
    fig.update_layout(
        uirevision=uirevision,
        map={"style": BASE_STYLE, "center": center, "zoom": zoom},
        margin={"l": 0, "r": 0, "t": 0, "b": 0},
        paper_bgcolor="#0b0f14",
        legend={
            "x": 0.01,
            "y": 0.99,
            "bgcolor": "rgba(11,15,20,0.78)",
            "font": {"color": "#d8dee9", "size": 11},
            "itemsizing": "constant",
        },
        hoverlabel={"bgcolor": "#11161d", "font": {"color": "#e5e9f0"}},
    )
    return fig
