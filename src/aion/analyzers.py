# casual rca Analyzers, toml config loader, and regression comparator.
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from aion.ingest import TraceSession
from aion.model import (
    CausalEdge,
    CausalFinding,
    Confidence,
    EvidenceRef,
    FindingType,
    StateInterval,
    ThreadTimeline,
)

# System processes that should only appear as culprits/interferers, never as false-positive victims
SYSTEM_DAEMON_PREFIXES = (
    "/proc/boot/",
    "proc/boot/",
    "system/bin/",
    "/system/bin/",
)


class ConfigError(Exception):
    """Raised when the TOML configuration file is invalid or missing required fields."""


@dataclass
class TimingRule:
    name: str
    thread: str
    period_ms: Optional[float] = None
    deadline_ms: Optional[float] = None
    max_lateness_ms: Optional[float] = None
    max_sched_latency_ms: Optional[float] = None


@dataclass
class RuleConfig:
    trace: str = ""
    target_env: str = "qemu"
    margin_pct: float = 10.0
    scope_processes: List[str] = field(default_factory=list)
    priority_inversion: bool = True
    semaphore_locks: bool = True
    forbid_blocking_ipc: List[str] = field(default_factory=list)
    timing_rules: List[TimingRule] = field(default_factory=list)

    @property
    def effective_margin_multiplier(self) -> float:
        if self.target_env.lower() == "qemu":
            return 1.0 + (self.margin_pct / 100.0)
        return 1.0


def load_config(filepath: str) -> RuleConfig:
    try:
        with open(filepath, "rb") as f:
            data = tomllib.load(f)
    except Exception as exc:
        raise ConfigError(f"Failed to parse TOML config '{filepath}': {exc}") from exc

    settings = data.get("settings", {})
    logical = data.get("logical_checks", data.get("checks", {}))
    raw_rules = data.get("timing_rule", data.get("rule", []))

    if not isinstance(raw_rules, list):
        raise ConfigError("'timing_rule' must be an array of tables ([[timing_rule]]).")

    rules = []
    for idx, r in enumerate(raw_rules, 1):
        if "name" not in r or "thread" not in r:
            raise ConfigError(f"Rule #{idx} is missing required 'name' or 'thread' field.")
        rules.append(
            TimingRule(
                name=str(r["name"]),
                thread=str(r["thread"]),
                period_ms=float(r["period_ms"]) if "period_ms" in r else None,
                deadline_ms=float(r["deadline_ms"]) if "deadline_ms" in r else None,
                max_lateness_ms=float(r["max_lateness_ms"]) if "max_lateness_ms" in r else None,
                max_sched_latency_ms=float(r["max_sched_latency_ms"]) if "max_sched_latency_ms" in r else None,
            )
        )

    return RuleConfig(
        trace=str(settings.get("trace", "")),
        target_env=str(settings.get("target_env", "qemu")),
        margin_pct=float(settings.get("margin_pct", 10.0)),
        scope_processes=list(settings.get("scope_processes", [])),
        priority_inversion=bool(logical.get("priority_inversion", True)),
        semaphore_locks=bool(logical.get("semaphore_locks", True)),
        forbid_blocking_ipc=list(logical.get("forbid_blocking_ipc", [])),
        timing_rules=rules,
    )


def _is_target_workload_thread(th: ThreadTimeline, config: RuleConfig) -> bool:
    """Returns True if `th` belongs to the user workload rather than a background QNX OS driver."""
    if th.pid == 1:
        return False
    if config.scope_processes:
        return any(sp in th.process_name or sp == th.name for sp in config.scope_processes)
    rule_threads: Set[str] = {r.thread for r in config.timing_rules} | set(config.forbid_blocking_ipc)
    if th.name in rule_threads:
        return True
    if any(th.process_name.startswith(pref) for pref in SYSTEM_DAEMON_PREFIXES):
        return False
    return bool(th.process_name)


def analyze_session(session: TraceSession, config: RuleConfig) -> List[CausalFinding]:
    """Runs all configured logical and wall-clock detectors on a TraceSession."""
    findings: List[CausalFinding] = []

    if config.priority_inversion:
        findings.extend(_detect_mutex_priority_inversions(session, config))
        findings.extend(_detect_ipc_channel_inversions(session, config))

    if config.semaphore_locks:
        findings.extend(_detect_semaphore_inversions(session, config))

    if config.forbid_blocking_ipc:
        findings.extend(_detect_forbidden_blocking_ipc(session, config.forbid_blocking_ipc))

    for rule in config.timing_rules:
        findings.extend(_check_timing_rule(session, rule, config.effective_margin_multiplier))

    return sorted(findings, key=lambda f: f.start_ms)


