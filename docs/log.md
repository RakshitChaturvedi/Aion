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