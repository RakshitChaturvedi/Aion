import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from aion.model import EvidenceRef, JobRun, StateInterval, ThreadTimeline

def _unswap_qnx_usrstr(s: str) -> str:
    # fix qnx 8 x86_64 traceprinter byte-order on USRSTREVENT strings where middle 32-bit words
    # have adjacent chars swapped
    return s.replace("sesnro", "sensor")

@dataclass
class UserMarker:
    event_id: int
    line_no: int
    timestamp_cycles: int
    timestamp_ms: float
    cpu: int
    code: int
    payload: str

@dataclass
class TraceSession:
    # primary reusable sdk obj representing parsed qnx kernel trace. 
    filepath: str
    cycles_per_sec: int
    num_cpus: int
    trace_data: str = ""
    tracelogger_args: str = ""
    base_cycles: int = 0
    processes: Dict[int, str] = field(default_factory=dict)
    threads: Dict[Tuple[int, int], ThreadTimeline]  = field(default_factory=dict)
    markers: List[UserMarker] = field(default_factory=list)
    malformed_lines: int = 0

    def cycles_to_ms(self, cycles: int) -> float:
        if self.base_cycles == 0:
            return 0.0
        return ((cycles - self.base_cycles) / self.cycles_per_sec) * 1000.0

    def find_threads_by_name(self, name: str ) -> List[ThreadTimeline]:
        # find all threads matching given thread name or process name
        results = []
        for thread in self.threads.values():
            if thread.name == name or thread.process_name.endswith(f"/{name}") or thread.process_name == name:
                results.append(thread)
        return results

    def cpu_runner_at(self, cpu: int, start_ms: float, end_ms: float) -> List[Tuple[ThreadTimeline, StateInterval]]:
        # return all (ThreadTimeline, StateIntervel) that were THRUNNING on cpu during [start_ms, end_ms]
        runners = []
        for thread in self.threads.values():
            for iv in thread.intervals:
                if iv.state == "THRUNNING" and iv.cpu == cpu:
                    overlap_start = max(start_ms, iv.start_ms)
                    overlap_end = min(end_ms, iv.end_ms)

                    if overlap_end > overlap_start:
                        runners.append((thread, iv))
        return runners