def _iv_to_evidence(session: TraceSession, th: ThreadTimeline, iv: StateInterval, extra: str = "") -> EvidenceRef:
    details = f"pid:{th.pid} tid:{th.tid} ({th.name or 'main'}) prio:{iv.prio}"
    if extra:
        details += f" {extra}"
    return EvidenceRef(
        event_id=iv.start_event_id,
        line_no=iv.start_line_no,
        timestamp_cycles=iv.start_cycles,
        timestamp_ms=iv.start_ms,
        cpu=iv.cpu,
        raw_event=f"THREAD:{iv.state}",
        details=details,
    )


def _detect_mutex_priority_inversions(session: TraceSession, config: RuleConfig) -> List[CausalFinding]:
    """
    Scans for Mutex Priority Inversion on target workload threads:
    1. Victim thread T (prio P_T) is in THMUTEX waiting on mutex held by Owner L (prio P_L < P_T).
    2. While T is blocked, checks which medium-priority threads M (P_L < P_M < P_T) preempted L
       on L's CPU or ran in place of L.
    """
    findings: List[CausalFinding] = []
    for th in session.threads.values():
        if not _is_target_workload_thread(th, config):
            continue
        for iv in th.intervals:
            if iv.state != "THMUTEX":
                continue

            owner_th: Optional[ThreadTimeline] = None
            confidence = Confidence.CONFIRMED
            if iv.owner_key and iv.owner_key in session.threads:
                owner_th = session.threads[iv.owner_key]
            elif iv.sync_p:
                candidates = [
                    other for (p, t), other in session.threads.items()
                    if p == th.pid and t != th.tid and other.prio < iv.prio
                ]
                if candidates:
                    owner_th = min(candidates, key=lambda c: c.prio)
                    confidence = Confidence.INFERRED

            if not owner_th or owner_th.prio >= iv.prio:
                continue

            # Hop 1: Victim T -> Blocked on Mutex held by Owner L
            edge1 = CausalEdge(
                relation="BLOCKED_ON_MUTEX",
                victim_thread=th.display_name,
                culprit_thread=owner_th.display_name,
                mechanism=f"mutex {iv.sync_p or 'unknown'} (owner prio {owner_th.prio} < waiter prio {iv.prio})",
                duration_ms=iv.duration_ms,
                start_ms=iv.start_ms,
                end_ms=iv.end_ms,
                confidence=confidence,
                evidence=[_iv_to_evidence(session, th, iv, f"sync_p={iv.sync_p}")],
            )
            chain = [edge1]

            # Hop 2: Find medium-priority threads (P_L < P_M < P_T) that ran while T was blocked
            preemptors: Dict[str, Tuple[ThreadTimeline, float, List[EvidenceRef]]] = {}
            for owner_iv in owner_th.intervals:
                if owner_iv.state not in ("THREADY", "THRUNNING"):
                    continue
                w_start = max(iv.start_ms, owner_iv.start_ms)
                w_end = min(iv.end_ms, owner_iv.end_ms)
                if w_end <= w_start:
                    continue

                # Check all CPUs where medium threads in the same process or system ran
                for cpu_id in range(session.num_cpus):
                    for runner_th, run_iv in session.cpu_runner_at(cpu_id, w_start, w_end):
                        if (runner_th.pid, runner_th.tid) in ((th.pid, th.tid), (owner_th.pid, owner_th.tid)):
                            continue
                        if runner_th.pid == 1 and runner_th.tid in (1, 2):
                            continue  # Skip idle threads
                        if owner_th.prio < run_iv.prio < iv.prio and (
                            owner_iv.state == "THREADY" or runner_th.pid == th.pid
                        ):
                            ov_s = max(w_start, run_iv.start_ms)
                            ov_e = min(w_end, run_iv.end_ms)
                            dur = ov_e - ov_s
                            key = runner_th.display_name
                            prev_th, prev_dur, prev_evs = preemptors.get(key, (runner_th, 0.0, []))
                            if len(prev_evs) < 4:
                                prev_evs.append(_iv_to_evidence(session, runner_th, run_iv, f"preempted {owner_th.name}"))
                            preemptors[key] = (prev_th, prev_dur + dur, prev_evs)

            for m_name, (m_th, m_dur, m_evs) in preemptors.items():
                chain.append(
                    CausalEdge(
                        relation="PREEMPTED_BY_MEDIUM_THREAD",
                        victim_thread=owner_th.display_name,
                        culprit_thread=m_name,
                        mechanism=f"strict priority preemption (prio {m_th.prio} > prio {owner_th.prio})",
                        duration_ms=m_dur,
                        start_ms=iv.start_ms,
                        end_ms=iv.end_ms,
                        confidence=confidence,
                        evidence=m_evs,
                    )
                )

            preempt_desc = (
                f" while {', '.join(preemptors.keys())} ran at medium priority"
                if preemptors
                else " (uninherited mutex lock)"
            )
            findings.append(
                CausalFinding(
                    finding_type=FindingType.PRIORITY_INVERSION_MUTEX,
                    severity="FAIL",
                    rule_name="logical_checks.priority_inversion",
                    victim_thread=th.display_name,
                    summary=(
                        f"{th.display_name} waited {iv.duration_ms:.3f} ms on mutex {iv.sync_p} "
                        f"held by lower-priority {owner_th.display_name}{preempt_desc}."
                    ),
                    suggested_fix=(
                        "Enable priority inheritance on the mutex via "
                        "pthread_mutexattr_setprotocol(&attr, PTHREAD_PRIO_INHERIT)."
                    ),
                    confidence=confidence,
                    start_ms=iv.start_ms,
                    end_ms=iv.end_ms,
                    duration_ms=iv.duration_ms,
                    causal_chain=chain,
                )
            )
    return findings


