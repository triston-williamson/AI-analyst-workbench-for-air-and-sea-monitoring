# Watchfloor

**An AI-assisted maritime and air domain awareness workbench.** It streams live aircraft (ADS-B)
and ship (AIS) positions through Kafka into Snowflake, flags suspicious behaviour such as vessels
going dark, loitering, ship-to-ship meetings, aircraft orbits and vessels on the OFAC sanctions
list, and uses an LLM analyst agent to draft cited intelligence-style reports that a human must
approve.

> Unclassified. Uses only public data feeds and textbook methods. See [docs/DESIGN.md](docs/DESIGN.md).

**Read the [technical write-up (PDF, 23 pages)](docs/Watchfloor_Technical_Write-Up.pdf)** for the
whole project in one place: architecture, ingestion, storage, each detector, the ML layer,
sanctions screening, the AI analyst and its guardrails, the evaluation and hill-climb, the
console, security and responsible-AI controls, and lessons learned.

[![CI](https://github.com/triston-williamson/AI-analyst-workbench-for-air-and-sea-monitoring/actions/workflows/ci.yml/badge.svg)](https://github.com/triston-williamson/AI-analyst-workbench-for-air-and-sea-monitoring/actions/workflows/ci.yml)

![Watchfloor operator console: about 1,950 vessels and 75 aircraft over the Baltic coloured by type, cables and pipelines overlaid, alert rings on the map, a system-health strip across the top and the alert queue on the right](docs/images/console-overview.jpg)

*The whole picture, live: about 1,950 vessels and 75 aircraft over the Baltic, coloured by type,
with undersea cables and pipelines, alert rings, a system-health strip and the ranked alert
queue. Queue cards show the AI analyst's recommendation (here "AI · dismiss" on a naval vessel).*

## At a glance

| | |
|---|---|
| **Live picture** | About 700 messages/min; about 1,900 vessels and 150 aircraft tracked |
| **Detectors** | Went dark, loitering, ship-to-ship meeting, aircraft circling, OFAC sanctions match, Isolation Forest anomalies |
| **Tuned on live data** | Ship meetings 129 → 7, ML anomalies 43 → 2, loitering 163 → 26 |
| **Sanctions screening** | Every vessel seen (3,329) screened against the OFAC SDN list in about 60 ms |
| **AI analyst** | Claude Opus 5.5 with read-only tools; every factual sentence must cite a sensor record |
| **Evaluated** | Right call 82.7% → **90.4%**, citation support 80.2%, grounding 100%, prompt injection resisted 4/4 |
| **Cost** | About $0.11 and 18 s per AI report; one X-Small Snowflake warehouse that auto-suspends |
| **Quality** | 167 tests, ruff, gitleaks secret scanning, GitHub Actions CI |

## What it finds

![A vessel's AIS track goes dark at 0304Z and reappears 35.6 nm away at 0515Z; the alert queue and evidence panel are on the right](docs/images/console-went-dark.jpg)

*Going dark. A vessel stops transmitting AIS at 0304Z and reappears 35.6 nm away at 0515Z
(dotted segment). The analyst sees why it was flagged, the cited sensor records, and the
audit history, then approves or rejects it.*

![A cargo ship on the OFAC SDN list sails south through the Øresund past Helsingborg and Copenhagen; the alert explains the strong IMO match and that presence alone is not wrongdoing](docs/images/console-sanctions.jpg)

*Who, not just how. A cargo ship listed under RUSSIA-EO14024 sails a normal transit through the
Øresund. No behaviour rule would flag it, so the sanctions rule matches every vessel's IMO and
MMSI against the OFAC SDN list. The alert says plainly that presence is not wrongdoing; the AI
analyst recommends monitoring.*

![A tanker and a cargo ship converge and lie 20 m apart outside Gothenburg; the alert explains the ship-to-ship transfer pattern and lists both IMO numbers](docs/images/console-tanker-alongside.jpg)

*Ship-to-ship detection after tuning on live data (129 alerts → 7): a tanker lies 0.011 nm
alongside a cargo ship outside Gothenburg. Both tracks converge on the meeting point.*

![A vessel stopped 0.058 nm from the Baltic 1 wind-farm power cable; the alert leads with NEAR SEABED INFRASTRUCTURE](docs/images/console-loiter-cable.jpg)

*Loitering is ranked by what lies underneath: a vessel stopped 0.058 nm from the Baltic 1
export cable is raised first. Anchor dragging damaged Baltic cables and pipelines in 2023–24.*

| Traffic density | Where vessels went dark |
|---|---|
| ![Traffic-density heatmap showing the main Baltic shipping lanes through the Danish straits, the Øresund, and along the Swedish and Finnish coasts](docs/images/console-traffic-density.jpg) | ![Dark-events heatmap: where AIS gaps began, with the strongest cluster in the western Baltic near Fehmarn](docs/images/console-dark-events.jpg) |

## The AI analyst

![AI analyst report in the console: a patrol-pattern track on the map, and a report that passed the grounding check with compact citations](docs/images/console-ai-report.jpg)

*The AI analyst (Claude) investigated an ML alert with read-only tools and drafted a cited
report: a self-identified naval vessel on a patrol pattern, 4.4 nm from Balticconnector, no
partner vessel, identity unverified. Every factual sentence cites a sensor record; the
reviewer still decides.*

![The AI reports list below the map: six reports with their recommendations; the open report on a sanctioned cargo ship has Summary, Observations, Assessment and Recommended action sections, each observation citing record IDs, and a Grounding PASSED badge](docs/images/console-ai-reports-list.jpg)

*All AI reports in one list below the map, filterable by recommendation. Each report keeps fact
(Summary, Observations: every sentence cites `rec:`, `alert:` or `sdn:` IDs) apart from
judgement (Assessment, Recommended action), shows whether it passed the grounding check, and
has a Show on map button. Nothing is final until an analyst approves the alert.*

**How it works.** The agent runs on demand (the console's **Draft AI report** button or
`--top N`), never on a schedule. Its tools are read-only: track history, vessels nearby,
self-reported identity, OFAC screening and nearest undersea infrastructure. A grounding
guardrail rejects any uncited factual sentence or invented record ID and sends the draft back
once; a draft that still fails is saved as failing, never silently fixed. Ship names and other
broadcast text are treated as data, never instructions. Every step lands in a hash-chained,
tamper-evident audit log.

**Evaluated, not just tested.** An eval of 26 frozen real alerts (labels verified by a human)
runs the agent end to end and grades each report: right call **90.4%** (baseline 82.7%),
citation support **80.2%** (graded by a separate Claude judge), grounding guardrail 100%,
prompt-injection attacks resisted 4/4, at about $0.11 and 19 s per report. It exposed what the
guardrail can't see (claims cited to the wrong record) and a stale-position ingestion bug
that made an airliner look like a flight into Kaliningrad: 33% of stored aircraft records
were stale repeats. Fixing that bug and the tool gap behind it removed the false escalation.
About half of the 7.7-point gain is run-to-run noise, and the write-up says so. See
[DESIGN.md §8](docs/DESIGN.md#8-evaluating-the-analyst-evalsanalyst).

**Tuned on live data, not guessed.** Each detector was profiled against hours of real Baltic
traffic and tightened from evidence: ship meetings 129 → 7, ML anomalies 43 → 2 (the top one a
self-identified naval vessel on a patrol pattern), loitering 163 → 26 with ships near seabed
infrastructure ranked first. Every change is pinned by a regression test; see
[DESIGN.md §5](docs/DESIGN.md#5-detection-design-phase-2).

## Why

In the Baltic Sea, AIS gaps and ship-to-ship transfers are linked to sanctions evasion and to
damage to undersea infrastructure. Analysts can't watch thousands of tracks at once. Watchfloor
narrows the picture to the few that matter and explains why, with every claim tied to source data.

## Architecture

![Architecture: OpenSky ADS-B and aisstream AIS feed the producers (Pydantic validation, stale-position filter, rejects to a dead-letter topic), then Kafka and a Snowflake sink into RAW, CORE and OPS schemas; detection and the AI analyst read Snowflake and local reference data (OFAC, EMODnet, port zones); everything ends in the analyst console, where a human approves or rejects, with every action in a hash-chained audit log](docs/images/architecture.png)

| | |
|---|---|
| Streaming | Kafka (KRaft) in Docker |
| Warehouse | Snowflake (RAW / CORE / OPS schemas, versioned migrations) |
| Validation | Pydantic v2: all feed data is treated as untrusted |
| Detection | Rule engine (gap, loiter, rendezvous, orbit, OFAC sanctions match) + Isolation Forest anomaly scoring |
| AI | Claude Opus 5.5 through the Anthropic SDK tool runner: read-only tools, citation grounding check, human-in-the-loop |
| UI | Plotly Dash |
| Quality | pytest, ruff, gitleaks, GitHub Actions CI |

## Status

| Phase | Scope | |
|---|---|---|
| 1 | Kafka ingest (ADS-B + AIS), Snowflake sink, live map | ✅ built |
| 2 | Detection: AIS gaps, loitering, ship-to-ship rendezvous, aircraft orbits, ML anomaly scoring, vessel-type context, alerts on map | ✅ built |
| 3 | AI analyst agent (Claude): read-only tools, OFAC sanctions screening, cited reports with a grounding retry loop, 26-case evaluation with a hill-climb (v1, v2) | ✅ built |
| 4 | Operator console: alert queue, evidence trails, approve/reject with audit, replay, heatmaps, system health, AI report badges and reports list | ✅ built (metrics page 📋) |

Requirements and how each is verified are listed in [DESIGN.md §4](docs/DESIGN.md#4-requirements-and-traceability).
Next up: a sanctions escalation policy in the agent prompt (measured as an eval variant), a
citable ID for the agent's data-quality note, and a metrics page.

## Operator console

| Feature | What it shows |
|---|---|
| **Alert queue** | Alerts ranked by score, filterable by rule and review status. Click to fly the map to it. |
| **AI badges** | Each queue card with an AI report shows its recommendation: escalate, monitor or dismiss. |
| **AI reports list** | Below the map: the latest report per alert, filterable by recommendation, each with grounding status and **Show on map**. |
| **Evidence trails** | *Went dark:* track, dotted gap, "went dark" / "reappeared" times. *Ship meeting:* both tracks converging. *Circling:* the orbit. Click any ship for its last 2 h. |
| **Alert detail** | Plain-language reason, vessel identity (name, type, IMO), the cited sensor records with raw payload, audit history. |
| **Approve / Reject** | Analyst decision with a note, written to `OPS.ALERTS` and the hash-chained audit log; can be reopened. |
| **Map interaction** | The map keeps your zoom across the 30 s refresh. Click empty map or press Esc to clear a selection; click a selected card or track again to deselect. |
| **Vessel display** | Colour by ship type (tanker, cargo, passenger, working craft, ...) with heading leaders whose length shows speed; stationary vessels have none. |
| **Replay** | Scrub or play back the collected history in 10-minute steps. |
| **Heatmaps** | Traffic density (shipping lanes) and where vessels went dark. |
| **Undersea infrastructure** | Pipelines (incl. Nord Stream 1/2, Balticconnector) and power/telecom cables from EMODnet. |
| **System health** | Live tracks, messages/min, sensor-to-warehouse latency, data freshness, rejected records, detector heartbeat. |

Times are shown in Zulu (UTC) date-time groups, e.g. `221432Z NOV 26`.

## Quick start (Windows / PowerShell)

Prerequisites: Python 3.11+, Docker Desktop, a free [Snowflake trial](https://signup.snowflake.com/),
and a free [aisstream.io](https://aisstream.io) API key. AI reports also need an
[Anthropic API key](https://console.anthropic.com).

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env      # then fill in your keys
docker compose up -d        # Kafka on :9092, Kafka UI on http://localhost:8080
python -m watchfloor.load.setup_snowflake   # creates warehouse, role, DB, tables; re-run after pulling new migrations
```

Or start, check and stop everything with one script (runs each piece hidden in the background,
logs to `logs/<name>.log`, and `stop` also suspends the Snowflake warehouse):

```powershell
.\scripts\pipeline.ps1 start
.\scripts\pipeline.ps1 status
.\scripts\pipeline.ps1 stop
```

Or run each piece in its own terminal (or use **Run and Debug → "Run full pipeline"** in VS Code):

```powershell
python -m watchfloor.ingest.adsb_producer   # aircraft -> Kafka
python -m watchfloor.ingest.ais_producer    # ships -> Kafka
python -m watchfloor.load.snowflake_sink    # Kafka -> Snowflake
python -m watchfloor.detect.runner          # detectors every 5 min -> OPS.ALERTS
python -m watchfloor.dashboard.app          # http://127.0.0.1:8050
```

Draft AI analyst reports (needs `ANTHROPIC_API_KEY` in `.env`; each report is a few Claude
API calls), or use the **Draft AI report** button on any alert in the console:

```powershell
python scripts/fetch_sanctions.py                    # once: OFAC SDN list for sanctions checks
python -m watchfloor.agent.analyst --top 3           # top 3 open alerts without a report
python -m watchfloor.agent.analyst --top 1 --dry-run # print only
```

Run the analyst evaluation (26 cases × 2 runs costs about $5.60 in API usage; a changed agent
or eval must be approved first with `--approve-harness`):

```powershell
python evals/analyst/run_eval.py --variant v3 --reps 2
```

Try the detectors without writing anything: `python -m watchfloor.detect.runner --once --dry-run`.
Rules such as loitering (3 h) and going dark (2 h) need a few hours of collected data to fire.

Run the tests with `pytest`. Change the area of interest with `AOI_BBOX` in `.env`.

**Running cost.** While the pipeline runs, Snowflake uses about 1 credit per hour on an X-Small
warehouse. It auto-suspends after 60 s idle, and `pipeline.ps1 stop` suspends it straight away.
OpenSky and aisstream are free.

## Secrets handling

- API keys and passwords live only in `.env`, which is git-ignored. `.env.example` holds blank
  placeholders only.
- Secrets load as Pydantic `SecretStr`, so they print as `**********` in logs and tracebacks.
- A pre-commit hook ([.githooks/pre-commit](.githooks/pre-commit)) blocks secret files and runs
  [gitleaks](https://github.com/gitleaks/gitleaks) on staged changes. Turn it on after cloning:
  `git config core.hooksPath .githooks`
- CI scans the full git history with gitleaks on every push.
- Downloaded OFAC data, eval traces and audit logs stay local (git-ignored).

## Repository layout

```
src/watchfloor/
  ingest/      OpenSky + aisstream producers, validation, stale-position filter, dead-letter routing
  load/        Kafka -> Snowflake sink, connection, one-time setup
  sql/         Snowflake DDL and versioned migrations (RAW / CORE / OPS schemas)
  detect/      rules (gap, loiter, rendezvous, orbit, sanctions), ML scoring, port zones, runner
  agent/       AI analyst agent: tools, OFAC sanctions screening, investigation loop
  guardrails/  hash-chained audit log, citation grounding checker
  dashboard/   operator console: app (layout/callbacks), data (queries), figures (map drawing), assets
evals/analyst/ 26-case evaluation: cases, graders (incl. LLM judge), runner
scripts/       pipeline start/stop/status, OFAC and EMODnet downloads
tests/         167 pytest tests: models, detectors, agent, guardrails, console, eval graders
docs/
  DESIGN.md                          requirements, architecture, Responsible AI mapping, risks
  Watchfloor_Technical_Write-Up.pdf  full technical write-up
  images/                            screenshots and the architecture diagram
```

## Data sources

- [OpenSky Network](https://opensky-network.org): ADS-B state vectors (non-commercial use)
- [aisstream.io](https://aisstream.io): live AIS over websocket
- [OFAC SDN list](https://sanctionslist.ofac.treas.gov): sanctions screening
- [EMODnet Human Activities](https://emodnet.ec.europa.eu/en/human-activities): pipelines and
  cables (EU open data). Refresh with `python scripts/fetch_overlays.py`.
