# Watchfloor: Design Document

| | |
|---|---|
| **Status** | Phase 1 (ingest + storage) done; Phase 2 (detection) done |
| **Classification** | Unclassified. Public data only. |
| **Default AOI** | Baltic Sea (53.5°N–60.5°N, 9.5°E–30.0°E) |

## 1. Problem

Analysts monitoring a maritime or air area of interest face far more tracks than they can watch.
The behaviours that matter are rare and buried in noise: a tanker switching off its transponder,
two vessels meeting at sea away from port, an aircraft flying repeated orbits. In the Baltic in
particular, AIS gaps and ship-to-ship transfers are associated with sanctions evasion by the
so-called "shadow fleet," and with damage to undersea cables.

Watchfloor ingests live public air (ADS-B) and maritime (AIS) feeds, detects anomalous behaviour,
and uses an LLM agent to draft short, cited assessments. **A human approves every assessment**
before it counts as finished work.

## 2. Goals and non-goals

**Goals**
- Near-real-time common operating picture (COP) for one configurable AOI.
- Explainable detections. Every alert carries the raw records that triggered it.
- AI-drafted reports where every claim is traceable to source data, with hallucination measured.
- Auditability: an independent reviewer can reconstruct who/what did what, and when.

**Non-goals**
- Classified or export-controlled data, algorithms or capabilities (ITAR/EAR). All inputs are
  public and all methods are textbook.
- Automated action of any kind. The system informs a human; it does not decide.
- Global coverage or production-scale throughput.

## 3. Architecture

```
 OpenSky REST ──► adsb_producer ─┐                              ┌─► detect (rules + ML) ─► OPS.ALERTS
                                 ├─► Kafka ─► snowflake_sink ─► │                              │
 aisstream WS ──► ais_producer  ─┘   │                RAW.*     │                              ▼
                    │                │                          └─► CORE views        analyst agent
              Pydantic validation    └─► ingest.dead_letter ─► OPS.REJECTED_RECORDS   (read-only tools)
              (untrusted input)                                                              │
                                                                                grounding check
                                                                                             ▼
                              Dash console ◄── CORE.*_LATEST, OPS.ALERTS, OPS.REPORTS ◄─ human approve/reject
                                                         │
                                            hash-chained audit log (every step)
```

| Layer | Technology | Why |
|---|---|---|
| Transport | Kafka (KRaft, Docker) | Decouples bursty feeds from storage; replayable; standard in C2 and ISR data pipelines |
| Storage | Snowflake (RAW / CORE / OPS schemas) | Elastic SQL warehouse; time-series analytics with window functions; clean separation of raw evidence from derived data |
| Validation | Pydantic v2 | Typed schemas at the trust boundary |
| Detection | Python + SQL; scikit-learn later | Rules first for explainability, ML for ranking |
| Agent | LLM with read-only tool calling | Speeds up triage; constrained by guardrails |
| UI | Plotly Dash | Python-native, fast to iterate |

### Key decisions

- **Hand-rolled Kafka→Snowflake sink vs. Snowflake Kafka Connector.** The connector with Snowpipe
  Streaming is the production answer (lower latency, managed). The Python sink is used here so
  validation, lineage columns (topic/partition/offset) and at-least-once semantics are explicit
  and testable. Swapping it out later needs no schema change.
- **Deterministic `RECORD_ID`** (SHA-256 of source + raw payload). This makes replays idempotent,
  and it gives the agent a stable ID to cite.
- **Raw payload retained** on every row so any derived value can be re-checked against what the
  sensor actually sent.
- **Rules before ML.** Analysts must be able to see *why* something was flagged. ML is added to
  score and rank rule hits and to find unusual tracks, not to replace the rules.

## 4. Requirements and traceability

