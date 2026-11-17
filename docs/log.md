# Aion Project Log

## Sun Nov 8, 2026 (Phase 0: Setup)
- **What was done:**
  - Requested QNX SDP 8.0 Non-Commercial license via myQNX and sent follow-up email.
  - Verified Pop!_OS host supports `/dev/kvm` and installed `qemu-system-x86`.
  - Created repository structure (`src/aion/`, `samples/`, `traces/`, `tests/`, `ci/`, `docs/`).
  - Configured `pyproject.toml`, Python virtual environment, `pytest`, and `ruff`.
  - Experimented with a standalone Python prototype parsing mock `traceprinter` events to detect mutex priority inversion.
- **What broke / Blockers:**
  - Waiting on myQNX license delivery to download QNX SDP 8.0 and test `qcc`.
- **What is next (Mon Nov 9):**
  - Follow up on QNX license in Hackathon Discord if email hasn't arrived.
  - Install QNX SDP 8.0, source `qnxsdp-env.sh`, and compile `hello.c` with `qcc -Vgcc_ntox86_64` to pass the Phase 0 Gate.

## Mon Nov 9, 2026 (Phase 0 Setup & Phase 1 Feasibility Spike - Completed)

- **What was done:**
  - **Phase 0:**
    - Installed QNX Software Center (2.0.4) and QNX SDP 8.0 into `~/qnx800`.
    - Verified KVM virtualization support (`/dev/kvm` exists and `kvm-ok` passed) and set up the QEMU/KVM environment on Pop!_OS.
    - Initialized the public Git repository with standard directory layout (`src/timingguard/`, `samples/`, `traces/`, `tests/`, `ci/`, `docs/`).
    - Configured Python virtual environment, `pyproject.toml`, `pytest`, `ruff`, and verified a passing smoke test.
  - **Phase 1:**
    - Booted the QNX 8.0 x86_64 SMP instrumented microkernel (`procnto-smp-instr`) inside QEMU.
    - Verified host-to-target workflow by compiling `hello.c` with `qcc -Vgcc_ntox86_64`, copying it via `scp`, and executing it on the QNX target.
    - Captured real binary kernel traces (`.kev` files) using `tracelogger -w` (wide mode) under multi-core load.
    - Converted binary `.kev` traces to human-readable text logs using the host `traceprinter` tool.
    - Built and ran a multi-threaded priority inversion spike program (`spike_inversion.c`) featuring threads with explicit `SCHED_FIFO` priorities (10, 25, 40) and mutex contention.
    - Developed and tested the Phase 1 Python ingestion script (`src/timingguard/ingest.py`) to parse multi-line QNX `traceprinter` outputs, mapping thread states (`THRUNNING`, `THREADY`, `THMUTEX`) and process/thread IDs.
    - Completed the 10-item Verification Table and documented the trace structure in `docs/trace-format.md`.

- **What we got (Gate Passed):**
  - **Phase 0 Gate:** `qcc` successfully compiles native QNX ELF executables on the Pop!_OS host, and repository skeleton is fully established.
  - **Phase 1 Gate:** A real QNX 8.0 kernel trace (`traces/inversion_spike_clean.txt`) exists in the repo; we successfully extracted thread states, priorities, and sync-object interactions, passing the go/no-go milestone.

- **What is next (Phase 2: Core Engine):**
  - Build the clean event model dataclasses (`src/timingguard/model.py`).
  - Implement the per-thread interval timeline builder and metrics engine (release, lateness, response time).
  - Implement the TOML config loader and the first deadline/priority inversion checker.

