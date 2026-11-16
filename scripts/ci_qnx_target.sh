#!/usr/bin/env bash
# End-to-End QNX 8.0 Target CI Wrapper for Aion.
# Modes:
#   1. Live QNX Target Mode (default when QNX_TARGET_IP is reachable & qcc is sourced):
#      - Cross-compiles C workload with `qcc -Vgcc_ntox86_64`
#      - Copies binary to booted QNX 8.0 target via `scp`
#      - Runs workload under `tracelogger` on QNX target, converts `.kev` via `traceprinter`
#      - Pulls trace to host, runs `aion check` (and optional `aion diff` vs baseline)
#   2. Pre-Captured Fallback Mode (`--pre-captured`):
#      - Runs `aion check` and `aion diff` directly on stored traces for hosted CI runners.

set -euo pipefail

usage() {
    cat <<EOF
Usage: $0 [OPTIONS]
  --source <file.c>       C workload source file (e.g., samples/ipc_msgsend_demo.c)
  --config <rules.toml>   TOML rule config (e.g., samples/ipc_rules.toml)
  --trace-out <file.txt>  Path to store or read captured traceprinter text
  --baseline <base.txt>   Optional baseline trace for `aion diff` regression gating
  --thread <name>         Target thread name for `aion diff`
  --target-ip <ip>        QNX 8.0 target IP (default: \${QNX_TARGET_IP:-192.168.56.101})
  --report-html <out.html>Optional path to emit standalone HTML report
  --pre-captured          Skip live QNX build/capture and verify existing --trace-out file
EOF
    exit 2
}

SRC_FILE=""
CONFIG_FILE=""
TRACE_OUT=""
BASELINE_TRACE=""
TARGET_THREAD=""
TARGET_IP="${QNX_TARGET_IP:-192.168.56.101}"
TARGET_USER="${QNX_TARGET_USER:-root}"
REPORT_HTML=""
PRE_CAPTURED=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source) SRC_FILE="$2"; shift 2 ;;
        --config) CONFIG_FILE="$2"; shift 2 ;;
        --trace-out) TRACE_OUT="$2"; shift 2 ;;
        --baseline) BASELINE_TRACE="$2"; shift 2 ;;
        --thread) TARGET_THREAD="$2"; shift 2 ;;
        --target-ip) TARGET_IP="$2"; shift 2 ;;
        --report-html) REPORT_HTML="$2"; shift 2 ;;
        --pre-captured) PRE_CAPTURED=1; shift ;;
        -h|--help) usage ;;
        *) echo "[ERROR] Unknown argument: $1" >&2; usage ;;
    esac
done

if [[ -z "$CONFIG_FILE" || -z "$TRACE_OUT" ]]; then
    echo "[ERROR] --config and --trace-out are required." >&2
    usage
fi

if [[ "$PRE_CAPTURED" -eq 0 ]]; then
    if [[ -z "$SRC_FILE" ]]; then
        echo "[ERROR] --source <file.c> is required in live QNX target mode." >&2
        exit 2
    fi
    if ! command -v qcc >/dev/null 2>&1; then
        echo "[ERROR] 'qcc' not found in PATH. Source ~/qnx800/qnxsdp-env.sh first." >&2
        exit 2
    fi

    BIN_NAME="$(basename "${SRC_FILE%.c}")"
    BUILD_DIR="$(mktemp -d)"
    trap 'rm -rf "$BUILD_DIR"' EXIT

    echo "[1/4] Cross-compiling ${SRC_FILE} with qcc -Vgcc_ntox86_64..."
    qcc -Vgcc_ntox86_64 -O2 -I"$(dirname "$SRC_FILE")" "$SRC_FILE" -o "${BUILD_DIR}/${BIN_NAME}"

    echo "[2/4] Copying ${BIN_NAME} to QNX target ${TARGET_USER}@${TARGET_IP}:/tmp/${BIN_NAME}..."
    scp -q "${BUILD_DIR}/${BIN_NAME}" "${TARGET_USER}@${TARGET_IP}:/tmp/${BIN_NAME}"

    echo "[3/4] Running workload under tracelogger on QNX target..."
    ssh "${TARGET_USER}@${TARGET_IP}" "
        chmod +x /tmp/${BIN_NAME} &&
        rm -f /tmp/${BIN_NAME}.kev &&
        tracelogger -n 1 -f /tmp/${BIN_NAME}.kev &
        TL_PID=\$!
        sleep 0.2
        /tmp/${BIN_NAME}
        kill -INT \$TL_PID 2>/dev/null || true
        wait \$TL_PID 2>/dev/null || true
        traceprinter -f /tmp/${BIN_NAME}.kev > /tmp/${BIN_NAME}.txt
    "

    echo "[4/4] Pulling traceprinter output to ${TRACE_OUT}..."
    scp -q "${TARGET_USER}@${TARGET_IP}:/tmp/${BIN_NAME}.txt" "$TRACE_OUT"
else
    echo "[INFO] Running in --pre-captured mode using stored trace: ${TRACE_OUT}"
fi

# Run Aion Check
CHECK_RC=0
aion check --config "$CONFIG_FILE" "$TRACE_OUT" || CHECK_RC=$?

if [[ -n "$REPORT_HTML" ]]; then
    aion check --config "$CONFIG_FILE" --format html --output "$REPORT_HTML" "$TRACE_OUT" || true
    echo "[INFO] Wrote standalone HTML report to ${REPORT_HTML}"
fi

# Optional Baseline Comparison (`aion diff`)
DIFF_RC=0
if [[ -n "$BASELINE_TRACE" && -n "$TARGET_THREAD" ]]; then
    echo "--------------------------------------------------------------------------------"
    aion diff "$BASELINE_TRACE" "$TRACE_OUT" --thread "$TARGET_THREAD" || DIFF_RC=$?
fi

if [[ "$CHECK_RC" -ne 0 ]]; then
    exit "$CHECK_RC"
fi
exit "$DIFF_RC"