def _detect_forbidden_blocking_ipc(session: TraceSession, thread_names: List[str]) -> List[CausalFinding]:
    """Detects when a critical real-time thread enters THSEND or THREPLY (excluding internal procnto TraceEvent calls)."""
    findings: List[CausalFinding] = []
    for tname in thread_names:
        for th in session.find_threads_by_name(tname):
            for iv in th.intervals:
                if iv.state not in ("THREPLY", "THSEND"):
                    continue
                srv_pid = iv.ipc_server_pid or 0
                # Ignore microsecond kernel calls to procnto (pid 1) unless they block > 0.5 ms
                if srv_pid == 1 and iv.duration_ms < 0.5:
                    continue
                srv_name = session.processes.get(srv_pid, f"pid:{srv_pid}")
                edge = CausalEdge(
                    relation="BLOCKED_ON_IPC_SERVER",
                    victim_thread=th.display_name,
                    culprit_thread=f"{srv_name} (pid:{srv_pid})",
                    mechanism=f"QNX synchronous message passing ({iv.state})",
                    duration_ms=iv.duration_ms,
                    start_ms=iv.start_ms,
                    end_ms=iv.end_ms,
                    confidence=Confidence.CONFIRMED if srv_pid else Confidence.INFERRED,
                    evidence=[_iv_to_evidence(session, th, iv, f"server={srv_name}")],
                )
                findings.append(
                    CausalFinding(
                        finding_type=FindingType.FORBIDDEN_BLOCKING_IPC,
                        severity="FAIL",
                        rule_name="logical_checks.forbid_blocking_ipc",
                        victim_thread=th.display_name,
                        summary=(
                            f"Critical thread {th.display_name} blocked in {iv.state} for "
                            f"{iv.duration_ms:.3f} ms waiting on server '{srv_name}'."
                        ),
                        suggested_fix=(
                            "Move synchronous I/O (printf, slogf, file writes, MsgSend) out of the "
                            "real-time loop into a low-priority worker, or decouple with non-blocking MsgSendPulse()."
                        ),
                        confidence=edge.confidence,
                        start_ms=iv.start_ms,
                        end_ms=iv.end_ms,
                        duration_ms=iv.duration_ms,
                        causal_chain=[edge],
                    )
                )
    return findings


