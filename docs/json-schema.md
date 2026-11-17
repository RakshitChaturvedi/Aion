# Aion JSON Output Schema (`schema_version = "1.0"`)

Both `aion check --format json` and `aion check --format html` generate their output from the exact same normalized dictionary payload.

## Top-Level `aion check --format json` Payload

| Field | Type | Description |
| :--- | :--- | :--- |
| `schema_version` | `string` | Schema version identifier (`"1.0"`). |
| `verdict` | `string` | Overall verdict: `"PASS"`, `"FAIL"`, or `"INCONCLUSIVE"`. |
| `logical_verdict` | `string` | Verdict for cycle-exact logical checks (`"PASS"` or `"FAIL"`). |
| `wall_clock_verdict` | `string` | Verdict for deadline/latency/lateness checks (`"PASS"`, `"FAIL"`, or `"INCONCLUSIVE"`). |
| `inconclusive_reasons` | `array<string>` | Reasons why a measurement was inconclusive (empty if conclusive). |
| `exit_code` | `integer` | Deterministic CLI exit code (`0`, `1`, or `4`). |
| `trace` | `string` | Path to the evaluated QNX traceprinter file. |
| `cpus` | `integer` | Number of SMP cores detected in the trace header. |
| `cycles_per_sec` | `integer` | QNX `TRACE_CYCLES_PER_SEC` clock frequency in Hz. |
| `threads_tracked` | `integer` | Total kernel/application threads reconstructed. |
| `markers` | `integer` | Number of `USREVENT` job boundary markers parsed. |
| `environment` | `object` | Capture metadata (`target_env`, `platform_profile`, `margin_pct`, `margin_calibrated`, `marker_overhead_us`). |
| `metrics` | `array<object>` | Per-thread job timing percentiles (`p50_ms`, `p95_ms`, `p99_ms`, `max_ms`, `jitter_ms`, `exec_avg_ms`, `preempt_max_ms`, `block_max_ms`). |
| `findings_count` | `integer` | Total number of `CausalFinding` violations emitted. |
| `findings` | `array<object>` | List of `CausalFinding` objects, each containing `finding_type`, `confidence`, `summary`, `suggested_fix`, and `causal_chain` (`CausalEdge` steps + `EvidenceRef` pointers). |
| `timeline_tracks` | `array<object>` | Per-thread state intervals (`state`, `start_ms`, `end_ms`, `cpu`, `prio`) used by the HTML Thread Explorer. |
| `ipc_summary` | `array<object>` | Blocking IPC intervals (`THREPLY` / `THSEND`) with client, server, and duration. |
| `cpu_summary` | `array<object>` | Per-core active time, utilization percentage, and top running threads. |