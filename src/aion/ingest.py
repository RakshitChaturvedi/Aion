import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

@dataclass
class StateInterval:
    state: str
    start_cycles: int
    end_cycles: int
    cpu: int
    prio: int
    sync_p: Optional[str] = None

    @property
    def duration_cycles(self) -> int:
        return max(0, self.end_cycles - self.start_cycles)

@dataclass
class ThreadTimeline:
    pid: int
    tid: int
    name: str = ""
    prio: int = 0
    policy: int = 0
    current_state: Optional[str] = None
    state_since: int = 0
    current_cpu: int = 0
    blocked_sync_p: Optional[str] = None
    intervals: List[StateInterval] = field(default_factory=list)

def parse_traceprinter_file(filepath: str):
    cycles_per_sec = 2495955300
    processes: Dict[int, str] = {}
    threads: Dict[Tuple[int, int], ThreadTimeline] = {}
    mutex_waiters: Dict[Tuple[int, int], str] = {}
    mutex_owners: Dict[str, Tuple[int, int]] = {}
    markers: List[Tuple[int, str, str]] = []

    current_event = None
    kv_block: Dict[str, str] = {}

    def flush_multiline():
        nonlocal current_event, kv_block
        if not current_event:
            return
        t_cyc, cpu, cls, ev = current_event
        if cls == "PROCESS" and ev == "PROCCREATE_NAME":
            pid = int(kv_block.get("pid", 0))
            processes[pid] = kv_block.get("name", "")
        elif cls == "PROCESS" and ev == "PROCTHREAD_NAME":
            pid = int(kv_block.get("pid", 0))
            tid = int(kv_block.get("tid", 0))
            if (pid, tid) not in threads:
                threads[(pid, tid)] = ThreadTimeline(pid=pid, tid=tid)
            threads[(pid, tid)].name = kv_block.get("name", "")
        elif cls == "KER_CALL" and ev.startswith("SYNC_MUTEX_LOCK"):
            sync_p = kv_block.get("sync_p")
            owner_raw = int(kv_block.get("owner", "0"))
            owner_tid = owner_raw & 0xFFFF

            # find which thread curr thrunning on curr cpu
            for (p, t), th in threads.items():
                if th.current_state == "THRUNNING" and th.current_cpu == cpu:
                    if sync_p:
                        mutex_waiters[(p,t)] = sync_p
                        if owner_tid > 0:
                            mutex_owners[sync_p] = (p, owner_tid)
                    break
        current_event = None
        kv_block = {}

    with open(filepath, "r", errors="replace") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")
            if "TRACE_CYCLES_PER_SEC::" in line:
                m = re.search(r"(\d+)", line.split("::")[1])
                if m:
                    cycles_per_sec = int(m.group(1))
                continue
            if line.startswith("t:0x"):
                flush_multiline()
                m = re.match(
                    r"^t:0x([0-9a-fA-F]+)\s+CPU:(\d+)\s+(\S+)\s+:(\S+)\s*(.*)$",
                    line,
                )
                if not m:
                    continue
                t_cyc = int(m.group(1), 16)
                cpu = int(m.group(2))
                cls = m.group(3)
                ev = m.group(4)
                rest = m.group(5).strip()

                if cls == "THREAD" and ev.startswith("TH"):
                    kvs = dict(re.findall(r"(\w+):(\S+)", rest))
                    if "pid" in kvs and "tid" in kvs:
                        pid, tid = int(kvs["pid"]), int(kvs["tid"])
                        th = threads.setdefault((pid, tid), ThreadTimeline(pid=pid, tid=tid))
                        if "priority" in kvs:
                            th.prio = int(kvs["priority"])
                        if "policy" in kvs:
                            th.policy = int(kvs["policy"])
                        if ev not in ("THCREATE", "THDESTROY"):
                            if th.current_state is not None:
                                th.intervals.append(
                                    StateInterval(
                                        state=th.current_state,
                                        start_cycles=th.state_since,
                                        end_cycles=t_cyc,
                                        cpu=th.current_cpu,
                                        prio=th.prio,
                                        sync_p=th.blocked_sync_p if th.current_state == "THMUTEX" else None,
                                    )
                                )
                            th.current_state = ev
                            th.state_since = t_cyc
                            th.current_cpu = cpu
                            if ev == "THMUTEX":
                                th.blocked_sync_p = mutex_waiters.get((pid, tid))
                elif cls == "USREVENT":
                    markers.append((t_cyc, ev, rest))
                else:
                    current_event = (t_cyc, cpu, cls, ev)
                    kv_block = dict(re.findall(r"(\w+):(\S+)", rest))
            elif current_event and line.startswith(" "):
                k_m = re.match(r"^\s*([\w\->\[\]]+):(.*)$", line)
                if k_m:
                    kv_block[k_m.group(1).strip()] = k_m.group(2).strip()

    flush_multiline()
    return cycles_per_sec, processes, threads, mutex_owners, markers

if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "traces/inversion_spike_clean.txt"
    cps, procs, threads, owners, markers = parse_traceprinter_file(path)
    print(f"Parsed trace: {path} (cycles/sec = {cps})")
    target_pids = [pid for pid, name in procs.items() if "spike_inversion" in name]
    for pid in target_pids:
        print(f"\n=== Process pid:{pid} ({procs[pid]}) ===")
        for (p, tid), th in sorted(threads.items()):
            if p != pid:
                continue
            label = th.name or f"main/tid-{tid}"
            print(f"  Thread tid:{tid} ({label}) | prio:{th.prio} | intervals:{len(th.intervals)}")
            for iv in th.intervals:
                ms = (iv.duration_cycles / cps) * 1000.0
                extra = f" [sync_p={iv.sync_p}]" if iv.sync_p else ""
                print(f"    - {iv.state:<12} on CPU:{iv.cpu:02d} | {ms:8.3f} ms (prio {iv.prio}){extra}")
    if markers:
        print("\n=== User Markers ===")
        for cyc, ev, rest in markers:
            print(f"  t={cyc:#x} {ev} {rest}")