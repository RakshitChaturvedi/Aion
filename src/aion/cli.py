"""
Aion Command-Line Interface (Thin Front-End over Aion SDK).
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
from aion.ingest import parse_traceprinter_file


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="Aion",
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