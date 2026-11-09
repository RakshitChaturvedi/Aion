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