def parse_traceprinter_file(filepath: str) -> TraceSession:
    session = TraceSession(filepath=filepath, cycles_per_sec=2495955300, num_cpus=1)

    # active tracking state during single-pass stream ingestion
    open_state: Dict[Tuple[int, int], dict] = {}
    cpu_running: Dict[int, Tuple[int, int]] = {}
    pending_mutex_lock: Dict[Tuple[int, int], Tuple[str, Optional[Tuple[int, int]], EvidenceRef]] = {}
    pending_sem_wait: Dict[Tuple[int, int], Tuple[str, EvidenceRef]] = {}
    pending_ipc_target: Dict[Tuple[int, int], Tuple[int, Optional[int], EvidenceRef]] = {}
    active_mutex_owners: Dict[str, Tuple[int, int]] = {}

    event_counter = 0
    current_event = None
    kv_block: Dict[str, str] = {}

    def get_thread(pid: int, tid: int) -> ThreadTimeline:
        key = (pid, tid)
        if key not in session.threads:
            session.threads[key] = ThreadTimeline(pid=pid, tid=tid, process_name=session.processes.get(pid, ""))
        elif not session.threads[key].process_name and pid in session.processes:
            session.threads[key].process_name = session.processes[pid]
        return session.threads[key]

    def flush_multiline():
        nonlocal current_event, kv_block
        if not current_event:
            return
        ev_id, line_no, t_cyc, cpu, cls, ev = current_event
        t_ms = session.cycles_to_ms(t_cyc)

        if cls == "PROCESS" and ev == "PROCCREATE_NAME":
            pid = int(kv_block.get("pid", 0))
            pname = kv_block.get("name", "")
            session.processes[pid] = pname

            for (p, _), th in session.threads.items():
                if p == pid:
                    th.process_name = pname

        elif cls == "PROCESS" and ev == "PROCTHREAD_NAME":
            pid = int(kv_block.get("pid", 0))
            tid = int(kv_block.get("tid", 0))
            th = get_thread(pid, tid)
            th.name = kv_block.get("name", "")

        elif cls == "KER_CALL" and ev.startswith("SYNC_MUTEX_LOCK"):
            sync_p = kv_block.get("sync_p")
            owner_raw = int(kv_block.get("owner", "0"))
            owner_tid = owner_raw & 0xFFFF
            runner_key = cpu_running.get(cpu)

            if runner_key and sync_p:
                pid, tid = runner_key
                owner_key = (pid, owner_tid) if owner_tid > 0 else active_mutex_owners.get(sync_p)

                if owner_key:
                    active_mutex_owners[sync_p] = owner_key

                ev_ref = EvidenceRef(
                    event_id=ev_id,
                    line_no=line_no,
                    timestamp_cycles=t_cyc,
                    timestamp_ms=t_ms,
                    cpu=cpu,
                    raw_event=f"{cls}:{ev}",
                    details=f"sync_p={sync_p} owner_tid={owner_key[1] if owner_key else 'unknown'}",
                )
                pending_mutex_lock[runner_key] = (sync_p, owner_key, ev_ref)

        elif cls == "KER_CALL" and ev.startswith("SYNC_MUTEX_UNLOCK"):
            sync_p = kv_block.get("sync_p")
            if sync_p:
                active_mutex_owners.pop(sync_p, None)

        elif cls == "KER_CALL" and ev.startswith("SYNC_SEM_WAIT"):
            sync_p = kv_block.get("sync_p")
            runner_key = cpu_running.get(cpu)

            if runner_key and sync_p:
                ev_ref = EvidenceRef(
                    event_id=ev_id,
                    line_no=line_no,
                    timestamp_cycles=t_cyc,
                    timestamp_ms=t_ms,
                    cpu=cpu,
                    raw_event=f"{cls}:{ev}",
                    details=f"sync_p={sync_p}",
                )
                pending_sem_wait[runner_key] = (sync_p, ev_ref)

        current_event = None
        kv_block = {}

    with open(filepath, "r", errors="replace") as f:
        for line_no, raw_line in enumerate(f, 1):
            line = raw_line.rstrip("\n")
            if "TRACE_CYCLES_PER_SEC::" in line:
                m = re.search(r"(\d+)", line.split("::")[1])
                if m:
                    session.cycles_per_sec = int(m.group(1))
                continue
            if "TRACE_CPU_NUM::" in line:
                m = re.search(r"(\d+)", line.split("::")[1])
                if m:
                    session.num_cpus = int(m.group(1))
                continue
            if "TRACE_DATE::" in line:
                session.trace_date = line.split("::", 1)[1].strip()
                continue
            if "TRACE_TRACELOGGER_ARGS::" in line:
                session.tracelogger_args = line.split("::", 1)[1].strip()
                continue

            if line.startswith("t:0x"):
                flush_multiline()
                m = re.match(
                    r"^t:0x([0-9a-fA-F]+)\s+CPU:(\d+)\s+(\S+)\s+:(\S+)\s*(.*)$",
                    line,
                )
                if not m:
                    session.malformed_lines += 1
                    continue

                event_counter += 1
                t_cyc = int(m.group(1), 16)
                if session.base_cycles == 0:
                    session.base_cycles = t_cyc
                t_ms = session.cycles_to_ms(t_cyc)
                cpu = int(m.group(2))
                cls = m.group(3)
                ev = m.group(4)
                rest = m.group(5).strip()

                if cls == "THREAD" and ev.startswith("TH"):
                    kvs = dict(re.findall(r"(\w+):(\S+)", rest))
                    if "pid" in kvs and "tid" in kvs:
                        pid, tid = int(kvs["pid"]), int(kvs["tid"])
                        key = (pid, tid)
                        th = get_thread(pid, tid)
                        if "priority" in kvs:
                            th.prio = int(kvs["priority"])
                        if "policy" in kvs:
                            th.policy = int(kvs["policy"])

                        if ev not in ("THCREATE", "THDESTROY"):
                            # Close previous state interval if open
                            if key in open_state:
                                prev = open_state[key]
                                th.intervals.append(
                                    StateInterval(
                                        state=prev["state"],
                                        start_cycles=prev["start_cycles"],
                                        end_cycles=t_cyc,
                                        start_ms=prev["start_ms"],
                                        end_ms=t_ms,
                                        cpu=prev["cpu"],
                                        prio=prev["prio"],
                                        policy=prev["policy"],
                                        start_event_id=prev["event_id"],
                                        start_line_no=prev["line_no"],
                                        end_event_id=event_counter,
                                        end_line_no=line_no,
                                        sync_p=prev.get("sync_p"),
                                        owner_key=prev.get("owner_key"),
                                        ipc_server_pid=prev.get("ipc_server_pid"),
                                        ipc_server_tid=prev.get("ipc_server_tid"),
                                    )
                                )

                            # Open new state interval
                            new_state_info = {
                                "state": ev,
                                "start_cycles": t_cyc,
                                "start_ms": t_ms,
                                "cpu": cpu,
                                "prio": th.prio,
                                "policy": th.policy,
                                "event_id": event_counter,
                                "line_no": line_no,
                            }
                            if ev == "THRUNNING":
                                cpu_running[cpu] = key
                            elif ev == "THMUTEX" and key in pending_mutex_lock:
                                sync_p, owner_key, _ = pending_mutex_lock.pop(key)
                                new_state_info["sync_p"] = sync_p
                                new_state_info["owner_key"] = owner_key
                            elif ev == "THSEM" and key in pending_sem_wait:
                                sync_p, _ = pending_sem_wait.pop(key)
                                new_state_info["sync_p"] = sync_p
                            elif ev == "THREPLY" and key in pending_ipc_target:
                                srv_pid, srv_tid, _ = pending_ipc_target.pop(key)
                                new_state_info["ipc_server_pid"] = srv_pid
                                new_state_info["ipc_server_tid"] = srv_tid

                            open_state[key] = new_state_info

                elif cls == "COMM" and ev == "SND_MESSAGE":
                    kvs = dict(re.findall(r"(\w+):(\S+)", rest))
                    srv_pid = int(kvs["pid"]) if "pid" in kvs else None
                    runner_key = cpu_running.get(cpu)
                    if runner_key and srv_pid is not None:
                        ev_ref = EvidenceRef(
                            event_id=event_counter,
                            line_no=line_no,
                            timestamp_cycles=t_cyc,
                            timestamp_ms=t_ms,
                            cpu=cpu,
                            raw_event=f"{cls}:{ev}",
                            details=rest,
                        )
                        pending_ipc_target[runner_key] = (srv_pid, None, ev_ref)

                elif cls == "USREVENT":
                    code_m = re.search(r"EVENT:(\d+)", ev)
                    str_m = re.search(r'STR:"([^"]*)"', rest)
                    code = int(code_m.group(1)) if code_m else 0
                    payload = _unswap_qnx_usrstr(str_m.group(1)) if str_m else rest
                    session.markers.append(
                        UserMarker(
                            event_id=event_counter,
                            line_no=line_no,
                            timestamp_cycles=t_cyc,
                            timestamp_ms=t_ms,
                            cpu=cpu,
                            code=code,
                            payload=payload,
                        )
                    )
                else:
                    current_event = (event_counter, line_no, t_cyc, cpu, cls, ev)
                    kv_block = dict(re.findall(r"(\w+):(\S+)", rest))
            elif current_event and line.startswith(" "):
                k_m = re.match(r"^\s*([\w\->\[\]]+):(.*)$", line)
                if k_m:
                    kv_block[k_m.group(1).strip()] = k_m.group(2).strip()

    flush_multiline()
    _extract_jobs(session)
    return session

