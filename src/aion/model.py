"""
    Aion Core Data Model
        Provides reusable dataclasses for trace events, thread timelines, navigable causal RCA graphs
        with evidence links, and job/regression profiles.
"""

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, List, Optional, Tuple

class Confidence(str, Enum):
    CONFIRMED = "confirmed" # proven by kernel trace IDs/owners
    INFERRED = "inferred"   # inferred from recent lock/post history
    POSSIBLE = "possible"   # structural risk or incomplete trace evidence

class FindingType(str, Enum):
    PRIORITY_INVERSION_MUTEX = "priority_inversion_mutex"
    PRIORITY_INVERSION_SEM = "priority_inversion_semaphore"
    PRIORITY_INVERSION_IPC = "priority_inversion_ipc"
    FORBIDDEN_BLOCKING_IPC = "forbidden_blocking_ipc"

    DEADLINE_MISS = "deadline_miss"
    LATENESS_MISS = "lateness_miss"
    SCHED_LATENCY_MISS = "sched_latency_miss"
    CPU_HOG = "cpu_hog"

@dataclass(frozen=True)
class EvidenceRef:
    # exact pointer to raw event in qnx kernel trace
    event_id: int
    line_no: int
    timestamp_cycles: int
    timestamp_ms: float
    cpu: int
    raw_event: str
    details: str

@dataclass
class CausalEdge:
    """
        A single directed link in RCA chain: victim --[mechanism (duration_ms)]--> culprit
        Example:
            Edge
    """
    relation: str
    victim_thread: str
    culprit_thread: str
    mechanism: str
    duration_ms: float
    start_ms: float
    end_ms: float
    confidence: Confidence
    evidence: List[EvidenceRef] = field(default_factory=list)

@dataclass
class CausalFinding:
    # complete RCA diagnosis with navigable causal chain and fix recommendation
    finding_type: FindingType
    severity: str
    rule_name: Optional[str]
    victim_thread: str
    summary: str
    suggested_fix: str
    confidence: Confidence
    start_ms: float
    end_ms: float
    duration_ms: float
    causal_chain: List[CausalEdge] = field(default_factory=list)

    def format_chain_ascii(self) -> str:
        # renders navigable causal graph with evidence timestamps and line numbers
        lines = [
            f"[{self.severity}] {self.finding_type.value.upper()} ({self.confidence.value})"
            f" | Window: {self.start_ms:.3f} ms -> {self.end_ms:.3f} ms ({self.duration_ms:.3f} ms)",
            f"  Summary: {self.summary}",
        ]

        if self.causal_chain:
            lines.append(" Causal Graph (RCA Chain):")
            for idx, edge in enumerate(self.causal_chain, 1):
                lines.append(
                    f"    Step {idx}: {edge.victim_thread} --[{edge.relation}: {edge.mechanism} "
                    f"for {edge.duration_ms:.3f} ms]--> {edge.culprit_thread}"
                )
                for ev in edge.evidence:
                    lines.append(
                        f"      * Evidence [line {ev.line_no}, t={ev.timestamp_ms:.3f}ms "
                        f"({ev.timestamp_cycles:#x}), CPU:{ev.cpu:02d}]: {ev.raw_event} ({ev.details})"
                    )
        lines.append(f"    Suggested Fix: {self.suggested_fix}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["finding_type"] = self.finding_type.value
        data["confidence"] = self.confidence.value
        for edge in data["causal_chain"]:
            edge["confidence"] = edge["confidence"].value if hasattr(edge["confidence"], "value") else str(edge["confidence"])
        return data

@dataclass
class StateInterval:
    # contiguous time interval where thread remained in single state
    state: str                
    start_cycles: int
    end_cycles: int
    start_ms: float
    end_ms: float
    cpu: int
    prio: int
    policy: int
    start_event_id: int
    start_line_no: int
    end_event_id: int
    end_line_no: int
    sync_p: Optional[str] = None
    owner_key: Optional[Tuple[int, int]] = None
    ipc_server_pid: Optional[int] = None
    ipc_server_tid: Optional[int] = None

    @property
    def duration_ms(self) -> float:
        return max(0.0, self.end_ms - self.start_ms)

@dataclass
class JobRun:
    # single release-to-completion execution of periodic or marker-delimited thread
    job_index: int
    thread_name: str
    release_ms: float
    start_running_ms: float
    completion_ms: float
    inferred: bool = False
    exec_ms: float = 0.0
    ready_preempted_ms: float = 0.0
    mutex_blocked_ms: float = 0.0
    sem_blocked_ms: float = 0.0
    ipc_blocked_ms: float = 0.0
    other_blocked_ms: float = 0.0
    blocker_attribution: Dict[str, float] = field(default_factory=dict) 

    @property
    def start_lateness_ms(self) -> float:
        return max(0.0, self.start_running_ms - self.release_ms)

    @property
    def response_time_ms(self) -> float:
        return max(0.0, self.completion_ms - self.release_ms)

@dataclass
class ThreadTimeline:
    pid: int
    tid: int
    name: str = ""
    process_name: str = ""
    prio: int = 10
    policy: int = 2
    intervals: List[StateInterval] = field(default_factory=list)
    jobs: List[JobRun] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        t_label = self.name or f"tid-{self.tid}"
        p_label = self.process_name.split("/")[-1] if self.process_name else f"pid-{self.pid}"
        return f"{t_label} ({p_label}#{self.tid}, prio:{self.prio})"

    def display_name_at(self, prio: Optional[int] = None) -> str:
        t_label = self.name or f"tid-{self.tid}"
        p_label = self.process_name.split("/")[-1] if self.process_name else f"pid-{self.pid}"
        effective_prio = self.prio if prio is None else prio
        return f"{t_label} ({p_label}#{self.tid}, prio:{effective_prio})"