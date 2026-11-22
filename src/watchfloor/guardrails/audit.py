"""Append-only, hash-chained audit log.

Every agent action (tool call, draft, human decision) is written as one JSON line whose hash
covers the previous line's hash. Editing or deleting any earlier entry breaks the chain, which
``verify_chain`` detects. This gives reviewers tamper evidence without needing extra infrastructure.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

GENESIS = "0" * 64


def _hash(entry: dict) -> str:
    return hashlib.sha256(json.dumps(entry, sort_keys=True, default=str).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._last_hash = self._read_last_hash()

    def _read_last_hash(self) -> str:
        if not self.path.exists():
            return GENESIS
        last = GENESIS
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last = json.loads(line)["hash"]
        return last

    def record(self, actor: str, action: str, **details) -> dict:
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "actor": actor,  # e.g. "agent:analyst", "human:jdoe", "system:detector"
            "action": action,  # e.g. "tool_call", "draft_report", "approve", "reject"
            "details": details,
            "prev_hash": self._last_hash,
        }
        entry["hash"] = _hash(entry)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        self._last_hash = entry["hash"]
        return entry


def verify_chain(path: str | Path) -> tuple[bool, int | None]:
    """Return (ok, first_bad_line_number)."""
    prev = GENESIS
    with Path(path).open(encoding="utf-8") as f:
        for n, line in enumerate(f, start=1):
            if not line.strip():
                continue
            entry = json.loads(line)
            claimed = entry.pop("hash")
            if entry["prev_hash"] != prev or _hash(entry) != claimed:
                return False, n
            prev = claimed
    return True, None
