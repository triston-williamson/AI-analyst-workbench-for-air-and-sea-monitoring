# Watchfloor: Design Document

| | |
|---|---|
| **Status** | Phase 1 (ingest + storage) in progress |
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
| STO-01 | Persist validated records in Snowflake with Kafka lineage columns | `snowflake_sink`, `001_setup.sql` | Done, needs account |
| STO-02 | At-least-once delivery; commit offsets only after a successful load | `snowflake_sink.main` | Done |
| DET-01 | Detect AIS transmission gaps ≥ N hours and score by duration and displacement | `tests/test_detect.py` | Done |
| DET-02 | Detect loitering within radius R for ≥ T | | Planned |
| DET-03 | Detect vessel rendezvous (two vessels within D nm at < S kts away from port) | | Planned |
| DET-04 | Detect aircraft orbits (cumulative turn ≥ 720°) | | Planned |
| DET-05 | ML anomaly score per track (e.g. Isolation Forest on kinematic features) | | Planned |
| AGT-01 | Agent tools are read-only; the agent cannot change alert or report status | | Planned |
| AGT-02 | Every factual sentence in a draft cites `[rec:<id>]` | `guardrails.grounding` | Checker done |
| AGT-03 | Citations must reference records supplied to the agent; invented IDs fail | `tests/test_guardrails.py` | Checker done |
| AGT-04 | Feed text (ship names, callsigns) is treated as data, never as instructions | | Planned (red-team tests) |
| AGT-05 | Sanctions screening against the OFAC SDN list | | Planned |
| HIL-01 | No report is final without explicit human approval in the console | | Planned |
| AUD-01 | All agent and human actions are written to a tamper-evident audit log | `tests/test_guardrails.py` | Done |
| UI-01 | Live map of vessels and aircraft, refreshed every 30 s | `dashboard/app.py` | Done |
| UI-02 | Alert queue and report review (approve / reject / edit) | | Planned |

## 5. Responsible AI alignment

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

## 6. Risks

| Risk | Mitigation |
|---|---|
| AIS spoofing or junk data | Schema validation, implied-speed checks, dead-letter review |
| Coverage gaps look like "going dark" | Score uses displacement as well as duration; reviewers see coverage context |
| Prompt injection via free-text AIS fields | Tool output passed as data; red-team test cases (AGT-04) |
| LLM hallucination | Mandatory citations, automated grounding check, human approval |
| Feed rate limits | Backoff and retry; configurable poll interval |

## 7. Roadmap

1. **Phase 1: Ingest and storage.** Kafka, producers, Snowflake sink, live map. *(current)*
2. **Phase 2: Detection.** Remaining rules, ML scoring, `OPS.ALERTS` writer, port polygons.
3. **Phase 3: Agent.** Tool-calling analyst, OFAC screening, grounding retry loop, evaluation set.
4. **Phase 4: Console.** Alert queue, report review, audit viewer, metrics page (hallucination
   rate, alert precision, ingest lag).