| ID | Requirement | Verified by | Status |
|---|---|---|---|
| ING-01 | Ingest ADS-B state vectors for the AOI at ≤30 s cadence | `adsb_producer`, live run | Done |
| ING-02 | Ingest AIS position reports for the AOI in real time | `ais_producer` | Done, needs API key |
| ING-03 | Validate all external records against typed schemas; reject out-of-range values | `tests/test_models.py` | Done |
| ING-04 | Route rejected records to a dead-letter topic with reasons, never drop silently | `kafka_utils.Publisher` | Done |
| ING-05 | Translate AIS "not available" sentinels (SOG 102.3, COG 360, HDG 511) to NULL | `tests/test_models.py` | Done |
| ING-06 | Ingest AIS static data (ship type, IMO, call sign, dimensions, destination) for class A and B | `tests/test_vessel_types.py` | Done |
| STO-01 | Persist validated records in Snowflake with Kafka lineage columns | `snowflake_sink`, `001_setup.sql` | Done, needs account |
| STO-02 | At-least-once delivery; commit offsets only after a successful load | `snowflake_sink.main` | Done |
| DET-01 | Detect AIS transmission gaps ≥ N hours and score by duration and displacement | `tests/test_detect.py` | Done |
| DET-02 | Detect loitering within radius R for ≥ T, away from port | `tests/test_phase2_detectors.py` | Done |
| DET-03 | Detect vessel rendezvous (two vessels within D nm at < S kts away from port) | `tests/test_phase2_detectors.py` | Done |
| DET-04 | Detect aircraft orbits (cumulative turn ≥ 720°) | `tests/test_phase2_detectors.py` | Done |
| DET-05 | ML anomaly score per track (Isolation Forest on kinematic features), with explanation | `tests/test_phase2_detectors.py` | Done |
| DET-06 | Suppress port activity for loiter / rendezvous / AIS-gap rules | `tests/test_phase2_detectors.py` | Done |
| DET-07 | Re-running detection must not duplicate alerts for the same episode | `tests/test_phase2_detectors.py`, live run | Done |
| DET-08 | A rule creating > 50 new alerts in one pass is held back and audited (storm guard) | `tests/test_phase2_detectors.py` | Done |
| DET-09 | Alerts record the detector version; superseded alerts are retracted with a reason, never deleted | `runner --retract-rule`, live run | Done |
| DET-10 | Exclude harbour/working craft (pilot, tug, SAR, fishing, pleasure, law enforcement) from LOITER, RENDEZVOUS and ML, but not from AIS_GAP | `tests/test_vessel_types.py` | Done |
| DET-11 | Rank tanker-tanker rendezvous above other pairs (ship-to-ship oil transfer pattern) | `tests/test_vessel_types.py` | Done |
| DET-12 | Vessel alerts carry self-reported name, type and IMO for the analyst | `tests/test_vessel_types.py` | Done |
| DET-13 | RENDEZVOUS only between transfer-capable ships (class A, ≥ 50 m, non-passenger) alongside (≤ 0.15 nm); recurring berths suppressed | `tests/test_vessel_types.py`, live run (129 → 7) | Done |
| DET-14 | Automated retraction never overrides a human review decision | `tests/test_vessel_types.py` | Done |
| AUD-02 | Audit log stays a single valid chain with several concurrent writers; broken chains are archived and re-anchored, never edited | `tests/test_guardrails.py` | Done |
| DET-15 | ML scores merchant ships at sea only, with minimum track length and an absolute score floor; at most 5 per pass | `tests/test_phase2_detectors.py` (incl. positive control), live run (43 → 2) | Done |
| DET-16 | LOITER near undersea cables/pipelines (≤ 1 nm) is always raised, ranked up and names the asset; anchorage crowds suppressed | `tests/test_vessel_types.py`, live run (163 → 26) | Done |
| DET-17 | The storm guard never holds back priority alerts (near infrastructure, tanker pairs) | `tests/test_vessel_types.py` | Done |
| AGT-01 | Agent tools are read-only; the agent cannot change alert or report status | | Planned |
| AGT-02 | Every factual sentence in a draft cites `[rec:<id>]` | `guardrails.grounding` | Checker done |
| AGT-03 | Citations must reference records supplied to the agent; invented IDs fail | `tests/test_guardrails.py` | Checker done |
| AGT-04 | Feed text (ship names, callsigns) is treated as data, never as instructions | | Planned (red-team tests) |
| AGT-05 | Sanctions screening against the OFAC SDN list | | Planned |
| HIL-01 | No alert or report is final without explicit human approval in the console | `dashboard/app.py` review, live run | Done for alerts; reports in Phase 3 |
| AUD-01 | All agent and human actions are written to a tamper-evident audit log | `tests/test_guardrails.py` | Done |
| UI-01 | Live map of vessels and aircraft, refreshed every 30 s | `dashboard/app.py` | Done |
| UI-02 | Alerts on the live map, coloured by rule, with a plain-language reason on hover | `dashboard/app.py`, live run | Done |
| UI-03 | Alert queue with rule/status filters; approve / reject / reopen with note, written to OPS.ALERTS and the audit log | `tests/test_console.py`, live run | Done |
| UI-04 | Evidence trail per alert (gap, rendezvous, loiter radius, orbit) and per clicked track | `tests/test_console.py`, live run | Done |
| UI-05 | Alert detail: plain-language reason, identity, cited sensor records, raw payload, audit history | `tests/test_console.py`, live run | Done |
| UI-06 | Vessel symbology: colour by ship type, heading leaders scaled by speed and zoom | `tests/test_console.py` | Done |
| UI-07 | Replay of collected history; traffic-density and dark-event heatmaps | live run | Done |
| UI-08 | Undersea infrastructure overlay (EMODnet pipelines and cables) with attribution | `tests/test_console.py` | Done |
| UI-09 | System-health strip: tracks, msg rate, sensor-to-DB latency, freshness, rejects, detector heartbeat | `tests/test_console.py`, live run | Done |
| UI-10 | Classification-style banners marking the system as unclassified, public-data, demonstration only | `dashboard/app.py` | Done |

