# Aion — Automated Real-Time Timing Verification & Causal RCA for QNX Neutrino 8.0

Aion is a command-line verification tool and Python SDK for QNX Neutrino RTOS (`tracelogger` / `traceprinter`) kernel traces. It automatically extracts per-thread job timing metrics (`p50`, `p95`, `p99`, `Max`, `Jitter`), verifies declarative TOML timing and logical scheduling contracts, and constructs multi-hop **Causal Root-Cause Analysis (RCA) Chains** pinpointing exact trace line numbers, timestamps, and remediation steps.

---

## Key Capabilities

1. **Single-Pass Kernel Trace Ingestion (`aion/ingest.py`)**
   * Reconstructs exact state timelines (`THRUNNING`, `THREADY`, `THMUTEX`, `THSEM`, `THREPLY`, `THSEND`, `THNANOSLEEP`) across SMP cores.
   * Supports both explicit `TraceEvent` user markers (`tg_job_begin:<thread>` / `tg_job_end:<thread>`) and automatic timer-wakeup job boundary inference (`*`).
   * Correlates `KER_CALL:SYNC_MUTEX_LOCK`, `KER_CALL:SYNC_SEM_WAIT`, `KER_CALL:SYNC_SEM_POST`, and `COMM:SND_MESSAGE` / `REPLY_MESSAGE` events to their exact owner, poster, or IPC server threads.

2. **Multi-Hop Causal RCA Detectors (`aion/analyzers.py`)**
   * **`PRIORITY_INVERSION_MUTEX`**: Detects high-priority threads blocked in `THMUTEX` on lower-priority lock holders preempted by medium-priority threads.
   * **`PRIORITY_INVERSION_SEMAPHORE`**: Distinguishes legitimate top-down producer-consumer signaling (`poster_prio >= waiter_prio`) from inverted semaphore waits (`poster_prio < waiter_prio`) where QNX counting semaphores lack priority inheritance.
   * **`PRIORITY_INVERSION_IPC`**: Detects synchronous `MsgSend` (`THREPLY`) priority drops into channels configured with `_NTO_CHF_FIXED_PRIORITY` and traces medium-priority preemptors of the server thread.
   * **`FORBIDDEN_BLOCKING_IPC`**: Flags real-time threads performing synchronous IPC (`MsgSend`, `printf`, `slogf`, filesystem I/O) inside time-critical loops.
   * **`DEADLINE_MISS`, `SCHED_LATENCY_MISS`, & `LATENESS_MISS`**: Verifies wall-clock response time, `THREADY` scheduling latency, and release-to-start lateness with configurable QEMU/hardware margin multipliers.

3. **Statistical Regression Comparator (`aion diff`)**
   * Compares baseline vs. candidate trace runs for a target thread, accounts for run-to-run noise floors, and attributes `p99` response time regressions to the exact new blocking mechanism (`ipc:<server>`, `semaphore:<poster>`, `mutex:<holder>`, or `cpu_preempt:<hog>`).

---

## Quick Start

### Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pip install pytest