# casual rca Analyzers, toml config loader, and regression comparator.
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Union, Sequence

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
    inconclusive_policy: str = "warn" # exit 4 warn or 1 fail
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

    inc_policy=str(settings.get("inconclusive_policy", "warn")).lower()
    if inc_policy not in ("warn", "fail"):
        raise ConfigError("'inconclusive_policy' in [settings] must be 'warn' or 'fail'.")

    return RuleConfig(
        trace=str(settings.get("trace", "")),
        target_env=str(settings.get("target_env", "qemu")),
        margin_pct=float(settings.get("margin_pct", 10.0)),
        inconclusive_policy=inc_policy,
        scope_processes=list(settings.get("scope_processes", [])),
        priority_inversion=bool(logical.get("priority_inversion", True)),
        semaphore_locks=bool(logical.get("semaphore_locks", True)),
        forbid_blocking_ipc=list(logical.get("forbid_blocking_ipc", [])),
        timing_rules=rules,
    )


def _is_target_workload_thread(th: ThreadTimeline, config: RuleConfig) -> bool:
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

LOGICAL_FINDING_TYPES = {
    FindingType.PRIORITY_INVERSION_MUTEX,
    FindingType.PRIORITY_INVERSION_SEM,
    FindingType.PRIORITY_INVERSION_IPC,
    FindingType.FORBIDDEN_BLOCKING_IPC
}

WALL_CLOCK_FINDING_TYPES = {
    FindingType.DEADLINE_MISS,
    FindingType.LATENESS_MISS,
    FindingType.SCHED_LATENCY_MISS,
    FindingType.CPU_HOG
}

@dataclass
class VerdictSummary:
    # 3 verdict classification -- logical, wall-clock, inconclusive.
    overall_verdict: str        # pass, fail or inconclusive
    logical_verdict: str        # pass or fail
    wall_clock_verdict: str     # pass, fail or inconclusive
    inconclusive_reasons: List[str] = field(default_factory=list)
    exit_code: int = 0          # 0 = pass, 1 = fail, 4 = inconclusive

def evaluate_verdicts(session:TraceSession, config:RuleConfig, findings:List[CausalFinding]) -> VerdictSummary:
    logical_fails = [f for f in findings if f.finding_type in LOGICAL_FINDING_TYPES]
    wall_fails = [f for f in findings if f.finding_type in WALL_CLOCK_FINDING_TYPES]
    inc_reasons: List[str] = []

    # 1. target timing rule thread missing or had 0 completed jobs in trace
    for rule in config.timing_rules:
        matched = session.find_threads_by_name(rule.thread)
        if not matched:
            inc_reasons.append(f"Rule '{rule.name}': target thread '{rule.thread}' not found in trace.")
        elif not any(th.jobs for th in matched) and not any(th.intervals for th in matched):
            inc_reasons.append(f"Rule '{rule.name}': thread '{rule.thread}' had 0 state intervals.")
        elif rule.deadline_ms is not None and not any(th.jobs for th in matched):
            inc_reasons.append(f"Rule '{rule.name}': thread '{rule.thread}' had 0 completed jobs for deadline check.")

    # 2. excessive malformed trace lines
    if session.malformed_lines > 50:
        inc_reasons.appnd(f"Trace contained {session.malformed_lines} malformed lines (>50 threshold).")
    logical_v = "FAIL" if logical_fails else "PASS"
    if wall_fails:
        wall_v = "FAIL"
    elif inc_reasons:
        wall_v = "INCONCLUSIVE"
    else:
        wall_v = "PASS"

    if logical_fails or wall_fails:
        return VerdictSummary(
            overall_verdict="FAIL",
            logical_verdict=logical_v,
            wall_clock_verdict=wall_v,
            inconclusive_reasons=inc_reasons,
            exit_code=1
        )
    if inc_reasons:
        if config.inconclusive_policy == "fail":
            return VerdictSummary(
                overall_verdict="FAIL",
                logical_verdict=logical_v,
                wall_clock_verdict="INCONCLUSIVE",
                inconclusive_reasons=inc_reasons,
                exit_code=1
            )
        return VerdictSummary(
            overall_verdict="INCONCLUSIVE",
            logical_verdict=logical_v,
            wall_clock_verdict="INCONCLUSIVE",
            inconclusive_reasons=inc_reasons,
            exit_code=4
        )
    return VerdictSummary(
        overall_verdict="PASS",
        logical_verdict="PASS",
        wall_clock_verdict="PASS",
        inconclusive_reasons=[],
        exit_code=0
    )
    

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


