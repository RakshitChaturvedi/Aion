"""
    Automated End-to-End & Unit test suite for aion
    Covers:
        1. Phase 3 QNX 8.0 trace verification matrix (semaphore, cpu_hog, ipc_msgsend, negative_cases)
        2. Regression comparision (`compare_runs` / `aion diff`)
        3. TOML config loader validation and margin multiplier calculations
        4. CLI exit codes (0=PASS, 1=FAIL, 2=CONFIG/USAGE ERROR, 3=PARSE ERROR)
"""

from pathlib import Path
import pytest
import json

from aion.analyzers import ConfigError, analyze_session, compare_runs, load_config
from aion.cli import main as cli_main
from aion.ingest import parse_traceprinter_file
from aion.model import Confidence, FindingType

ROOT_DIR = Path(__file__).resolve().parent.parent
SAMPLES_DIR = ROOT_DIR/"samples"
TRACES_DIR = ROOT_DIR/"traces"

# --- 1. E-to-E trace verification matrix ---

def test_semaphore_demo_fails_with_confirmed_inversion():
    config = load_config(str(SAMPLES_DIR/"semaphore_rules.toml"))
    session = parse_traceprinter_file(str(TRACES_DIR/"semaphore_demo.txt"))
    findings = analyze_session(session, config)

    assert len(findings) == 2
    by_type = {f.finding_type: f for f in findings}
    assert FindingType.DEADLINE_MISS in by_type
    assert FindingType.PRIORITY_INVERSION_SEM in by_type

    sem_f = by_type[FindingType.PRIORITY_INVERSION_SEM]
    assert sem_f.confidence == Confidence.CONFIRMED
    assert "diag_low" in sem_f.summary
    assert "crunch_med" in sem_f.summary

    # verify step 1 links control high -> diag low
    assert sem_f.causal_chain[0].relation == "BLOCKED_ON_SEMAPHORE"
    assert "diag_low" in sem_f.causal_chain[0].culprit_thread

    # verify dynamic priority attribution on proc_pool_thread
    proc_edges = [e for e in sem_f.causal_chain if "proc_pool_thread" in e.culprit_thread]
    assert proc_edges and "prio:25" in proc_edges[0].culprit_thread

def test_semaphore_fixed_passes():
    config = load_config(str(SAMPLES_DIR/"semaphore_rules.toml"))
    session = parse_traceprinter_file(str(TRACES_DIR/"semaphore_fixed.txt"))
    findings = analyze_session(session, config)
    assert findings == []

def test_cpu_hog_demo_fails_with_sched_latency_miss():
    config = load_config(str(SAMPLES_DIR/"cpu_hog_rules.toml"))
    session = parse_traceprinter_file(str(TRACES_DIR/"cpu_hog_demo.txt"))
    findings = analyze_session(session, config)

    assert len(findings) == 1
    f = findings[0]
    assert f.finding_type == FindingType.SCHED_LATENCY_MISS
    assert f.confidence == Confidence.CONFIRMED
    assert f.duration_ms > 15.0

    # verify consecutive slices for same thread were deduplicated into single edge
    culprits = [edge.culprit_thread for edge in f.causal_chain]
    assert len(culprits) == len(set(culprits)), "Duplicate consecutive culprit edges should be merged"
    assert any("telemetry_hog" in c and "prio:45" in c for c in culprits)
    assert any("proc_pool_thread" in c and "prio:45" in c for c in culprits)

def test_cpu_hog_fixed_passes():
    config = load_config(str(SAMPLES_DIR / "cpu_hog_rules.toml"))
    session = parse_traceprinter_file(str(TRACES_DIR / "cpu_hog_fixed.txt"))
    findings = analyze_session(session, config)
    assert findings == []

def test_ipc_msgsend_demo_fails_with_three_findings():
    config = load_config(str(SAMPLES_DIR / "ipc_rules.toml"))
    session = parse_traceprinter_file(str(TRACES_DIR / "ipc_msgsend_demo.txt"))
    findings = analyze_session(session, config)

    assert len(findings) == 3
    types = {f.finding_type for f in findings}
    assert types == {
        FindingType.DEADLINE_MISS,
        FindingType.PRIORITY_INVERSION_IPC,
        FindingType.FORBIDDEN_BLOCKING_IPC,
    }
    for f in findings:
        assert f.confidence == Confidence.CONFIRMED

