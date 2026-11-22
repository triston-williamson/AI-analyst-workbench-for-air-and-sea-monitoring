Aircraft track tool returns altitude and callsign and flags stale repeated positions; ADS-B ingest drops stale positions

Why: in the baseline, ml_anomaly-23 (an airliner climbing out near Kaliningrad) was escalated
twice. The agent saw only lat/lon/speed for aircraft, and 33% of stored aircraft rows were
OpenSky re-sending the last known position (same time_position) while altitude kept changing,
so the track looked like an aircraft hanging in place.

Changes
- agent/tools.py (under test): aircraft get_track_history selects BARO_ALTITUDE_M, CALLSIGN,
  ON_GROUND; keeps one point per OBSERVED_AT; adds a data_quality note saying how many
  repeated position reports were removed and what they mean.
- ingest/adsb_producer.py: StaleFilter drops states with no position time, positions more than
  30 s older than last contact, and repeats. Does not affect the frozen eval data.
- detect/runner.py, dashboard/data.py: dedupe stored aircraft rows by (ICAO24, OBSERVED_AT).

Expected: ml_anomaly-23 moves from escalate to dismiss; other cases (all vessels except the
aircraft ones) unchanged. Harness (eval files) unchanged; tools.py is in the harness hash, so
the gate needs re-approval.
