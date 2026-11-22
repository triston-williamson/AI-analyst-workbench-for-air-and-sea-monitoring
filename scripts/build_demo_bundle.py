"""Assemble the public demo's deployment bundle in build/demo/.

    python scripts/export_demo_snapshot.py --start ... --end ...   # first, once
    python scripts/build_demo_bundle.py

The folder holds the Dockerfile, render.yaml, a README, the package source and the snapshot,
and nothing else: no .env, logs, keys or git history. Push it to its own GitHub repository
and deploy that on Render, or build it with Docker anywhere (see docs/DEMO.md).
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_FILES = (
    "meta.json",
    "audit.jsonl",
    "vessel_positions.parquet",
    "aircraft_positions.parquet",
    "vessel_identity.parquet",
    "alerts.parquet",
    "reports.parquet",
    "evidence.parquet",
)
DEPLOY_FILES = ("Dockerfile", "render.yaml", "README.md")


def build(snapshot: Path, out: Path) -> list[Path]:
    """Recreate ``out`` with everything the container needs. Keeps a .git folder if present,
    so the bundle can be its own repository and simply be re-committed after a rebuild."""
    missing = [f for f in SNAPSHOT_FILES if not (snapshot / f).exists()]
    if missing:
        raise SystemExit(f"Snapshot incomplete ({', '.join(missing)}); run export_demo_snapshot.py")
    out.mkdir(parents=True, exist_ok=True)
    for item in out.iterdir():
        if item.name == ".git":
            continue
        shutil.rmtree(item) if item.is_dir() else item.unlink()

    for f in DEPLOY_FILES:
        shutil.copy(ROOT / "deploy" / "demo" / f, out / f)
    shutil.copy(ROOT / "pyproject.toml", out / "pyproject.toml")
    shutil.copytree(
        ROOT / "src",
        out / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
    )
    (out / "demo_snapshot").mkdir()
    for f in SNAPSHOT_FILES:
        shutil.copy(snapshot / f, out / "demo_snapshot" / f)
    return [p for p in out.rglob("*") if p.is_file() and ".git" not in p.relative_to(out).parts]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--snapshot", default=str(ROOT / "demo_snapshot"))
    ap.add_argument("--out", default=str(ROOT / "build" / "demo"))
    args = ap.parse_args()
    files = build(Path(args.snapshot), Path(args.out))
    size = sum(p.stat().st_size for p in files) / 1e6
    print(f"{len(files)} files, {size:.1f} MB -> {args.out}")


if __name__ == "__main__":
    main()
