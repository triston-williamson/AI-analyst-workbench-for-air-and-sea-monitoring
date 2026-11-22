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


def test_two_writers_on_one_file_keep_the_chain_intact(tmp_path):
    """Regression: each process cached the last hash, so a second writer forked the chain
    (found live: detector loop + a manual run + the console all append to one file)."""
    path = tmp_path / "audit.jsonl"
    detector, console = AuditLog(path), AuditLog(path)
    for i in range(5):
        detector.record("system:detector", "alert_created", alert_id=f"d{i}")
        console.record("human:analyst", "approve", alert_id=f"d{i}")
    assert verify_chain(path) == (True, None)


def test_concurrent_threads_keep_the_chain_intact(tmp_path):
    import threading

    path = tmp_path / "audit.jsonl"

    def write(name):
        log = AuditLog(path)
        for i in range(25):
            log.record(name, "tick", i=i)

    threads = [threading.Thread(target=write, args=(f"w{k}",)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(path.read_text().splitlines()) == 100
    assert verify_chain(path) == (True, None)


def test_rotate_archives_unchanged_and_links_new_chain(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record("system:detector", "alert_created", alert_id="a1")
    before = path.read_text()
    last = json.loads(before.splitlines()[-1])["hash"]
    archived = log.rotate("test", break_at_line=1)
    assert archived.read_text() == before  # archived byte-for-byte
    first = json.loads(path.read_text().splitlines()[0])
    assert first["action"] == "chain_rotated"
    assert first["details"]["previous_last_hash"] == last
    assert first["details"]["previous_file"] == archived.name
    assert verify_chain(path) == (True, None)


def test_process_logs_reach_their_file_immediately(tmp_path):
    """Regression: hidden background processes on Windows lost log lines written to
    redirected stderr; each process now logs to its own file, flushed per record."""
    import logging

    from watchfloor.logsetup import configure

    path = configure("unit_test_process", log_dir=tmp_path)
    logging.getLogger("detect").info("pass at 0600Z")
    assert "pass at 0600Z" in path.read_text(encoding="utf-8")
    logging.basicConfig(force=True, handlers=[logging.NullHandler()])