## 5. Detection design (Phase 2)

| Rule | Logic | Default thresholds | Known benign look-alikes |
|---|---|---|---|
| AIS_GAP | Consecutive reports from one MMSI more than N apart; score = duration and displacement | gap ≥ 2 h | Terrestrial AIS coverage holes far offshore; vessels powering down alongside (port-suppressed) |
| LOITER | Two-pointer scan: from each anchor point, extend while the track stays within R and reports keep arriving. Merchant ships only (same pool as RENDEZVOUS) that have broadcast what they are and aren't fishing; loiterers with ≥ 2 others within 3 nm dropped as an anchorage. **Any loiterer within 1 nm of an undersea cable or pipeline is always kept, +0.3, and named with the asset and distance** | R = 1 nm, ≥ 3 h | Lone ships waiting at anchor for a berth; cable maintenance/survey vessels (legitimate, but must be seen) |
| RENDEZVOUS | 10-minute time slots; spatial grid hash finds close pairs per slot (neighbours only, not all pairs); groups of more than 3 slow vessels are dropped as berths/anchorages; consecutive slots form an episode. Only ships capable of a transfer: class A transponder, ≥ 50 m, not passenger, not moored/fishing. Spots where ≥ 4 different vessels pair up over time are dropped as berths. Tanker pairs +0.3, tanker alongside cargo +0.2, both identities unknown −0.2 | ≤ 0.15 nm (alongside), ≤ 3 kts, ≥ 30 min, group ≤ 3 | Legitimate bunkering; berths in inland waters (e.g. Lake Mälaren) |
| ORBIT | Signed heading change summed over a sliding window, so S-turns cancel and only sustained circling accumulates | ≥ 720° in ≤ 45 min | Airport holding patterns, survey and training flights |
| ML_ANOMALY | Isolation Forest over 8 kinematic features per track (speed mean/std/max, path length, straightness, turn rate, max report gap, report rate); robust z-scores name the top 3 drivers. Baseline = merchant ships at sea: same transfer-capable pool as RENDEZVOUS, positions inside port zones dropped | top 5, ≥ 99th percentile **and** raw score ≥ 0.65, tracks ≥ 2 h (aircraft 0.5 h) and ≥ 20 points, ≥ 30 tracks | Naval patrols, research/survey ships, vessels on inland waters (e.g. Lake Mälaren) |

- **Thresholds** live in one dataclass (`detect/runner.py: Thresholds`) so they can be tuned
  without touching detector code.
- **Port zones** are hand-curated circles around 31 Baltic ports (`detect/ports.py`). The
  production upgrade is the NGA World Port Index or harbour polygons.
- **Lesson from the first live run.** v1 produced 655 RENDEZVOUS alerts in 10 minutes: ships
  moored side by side in ports missing from the list (Hamburg, Hanko, Fredrikstad), with every
  pair in a crowded berth alerting separately. v2 adds data-driven defences that do not depend
  on a complete port list (AIS moored/fishing status, crowd filter), a **storm guard** that holds
  back any rule creating more than 50 new alerts in one pass, and **versioned alerts** so the
  734 v1 alerts were retracted with a reason instead of deleted. Result: 655 → 7, and the 7
  remaining are harbour craft that AIS vessel type will remove.