def _extract_jobs(session: TraceSession) -> None:
    """
    Extracts JobRun instances for threads using either explicit TraceEvent markers
    (tg_job_begin:<thread> / tg_job_end:<thread>) or timer-wakeup inference
    (THNANOSLEEP -> THREADY/THRUNNING -> THNANOSLEEP/THDEAD).
    """
    # 1. Explicit marker-based extraction
    marker_begins: Dict[str, UserMarker] = {}
    for mk in session.markers:
        if mk.code == 100 or mk.payload.startswith("tg_job_begin:"):
            tname = mk.payload.split(":")[-1].strip()
            marker_begins[tname] = mk
        elif mk.code == 101 or mk.payload.startswith("tg_job_end:"):
            tname = mk.payload.split(":")[-1].strip()
            if tname in marker_begins:
                b_mk = marker_begins.pop(tname)
                for th in session.find_threads_by_name(tname):
                    _build_job_metrics(session, th, b_mk.timestamp_ms, mk.timestamp_ms, inferred=False)

    # 2. Inferred timer-release extraction for threads without explicit markers
    for th in session.threads.values():
        if th.jobs or not th.intervals:
            continue
        in_job = False
        rel_ms = 0.0
        first_run_ms = 0.0
        for iv in th.intervals:
            if not in_job and iv.state in ("THREADY", "THRUNNING"):
                in_job = True
                rel_ms = iv.start_ms
                first_run_ms = iv.start_ms if iv.state == "THRUNNING" else 0.0
            elif in_job:
                if iv.state == "THRUNNING" and first_run_ms == 0.0:
                    first_run_ms = iv.start_ms
                elif iv.state in ("THNANOSLEEP", "THDEAD", "THJOIN"):
                    _build_job_metrics(
                        session,
                        th,
                        rel_ms,
                        iv.start_ms,
                        inferred=True,
                        first_run_override=first_run_ms or rel_ms,
                    )
                    in_job = False


