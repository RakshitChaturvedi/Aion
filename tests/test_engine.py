import os
import pytest
from aion import (
    FindingType,
    analyze_session,
    compare_runs,
    load_config,
    parse_traceprinter_file,
)
from aion.cli import main


def test_parse_and_detect_inversion_causal_graph():
    session = parse_traceprinter_file("traces/inversion_broken.txt")
    config = load_config("samples/inversion_rules.toml")
    findings = analyze_session(session, config)

    inv_findings = [f for f in findings if f.finding_type == FindingType.PRIORITY_INVERSION_MUTEX]
    assert len(inv_findings) == 1
    f0 = inv_findings[0]
    assert "sensor_high" in f0.victim_thread
    assert len(f0.causal_chain) >= 2
    assert f0.causal_chain[0].relation == "BLOCKED_ON_MUTEX"
    assert f0.causal_chain[1].relation == "PREEMPTED_BY_MEDIUM_THREAD"
    assert "worker_med" in f0.causal_chain[1].culprit_thread


def test_truncated_and_unmarked_trace_resilience(tmp_path):
    # Read first 400 lines of real trace + 1 malformed line to test truncated/unmarked resilience
    with open("traces/inversion_broken.txt", "r") as f:
        head_lines = [next(f) for _ in range(400)]
    head_lines.append("t:0xGGGGGG malformed line that should not crash\n")

    trunc_file = tmp_path / "truncated.txt"
    trunc_file.write_text("".join(head_lines))

    session = parse_traceprinter_file(str(trunc_file))
    assert session.malformed_lines == 1
    assert len(session.threads) > 0


@pytest.mark.skipif(not os.path.exists("traces/inversion_fixed.txt"), reason="Fixed trace not captured yet")
def test_broken_vs_fixed_demo_gate():
    # Gate 1: Broken trace exits 1
    assert main(["check", "--config", "samples/inversion_rules.toml", "traces/inversion_broken.txt"]) == 1
    # Gate 2: Fixed trace exits 0
    assert main(["check", "--config", "samples/inversion_rules.toml", "traces/inversion_fixed.txt"]) == 0
    # Gate 3: Regression comparator attributes slowdown to mutex:logger_low
    base_s = parse_traceprinter_file("traces/inversion_fixed.txt")
    cand_s = parse_traceprinter_file("traces/inversion_broken.txt")
    rep = compare_runs(base_s, cand_s, "sensor_high")
    assert rep.is_regression is True
    assert "logger_low" in rep.primary_new_blocker


def test_cli_exit_codes(tmp_path):
    assert main(["check", "--config", "samples/inversion_rules.toml", "traces/inversion_broken.txt"]) == 1
    assert main(["check", "--config", str(tmp_path / "nonexistent.toml")]) == 2
