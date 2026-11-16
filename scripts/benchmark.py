#!/usr/bin/env python3
"""
Phase 4 Task 9: Aion Ground-Truth Benchmark Harness (`scripts/benchmark.py`).
Evaluates Aion across all labelled QNX 8.0 kernel traces and computes:
  - Precision (%)
  - Recall (%)
  - False-Positive Rate (%)
  - Causal Attribution Accuracy (%)
  - Ingestion & Analysis Throughput (lines/sec & traces/sec)
  - Run-to-Run Deterministic Reproducibility (%)
"""
from dataclasses import dataclass
from pathlib import Path
import time
from typing import List, Set

from aion.analyzers import analyze_session, load_config
from aion.ingest import parse_traceprinter_file
from aion.model import FindingType

ROOT_DIR = Path(__file__).resolve().parent.parent
SAMPLES_DIR = ROOT_DIR / "samples"
TRACES_DIR = ROOT_DIR / "traces"


@dataclass(frozen=True)
class GroundTruthCase:
    name: str
    config_file: str
    trace_file: str
    expected_verdict: str  # "PASS" or "FAIL"
    expected_types: Set[FindingType]
    expected_culprits: List[str]  # substrings that MUST appear in causal chains


# Hand-written ground-truth labels for all committed QNX 8.0 traces
BENCHMARK_SUITE: List[GroundTruthCase] = [
    GroundTruthCase(
        name="Mutex Priority Inversion (Broken)",
        config_file="inversion_rules.toml",
        trace_file="inversion_broken.txt",
        expected_verdict="FAIL",
        expected_types={FindingType.DEADLINE_MISS, FindingType.PRIORITY_INVERSION_MUTEX},
        expected_culprits=["logger_low", "worker_med"],
    ),
    GroundTruthCase(
        name="Mutex Priority Inversion (Fixed w/ PTHREAD_PRIO_INHERIT)",
        config_file="inversion_rules.toml",
        trace_file="inversion_fixed.txt",
        expected_verdict="PASS",
        expected_types=set(),
        expected_culprits=[],
    ),
    GroundTruthCase(
        name="Semaphore Priority Inversion (Broken)",
        config_file="semaphore_rules.toml",
        trace_file="semaphore_demo.txt",
        expected_verdict="FAIL",
        expected_types={FindingType.DEADLINE_MISS, FindingType.PRIORITY_INVERSION_SEM},
        expected_culprits=["diag_low", "crunch_med"],
    ),
    GroundTruthCase(
        name="Semaphore Priority Inversion (Fixed)",
        config_file="semaphore_rules.toml",
        trace_file="semaphore_fixed.txt",
        expected_verdict="PASS",
        expected_types=set(),
        expected_culprits=[],
    ),
    GroundTruthCase(
        name="CPU Core 0 Starvation / Hog (Broken)",
        config_file="cpu_hog_rules.toml",
        trace_file="cpu_hog_demo.txt",
        expected_verdict="FAIL",
        expected_types={FindingType.SCHED_LATENCY_MISS},
        expected_culprits=["telemetry_hog"],
    ),
    GroundTruthCase(
        name="CPU Core 0 Starvation / Hog (Fixed)",
        config_file="cpu_hog_rules.toml",
        trace_file="cpu_hog_fixed.txt",
        expected_verdict="PASS",
        expected_types=set(),
        expected_culprits=[],
    ),
    GroundTruthCase(
        name="IPC Channel Priority Drop & Forbidden MsgSend (Broken)",
        config_file="ipc_rules.toml",
        trace_file="ipc_msgsend_demo.txt",
        expected_verdict="FAIL",
        expected_types={
            FindingType.DEADLINE_MISS,
            FindingType.PRIORITY_INVERSION_IPC,
            FindingType.FORBIDDEN_BLOCKING_IPC,
        },
        expected_culprits=["diag_server", "bg_worker"],
    ),
    GroundTruthCase(
        name="IPC Decoupled w/ Non-Blocking MsgSendPulse (Fixed)",
        config_file="ipc_rules.toml",
        trace_file="ipc_msgsend_fixed.txt",
        expected_verdict="PASS",
        expected_types=set(),
        expected_culprits=[],
    ),
    GroundTruthCase(
        name="Negative Control Suite (Legitimate Semaphore Signal + PI Lock)",
        config_file="negative_rules.toml",
        trace_file="negative_cases.txt",
        expected_verdict="PASS",
        expected_types=set(),
        expected_culprits=[],
    ),
]


