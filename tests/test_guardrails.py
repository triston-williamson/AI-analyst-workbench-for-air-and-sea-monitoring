import json

from watchfloor.guardrails.audit import AuditLog, verify_chain
from watchfloor.guardrails.grounding import check_grounding

A, B = "a" * 32, "b" * 32


def test_audit_chain_verifies_and_detects_tampering(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record("system:detector", "alert_created", alert_id="1")
    log.record("agent:analyst", "draft_report", alert_id="1")
    log.record("human:reviewer", "approve", alert_id="1")
    assert verify_chain(path) == (True, None)

    # Reopening continues the same chain.
    AuditLog(path).record("human:reviewer", "note", text="ok")
    assert verify_chain(path) == (True, None)

    lines = path.read_text().splitlines()
    entry = json.loads(lines[1])
    entry["details"]["alert_id"] = "999"
    lines[1] = json.dumps(entry)
    path.write_text("\n".join(lines) + "\n")
    assert verify_chain(path) == (False, 2)


def test_grounding_passes_when_all_claims_cited():
    report = (
        "## Summary\n"
        f"MMSI 273456789 stopped transmitting at 02:10Z [rec:{A}]. "
        f"It reappeared 48 nm east six hours later [rec:{B}].\n"
    )
    result = check_grounding(report, {A, B})
    assert result.grounding_score == 1.0 and result.passed


def test_grounding_flags_uncited_and_invented_citations():
    report = (
        f"The vessel went dark [rec:{A}]. "
        "It is probably carrying sanctioned oil. "
        f"It met another tanker [rec:{'c' * 32}].\n"
    )
    result = check_grounding(report, {A, B})
    assert result.grounding_score < 1.0
    assert result.uncited_sentences == ["It is probably carrying sanctioned oil."]
    assert result.unknown_citations == ["c" * 32]
    assert not result.passed
