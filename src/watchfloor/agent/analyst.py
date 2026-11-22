"""Analyst agent (Phase 3 - skeleton).

Workflow for one alert:
  1. Load the alert and its evidence records from Snowflake.
  2. Let the LLM call read-only tools:
       - get_track_history(track_id, hours)     -> position records with RECORD_IDs
       - get_nearby_tracks(lat, lon, radius, t) -> other vessels/aircraft near the event
       - check_sanctions(name, mmsi, imo)       -> OFAC SDN list matches
  3. LLM drafts a short report; every claim cites [rec:<id>] from tool results.
  4. guardrails.grounding.check_grounding() validates citations; failures are sent back to the
     model once, then flagged for the reviewer rather than hidden.
  5. Draft is stored in OPS.REPORTS with status DRAFTED; nothing is "final" until a human
     approves it in the dashboard.
  6. Every step is written to the hash-chained audit log.

Security posture: tool results (ship names, callsigns, free text in AIS) are untrusted data and
are passed to the model as data, never as instructions. Tools are read-only; the agent cannot
change alert status or approve its own work.
"""

from __future__ import annotations

from dataclasses import dataclass

from watchfloor.detect.rules import Alert


@dataclass
class DraftReport:
    alert_id: str
    markdown: str
    cited_ids: list[str]
    grounding_score: float
    model: str


def investigate(alert: Alert) -> DraftReport:
    raise NotImplementedError("Phase 3: see docs/DESIGN.md, requirements AGT-01..AGT-05")