def test_ipc_msgsend_fixed_passes():
    config = load_config(str(SAMPLES_DIR / "ipc_rules.toml"))
    session = parse_traceprinter_file(str(TRACES_DIR / "ipc_msgsend_fixed.txt"))
    findings = analyze_session(session, config)
    assert findings == []

def test_negative_cases_zero_false_positives():
    """Legitimate top-down semaphore signalling (prio 45 -> prio 30) must produce 0 findings."""
    config = load_config(str(SAMPLES_DIR / "negative_rules.toml"))
    session = parse_traceprinter_file(str(TRACES_DIR / "negative_cases.txt"))
    findings = analyze_session(session, config)
    assert findings == []

# --- 2. Regression comparision ---

def test_compare_runs_detects_ipc_regression():
    base = parse_traceprinter_file(str(TRACES_DIR/"ipc_msgsend_fixed.txt"))
    cand = parse_traceprinter_file(str(TRACES_DIR/"ipc_msgsend_demo.txt"))

    report = compare_runs(base, cand, "sensor_high")
    assert report.is_regression is True
    assert report.severity == "CRITICAL"
    assert report.ratio > 20.0
    assert "ipc:diag_server" in report.primary_new_blocker

def test_compare_runs_no_regression_when_identical():
    base = parse_traceprinter_file(str(TRACES_DIR/"ipc_msgsend_fixed.txt"))
    report = compare_runs(base, base, "sensor_high")
    assert report.is_regression is False
    assert report.severity == "NONE"

# --- 3. Config loader and ingest unit tests ---

def test_unswap_qnx_usrstr(tmp_path: Path):
    trace_file = tmp_path/"swapped_marker.txt"
    trace_file.write_text(
        'TRACE_CYCLES_PER_SEC:: 2500000000\n'
        't:0x1000 CPU:00 USREVENT :EVENT:100 STR:"tg_job_begin:sesnro_high"\n'
    )
    session = parse_traceprinter_file(str(trace_file))
    assert len(session.markers) == 1
    assert session.markers[0].payload == "tg_job_begin:sensor_high"


def test_config_margin_multiplier(tmp_path: Path):
    qemu_toml = tmp_path / "qemu.toml"
    qemu_toml.write_text('[settings]\ntarget_env = "qemu"\nmargin_pct = 15\n')
    cfg_qemu = load_config(str(qemu_toml))
    assert cfg_qemu.effective_margin_multiplier == pytest.approx(1.15)

    hw_toml = tmp_path / "hw.toml"
    hw_toml.write_text('[settings]\ntarget_env = "hardware"\nmargin_pct = 15\n')
    cfg_hw = load_config(str(hw_toml))
    assert cfg_hw.effective_margin_multiplier == pytest.approx(1.0)


def test_config_missing_required_rule_fields_raises(tmp_path: Path):
    bad_toml = tmp_path / "bad.toml"
    bad_toml.write_text('[[timing_rule]]\nname = "missing_thread_field"\n')
    with pytest.raises(ConfigError):
        load_config(str(bad_toml))

# --- 4. CLI exit code tests (0=PASS, 1=FAIL, 2=CONFIG, 3=PARSE)

def test_cli_exit_code_0_on_pass():
    rc = cli_main(["check", "--config", str(SAMPLES_DIR/"negative_rules.toml"), str(TRACES_DIR/"negative_cases.txt")])
    assert rc == 0

def test_cli_exit_code_1_on_fail():
    rc = cli_main(["check", "--config", str(SAMPLES_DIR/"semaphore_rules.toml"), str(TRACES_DIR/"semaphore_demo.txt")])
    assert rc == 1

def test_cli_exit_code_2_on_missing_config():
    rc = cli_main(["check", "--config", str(SAMPLES_DIR/"nonexistent.toml"), str(TRACES_DIR/"negative_cases.txt")])
    assert rc == 2

def test_cli_exit_code_3_on_missing_trace():
    rc = cli_main(["check", "--config", str(SAMPLES_DIR/"negative_rules.toml"), str(TRACES_DIR/"nonexistent.txt")])
    assert rc == 3

def test_cli_json_format_output(tmp_path: Path):
    out_file=tmp_path/"report.json"
    rc = cli_main(["check", "--config", str(SAMPLES_DIR/"semaphore_rules.toml"), "--format", 
                   "json", "--output", str(out_file), str(TRACES_DIR/"semaphore_demo.txt")])
    assert rc ==1
    data =json.loads(out_file.read_text(encoding="utf-8"))
    assert data["verdict"] == "FAIL"
    assert data["findings_count"] == 2
    assert data["findings"][1]["finding_type"] == "priority_inversion_semaphore"
    assert data["findings"][1]["confidence"] == "confirmed"

