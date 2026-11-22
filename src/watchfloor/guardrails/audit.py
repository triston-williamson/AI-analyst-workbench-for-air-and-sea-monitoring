"""Append-only, hash-chained audit log, safe for several writer processes.

Every agent action (tool call, draft, human decision) is written as one JSON line whose hash
covers the previous line's hash. Editing or deleting any earlier entry breaks the chain, which
``verify_chain`` detects. This gives reviewers tamper evidence without extra infrastructure.

Concurrency: the detector, the console and ad-hoc maintenance runs all append to the same file.
Each write therefore takes an OS file lock and re-reads the current last entry before linking
to it. (An earlier version cached the last hash in memory per process; two processes writing
in turn forked the chain. ``verify_chain`` caught it, which is how the bug was found.)

Rotation: a chain that is found broken is never "repaired" in place. ``rotate`` archives the
file untouched and starts a new chain whose first entry records the archived file, its last
hash, and the reason.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

GENESIS = "0" * 64


def _hash(entry: dict) -> str:
    return hashlib.sha256(json.dumps(entry, sort_keys=True, default=str).encode()).hexdigest()


@contextlib.contextmanager
def _locked(path: Path, timeout_s: float = 10.0):
    """Exclusive inter-process lock on ``<path>.lock`` (Windows and POSIX)."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT)
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"could not lock {lock_path}") from None
                time.sleep(0.01)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _last_hash(path: Path) -> str:
    """Hash of the last entry, read from the end of the file (no full scan)."""
    if not path.exists() or path.stat().st_size == 0:
        return GENESIS
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        pos, chunk, buf = f.tell(), 4096, b""
        while pos > 0:
            step = min(chunk, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            lines = [ln for ln in buf.splitlines() if ln.strip()]
            if len(lines) >= 2 or (pos == 0 and lines):
                return json.loads(lines[-1])["hash"]
    return GENESIS


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, actor: str, action: str, **details) -> dict:
        with _locked(self.path):
            entry = {
                "ts": datetime.now(UTC).isoformat(),
                "actor": actor,  # e.g. "agent:analyst", "human:jdoe", "system:detector"
                "action": action,  # e.g. "tool_call", "draft_report", "approve", "reject"
                "details": details,
                "prev_hash": _last_hash(self.path),
            }
            entry["hash"] = _hash(entry)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        return entry

    def rotate(self, reason: str, **details) -> Path:
        """Archive the current file unchanged and start a new chain that points back to it."""
        with _locked(self.path):
            last = _last_hash(self.path)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            archived = self.path.with_name(f"{self.path.stem}-{stamp}{self.path.suffix}")
            if self.path.exists():
                self.path.rename(archived)
        self.record(
            "system:maintenance",
            "chain_rotated",
            previous_file=archived.name,
            previous_last_hash=last,
            reason=reason,
            **details,
        )
        return archived


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