def _detect_ipc_channel_inversions(session: TraceSession, config: RuleConfig) -> List[CausalFinding]:
    """Detects when a high-priority workload client blocks in THREPLY on a user/system server running at lower priority."""
    findings: List[CausalFinding] = []
    for th in session.threads.values():
        if not _is_target_workload_thread(th, config) or th.prio < 25:
            continue
        if th.tid == 1 and not th.name:
            continue  # Skip main() thread setup IPC
        for iv in th.intervals:
            if iv.state != "THREPLY" or not iv.ipc_server_pid or iv.ipc_server_pid == 1:
                continue
            srv_threads = [
                st for (p, _), st in session.threads.items() if p == iv.ipc_server_pid and st.prio > 0
            ]
            for st in srv_threads:
                for s_iv in st.intervals:
                    ov_s = max(iv.start_ms, s_iv.start_ms)
                    ov_e = min(iv.end_ms, s_iv.end_ms)
                    if ov_e > ov_s and 0 < s_iv.prio < iv.prio and s_iv.state in ("THREADY", "THRUNNING"):
                        edge = CausalEdge(
                            relation="IPC_CHANNEL_PRIORITY_DROP",
                            victim_thread=th.display_name,
                            culprit_thread=st.display_name,
                            mechanism=f"THREPLY wait on server running at prio {s_iv.prio} (< client prio {iv.prio})",
                            duration_ms=ov_e - ov_s,
                            start_ms=ov_s,
                            end_ms=ov_e,
                            confidence=Confidence.CONFIRMED,
                            evidence=[
                                _iv_to_evidence(session, th, iv, "client THREPLY"),
                                _iv_to_evidence(session, st, s_iv, f"server prio {s_iv.prio}"),
                            ],
                        )
                        findings.append(
                            CausalFinding(
                                finding_type=FindingType.PRIORITY_INVERSION_IPC,
                                severity="FAIL",
                                rule_name="logical_checks.priority_inversion",
                                victim_thread=th.display_name,
                                summary=(
                                    f"{th.display_name} blocked in THREPLY while server {st.display_name} "
                                    f"executed at uninherited priority {s_iv.prio}."
                                ),
                                suggested_fix=(
                                    "Ensure channel does not set _NTO_CHF_FIXED_PRIORITY, or decouple "
                                    "the real-time client using non-blocking pulses (MsgSendPulse)."
                                ),
                                confidence=Confidence.CONFIRMED,
                                start_ms=iv.start_ms,
                                end_ms=iv.end_ms,
                                duration_ms=iv.duration_ms,
                                causal_chain=[edge],
                            )
                        )
                        break
    return findings


def _detect_semaphore_inversions(session: TraceSession, config: RuleConfig) -> List[CausalFinding]:
    """Flags high-priority target workload threads blocking on semaphores."""
    findings: List[CausalFinding] = []
    for th in session.threads.values():
        if not _is_target_workload_thread(th, config) or th.prio < 25:
            continue
        for iv in th.intervals:
            if iv.state == "THSEM" and iv.duration_ms > 0.1:
                edge = CausalEdge(
                    relation="BLOCKED_ON_SEMAPHORE",
                    victim_thread=th.display_name,
                    culprit_thread=f"semaphore {iv.sync_p or 'unknown'}",
                    mechanism="QNX semaphores (THSEM) do not support priority inheritance",
                    duration_ms=iv.duration_ms,
                    start_ms=iv.start_ms,
                    end_ms=iv.end_ms,
                    confidence=Confidence.CONFIRMED,
                    evidence=[_iv_to_evidence(session, th, iv, f"sync_p={iv.sync_p}")],
                )
                findings.append(
                    CausalFinding(
                        finding_type=FindingType.PRIORITY_INVERSION_SEM,
                        severity="FAIL",
                        rule_name="logical_checks.semaphore_locks",
                        victim_thread=th.display_name,
                        summary=(
                            f"Real-time thread {th.display_name} blocked on semaphore {iv.sync_p} "
                            f"for {iv.duration_ms:.3f} ms."
                        ),
                        suggested_fix="Replace lock semaphore with a pthread_mutex_t configured with PTHREAD_PRIO_INHERIT.",
                        confidence=Confidence.CONFIRMED,
                        start_ms=iv.start_ms,
                        end_ms=iv.end_ms,
                        duration_ms=iv.duration_ms,
                        causal_chain=[edge],
                    )
                )
    return findings