# --- 5. MISC ---

def test_max_lateness_ms_rule_triggers_lateness_miss(tmp_path: Path):
    """Verify TimingRule.max_lateness_ms emits FindingType.LATENESS_MISS when start_lateness_ms exceeds limit."""
    trace_file = tmp_path / "lateness.txt"
    trace_file.write_text(
        "TRACE_CYCLES_PER_SEC:: 1000000\n"
        "TRACE_CPU_NUM:: 1\n"
        "t:0x0001 CPU:00 PROCESS :PROCCREATE_NAME\n"
        " pid:100\n"
        " name:app\n"
        "t:0x0002 CPU:00 PROCESS :PROCTHREAD_NAME\n"
        " pid:100\n"
        " tid:2\n"
        " name:late_worker\n"
        "t:0x03e8 CPU:00 THREAD  :THREADY pid:100 tid:2 priority:30 policy:2\n"
        "t:0x1770 CPU:00 THREAD  :THRUNNING pid:100 tid:2 priority:30 policy:2\n"
        "t:0x1b58 CPU:00 THREAD  :THNANOSLEEP pid:100 tid:2 priority:30 policy:2\n"
        "t:0x1f40 CPU:00 THREAD  :THDEAD pid:100 tid:2 priority:30 policy:2\n"
    )
    rule_file = tmp_path / "lateness_rules.toml"
    rule_file.write_text(
        '[settings]\ntarget_env = "hardware"\n'
        '[logical_checks]\npriority_inversion = false\nsemaphore_locks = false\n'
        '[[timing_rule]]\nname = "strict start"\nthread = "late_worker"\nmax_lateness_ms = 2.0\n'
    )
    cfg = load_config(str(rule_file))
    sess = parse_traceprinter_file(str(trace_file))
    findings = analyze_session(sess, cfg)

    assert len(findings) == 1
    assert findings[0].finding_type == FindingType.LATENESS_MISS
    assert findings[0].duration_ms == pytest.approx(5.0, abs=0.05)

def test_cli_html_format_output(tmp_path: Path):
    out_file = tmp_path / "report.html"
    rc = cli_main([
        "check",
        "--config", str(SAMPLES_DIR / "semaphore_rules.toml"),
        "--format", "html",
        "--output", str(out_file),
        str(TRACES_DIR / "semaphore_demo.txt"),
    ])
    assert rc == 1
    html_text = out_file.read_text(encoding="utf-8")
    assert "VERDICT: FAIL" in html_text
    assert "PRIORITY_INVERSION_SEMAPHORE" in html_text
    assert "Why Aion is Not \"Just Another Profiler\":" in html_text

def test_cli_exit_code_4_on_inconclusive_warn_policy(tmp_path: Path):
    """When a target thread in [[timing_rule]] is missing from the trace, exit code is 4 under warn policy."""
    cfg_file = tmp_path / "inc_warn.toml"
    cfg_file.write_text(
        '[settings]\ntarget_env = "qemu"\ninconclusive_policy = "warn"\n'
        '[logical_checks]\npriority_inversion = false\nsemaphore_locks = false\n'
        '[[timing_rule]]\nname = "ghost rule"\nthread = "nonexistent_thread_xyz"\ndeadline_ms = 5.0\n'
    )
    rc = cli_main([
        "check",
        "--config", str(cfg_file),
        str(TRACES_DIR / "negative_cases.txt"),
    ])
    assert rc == 4


def test_cli_exit_code_1_on_inconclusive_fail_policy(tmp_path: Path):
    """When inconclusive_policy = 'fail', an inconclusive trace returns exit code 1."""
    cfg_file = tmp_path / "inc_fail.toml"
    cfg_file.write_text(
        '[settings]\ntarget_env = "qemu"\ninconclusive_policy = "fail"\n'
        '[logical_checks]\npriority_inversion = false\nsemaphore_locks = false\n'
        '[[timing_rule]]\nname = "ghost rule"\nthread = "nonexistent_thread_xyz"\ndeadline_ms = 5.0\n'
    )
    rc = cli_main([
        "check",
        "--config", str(cfg_file),
        str(TRACES_DIR / "negative_cases.txt"),
    ])
    assert rc == 1