"""ML anomaly scoring (DET-05): which tracks move unlike the others?

Each track is summarised as a vector of kinematic features. An Isolation Forest, an unsupervised
model that isolates outliers with few random splits, scores every track against the rest of the
current picture. No labels are needed, which matters because real "suspicious" examples are rare.

Explainability: alongside the score, each alert lists the features that are furthest from normal
(robust z-scores), so a reviewer sees e.g. "turn_rate 6.2 sigma above typical" rather than a
bare number. Vessels and aircraft are scored separately because their physics differ.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from sklearn.ensemble import IsolationForest

from watchfloor.detect.geo import haversine_nm, initial_bearing_deg, signed_turn_deg
from watchfloor.detect.rules import Alert, Point, group_tracks, sample_evidence

FEATURES = (
    "mean_speed_kts",
    "speed_std_kts",
    "max_speed_kts",
    "path_length_nm",
    "straightness",  # displacement / path length: 1 = straight line, ~0 = circling or idling
    "turn_rate_deg_per_min",
    "max_report_gap_min",
    "reports_per_hour",
)


@dataclass
class TrackFeatures:
    track_id: str
    points: list[Point]
    values: dict[str, float]


def track_features(
    track_id: str, pts: list[Point], min_points: int = 5, min_hours: float = 0.0
) -> TrackFeatures | None:
    """Kinematic summary of one track, or None if there's too little of it to judge.

    Short tracks make every feature extreme (a 10-minute track of 30 reports has a huge
    report rate and any wobble is a huge turn rate), so they are excluded, not scored.
    """
    if len(pts) < max(min_points, 5):
        return None
    duration_h = (pts[-1].observed_at - pts[0].observed_at).total_seconds() / 3600
    if duration_h <= 0 or duration_h < min_hours:
        return None

    steps = [haversine_nm(a.lat, a.lon, b.lat, b.lon) for a, b in zip(pts, pts[1:], strict=False)]
    path = sum(steps)
    displacement = haversine_nm(pts[0].lat, pts[0].lon, pts[-1].lat, pts[-1].lon)
    speeds = [p.speed_kts for p in pts if p.speed_kts is not None]
    gaps_min = [
        (b.observed_at - a.observed_at).total_seconds() / 60
        for a, b in zip(pts, pts[1:], strict=False)
    ]

    bearings = [
        initial_bearing_deg(a.lat, a.lon, b.lat, b.lon)
        for a, b, step in zip(pts, pts[1:], steps, strict=False)
        if step >= 0.05  # ignore jitter while stationary
    ]
    total_turn = sum(
        abs(signed_turn_deg(a, b)) for a, b in zip(bearings, bearings[1:], strict=False)
    )

    return TrackFeatures(
        track_id,
        pts,
        {
            "mean_speed_kts": float(np.mean(speeds)) if speeds else 0.0,
            "speed_std_kts": float(np.std(speeds)) if speeds else 0.0,
            "max_speed_kts": float(max(speeds)) if speeds else 0.0,
            "path_length_nm": path,
            "straightness": displacement / path if path > 0.1 else 1.0,
            "turn_rate_deg_per_min": total_turn / (duration_h * 60),
            "max_report_gap_min": max(gaps_min),
            "reports_per_hour": len(pts) / duration_h,
        },
    )


def score_tracks(
    points: list[Point],
    track_type: str,
    top_k: int = 10,
    min_tracks: int = 30,
    min_percentile: float = 0.98,
    random_state: int = 0,
    min_points: int = 5,
    min_hours: float = 0.0,
    min_anomaly_score: float = 0.0,
) -> list[Alert]:
    """Return ML_ANOMALY alerts for the most unusual tracks (at most ``top_k``).

    A track must clear both a relative bar (``min_percentile`` of this picture) and an absolute
    one (``min_anomaly_score``, Isolation Forest's score where ~0.5 is ordinary), so a quiet
    picture doesn't still produce its "top k" every pass.
    """
    feats = [
        f
        for tid, pts in group_tracks(points).items()
        if (f := track_features(tid, pts, min_points=min_points, min_hours=min_hours))
    ]
    if len(feats) < min_tracks:
        return []  # too few tracks for "unusual" to mean anything

    X = np.array([[f.values[name] for name in FEATURES] for f in feats])
    # Log-compress heavy-tailed features so one extreme value doesn't dominate.
    X = np.sign(X) * np.log1p(np.abs(X))
    median = np.median(X, axis=0)
    mad = np.median(np.abs(X - median), axis=0) * 1.4826
    # Floor the spread: when most tracks are near-identical (e.g. ships at anchor) the MAD
    # collapses towards zero and z-scores explode into meaningless hundreds.
    scale = np.maximum(mad, 0.5 * np.std(X, axis=0)) + 1e-6
    Z = (X - median) / scale

    model = IsolationForest(n_estimators=300, contamination="auto", random_state=random_state)
    model.fit(Z)
    raw = -model.score_samples(Z)  # higher = more anomalous
    percentile = raw.argsort().argsort() / (len(raw) - 1)

    order = np.argsort(-raw)
    alerts: list[Alert] = []
    for idx in order[:top_k]:
        if percentile[idx] < min_percentile or raw[idx] < min_anomaly_score:
            break
        f = feats[idx]
        drivers = sorted(
            (
                {"feature": name, "value": round(f.values[name], 3), "robust_z": round(float(z), 1)}
                for name, z in zip(FEATURES, Z[idx], strict=True)
            ),
            key=lambda d: -abs(d["robust_z"]),
        )[:3]
        lat = sum(p.lat for p in f.points) / len(f.points)
        lon = sum(p.lon for p in f.points) / len(f.points)
        alerts.append(
            Alert(
                rule="ML_ANOMALY",
                track_type=track_type,
                track_id=f.track_id,
                score=round(float(percentile[idx]), 3),
                evidence_ids=sample_evidence(f.points),
                details={
                    "model": "IsolationForest",
                    "anomaly_score": round(float(raw[idx]), 4),
                    "percentile": round(float(percentile[idx]), 4),
                    "tracks_compared": len(feats),
                    "top_drivers": drivers,
                },
                lat=None if math.isnan(lat) else lat,
                lon=None if math.isnan(lon) else lon,
                start_at=f.points[0].observed_at,
                end_at=f.points[-1].observed_at,
            )
        )
    return alerts
