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