def _find_medium_preemptors(
    session: TraceSession,
    waiter_th: ThreadTimeline,
    waiter_prio: int,
    owner_th: ThreadTimeline,
    window_start_ms: float,
    window_end_ms: float,
) -> Dict[str, Tuple[ThreadTimeline, int, float, List[EvidenceRef]]]:
    """Finds all medium-priority threads (owner_prio < prio < waiter_prio) that preempted `owner_th` while it was THREADY."""
    preemptors: Dict[str, Tuple[ThreadTimeline, int, float, List[EvidenceRef]]] = {}
    for owner_iv in owner_th.intervals:
        if owner_iv.state != "THREADY":
            continue

        w_start = max(window_start_ms, owner_iv.start_ms)
        w_end = min(window_end_ms, owner_iv.end_ms)
        if (w_end - w_start) <= 0.05:
            continue

        for cpu_id in range(session.num_cpus):
            for runner_th, run_iv in session.cpu_runner_at(cpu_id, w_start, w_end):
                if (runner_th.pid, runner_th.tid) in ((waiter_th.pid, waiter_th.tid), (owner_th.pid, owner_th.tid)):
                    continue
                if runner_th.pid == 1 and runner_th.tid in (1,2):
                    continue
                if owner_iv.prio < run_iv.prio < waiter_prio:
                    ov_s = max(w_start, run_iv.start_ms)
                    ov_e = min(w_end, run_iv.end_ms)
                    dur = ov_e - ov_s

                    key = runner_th.display_name_at(run_iv.prio)
                    prev_th, prev_prio, prev_dur, prev_evs = preemptors.get(key, (runner_th, run_iv.prio, 0.0, []))
                    cand_ev = _iv_to_evidence(session, runner_th, run_iv, f"preempted {owner_th.name}")
                    if len(prev_evs) < 4 and all(e.line_no != cand_ev.line_no for e in prev_evs):
                        prev_evs.append(cand_ev)
                    preemptors[key] = (prev_th, max(prev_prio, run_iv.prio), prev_dur + dur, prev_evs)
    return dict(sorted(preemptors.items(), key=lambda kv: kv[1][2], reverse=True))


