"""Grounding checks for AI-drafted reports.

Reports must cite evidence inline as ``[rec:<record_id>]``. Before a draft reaches a human
reviewer we check that:
  * every factual sentence has at least one citation, and
  * every cited ID is one the agent was actually given (no invented sources).

The resulting ``grounding_score`` (share of sentences that are properly cited) is stored with each
report and tracked over time as the project's hallucination metric.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CITATION = re.compile(r"\[rec:([0-9a-f]{8,64})\]")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
# Lines that are structure, not claims (headings, empty, labels).
NON_CLAIM = re.compile(r"^\s*(#|$|\*\*[^*]+\*\*:?\s*$)")


@dataclass
class GroundingResult:
    grounding_score: float
    uncited_sentences: list[str] = field(default_factory=list)
    unknown_citations: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.unknown_citations and self.grounding_score >= 0.9


def check_grounding(report_markdown: str, allowed_ids: set[str]) -> GroundingResult:
    sentences: list[str] = []
    for line in report_markdown.splitlines():
        if NON_CLAIM.match(line):
            continue
        sentences.extend(s for s in SENTENCE_SPLIT.split(line.strip()) if s)

    if not sentences:
        return GroundingResult(grounding_score=0.0)

    uncited, unknown = [], []
    for s in sentences:
        ids = CITATION.findall(s)
        if not ids:
            uncited.append(s)
        unknown.extend(i for i in ids if i not in allowed_ids)

    score = 1 - len(uncited) / len(sentences)
    return GroundingResult(round(score, 3), uncited, sorted(set(unknown)))