- **Vessel type (detector v3).** AIS static data gives each vessel's self-reported type
  (ITU-R M.1371). Working craft whose job looks like loitering or meeting ships (pilots, tugs,
  SAR, fishing, pleasure, law enforcement) are left out of LOITER, RENDEZVOUS and the ML
  baseline. They stay in AIS_GAP, because fishing vessels going dark is itself a known
  illegal-fishing indicator. Dredging/underwater-ops (33) and military (35) are deliberately
  kept in scope: they matter for undersea-infrastructure monitoring. Tanker-tanker rendezvous
  gets a +0.3 score boost. The type is self-reported and can be spoofed, so it adjusts what
  is flagged; it is never treated as proof. A vessel with no static data yet is treated as
  unknown and still checked.
- **Tuning ship-to-ship detection on live data (detector v4).** After ~5 h, 129 RENDEZVOUS
  alerts were open. Profiling them: 87 involved a vessel under 30 m, 43 a class B (small-craft)
  transponder, and most large pairs were ferries at terminals or ships anchored ~0.4 nm apart.
  A cargo transfer needs ocean-going ships moored *alongside*, so v4 requires class A, ≥ 50 m,
  non-passenger vessels within 0.15 nm, and drops spots where four or more different vessels
  rotate through (an unlisted berth). The AIS class is not a column: it is read from the raw
  message kept on every row, which is why raw payloads are retained. **Result: 129 → 7.** The top
  identified alert is a tanker 0.011 nm (~20 m) from a cargo ship outside Gothenburg, the exact
  alongside pattern (probably routine bunkering, which is the analyst's call). Each rule is pinned
  to the live case that motivated it by a regression test.
- **Tuning the ML layer on live data (detector v5).** 43 ML alerts were open and every pass
  added up to 10 more. Profiling them: 23 of 38 vessel alerts were centred inside port zones
  (ships berthing and turning), 11 were ferries, pilots/tugs/17–40 m craft remained, and a
  quarter came from tracks under an hour, where every feature is extreme. v5 scores only
  merchant ships at sea (same pool as RENDEZVOUS, positions in port zones dropped), needs ≥ 2 h
  and ≥ 20 points per track, and requires an absolute anomaly score as well as a high
  percentile, so a quiet picture produces nothing rather than its "top k". A positive-control
  test keeps the exclusion tests honest. **Result: 43 → 2.** The top flag was a vessel
  self-identifying as "NATO WARSHIP A1411" working a 7.8 nm box at ~10 kn for 2.8 h in the Gulf
  of Finland (a patrol pattern); the other a research ship. Both genuinely move unlike merchant
  traffic, and both are benign once an analyst looks, which is the intended division of labour.
  Names now fall back to position reports for vessels that never send static data.
- **Tuning loitering, and ranking by what's underneath (detector v6).** Once ~7 h of data
  existed, the 3 h loiter rule produced ~155 alerts per pass and the storm guard held them all.
  Profiling 163: almost all stationary, 59 ferries idle at small terminals, 42 in anchorage
  crowds, and unknown-identity loiterers overwhelmingly pilot/rescue/fishing boats. Each
  candidate filter was measured before choosing (e.g. dropping unknown identity 77 → 28;
  widening the anchorage radius 2 → 3 nm a further few). Distance to the EMODnet cable/pipeline
  overlay is now computed for every loiterer: within 1 nm is always kept and ranked first,
  because anchor dragging across Balticconnector and Estlink 2 is the real-world threat.
  **Result: 163 → 26**, the top three all within ~0.5 nm of power cables, led by a vessel
  reporting "restricted manoeuvrability" 0.058 nm from the Baltic 1 wind-farm export cable
  (most likely maintenance: legitimate, but exactly what must be seen). The storm guard now
  always lets priority alerts (near infrastructure, tanker pairs) through, so a flood of noise
  cannot hide them. Trade-off, documented: a vessel that never broadcasts identity and loiters
  far from infrastructure raises no LOITER alert; AIS_GAP and ML still cover it.
- **Idempotency.** Detection re-scans a sliding window. An alert whose rule and track overlap an
  existing alert's time window updates that alert (score, evidence, window) instead of creating a
  duplicate. Only new alerts are written to the audit log.
- **Next improvement.** AIS static data (vessel type) would let detectors expect tugs and pilots
  to move erratically, which removes the largest source of ML false positives seen in live runs.

## 6. Operator console (Phase 4)

- **Thin callbacks.** Queries live in `dashboard/data.py` (one reused Snowflake connection,
  parameterised SQL) and drawing in `dashboard/figures.py` (pure functions, unit-tested), so the
  Dash callbacks in `app.py` only wire them together.
- **Explain, don't assert.** Every alert opens with a plain-language reason, the evidence trail on
  the map, and the exact sensor records it cites, so a reviewer can check the claim rather than
  trust a score.
- **Decisions are auditable.** Approve / Reject / Reopen updates `OPS.ALERTS` (reviewer, time,
  note) and appends to the hash-chained audit log; the detail panel shows that history.
- **Banners.** "UNCLASSIFIED // PUBLIC DATA // DEMONSTRATION SYSTEM - NOT AFFILIATED WITH ANY
  GOVERNMENT": the convention signals how the data is handled, and the wording makes clear the
  project has no official status.
- **Found by using it.** Building the console against live data surfaced four real defects, each
  now covered by a regression test: the Kafka→Snowflake sink crashed on a timestamp with whole
  seconds (`...:26Z` vs `...:26.123Z`), which the new health strip exposed as a 15-minute
  freshness alarm (no data lost: offsets are committed only after load, so it replayed from
  Kafka); the 30 s refresh wiped a half-typed review note; the went-dark trail lost its
  "after" leg to sub-second rounding; and a review message leaked onto the next alert.
- **Audit chain fork (found by `verify_chain`).** Each process cached the last hash in memory,
  so when the detector loop, a manual detector run and the console all appended, the chain
  forked at line 789. Entries were intact; the link was wrong. Writes now take an OS file lock
  and re-read the true last entry. The broken file was **archived unchanged** and a new chain
  started whose first entry records the archived file, its last hash, the break line and the
  cause; it was not "repaired" in place. The console reads history across both files.
- **Automation must not overwrite people.** The v4 retraction flipped an analyst-REJECTED
  alert to RETRACTED. Retraction now touches only unreviewed (NEW) alerts; the decision was
  restored and the restore audited.

## 7. Responsible AI alignment

| DoD AI Ethical Principle | How Watchfloor addresses it |
|---|---|
| Responsible | A human approves every report; the agent cannot approve its own work (HIL-01, AGT-01) |
| Equitable | Detections are behaviour-based (kinematics), never based on flag state or nationality |
| Traceable | Record-level citations, raw payloads kept, hash-chained audit log (AGT-02, AUD-01) |
| Reliable | Grounding score tracked per report; detector unit tests; evaluation set of labelled alerts |
| Governable | Rule thresholds in config; agent can be disabled with detection still running |

Mapped to the NIST AI RMF functions: **Govern** (audit log, roles), **Map** (this document's
non-goals and risks), **Measure** (grounding score, false-positive rate per rule), **Manage**
(human approval, kill switch, dead-letter review).

## 8. Risks

| Risk | Mitigation |
|---|---|
| AIS spoofing or junk data | Schema validation, implied-speed checks, dead-letter review |
| Coverage gaps look like "going dark" | Score uses displacement as well as duration; reviewers see coverage context |
| Detector false positives (anchorages, harbour craft, holding patterns) | Port zones, conservative thresholds, explanations on every alert, human review |
| Prompt injection via free-text AIS fields | Tool output passed as data; red-team test cases (AGT-04) |
| LLM hallucination | Mandatory citations, automated grounding check, human approval |
| Feed rate limits | Backoff and retry; configurable poll interval |

## 9. Roadmap

1. **Phase 1: Ingest and storage.** Kafka, producers, Snowflake sink, live map. *(done)*
2. **Phase 2: Detection.** Loiter, rendezvous and orbit rules, ML scoring, port zones,
   `OPS.ALERTS` writer, alerts on the map. *(done)*
3. **Phase 3: Agent.** Tool-calling analyst, OFAC screening, grounding retry loop, evaluation set.
4. **Phase 4: Console.** *(done, except metrics)* Alert queue, evidence trails, review with
   audit, replay, heatmaps, infrastructure overlay, health strip. Still to do: metrics page (hallucination
   rate, alert precision, ingest lag).