def _detect_mutex_priority_inversions(session: TraceSession, config: RuleConfig) -> List[CausalFinding]:
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

            preemptors = _find_medium_preemptors(
                session, th, iv.prio, owner_th, iv.start_ms, iv.end_ms
            )
            if not preemptors:
                continue

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

            for m_name, (m_th, m_prio, m_dur, m_evs) in preemptors.items():
                chain.append(
                    CausalEdge(
                        relation="PREEMPTED_BY_MEDIUM_THREAD",
                        victim_thread=owner_th.display_name,
                        culprit_thread=m_name,
                        mechanism=f"strict priority preemption (prio {m_prio} > prio {owner_th.prio})",
                        duration_ms=m_dur,
                        start_ms=iv.start_ms,
                        end_ms=iv.end_ms,
                        confidence=confidence,
                        evidence=m_evs,
                    )
                )

            preempt_desc = f" while {', '.join(preemptors.keys())} ran at medium priority"
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
    findings: List[CausalFinding] = []
    for tname in thread_names:
        for th in session.find_threads_by_name(tname):
            for iv in th.intervals:
                if iv.state not in ("THREPLY", "THSEND"):
                    continue
                srv_pid = iv.ipc_server_pid or 0
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
    """
    Detects IPC Channel Priority Inversion:
    Client T (prio P_T) blocks in THREPLY waiting on Server S, while S runs or sits THREADY
    at lower priority P_S < P_T (because _NTO_CHF_FIXED_PRIORITY disabled priority inheritance),
    AND traces any medium-priority thread M (P_S < P_M < P_T) that preempted S!
    """
    findings: List[CausalFinding] = []
    for th in session.threads.values():
        if not _is_target_workload_thread(th, config) or th.prio < 25:
            continue
        if th.tid == 1 and not th.name:
            continue
        for iv in th.intervals:
            if iv.state != "THREPLY" or not iv.ipc_server_pid or iv.ipc_server_pid == 1:
                continue
            if iv.duration_ms < 0.2:
                continue

            if iv.ipc_server_tid and (iv.ipc_server_pid, iv.ipc_server_tid) in session.threads:
                srv_candidates = [session.threads[(iv.ipc_server_pid, iv.ipc_server_tid)]]
            else:
                srv_candidates = [
                    st for (p, _), st in session.threads.items()
                    if p == iv.ipc_server_pid and st.prio > 0 and (p, st.tid) != (th.pid, th.tid)
                ]

            for st in srv_candidates:
                matched_s_iv = None
                for s_iv in st.intervals:
                    ov_s = max(iv.start_ms, s_iv.start_ms)
                    ov_e = min(iv.end_ms, s_iv.end_ms)
                    if (ov_e - ov_s) > 0.1 and 0 < s_iv.prio < iv.prio and s_iv.state in ("THREADY", "THRUNNING"):
                        matched_s_iv = s_iv
                        break

                if not matched_s_iv:
                    continue

                edge1 = CausalEdge(
                    relation="IPC_CHANNEL_PRIORITY_DROP",
                    victim_thread=th.display_name,
                    culprit_thread=st.display_name,
                    mechanism=f"THREPLY wait on server at prio {matched_s_iv.prio} (< client prio {iv.prio})",
                    duration_ms=iv.duration_ms,
                    start_ms=iv.start_ms,
                    end_ms=iv.end_ms,
                    confidence=Confidence.CONFIRMED,
                    evidence=[
                        _iv_to_evidence(session, th, iv, "client THREPLY"),
                        _iv_to_evidence(session, st, matched_s_iv, f"server prio {matched_s_iv.prio}"),
                    ],
                )
                chain = [edge1]

                preemptors = _find_medium_preemptors(
                    session, th, iv.prio, st, iv.start_ms, iv.end_ms
                )
                for m_name, (m_th, m_prio, m_dur, m_evs) in preemptors.items():
                    chain.append(
                        CausalEdge(
                            relation="PREEMPTED_BY_MEDIUM_THREAD",
                            victim_thread=st.display_name,
                            culprit_thread=m_name,
                            mechanism=f"strict priority preemption (prio {m_prio} > server prio {matched_s_iv.prio})",
                            duration_ms=m_dur,
                            start_ms=iv.start_ms,
                            end_ms=iv.end_ms,
                            confidence=Confidence.CONFIRMED,
                            evidence=m_evs,
                        )
                    )

                preempt_desc = (
                    f" and was preempted by {', '.join(preemptors.keys())}"
                    if preemptors
                    else ""
                )
                findings.append(
                    CausalFinding(
                        finding_type=FindingType.PRIORITY_INVERSION_IPC,
                        severity="FAIL",
                        rule_name="logical_checks.priority_inversion",
                        victim_thread=th.display_name,
                        summary=(
                            f"{th.display_name} blocked in THREPLY for {iv.duration_ms:.3f} ms while server "
                            f"{st.display_name} executed at uninherited priority {matched_s_iv.prio}{preempt_desc}."
                        ),
                        suggested_fix=(
                            "Remove _NTO_CHF_FIXED_PRIORITY from ChannelCreate() so the server inherits "
                            "client priority, or decouple with non-blocking MsgSendPulse()."
                        ),
                        confidence=Confidence.CONFIRMED,
                        start_ms=iv.start_ms,
                        end_ms=iv.end_ms,
                        duration_ms=iv.duration_ms,
                        causal_chain=chain,
                    )
                )
                break
    return findings


