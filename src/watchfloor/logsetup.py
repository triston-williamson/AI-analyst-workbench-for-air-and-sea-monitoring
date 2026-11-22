"""Logging for the long-running processes.

Each process writes its own ``logs/<name>.log`` through a FileHandler, which flushes every
record. (Relying on redirected stderr lost lines when the processes ran hidden in the
background on Windows: the detector logged its first pass and then nothing, although Snowflake
showed every pass running.)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"
NOISY = {
    "snowflake": logging.ERROR,
    "snowflake.connector": logging.ERROR,
    "snowflake.connector._core": logging.ERROR,
    "kafka": logging.WARNING,
    "httpx": logging.WARNING,
    "urllib3": logging.WARNING,
    "werkzeug": logging.WARNING,
}


def configure(name: str, log_dir: str | Path = "logs", level: int = logging.INFO) -> Path:
    path = Path(log_dir) / f"{name}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=level,
        format=FORMAT,
        handlers=[logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler(sys.stderr)],
        force=True,
    )
    for logger, lvl in NOISY.items():
        logging.getLogger(logger).setLevel(lvl)
    return path
