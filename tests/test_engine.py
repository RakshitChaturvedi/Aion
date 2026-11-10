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
    session = parse_traceprinter_file("traces/inversion_spike_clean.txt")
    config = load_config("samples/inversion_rules.toml")
    findings = analyze_session(session, config)

    inv_findings = [f for f in findings if f.finding_type == FindingType.PRIORITY_INVERSION_MUTEX]
    assert len(inv_findings) >= 1
    f0 = inv_findings[0]
    assert "sensor_high" in f0.victim_thread
    assert len(f0.causal_chain) >= 1
    assert f0.causal_chain[0].relation == "BLOCKED_ON_MUTEX"
    assert len(f0.causal_chain[0].evidence) >= 1
    assert f0.causal_chain[0].evidence[0].line_no > 0


def test_cli_exit_codes(tmp_path):
    # Broken trace must exit 1
    assert main(["check", "--config", "samples/inversion_rules.toml", "traces/inversion_spike_clean.txt"]) == 1

    # Bad config path must exit 2
    assert main(["check", "--config", str(tmp_path / "nonexistent.toml")]) == 2
