"""
Aion Command-Line Interface.
Exit codes:
  0 = PASS
  1 = Timing or logical scheduling violation (FAIL)
  2 = Config or CLI usage error
  3 = Trace file could not be parsed
  4 = Inconclusive measurement (target thread missing/incomplete and inconclusive_policy="warn")
"""
import argparse
from dataclasses import asdict
import json
import sys
from typing import Any, Dict, List, Optional

from aion.analyzers import ConfigError, analyze_session, compare_runs, load_config, evaluate_verdicts
from aion.ingest import TraceSession, parse_traceprinter_file
from aion.report_html import render_check_html, render_diff_html

def _percentile(sorted_vals: List[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, int(round((pct / 100.0) * (len(sorted_vals) - 1))))
    return sorted_vals[idx]

def _collect_thread_metrics(session: TraceSession, rule_threads: List[str]) -> List[Dict[str, Any]]:
    metrics: List[Dict[str, Any]] = []
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
            inferred = any(j.inferred for j in th.jobs)
            metrics.append({
                "thread": th.name or f"tid-{th.tid}",
                "pid": th.pid,
                "tid": th.tid,
                "prio": th.prio,
                "inferred": inferred,
                "jobs": len(th.jobs),
                "exec_avg_ms": round(exec_avg, 3),
                "preempt_max_ms": round(pre_max, 3),
                "block_max_ms": round(blk_max, 3),
                "p50_ms": round(p50, 3),
                "p95_ms": round(p95, 3),
                "p99_ms": round(p99, 3),
                "max_ms": round(worst, 3),
                "jitter_ms": round(jitter, 3),
            })
    return metrics

def _print_metrics_table(session: TraceSession, rule_threads: List[str]) -> None:
    print("Per-Thread Job Timing Metrics:")
    print(f"  {'Thread':<22} {'Jobs':>4} {'Exec(ms)':>9} {'Preempt(ms)':>12} {'Block(ms)':>10} {'p50(ms)':>9} {'p95(ms)':>9} {'p99(ms)':>9} {'Max(ms)':>9} {'Jitter':>8}")
    print("  " + "-" * 106)
    for m in _collect_thread_metrics(session, rule_threads):
        mode_tag = "*" if m["inferred"] else ""
        label = f"{m['thread']}{mode_tag} (prio {m['prio']})"
        print(
            f"  {label:<22} {m['jobs']:>4} {m['exec_avg_ms']:>9.3f} {m['preempt_max_ms']:>12.3f} "
            f"{m['block_max_ms']:>10.3f} {m['p50_ms']:>9.3f} {m['p95_ms']:>9.3f} "
            f"{m['p99_ms']:>9.3f} {m['max_ms']:>9.3f} {m['jitter_ms']:>8.3f}"
        )
    print("  (* = job boundaries inferred from timer sleep/wakeup transitions)\n")

def _collect_timeline_and_views(
    session: TraceSession, rule_threads: List[str], findings: List[Any], config: Any
) -> Dict[str, Any]:
    """Extracts Thread Explorer timelines, CPU View stats, and IPC View summaries for JSON/HTML reports."""
    # Determine the active workload window (focus on jobs/findings or workload threads)
    relevant_threads = []
    for th in session.threads.values():
        if th.pid != 1 and (th.name in rule_threads or th.jobs or any(
            not th.process_name.startswith(p) for p in ("/proc/boot/", "proc/boot/", "system/bin/", "/system/bin/")
        )):
            if th.process_name and "procnto" not in th.process_name and "io-sock" not in th.process_name:
                relevant_threads.append(th)

    if not relevant_threads:
        relevant_threads = [th for th in session.threads.values() if th.jobs]

    all_ivs = [iv for th in relevant_threads for iv in th.intervals if iv.state != "THDEAD"]
    win_start = min((iv.start_ms for iv in all_ivs), default=0.0)
    win_end = max((iv.end_ms for iv in all_ivs), default=1.0)
    if win_end <= win_start:
        win_end = win_start + 1.0

    # 1. Thread Explorer tracks
    timeline_tracks = []
    for th in sorted(relevant_threads, key=lambda t: (-t.prio, t.pid, t.tid)):
        slices = []
        for iv in th.intervals:
            if iv.end_ms < win_start or iv.start_ms > win_end:
                continue
            s_ms = max(win_start, iv.start_ms)
            e_ms = min(win_end, iv.end_ms)
            if (e_ms - s_ms) <= 0.0005:
                continue
            slices.append({
                "state": iv.state,
                "start_ms": round(s_ms, 3),
                "end_ms": round(e_ms, 3),
                "duration_ms": round(e_ms - s_ms, 3),
                "cpu": iv.cpu,
                "prio": iv.prio,
                "sync_p": iv.sync_p,
            })
        if slices:
            timeline_tracks.append({
                "thread": th.display_name,
                "name": th.name or f"tid-{th.tid}",
                "prio": th.prio,
                "slices": slices,
            })

    # 2. IPC View summary
    ipc_summary = []
    for th in relevant_threads:
        for iv in th.intervals:
            if iv.state in ("THREPLY", "THSEND") and iv.duration_ms >= 0.01:
                srv_pid = iv.ipc_server_pid or 0
                srv_name = session.processes.get(srv_pid, f"pid:{srv_pid}")
                if iv.ipc_server_tid and (srv_pid, iv.ipc_server_tid) in session.threads:
                    srv_name = session.threads[(srv_pid, iv.ipc_server_tid)].display_name
                ipc_summary.append({
                    "client": th.display_name,
                    "state": iv.state,
                    "server": srv_name,
                    "start_ms": round(iv.start_ms, 3),
                    "end_ms": round(iv.end_ms, 3),
                    "duration_ms": round(iv.duration_ms, 3),
                })

    # 3. CPU View summary (within workload window)
    span_ms = max(0.001, win_end - win_start)
    cpu_summary = []
    for cpu_id in range(session.num_cpus):
        runners: Dict[str, float] = {}
        for th, iv in session.cpu_runner_at(cpu_id, win_start, win_end):
            if th.pid == 1 and th.tid in (1, 2):  # skip idle thread
                continue
            ov = min(win_end, iv.end_ms) - max(win_start, iv.start_ms)
            if ov > 0:
                label = th.display_name_at(iv.prio)
                runners[label] = runners.get(label, 0.0) + ov
        top_runners = sorted(runners.items(), key=lambda kv: kv[1], reverse=True)[:5]
        busy_ms = sum(runners.values())
        cpu_summary.append({
            "cpu": cpu_id,
            "busy_ms": round(busy_ms, 3),
            "utilization_pct": round(min(100.0, (busy_ms / span_ms) * 100.0), 1),
            "top_threads": [{"thread": k, "run_ms": round(v, 3)} for k, v in top_runners],
        })

    return {
        "environment": {
            "target_env": config.target_env,
            "margin_pct": config.margin_pct,
            "trace_date": getattr(session, "trace_date", "N/A"),
            "tracelogger_args": session.tracelogger_args or "tracelogger -f <trace.kev>",
        },
        "window_start_ms": round(win_start, 3),
        "window_end_ms": round(win_end, 3),
        "timeline_tracks": timeline_tracks,
        "ipc_summary": ipc_summary,
        "cpu_summary": cpu_summary,
    }

def _emit_output(text: str, output_path: Optional[str]) -> None:
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aion",
        description="Automated real-time timing verification and causal RCA for QNX 8.0",
    )
    sub = parser.add_subparsers(dest="cmd")

    check_p = sub.add_parser("check", help="Check a QNX trace against TOML timing & logical rules")
    check_p.add_argument("--config", "-c", required=True, help="Path to TOML rules configuration")
    check_p.add_argument("--format", "-f", choices=("text", "json", "html"), default="text", help="Output format (default: text)")
    check_p.add_argument("--output", "-o", help="Optional file path to write report output")
    check_p.add_argument("trace", nargs="?", help="Path to traceprinter .txt file (overrides config)")

    diff_p = sub.add_parser("diff", help="Compare baseline and candidate traces for regressions + attribution")
    diff_p.add_argument("baseline", help="Path to baseline traceprinter .txt file")
    diff_p.add_argument("candidate", help="Path to candidate traceprinter .txt file")
    diff_p.add_argument("--thread", "-t", required=True, help="Target thread name to compare")
    diff_p.add_argument("--format", "-f", choices=("text", "json", "html"), default="text", help="Output format (default: text)")
    diff_p.add_argument("--output", "-o", help="Optional file path to write report output")

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
        v_sum = evaluate_verdicts(session, config, findings)
        rule_threads = [r.thread for r in config.timing_rules] + config.forbid_blocking_ipc
        verdict = v_sum.overall_verdict

        if args.format in ("json", "html"):
            views = _collect_timeline_and_views(session, rule_threads, findings, config)
            payload = {
                "schema_version": "1.0",
                "verdict": verdict,
                "logical_verdict": v_sum.logical_verdict,
                "wall_clock_verdict": v_sum.wall_clock_verdict,
                "inconclusive_reasons": v_sum.inconclusive_reasons,
                "exit_code": v_sum.exit_code,
                "trace": trace_path,
                "cpus": session.num_cpus,
                "cycles_per_sec": session.cycles_per_sec,
                "threads_tracked": len(session.threads),
                "markers": len(session.markers),
                "metrics": _collect_thread_metrics(session, rule_threads) if rule_threads else [],
                "findings_count": len(findings),
                "findings": [f.to_dict() for f in findings],
                **views,
            }
            if args.format == "json":
                _emit_output(json.dumps(payload, indent=2), args.output)
            else:
                _emit_output(render_check_html(payload), args.output)
        else:
            print(f"=== Aion Verdict: {verdict} (Logical: {v_sum.logical_verdict} | Wall-Clock: {v_sum.wall_clock_verdict}) ===")
            print(f"Trace: {trace_path} | CPUs: {session.num_cpus} | Clock: {session.cycles_per_sec} Hz")
            print(f"Threads tracked: {len(session.threads)} | Markers: {len(session.markers)} | Findings: {len(findings)}\n")
            if v_sum.inconclusive_reasons:
                for reason in v_sum.inconclusive_reasons:
                    print(f"    [INCONCLUSIVE] {reason}")
                print()
            if rule_threads:
                _print_metrics_table(session, rule_threads)
            for f in findings:
                print(f.format_chain_ascii())
                print("-" * 80)

        return v_sum.exit_code

    if args.cmd == "diff":
        try:
            base_s = [parse_traceprinter_file(p) for p in args.baseline.split(",")]
            cand_s = [parse_traceprinter_file(p) for p in args.candidate.split(",")]
        except Exception as exc:
            print(f"[PARSE ERROR] {exc}", file=sys.stderr)
            return 3

        rep = compare_runs(base_s, cand_s, args.thread)
        status = "REGRESSION DETECTED (FAIL)" if rep.is_regression else "NO REGRESSION (PASS)"

        if args.format == "json":
            payload = {
                "verdict": "FAIL" if rep.is_regression else "PASS",
                "status": status,
                **asdict(rep),
            }
            _emit_output(json.dumps(payload, indent=2), args.output)
        else:
            print(f"=== Aion Regression Comparison: {status} ===")
            print(f"  {rep.attribution_summary}")

        return 1 if rep.is_regression else 0

    return 2

if __name__ == "__main__":
    raise SystemExit(main())