def _check_timing_rule(session: TraceSession, rule: TimingRule, margin_mult: float) -> List[CausalFinding]:
    findings: List[CausalFinding] = []
    matched = session.find_threads_by_name(rule.thread)
    for th in matched:
        if rule.max_sched_latency_ms is not None:
            limit = rule.max_sched_latency_ms * margin_mult
            for iv in th.intervals:
                if iv.state == "THREADY" and iv.duration_ms > limit:
                    edges = []
                    for runner_th, run_iv in session.cpu_runner_at(iv.cpu, iv.start_ms, iv.end_ms):
                        ov = min(iv.end_ms, run_iv.end_ms) - max(iv.start_ms, run_iv.start_ms)
                        edges.append(
                            CausalEdge(
                                relation="CPU_OCCUPIED_BY",
                                victim_thread=th.display_name,
                                culprit_thread=runner_th.display_name,
                                mechanism=f"CPU:{iv.cpu:02d} preemption/interference",
                                duration_ms=ov,
                                start_ms=max(iv.start_ms, run_iv.start_ms),
                                end_ms=min(iv.end_ms, run_iv.end_ms),
                                confidence=Confidence.CONFIRMED,
                                evidence=[_iv_to_evidence(session, runner_th, run_iv)],
                            )
                        )
                    findings.append(
                        CausalFinding(
                            finding_type=FindingType.SCHED_LATENCY_MISS,
                            severity="FAIL",
                            rule_name=rule.name,
                            victim_thread=th.display_name,
                            summary=(
                                f"{th.display_name} spent {iv.duration_ms:.3f} ms in THREADY "
                                f"(limit: {limit:.3f} ms including margin)."
                            ),
                            suggested_fix="Lower CPU hog priority/workload or isolate critical thread core affinity.",
                            confidence=Confidence.CONFIRMED,
                            start_ms=iv.start_ms,
                            end_ms=iv.end_ms,
                            duration_ms=iv.duration_ms,
                            causal_chain=edges,
                        )
                    )

        if rule.deadline_ms is not None:
            limit = rule.deadline_ms * margin_mult
            for job in th.jobs:
                if job.response_time_ms > limit:
                    top_culprit = (
                        max(job.blocker_attribution.items(), key=lambda kv: kv[1])
                        if job.blocker_attribution
                        else ("CPU preemption / execution", job.ready_preempted_ms + job.exec_ms)
                    )
                    findings.append(
                        CausalFinding(
                            finding_type=FindingType.DEADLINE_MISS,
                            severity="FAIL",
                            rule_name=rule.name,
                            victim_thread=th.display_name,
                            summary=(
                                f"Job #{job.job_index} of {th.display_name} took {job.response_time_ms:.3f} ms "
                                f"(deadline: {limit:.3f} ms). Primary delay source: {top_culprit[0]} "
                                f"({top_culprit[1]:.3f} ms)."
                            ),
                            suggested_fix="Inspect causal blockers (mutex inversion, IPC wait, or CPU interference) during this job window.",
                            confidence=Confidence.INFERRED if job.inferred else Confidence.CONFIRMED,
                            start_ms=job.release_ms,
                            end_ms=job.completion_ms,
                            duration_ms=job.response_time_ms,
                        )
                    )
    return findings


@dataclass
class RegressionReport:
    thread_name: str
    baseline_p99_ms: float
    candidate_p99_ms: float
    ratio: float
    delta_ms: float
    is_regression: bool
    primary_new_blocker: str
    attribution_summary: str


def compare_runs(
    baseline: TraceSession,
    candidate: TraceSession,
    thread_name: str,
    threshold_ratio: float = 1.25,
) -> RegressionReport:
    b_threads = baseline.find_threads_by_name(thread_name)
    c_threads = candidate.find_threads_by_name(thread_name)

    b_jobs = [j for th in b_threads for j in th.jobs]
    c_jobs = [j for th in c_threads for j in th.jobs]

    def p99(vals: List[float]) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        idx = min(len(s) - 1, int(len(s) * 0.99))
        return s[idx]

    b_p99 = p99([j.response_time_ms for j in b_jobs])
    c_p99 = p99([j.response_time_ms for j in c_jobs])
    ratio = (c_p99 / b_p99) if b_p99 > 0 else (999.0 if c_p99 > 0 else 1.0)
    delta = c_p99 - b_p99

    b_causes: Dict[str, float] = {}
    for j in b_jobs:
        for k, v in j.blocker_attribution.items():
            b_causes[k] = max(b_causes.get(k, 0.0), v)

    c_causes: Dict[str, float] = {}
    for j in c_jobs:
        for k, v in j.blocker_attribution.items():
            c_causes[k] = max(c_causes.get(k, 0.0), v)

    diffs: Dict[str, float] = {}
    for k, c_val in c_causes.items():
        diffs[k] = c_val - b_causes.get(k, 0.0)

    if diffs:
        top_cause, top_diff = max(diffs.items(), key=lambda kv: kv[1])
    else:
        top_cause, top_diff = ("CPU execution/preemption", delta)

    is_reg = ratio >= threshold_ratio and delta > 0.5
    summary = (
        f"Thread '{thread_name}' p99 response time changed from {b_p99:.3f} ms to {c_p99:.3f} ms "
        f"({ratio:.2f}x, {delta:+.3f} ms). "
        f"Primary attributed cause: {top_cause} ({top_diff:+.3f} ms)."
    )
    return RegressionReport(
        thread_name=thread_name,
        baseline_p99_ms=b_p99,
        candidate_p99_ms=c_p99,
        ratio=ratio,
        delta_ms=delta,
        is_regression=is_reg,
        primary_new_blocker=top_cause,
        attribution_summary=summary,
    )