def _detect_semaphore_inversions(session: TraceSession, config: RuleConfig) -> List[CausalFinding]:
    """
    Task 3: Separate legitimate semaphore signalling (where poster prio >= waiter prio, no finding)
    from lock-like / inverted semaphore waits (where high-priority waiter blocks in THSEM
    waiting for a lower-priority poster, with multi-hop medium-thread preemption attribution).
    """
    findings: List[CausalFinding] = []
    for th in session.threads.values():
        if not _is_target_workload_thread(th, config) or th.prio < 25:
            continue
        for iv in th.intervals:
            if iv.state != "THSEM" or iv.duration_ms <= 0.5:
                continue

            poster_th: Optional[ThreadTimeline] = None
            confidence = Confidence.CONFIRMED
            if iv.owner_key and iv.owner_key in session.threads:
                poster_th = session.threads[iv.owner_key]
            else:
                candidates = [
                    other for (p, t), other in session.threads.items()
                    if p == th.pid and t != th.tid and other.prio < iv.prio
                ]
                if candidates:
                    poster_th = min(candidates, key=lambda c: c.prio)
                    confidence = Confidence.INFERRED

            # If a higher-or-equal priority thread posted the semaphore, that's legitimate signalling!
            if not poster_th or poster_th.prio >= iv.prio:
                continue

            culprit_label = poster_th.display_name
            edge1 = CausalEdge(
                relation="BLOCKED_ON_SEMAPHORE",
                victim_thread=th.display_name,
                culprit_thread=culprit_label,
                mechanism=f"semaphore {iv.sync_p or 'unknown'} (QNX semaphores have no priority inheritance)",
                duration_ms=iv.duration_ms,
                start_ms=iv.start_ms,
                end_ms=iv.end_ms,
                confidence=confidence,
                evidence=[_iv_to_evidence(session, th, iv, f"sync_p={iv.sync_p}")],
            )
            chain = [edge1]

            preemptors = {}
            if poster_th:
                preemptors = _find_medium_preemptors(
                    session, th, iv.prio, poster_th, iv.start_ms, iv.end_ms
                )
                for m_name, (m_th, m_prio, m_dur, m_evs) in preemptors.items():
                    chain.append(
                        CausalEdge(
                            relation="PREEMPTED_BY_MEDIUM_THREAD",
                            victim_thread=poster_th.display_name,
                            culprit_thread=m_name,
                            mechanism=f"strict priority preemption (prio {m_prio} > poster prio {poster_th.prio})",
                            duration_ms=m_dur,
                            start_ms=iv.start_ms,
                            end_ms=iv.end_ms,
                            confidence=confidence,
                            evidence=m_evs,
                        )
                    )

            preempt_desc = (
                f" while {', '.join(preemptors.keys())} preempted {poster_th.name}"
                if preemptors and poster_th
                else ""
            )
            findings.append(
                CausalFinding(
                    finding_type=FindingType.PRIORITY_INVERSION_SEM,
                    severity="FAIL",
                    rule_name="logical_checks.semaphore_locks",
                    victim_thread=th.display_name,
                    summary=(
                        f"Real-time thread {th.display_name} blocked in THSEM on {iv.sync_p} "
                        f"for {iv.duration_ms:.3f} ms waiting on lower-priority {culprit_label}{preempt_desc}."
                    ),
                    suggested_fix="Replace shared-resource semaphore with a pthread_mutex_t configured with PTHREAD_PRIO_INHERIT.",
                    confidence=confidence,
                    start_ms=iv.start_ms,
                    end_ms=iv.end_ms,
                    duration_ms=iv.duration_ms,
                    causal_chain=chain,
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
                    hog_groups: Dict[str, Tuple[int, float, List[EvidenceRef]]] = {}
                    for runner_th, run_iv in session.cpu_runner_at(iv.cpu, iv.start_ms, iv.end_ms):
                        if runner_th.pid == 1 and runner_th.tid in (1, 2):
                            continue
                        ov = min(iv.end_ms, run_iv.end_ms) - max(iv.start_ms, run_iv.start_ms)
                        key = runner_th.display_name_at(run_iv.prio)
                        prev_prio, prev_dur, prev_evs = hog_groups.get(key, (run_iv.prio, 0.0, []))
                        cand_ev = _iv_to_evidence(session, runner_th, run_iv)
                        if len(prev_evs) < 4 and all(e.line_no != cand_ev.line_no for e in prev_evs):
                            prev_evs.append(cand_ev)
                        hog_groups[key] = (max(prev_prio, run_iv.prio), prev_dur + ov, prev_evs)

                    edges = [
                        CausalEdge(
                            relation="CPU_OCCUPIED_BY",
                            victim_thread=th.display_name,
                            culprit_thread=culprit_label,
                            mechanism=f"CPU:{iv.cpu:02d} interference (hog prio {h_prio} vs victim prio {iv.prio})",
                            duration_ms=h_dur,
                            start_ms=iv.start_ms,
                            end_ms=iv.end_ms,
                            confidence=Confidence.CONFIRMED,
                            evidence=h_evs,
                        )
                        for culprit_label, (h_prio, h_dur, h_evs) in hog_groups.items()
                    ]
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
                            suggested_fix="Lower CPU hog priority below critical thread or isolate core affinity (ThreadCtl/on -C).",
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
                        else ("CPU execution", job.exec_ms)
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
                            suggested_fix="Inspect causal blockers (mutex/semaphore inversion, IPC wait, or CPU hog) during this job window.",
                            confidence=Confidence.INFERRED if job.inferred else Confidence.CONFIRMED,
                            start_ms=job.release_ms,
                            end_ms=job.completion_ms,
                            duration_ms=job.response_time_ms,
                        )
                    )

        if rule.max_lateness_ms is not None:
            limit = rule.max_lateness_ms*margin_mult
            for job in th.jobs:
                if job.start_lateness_ms > limit:
                    preempt_causes = {
                        k:v for k, v in job.blocker_attribution.items() if k.startswith("cpu_preempt:")
                    }
                    top_culprit = (
                        max(preempt_causes.items(), key=lambda kv: kv[1])
                        if preempt_causes else ("THREADY dispatch delay", job.start_lateness_ms)
                    )
                    findings.append(
                        CausalFinding(
                            finding_type=FindingType.LATENESS_MISS,
                            severity="FAIL",
                            rule_name=rule.name,
                            victim_thread=th.display_name,
                            summary=(
                                f"Job #{job.job_index} of {th.display_name} started {job.start_lateness_ms:.3f} ms "
                                f"after release (lateness limit: {limit:.3f} ms). Primary delay source: "
                                f"{top_culprit[0]} ({top_culprit[1]:.3f} ms)."
                            ),
                            suggested_fix="Raise thread priority above interfering runnable threads or pin to an isolated CPU core.",
                            confidence=Confidence.INFERRED if job.inferred else Confidence.CONFIRMED,
                            start_ms=job.release_ms,
                            end_ms=job.start_running_ms,
                            duration_ms=job.start_lateness_ms,
                        )
                    )
    return findings


