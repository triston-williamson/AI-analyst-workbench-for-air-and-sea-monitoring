"""Run the analyst-report eval.

    python evals/analyst/run_eval.py --approve-harness            # once, by the analyst
    python evals/analyst/run_eval.py --cases loiter-09,rendezvous-06 --reps 1   # pilot
    python evals/analyst/run_eval.py --reps 2                     # full baseline
    python evals/analyst/run_eval.py --variant v1 --reps 2        # after a change

Calls the real entry point (watchfloor.agent.analyst.investigate) on frozen alert snapshots,
with the real read-only tools against Snowflake (RAW tables are append-only, so a frozen window
returns the same records), and writes the hillclimb layout:

    .claude/hillclimb/analyst-report/<variant>/results.jsonl   one row per (case, rep)
    .claude/hillclimb/analyst-report/<variant>/traces/<id>_rep<k>.json
    .claude/hillclimb/analyst-report/<variant>/errors.jsonl    attempts with no scorable output
    .claude/hillclimb/analyst-report/<variant>/audit.jsonl     the agent's audit log (not prod's)

Properties: rows written as each case finishes; resume skips (case, rep) already in
results.jsonl; hard per-case wall-clock ceiling; failures classed into errors.jsonl, never
scored as zeros; served model asserted; harness gate (sha of the eval and agent code) that only
the analyst can approve.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import math
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import graders  # noqa: E402

FLOW = ROOT / ".claude" / "hillclimb" / "analyst-report"
CASES = Path(__file__).parent / "cases.jsonl"
MODEL = "claude-opus-5-5"
HARNESS_PATHS = [
    "evals/analyst/run_eval.py",
    "evals/analyst/graders.py",
    "evals/analyst/cases.jsonl",
]
METRICS = [
    {"id": "disposition", "label": "Right call", "kind": "binary"},
    {"id": "citation_support", "label": "Cites support", "kind": "float", "scale": 1},
    {"id": "grounding", "label": "Grounding", "kind": "binary"},
    {"id": "format", "label": "Format", "kind": "binary"},
]
PERF = [
    {"id": "latency_s", "label": "Latency", "unit": "s"},
    {"id": "tool_calls", "label": "Tool calls"},
    {"id": "in_tokens", "label": "In tokens"},
    {"id": "out_tokens", "label": "Out tokens"},
    {"id": "cost_usd", "label": "Cost", "unit": "$"},
]
# $ per million tokens (first-party): input, output. Cache read 0.1x, write 1.25x input.
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0)}


def cost(model: str, u: dict) -> float:
    pin, pout = PRICES.get(model, (0.0, 0.0))
    return (
        u.get("input_tokens", 0) * pin
        + u.get("output_tokens", 0) * pout
        + u.get("cache_read_input_tokens", 0) * pin * 0.1
        + u.get("cache_creation_input_tokens", 0) * pin * 1.25
    ) / 1e6


def harness_sha() -> str:
    h = hashlib.sha256()
    for rel in HARNESS_PATHS + [
        "src/watchfloor/agent/analyst.py",
        "src/watchfloor/agent/tools.py",
        "src/watchfloor/guardrails/grounding.py",
    ]:
        h.update(rel.encode())
        h.update((ROOT / rel).read_bytes())
    return h.hexdigest()


def state_path() -> Path:
    return FLOW / "_state.json"


def write_state(approved_sha: str | None = None) -> None:
    FLOW.mkdir(parents=True, exist_ok=True)
    p = state_path()
    st = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    st.update(
        {
            "schema": "hillclimb/v2",
            "metrics": METRICS,
            "perf_fields": PERF,
            "harness_paths": HARNESS_PATHS,
            "prices": {m: {"in": a, "out": b} for m, (a, b) in PRICES.items()},
        }
    )
    if approved_sha:
        st["approved_harness_sha"] = approved_sha
    p.write_text(json.dumps(st, indent=1), encoding="utf-8")


# ------------------------------------------------------------------ traces
def _get(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def to_trace(system: str, history: list) -> list[dict]:
    turns = [{"role": "system", "content": system}]
    for msg in history:
        role, content = _get(msg, "role"), _get(msg, "content")
        if isinstance(content, str):
            turns.append({"role": role, "content": content})
            continue
        thinking = None
        for block in content or []:
            t = _get(block, "type")
            if t == "thinking":
                thinking = (_get(block, "thinking") or "").strip() or None
            elif t == "text":
                turns.append(
                    {
                        "role": role,
                        "content": _get(block, "text"),
                        **({"thinking": thinking} if thinking and role == "assistant" else {}),
                    }
                )
                thinking = None
            elif t == "tool_use":
                turns.append(
                    {
                        "role": "tool_call",
                        "name": _get(block, "name"),
                        "content": json.dumps(_get(block, "input"), indent=1),
                        **({"thinking": thinking} if thinking else {}),
                    }
                )
                thinking = None
            elif t == "tool_result":
                c = _get(block, "content")
                if isinstance(c, list):
                    c = "\n".join(_get(x, "text", "") for x in c)
                turns.append({"role": "tool_result", "content": str(c)})
    return turns


# ------------------------------------------------------------------ one case
def run_case(case: dict, rep: int, variant_dir: Path, client, timeout_s: float) -> dict:
    from watchfloor.agent import analyst
    from watchfloor.dashboard import data
    from watchfloor.guardrails.audit import AuditLog

    audit = AuditLog(variant_dir / "audit.jsonl")
    t0 = time.monotonic()
    draft = analyst.investigate(case["alert"], client, data.run_query, audit)
    latency = time.monotonic() - t0
    if latency > timeout_s:
        raise TimeoutError(f"case took {latency:.0f}s > ceiling {timeout_s:.0f}s")
    if draft.model != MODEL:
        raise RuntimeError(f"served model {draft.model!r} != requested {MODEL!r}")
    if draft.refused:
        status = "refused"
    elif draft.incomplete or not draft.markdown:
        status = "incomplete"
    else:
        status = "ok"

    report = draft.markdown
    got = graders.disposition(report)
    judge = graders.judge_citations(client, report, draft.history) if status == "ok" else None
    grade = {
        "disposition": float(got == case["label"]),
        "citation_support": judge["score"] if judge else 0.0,
        "grounding": float(draft.grounding.passed),
        "format": float(graders.format_ok(report)),
    }
    explanation = {
        "disposition": f"report: {got or 'unparsed'}; label: {case['label']}",
        "citation_support": "; ".join(
            f"#{v['index']} {v['verdict']}: {v['reason']}"
            for v in (judge or {}).get("verdicts", [])
            if v["verdict"] != "yes"
        )
        or "all bullets supported",
        "grounding": f"score {draft.grounding.grounding_score}; uncited "
        f"{len(draft.grounding.uncited_sentences)}; unknown "
        f"{draft.grounding.unknown_citations}",
    }
    trace = to_trace(analyst.SYSTEM_PROMPT, draft.history)
    (variant_dir / "traces").mkdir(exist_ok=True)
    (variant_dir / "traces" / f"{case['id']}_rep{rep}.json").write_text(
        json.dumps(trace, indent=1, default=str), encoding="utf-8"
    )
    usage = draft.usage
    row = {
        "prompt_id": case["id"],
        "rep": rep,
        "prompt": analyst.brief(case["alert"]),
        "tags": case["tags"],
        "status": status,
        "stop_reason": draft.stop_reason,
        "grade": grade,
        "explanation": explanation,
        "model": draft.model,
        "usage": usage,
        "judge_model": judge["model"] if judge else None,
        "judge_usage": judge["usage"] if judge else {},
        "latency_s": round(latency, 1),
        "tool_calls": draft.tool_calls,
        "in_tokens": usage["input_tokens"]
        + usage["cache_read_input_tokens"]
        + usage["cache_creation_input_tokens"],
        "out_tokens": usage["output_tokens"],
        "cost_usd": round(
            cost(draft.model, usage) + (cost(judge["model"], judge["usage"]) if judge else 0.0), 4
        ),
        "meta": {
            "label": case["label"],
            "reported": got,
            "attempts": draft.attempts,
            "injection_ok": graders.injection_ok(case, report),
            "report": report,
        },
    }
    return row


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--variant", default="baseline")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--cases", help="comma-separated case ids (default: all)")
    ap.add_argument("--timeout-s", type=float, default=300)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument(
        "--approve-harness",
        action="store_true",
        help="record the current harness as approved (the analyst runs this)",
    )
    args = ap.parse_args()

    if args.approve_harness:
        write_state(harness_sha())
        print(f"harness approved: {harness_sha()[:12]}")
        return
    write_state()
    st = json.loads(state_path().read_text(encoding="utf-8"))
    if st.get("approved_harness_sha") != harness_sha():
        print(
            "Harness changed or not yet approved. Review the eval code, then run:\n"
            "  python evals/analyst/run_eval.py --approve-harness",
            file=sys.stderr,
        )
        sys.exit(2)

    from watchfloor.agent.analyst import make_client
    from watchfloor.logsetup import configure

    configure("eval")
    sys.stdout.reconfigure(encoding="utf-8")
    cases = [json.loads(line) for line in CASES.open(encoding="utf-8")]
    if args.cases:
        want = set(args.cases.split(","))
        cases = [c for c in cases if c["id"] in want]
    vdir = FLOW / args.variant
    vdir.mkdir(parents=True, exist_ok=True)
    results, errors = vdir / "results.jsonl", vdir / "errors.jsonl"
    done = set()
    if results.exists():
        done = {(r["prompt_id"], r["rep"]) for r in map(json.loads, results.open(encoding="utf-8"))}
    todo = [(c, k) for c in cases for k in range(args.reps) if (c["id"], k) not in done]
    print(f"{len(todo)} attempts to run ({len(done)} already done) on {MODEL}")

    client = make_client()
    started = time.monotonic()
    with cf.ThreadPoolExecutor(args.concurrency) as pool:
        futs = {pool.submit(run_case, c, k, vdir, client, args.timeout_s): (c, k) for c, k in todo}
        for fut in cf.as_completed(futs):
            c, k = futs[fut]
            try:
                row = fut.result(timeout=args.timeout_s + 60)
            except Exception as exc:  # noqa: BLE001 - classed into errors.jsonl, never a zero
                cls = (
                    "timeout"
                    if isinstance(exc, (TimeoutError, cf.TimeoutError))
                    else "served_model_mismatch"
                    if "served model" in str(exc)
                    else "harness_or_serving_error"
                )
                with errors.open("a", encoding="utf-8") as f:
                    f.write(
                        json.dumps(
                            {
                                "prompt_id": c["id"],
                                "rep": k,
                                "class": cls,
                                "error": str(exc)[:500],
                                "at": datetime.now(UTC).isoformat(),
                            }
                        )
                        + "\n"
                    )
                print(f"  ERROR {c['id']} rep{k}: {cls}: {str(exc)[:120]}")
                continue
            with results.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, default=str) + "\n")
            g = row["grade"]
            print(
                f"  {c['id']:<15} rep{k} {row['status']:<9} call={row['meta']['reported']}/"
                f"{c['label']} cites={g['citation_support']:.2f} ground={g['grounding']:.0f} "
                f"fmt={g['format']:.0f} {row['latency_s']}s ${row['cost_usd']:.3f}"
            )
    print(f"wall clock {time.monotonic() - started:.0f}s")
    summarize(vdir)


def summarize(vdir: Path) -> None:
    rows = [json.loads(line) for line in (vdir / "results.jsonl").open(encoding="utf-8")]
    ok = [r for r in rows if r["status"] == "ok"]
    n = len(ok)
    if not n:
        print("no scorable rows")
        return
    print(f"\n{vdir.name}: {n} scored rows ({len(rows) - n} not ok)")
    for m in METRICS:
        vals = [r["grade"][m["id"]] for r in ok]
        mean = sum(vals) / n
        half = (
            1.96 * math.sqrt(max(mean * (1 - mean), 1e-9) / n)
            if m["kind"] == "binary"
            else (1.96 * (sum((v - mean) ** 2 for v in vals) / max(n - 1, 1)) ** 0.5 / math.sqrt(n))
        )
        print(f"  {m['label']:<14} {mean:6.1%}  ±{half:.1%}")
    for label in ("dismiss", "monitor", "escalate"):
        sub = [r for r in ok if r["meta"]["label"] == label]
        if sub:
            acc = sum(r["grade"]["disposition"] for r in sub) / len(sub)
            print(f"  right call when label={label:<8} {acc:6.1%} (n={len(sub)})")
    inj = [r for r in ok if r["meta"]["injection_ok"] is not None]
    if inj:
        print(
            f"  injection resisted        {sum(r['meta']['injection_ok'] for r in inj)}/{len(inj)}"
        )
    print(
        f"  cost ${sum(r['cost_usd'] for r in rows):.2f} total, "
        f"${sum(r['cost_usd'] for r in rows) / len(rows):.3f}/attempt; "
        f"median latency {sorted(r['latency_s'] for r in rows)[len(rows) // 2]}s"
    )


if __name__ == "__main__":
    main()
