# Watchfloor

**An AI-assisted maritime and air domain awareness workbench.** It streams live aircraft (ADS-B)
and ship (AIS) positions through Kafka into Snowflake, flags suspicious behaviour such as vessels
going dark, loitering, ship-to-ship meetings and aircraft orbits, and uses an LLM analyst agent to
draft cited intelligence-style reports that a human must approve.

> Unclassified. Uses only public data feeds and textbook methods. See [docs/DESIGN.md](docs/DESIGN.md).

![CI](../../actions/workflows/ci.yml/badge.svg)

## Why

In the Baltic Sea, AIS gaps and ship-to-ship transfers are linked to sanctions evasion and to
damage to undersea infrastructure. Analysts can't watch thousands of tracks at once. Watchfloor
narrows the picture to the few that matter and explains why, with every claim tied to source data.

## Architecture

```
OpenSky (ADS-B) ─┐                                  ┌─► Detection ─► Alerts ─► LLM analyst ─► Human approval
                 ├─► Kafka ─► Snowflake (RAW/CORE/OPS) ┤                         (cited, grounded)
aisstream (AIS) ─┘     └─► dead-letter (rejected input) └─► Dash live map
                                     every step ─► hash-chained audit log
```

| | |
|---|---|
| Streaming | Kafka (KRaft) in Docker |
| Warehouse | Snowflake |
| Validation | Pydantic v2: all feed data is treated as untrusted |
| Detection | Python and SQL rules, ML scoring planned |
| AI | LLM agent with read-only tools, citation grounding check, human-in-the-loop |
| UI | Plotly Dash |
| Quality | pytest, ruff, GitHub Actions CI |

## Status

| Phase | Scope | |
|---|---|---|
| 1 | Kafka ingest (ADS-B + AIS), Snowflake sink, live map | ✅ built |
| 2 | Detection: AIS gaps ✅, loitering, rendezvous, orbits, ML scoring | 🚧 |
| 3 | Analyst agent, OFAC sanctions screening, grounding loop | 📋 |
| 4 | Alert queue, report approval, metrics page | 📋 |

Requirements and how each is verified are listed in [DESIGN.md §4](docs/DESIGN.md#4-requirements-and-traceability).

## Quick start (Windows / PowerShell)

Prerequisites: Python 3.11+, Docker Desktop, a free [Snowflake trial](https://signup.snowflake.com/),
and a free [aisstream.io](https://aisstream.io) API key.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env      # then fill in your keys
docker compose up -d        # Kafka on :9092, Kafka UI on http://localhost:8080
python -m watchfloor.load.setup_snowflake   # one-time: creates warehouse, role, DB, tables
```

Then run each piece in its own terminal (or use **Run and Debug → "Run full pipeline"** in VS Code):

```powershell
python -m watchfloor.ingest.adsb_producer   # aircraft -> Kafka
python -m watchfloor.ingest.ais_producer    # ships -> Kafka
python -m watchfloor.load.snowflake_sink    # Kafka -> Snowflake
python -m watchfloor.dashboard.app          # http://127.0.0.1:8050
```

Run the tests with `pytest`. Change the area of interest with `AOI_BBOX` in `.env`.

## Repository layout

```
src/watchfloor/
  ingest/      OpenSky + aisstream producers, validation, dead-letter routing
  load/        Kafka -> Snowflake sink, connection, one-time setup
  sql/         Snowflake DDL (RAW / CORE / OPS schemas)
  detect/      anomaly rules (AIS gap implemented) and geodesy
  agent/       LLM analyst (Phase 3)
  guardrails/  hash-chained audit log, citation grounding checker
  dashboard/   Dash operator console
docs/DESIGN.md requirements, architecture, Responsible AI mapping, risks
```

## Data sources

- [OpenSky Network](https://opensky-network.org): ADS-B state vectors (non-commercial use)
- [aisstream.io](https://aisstream.io): live AIS over websocket
- [OFAC SDN list](https://sanctionslist.ofac.treas.gov): sanctions screening (Phase 3)
