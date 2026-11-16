[settings]
target_env = "qemu"          # "qemu" applies margin_pct; "hardware" uses 1.0x strict limits
margin_pct = 10              # +10% timing tolerance when target_env = "qemu"
scope_processes = []         # Optional list of process names to scope logical checks

[logical_checks]
priority_inversion = true    # Enable mutex and IPC channel priority inversion RCA
semaphore_locks = true       # Enable semaphore priority inversion RCA
forbid_blocking_ipc = ["sensor_high"]  # Threads forbidden from blocking in THREPLY/THSEND

[[timing_rule]]
name = "sensor_high IPC loop"
thread = "sensor_high"
deadline_ms = 10.0           # Max job release-to-completion response time (ms)
max_sched_latency_ms = 2.0   # Max contiguous THREADY wait before CPU dispatch (ms)
max_lateness_ms = 2.0        # Optional: Max delay between job release and first THRUNNING (ms)