Aircraft track tool keeps the first of each set of repeated reports, so altitude and speed match the position

Why: v1 collapsed repeated (stale) aircraft reports keeping the *last* copy, which pairs an
old position and time with the newest altitude. In both v1 runs of ml_anomaly-23 the report
claimed a "physically impossible" climb from 290 m to 10,973 m in 21 s; citation support was
0.625 and 0.571. The first copy is the report loaded when the position was fresh, so its
altitude and speed belong to that position. Detectors and console already keep the first.

Change: agent/tools.py `drop_duplicates(..., keep="last")` -> `keep="first"`.

Scope: only aircraft tracks with repeats; ml_anomaly-23 is the only aircraft case, so this is
a focused run: ml_anomaly-23 x 5 (~$0.55), not the full set.

Expected: dismiss holds in 5/5; no "impossible climb" claim; citation support at or above v1's
0.60 mean.
