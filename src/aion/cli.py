"""
Aion Command-Line Interface.
Exit codes:
  0 = PASS
  1 = Timing or logical scheduling violation (FAIL)
  2 = Config or CLI usage error
  3 = Trace file could not be parsed
"""
import argparse
import sys
from typing import List, Optional

from aion.analyzers import ConfigError, analyze_session, compare_runs, load_config
from aion.ingest import TraceSession, parse_traceprinter_file


def _percentile(sorted_vals: List[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, int(round((pct / 100.0) * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def _print_metrics_table(session: TraceSession, rule_threads: List[str]) -> None:
    print("Per-Thread Job Timing Metrics:")
    print(f"  {'Thread':<22} {'Jobs':>4} {'Exec(ms)':>9} {'Preempt(ms)':>12} {'Block(ms)':>10} {'p50(ms)':>9} {'p95(ms)':>9} {'p99(ms)':>9} {'Max(ms)':>9} {'Jitter':>8}")
    print("  " + "-" * 106)
    seen = set()
    for tname in rule_threads:
        for th in session.find_threads_by_name(tname):
            if (th.pid, th.tid) in seen or not th.jobs:
                continue
            seen.add((th.pid, th.tid))
            resps = sorted(j.response_time_ms for j in th.jobs)
            exec_avg = sum(j.exec_ms for j in th.jobs) / len(th.jobs)
            pre_max = max(j.ready_preempted_ms for j in th.jobs)
            blk_max = max(j.mutex_blocked_ms + j.sem_blocked_ms + j.ipc_blocked_ms for j in th.jobs)
            p50 = _percentile(resps, 50)
            p95 = _percentile(resps, 95) if len(resps) >= 2 else resps[-1]
            p99 = _percentile(resps, 99) if len(resps) >= 2 else resps[-1]
            worst = resps[-1]
            jitter = resps[-1] - resps[0] if len(resps) >= 2 else 0.0
            mode_tag = "*" if any(j.inferred for j in th.jobs) else ""
            label = f"{th.name or f'tid-{th.tid}'}{mode_tag} (prio {th.prio})"
            print(
                f"  {label:<22} {len(th.jobs):>4} {exec_avg:>9.3f} {pre_max:>12.3f} "
                f"{blk_max:>10.3f} {p50:>9.3f} {p95:>9.3f} {p99:>9.3f} {worst:>9.3f} {jitter:>8.3f}"
            )
    print("  (* = job boundaries inferred from timer sleep/wakeup transitions)\n")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aion",
        description="Automated real-time timing verification and causal RCA for QNX 8.0",
    )
    sub = parser.add_subparsers(dest="cmd")

    check_p = sub.add_parser("check", help="Check a QNX trace against TOML timing & logical rules")
    check_p.add_argument("--config", "-c", required=True, help="Path to TOML rules configuration")
    check_p.add_argument("trace", nargs="?", help="Path to traceprinter .txt file (overrides config)")

    diff_p = sub.add_parser("diff", help="Compare baseline and candidate traces for regressions + attribution")
    diff_p.add_argument("baseline", help="Path to baseline traceprinter .txt file")
    diff_p.add_argument("candidate", help="Path to candidate traceprinter .txt file")
    diff_p.add_argument("--thread", "-t", required=True, help="Target thread name to compare")

    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 2

    if args.cmd == "check":
        try:
            config = load_config(args.config)
        except ConfigError as exc:
            print(f"[CONFIG ERROR] {exc}", file=sys.stderr)
            return 2

        trace_path = args.trace or config.trace
        if not trace_path:
            print("[USAGE ERROR] No trace file specified on CLI or in config.", file=sys.stderr)
            return 2

        try:
            session = parse_traceprinter_file(trace_path)
        except Exception as exc:
            print(f"[PARSE ERROR] Could not parse '{trace_path}': {exc}", file=sys.stderr)
            return 3

        findings = analyze_session(session, config)
        print(f"=== Aion Verdict: {'FAIL' if findings else 'PASS'} ===")
        print(f"Trace: {trace_path} | CPUs: {session.num_cpus} | Clock: {session.cycles_per_sec} Hz")
        print(f"Threads tracked: {len(session.threads)} | Markers: {len(session.markers)} | Findings: {len(findings)}\n")

        rule_threads = [r.thread for r in config.timing_rules] + config.forbid_blocking_ipc
        if rule_threads:
            _print_metrics_table(session, rule_threads)

        for f in findings:
            print(f.format_chain_ascii())
            print("-" * 80)

        return 1 if findings else 0

    if args.cmd == "diff":
        try:
            base_s = parse_traceprinter_file(args.baseline)
            cand_s = parse_traceprinter_file(args.candidate)
        except Exception as exc:
            print(f"[PARSE ERROR] {exc}", file=sys.stderr)
            return 3

        rep = compare_runs(base_s, cand_s, args.thread)
        status = "REGRESSION DETECTED (FAIL)" if rep.is_regression else "NO REGRESSION (PASS)"
        print(f"=== Aion Regression Comparison: {status} ===")
        print(f"  {rep.attribution_summary}")
        return 1 if rep.is_regression else 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