## Tue Nov 10, 2026 (Phase 2: Core Engine — COMPLETED)
- **What was done:**
  - Built reusable Python SDK data model (`src/timingguard/model.py`) with navigable `CausalEdge` / `CausalFinding` RCA chains and `EvidenceRef` pointers (exact trace line numbers, hex cycle counts, millisecond timestamps, and confidence classes).
  - Upgraded `src/timingguard/ingest.py` (`TraceSession`) to extract explicit marker-delimited jobs (`tg_job_begin` / `tg_job_end` via `samples/tg_markers.h`) and inferred timer jobs, plus per-job blocker attribution (`exec_ms`, `ready_preempted_ms`, `mutex_blocked_ms`, `sem_blocked_ms`, `ipc_blocked_ms`).
  - Implemented TOML rule loader, global logical scheduling checks, wall-clock timing checks, and `compare_runs()` regression + causal attribution engine in `src/timingguard/analyzers.py`.
  - Created first demo pair (`samples/inversion_demo.c` with `PTHREAD_PRIO_NONE` and `samples/inversion_fixed.c` with one-line `PTHREAD_PRIO_INHERIT`), captured single-core traces using `on -C 0` on QNX 8.0 (`traces/inversion_broken.txt` and `traces/inversion_fixed.txt`).
- **What we got (Phase 2 Gate PASSED):**
  - `aion check` on `traces/inversion_broken.txt` detects the 24.037 ms deadline miss and 23.032 ms mutex priority inversion (`sensor_high` -> `logger_low` -> preempted by `worker_med` on `CPU:00`) and exits `1`.
  - `aion check` on `traces/inversion_fixed.txt` confirms `PTHREAD_PRIO_INHERIT` drops response time to 4.502 ms with 0 findings and exits `0`.
  - `aion diff` detects the 5.34x (+19.535 ms) p99 regression and attributes it to `mutex:logger_low (+23.032 ms)`.
  - All `pytest` unit and golden-file tests pass.


## [2026-11-15] Phase 3 — Multi-Scenario Pathologies, Causal Hardening, & Zero False-Positive Verification

### Summary
Completed Phase 3 validation across all QNX 8.0 synthetic real-time fault-injection workloads (`semaphore_demo`, `cpu_hog_demo`, `ipc_msgsend_demo`), their remediated counterparts (`semaphore_fixed`, `cpu_hog_fixed`, `ipc_msgsend_fixed`), `aion diff` regression attribution, and the `negative_cases` false-positive control suite.

### Changes Made
- **Semaphore Poster Correlation (`aion/ingest.py`, `aion/analyzers.py`)**:
  - Added `KER_CALL:SYNC_SEM_POST` parsing in `flush_multiline()` to populate `TraceSession.sem_posts` and resolve `StateInterval.owner_key` for `THSEM` intervals via `_resolve_semaphore_posters()`.
  - Updated `_detect_semaphore_inversions()` to require a resolved lower-priority poster (`poster_th.prio < iv.prio`), upgrading `semaphore_demo.txt` findings to `confirmed` and eliminating false positives on top-down producer-consumer signaling in `negative_cases.txt` (`producer_high` prio 45 posting to `consumer_med` prio 30).
- **Dynamic Preemptor Priority Attribution (`aion/model.py`, `aion/analyzers.py`)**:
  - Added `ThreadTimeline.display_name_at(prio)` and updated `_find_medium_preemptors()` to key and format preempting threads using the instantaneous `run_iv.prio` from the `THRUNNING` event rather than end-of-trace `ThreadTimeline.prio`.
  - Fixed `procnto-smp-instr` `proc_pool_thread` RCA labels across semaphore, IPC, and CPU hog detectors (`prio:25` and `prio:45` instead of stale `prio:10`).
- **SCHED_LATENCY_MISS Edge Aggregation (`aion/analyzers.py`)**:
  - Grouped `CPU_OCCUPIED_BY` interference intervals by `runner_th.display_name_at(run_iv.prio)` in `_check_timing_rule()` to merge consecutive preemption slices from the same thread into a single RCA step.