def _build_job_metrics(
    session: TraceSession,
    th: ThreadTimeline,
    rel_ms: float,
    comp_ms: float,
    inferred: bool,
    first_run_override: Optional[float] = None,
) -> None:
    first_run = first_run_override if first_run_override is not None else rel_ms
    job = JobRun(
        job_index=len(th.jobs) + 1,
        thread_name=th.name or f"tid-{th.tid}",
        release_ms=rel_ms,
        start_running_ms=first_run,
        completion_ms=comp_ms,
        inferred=inferred,
    )
    for iv in th.intervals:
        ov_start = max(rel_ms, iv.start_ms)
        ov_end = min(comp_ms, iv.end_ms)
        if ov_end <= ov_start:
            continue
        dur = ov_end - ov_start
        if iv.state == "THRUNNING":
            job.exec_ms += dur
        elif iv.state == "THREADY":
            job.ready_preempted_ms += dur
        elif iv.state == "THMUTEX":
            job.mutex_blocked_ms += dur
            if iv.owner_key and iv.owner_key in session.threads:
                owner_th = session.threads[iv.owner_key]
                key = f"mutex:{owner_th.display_name}"
                job.blocker_attribution[key] = job.blocker_attribution.get(key, 0.0) + dur
        elif iv.state == "THSEM":
            job.sem_blocked_ms += dur
        elif iv.state == "THREPLY":
            job.ipc_blocked_ms += dur
            srv_name = session.processes.get(iv.ipc_server_pid or 0, f"pid:{iv.ipc_server_pid}")
            key = f"ipc:{srv_name}"
            job.blocker_attribution[key] = job.blocker_attribution.get(key, 0.0) + dur
        else:
            job.other_blocked_ms += dur
    th.jobs.append(job)