def run_benchmark() -> int:
    tp = 0  # True Positives (correctly detected expected finding types)
    fp = 0  # False Positives (unexpected finding types emitted)
    fn = 0  # False Negatives (missed expected finding types)
    tn_cases = 0  # Negative/Fixed cases with 0 findings
    total_negative_cases = 0

    attribution_hits = 0
    attribution_total = 0
    reproducible_cases = 0

    total_lines = 0
    total_elapsed_sec = 0.0

    print("=" * 98)
    print(f"{'AION GROUND-TRUTH BENCHMARK HARNESS (QNX Neutrino 8.0 Traces)':^98}")
    print("=" * 98)
    print(f"{'Scenario':<52} {'Expected':<9} {'Actual':<9} {'Findings':<9} {'Attr':<7} {'Time(ms)':>8}")
    print("-" * 98)

    for case in BENCHMARK_SUITE:
        cfg_path = SAMPLES_DIR / case.config_file
        tr_path = TRACES_DIR / case.trace_file
        if not cfg_path.exists() or not tr_path.exists():
            print(f"[SKIP] Missing {case.config_file} or {case.trace_file}")
            continue

        line_count = sum(1 for _ in open(tr_path, "r", errors="replace"))
        total_lines += line_count

        t0 = time.perf_counter()
        config = load_config(str(cfg_path))
        session = parse_traceprinter_file(str(tr_path))
        findings = analyze_session(session, config)
        dt = time.perf_counter() - t0
        total_elapsed_sec += dt

        # Reproducibility check: run a second pass and verify identical findings & durations
        session2 = parse_traceprinter_file(str(tr_path))
        findings2 = analyze_session(session2, config)
        if [f.to_dict() for f in findings] == [f.to_dict() for f in findings2]:
            reproducible_cases += 1

        actual_verdict = "FAIL" if findings else "PASS"
        actual_types = {f.finding_type for f in findings}

        # Finding-level TP / FP / FN accounting
        tp += len(case.expected_types & actual_types)
        fp += len(actual_types - case.expected_types)
        fn += len(case.expected_types - actual_types)

        if case.expected_verdict == "PASS":
            total_negative_cases += 1
            if not findings:
                tn_cases += 1

        # Causal culprit attribution accuracy check
        all_culprit_str = " ".join(
            edge.culprit_thread for f in findings for edge in f.causal_chain
        ) + " " + " ".join(f.summary for f in findings)

        case_attr_ok = True
        for req_culprit in case.expected_culprits:
            attribution_total += 1
            if req_culprit in all_culprit_str:
                attribution_hits += 1
            else:
                case_attr_ok = False

        attr_label = "OK" if case_attr_ok else "MISS"
        print(
            f"{case.name:<52} {case.expected_verdict:<9} {actual_verdict:<9} "
            f"{len(findings):<9} {attr_label:<7} {dt * 1000.0:>8.1f}"
        )

    precision = (tp / (tp + fp) * 100.0) if (tp + fp) > 0 else 100.0
    recall = (tp / (tp + fn) * 100.0) if (tp + fn) > 0 else 100.0
    fpr = ((total_negative_cases - tn_cases) / total_negative_cases * 100.0) if total_negative_cases > 0 else 0.0
    attr_acc = (attribution_hits / attribution_total * 100.0) if attribution_total > 0 else 100.0
    repro_pct = (reproducible_cases / len(BENCHMARK_SUITE) * 100.0) if BENCHMARK_SUITE else 100.0
    throughput_lines = total_lines / max(0.001, total_elapsed_sec)

    print("-" * 98)
    print("Benchmark Summary Metrics:")
    print(f"  * Precision                : {precision:6.2f}%  (TP={tp}, FP={fp})")
    print(f"  * Recall                   : {recall:6.2f}%  (TP={tp}, FN={fn})")
    print(f"  * False-Positive Rate (FPR): {fpr:6.2f}%  ({total_negative_cases - tn_cases}/{total_negative_cases} clean/negative traces misflagged)")
    print(f"  * Causal Attribution Acc.  : {attr_acc:6.2f}%  ({attribution_hits}/{attribution_total} root-cause culprits & medium preemptors matched)")
    print(f"  * Reproducibility          : {repro_pct:6.2f}%  ({reproducible_cases}/{len(BENCHMARK_SUITE)} identical across repeated runs)")
    print(f"  * Ingestion Throughput     : {throughput_lines:,.0f} trace lines/sec ({total_lines:,} lines in {total_elapsed_sec:.2f}s)")
    print("=" * 98)

    return 0 if (fp == 0 and fn == 0 and attribution_hits == attribution_total) else 1


if __name__ == "__main__":
    raise SystemExit(run_benchmark())