### CLI Verification Matrix
- `aion check --config samples/semaphore_rules.toml traces/semaphore_demo.txt` -> `FAIL` (2 findings: `DEADLINE_MISS` [21.019 ms], `PRIORITY_INVERSION_SEMAPHORE` [confirmed, 20.016 ms on `diag_low` preempted by `crunch_med`]).
- `aion check --config samples/semaphore_rules.toml traces/semaphore_fixed.txt` -> `PASS` (0 findings; p99 = 4.028 ms).
- `aion check --config samples/cpu_hog_rules.toml traces/cpu_hog_demo.txt` -> `FAIL` (1 finding: `SCHED_LATENCY_MISS` [confirmed, 17.067 ms in `THREADY` starved by `telemetry_hog` at prio 45]).
- `aion check --config samples/cpu_hog_rules.toml traces/cpu_hog_fixed.txt` -> `PASS` (0 findings; max preempt = 0.012 ms).
- `aion check --config samples/ipc_rules.toml traces/ipc_msgsend_demo.txt` -> `FAIL` (3 findings: `DEADLINE_MISS` [23.973 ms], `PRIORITY_INVERSION_IPC` [confirmed, 22.969 ms], `FORBIDDEN_BLOCKING_IPC` [confirmed, 22.969 ms]).
- `aion check --config samples/ipc_rules.toml traces/ipc_msgsend_fixed.txt` -> `PASS` (0 findings; p99 = 1.006 ms, 0.000 ms block).
- `aion diff traces/ipc_msgsend_fixed.txt traces/ipc_msgsend_demo.txt --thread sensor_high` -> `REGRESSION DETECTED (FAIL)` (`[CRITICAL]` 1.006 ms -> 23.973 ms, 23.84x, attributed to `ipc:diag_server` +22.969 ms).
- `aion check --config samples/negative_rules.toml traces/negative_cases.txt` -> `PASS` (0 findings).

## [2026-11-16] Phase 4 Complete (Tasks 1–11) — Interactive HTML Report, Three-Verdict Engine (Exit Code 4), Benchmark Harness, Calibration & Packaging

### Summary
Completed all 11 deliverables and passed all 4 Phase 4 gates from the project roadmap:
1. Single-file interactive HTML report (`aion/report_html.py`) generated directly from the v1.0 JSON schema (`docs/json-schema.md`), featuring Overview, Timing Analysis, interactive Thread Explorer (timeline with clickable violation window highlighting), Root-Cause View, Recommendations, IPC View, CPU View, Regression View, CI History, Calibrated Environment Metadata, Honest-Limits Note, and the frozen "Profiler vs. Aion" differentiation footer.
2. Three-verdict engine (`evaluate_verdicts()` in `aion/analyzers.py`) separating Logical Violations, Wall-Clock Violations, and Inconclusive Measurements with Exit Code `4` (`inconclusive_policy = "warn" | "fail"`).
3. Full QNX target CI wrapper (`scripts/ci_qnx_target.sh`), pre-captured demo runner (`scripts/run_all_demos.sh`), and GitHub Actions workflow (`.github/workflows/aion-ci.yml`) documenting both hosted and self-hosted runners.
4. Empirical calibration and marker overhead profiler (`scripts/calibrate_and_measure.py` -> `docs/calibration.md`) validating `margin_pct = 10` under QEMU/KVM + `pin_to_cpu0()` (+1.4% to +4.2% baseline jitter; < 1.5 µs per `TraceEvent` marker).
5. Ground-truth benchmark harness (`scripts/benchmark.py`) across all 9 labelled QNX 8.0 traces achieving 100.0% Precision, 100.0% Recall, 0.0% False-Positive Rate, 100.0% Causal Attribution Accuracy, and 100.0% Reproducibility.
6. Clean `pip install .` packaging (`pyproject.toml`) verified in a fresh virtual environment and `aion(1)` man-style CLI documentation added to `README.md`.

### Verification Commands & Results
- `./scripts/run_all_demos.sh` -> `8 passed, 0 failed`
- `python3 scripts/benchmark.py` -> `Precision: 100.00%, Recall: 100.00%, FPR: 0.00%, Attribution: 100.00%, Reproducibility: 100.00%`
- `python3 scripts/calibrate_and_measure.py` -> Generated `docs/calibration.md` (9 traces profiled)
- `pytest -v` -> All unit, integration, JSON/HTML report, lateness, and exit code (`0, 1, 2, 3, 4`) tests passing
- Clean venv install check (`/tmp/aion_fresh_venv/bin/pip install .`) -> `PASS` (exit code 0)