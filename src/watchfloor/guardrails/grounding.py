"""Grounding checks for AI-drafted reports.

Reports cite evidence inline:
  ``[rec:<record_id>]``  a sensor record (AIS / ADS-B position or static message)
  ``[alert:<alert_id>]`` a value the detector computed for this alert (gap hours, distances)
  ``[sdn:<entry_id>]``   an OFAC SDN list entry returned by the sanctions tool

Reports keep fact and judgement apart, as intelligence analysis does. Sentences under
**Summary** and **Observations** are factual and each must carry a citation. **Assessment** and
**Recommended action** are judgement: they need no citation per sentence, but may not cite
anything the agent wasn't given. Before a draft reaches a reviewer we check that:
  * every factual sentence has at least one citation, and
  * every cited ID anywhere is one the agent was actually given (no invented sources).

The resulting ``grounding_score`` (share of factual sentences properly cited) is stored with
each report and tracked over time as the project's hallucination metric.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CITATION = re.compile(r"\[(rec|alert|sdn):([A-Za-z0-9-]{1,64})\]")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
HEADING = re.compile(r"^\s*#{1,6}\s*(.+?)\s*$")
# Lines that are structure, not claims (empty, bold labels, horizontal rules).
NON_CLAIM = re.compile(r"^\s*($|\*\*[^*]+\*\*:?\s*$|-{3,}\s*$)")
LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
JUDGEMENT_SECTIONS = ("assessment", "recommended action", "recommendation")


def citation_key(kind: str, ident: str) -> str:
    """Normalised ID used in ``allowed_ids``: bare for sensor records, prefixed otherwise."""
    return ident if kind == "rec" else f"{kind}:{ident}"


def cited_ids(text: str) -> list[str]:
    return [citation_key(k, i) for k, i in CITATION.findall(text)]


@dataclass
class GroundingResult:
    grounding_score: float
    uncited_sentences: list[str] = field(default_factory=list)
    unknown_citations: list[str] = field(default_factory=list)
    factual_sentences: int = 0

    @property
    def passed(self) -> bool:
        return not self.unknown_citations and self.grounding_score >= 0.9


def check_grounding(report_markdown: str, allowed_ids: set[str]) -> GroundingResult:
    factual: list[str] = []
    unknown: set[str] = set()
    in_judgement = False
    for line in report_markdown.splitlines():
        unknown.update(i for i in cited_ids(line) if i not in allowed_ids)
        heading = HEADING.match(line)
        if heading:
            in_judgement = heading.group(1).strip().lower().startswith(JUDGEMENT_SECTIONS)
            continue
        if in_judgement or NON_CLAIM.match(line):
            continue
        text = LIST_MARKER.sub("", line.strip())
        factual.extend(s for s in SENTENCE_SPLIT.split(text) if s)

    if not factual:
        return GroundingResult(0.0, [], sorted(unknown), 0)
    uncited = [s for s in factual if not CITATION.search(s)]
    score = 1 - len(uncited) / len(factual)
    return GroundingResult(round(score, 3), uncited, sorted(unknown), len(factual))