@dataclass
class RunProfile:
    """Task 10: Summarizes one or more TraceSessions into a statistical + causal attribution profile."""
    thread_name: str
    run_count: int
    job_count: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    worst_ms: float
    run_to_run_spread_ms: float
    exec_avg_ms: float
    blocking_max_ms: float
    preempted_max_ms: float
    ipc_wait_max_ms: float
    blocker_attribution: Dict[str, float] = field(default_factory=dict)


def _pct(vals: List[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = min(len(s) - 1, int(round((q / 100.0) * (len(s) - 1))))
    return s[idx]


def summarize_profile(
    sessions: Union[TraceSession, Sequence[TraceSession]],
    thread_name: str,
) -> RunProfile:
    sess_list = [sessions] if isinstance(sessions, TraceSession) else list(sessions)
    all_jobs = []
    per_run_p99s = []
    causes: Dict[str, float] = {}

    for s in sess_list:
        t_list = s.find_threads_by_name(thread_name)
        r_jobs = [j for th in t_list for j in th.jobs]
        if r_jobs:
            per_run_p99s.append(_pct([j.response_time_ms for j in r_jobs], 99))
            all_jobs.extend(r_jobs)
            for j in r_jobs:
                for k, v in j.blocker_attribution.items():
                    causes[k] = max(causes.get(k, 0.0), v)

    resps = [j.response_time_ms for j in all_jobs]
    spread = (max(per_run_p99s) - min(per_run_p99s)) if len(per_run_p99s) >= 2 else 0.0
    return RunProfile(
        thread_name=thread_name,
        run_count=len(sess_list),
        job_count=len(all_jobs),
        p50_ms=_pct(resps, 50),
        p95_ms=_pct(resps, 95),
        p99_ms=_pct(resps, 99),
        worst_ms=max(resps) if resps else 0.0,
        run_to_run_spread_ms=spread,
        exec_avg_ms=(sum(j.exec_ms for j in all_jobs) / len(all_jobs)) if all_jobs else 0.0,
        blocking_max_ms=max((j.mutex_blocked_ms + j.sem_blocked_ms + j.ipc_blocked_ms) for j in all_jobs) if all_jobs else 0.0,
        preempted_max_ms=max(j.ready_preempted_ms for j in all_jobs) if all_jobs else 0.0,
        ipc_wait_max_ms=max(j.ipc_blocked_ms for j in all_jobs) if all_jobs else 0.0,
        blocker_attribution=causes,
    )


@dataclass
class RegressionReport:
    thread_name: str
    baseline_p99_ms: float
    candidate_p99_ms: float
    ratio: float
    delta_ms: float
    noise_floor_ms: float
    is_regression: bool
    severity: str
    primary_new_blocker: str
    attribution_summary: str


def compare_runs(
    baseline: Union[TraceSession, Sequence[TraceSession]],
    candidate: Union[TraceSession, Sequence[TraceSession]],
    thread_name: str,
    threshold_ratio: float = 1.25,
) -> RegressionReport:
    """
    Task 11: Compares baseline vs. candidate profiles using run-to-run variability
    and attributes regressions to the exact new blocking mechanism.
    """
    b_prof = summarize_profile(baseline, thread_name)
    c_prof = summarize_profile(candidate, thread_name)

    b_p99 = b_prof.p99_ms
    c_p99 = c_prof.p99_ms
    ratio = (c_p99 / b_p99) if b_p99 > 0 else (999.0 if c_p99 > 0 else 1.0)
    delta = c_p99 - b_p99
    noise_floor = max(0.5, b_prof.run_to_run_spread_ms * 1.5, c_prof.run_to_run_spread_ms * 1.5)

    diffs: Dict[str, float] = {}
    for k, c_val in c_prof.blocker_attribution.items():
        diffs[k] = c_val - b_prof.blocker_attribution.get(k, 0.0)

    if diffs:
        top_cause, top_diff = max(diffs.items(), key=lambda kv: kv[1])
    else:
        top_cause, top_diff = ("CPU execution/preemption", delta)

    is_reg = (ratio >= threshold_ratio) and (delta > noise_floor)
    if not is_reg:
        severity = "NONE"
    elif ratio >= 2.0 or delta >= 5.0:
        severity = "CRITICAL"
    else:
        severity = "MODERATE"

    summary = (
        f"[{severity}] Thread '{thread_name}' p99 response time changed from {b_p99:.3f} ms "
        f"to {c_p99:.3f} ms ({ratio:.2f}x, {delta:+.3f} ms; noise floor: ±{noise_floor:.3f} ms). "
        f"Primary attributed cause: {top_cause} ({top_diff:+.3f} ms)."
    )
    return RegressionReport(
        thread_name=thread_name,
        baseline_p99_ms=b_p99,
        candidate_p99_ms=c_p99,
        ratio=ratio,
        delta_ms=delta,
        noise_floor_ms=noise_floor,
        is_regression=is_reg,
        severity=severity,
        primary_new_blocker=top_cause,
        attribution_summary=summary,
    )