#!/usr/bin/env bash
# scripts/run_all_demos.sh
# One-command end-to-end verification & live demo runner for Aion on QNX 8.0 traces.

set -u

BOLD="\033[1m"
GREEN="\033[1;32m"
RED="\033[1;31m"
CYAN="\033[1;36m"
YELLOW="\033[1;33m"
RESET="\033[0m"

PASS_COUNT=0
FAIL_COUNT=0

run_expect() {
    local expected_rc="$1"
    local title="$2"
    shift 2

    echo -e "\n${CYAN}================================================================================${RESET}"
    echo -e "${BOLD}Scenario:${RESET} ${title}"
    echo -e "${BOLD}Command :${RESET} $*"
    echo -e "${CYAN}--------------------------------------------------------------------------------${RESET}"

    "$@"
    local actual_rc=$?

    if [[ "$actual_rc" -eq "$expected_rc" ]]; then
        echo -e "${GREEN}[OK] Expected exit code ${expected_rc}, got ${actual_rc}${RESET}"
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        echo -e "${RED}[ERROR] Expected exit code ${expected_rc}, got ${actual_rc}${RESET}"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
}

echo -e "${BOLD}${YELLOW}Starting Aion End-to-End Verification Matrix...${RESET}"

# 1. Semaphore Priority Inversion (Broken -> FAIL [1], Fixed -> PASS [0])
run_expect 1 "Semaphore Priority Inversion (Broken Trace — Expect FAIL)" \
    aion check --config samples/semaphore_rules.toml traces/semaphore_demo.txt

run_expect 0 "Semaphore Priority Inversion Remediated with PTHREAD_PRIO_INHERIT (Expect PASS)" \
    aion check --config samples/semaphore_rules.toml traces/semaphore_fixed.txt

# 2. CPU Hog / Scheduling Latency Starvation (Broken -> FAIL [1], Fixed -> PASS [0])
run_expect 1 "CPU Core 0 Starvation by Higher-Priority Telemetry Hog (Expect FAIL)" \
    aion check --config samples/cpu_hog_rules.toml traces/cpu_hog_demo.txt

run_expect 0 "CPU Hog Remediated by Lowering Priority Below Actuator (Expect PASS)" \
    aion check --config samples/cpu_hog_rules.toml traces/cpu_hog_fixed.txt

# 3. IPC Channel Priority Drop & Forbidden Blocking IPC (Broken -> FAIL [1], Fixed -> PASS [0])
run_expect 1 "IPC Synchronous MsgSend into Fixed-Priority Channel (Expect FAIL)" \
    aion check --config samples/ipc_rules.toml traces/ipc_msgsend_demo.txt

run_expect 0 "IPC Decoupled via Non-Blocking MsgSendPulse (Expect PASS)" \
    aion check --config samples/ipc_rules.toml traces/ipc_msgsend_fixed.txt

# 4. Automated Regression Attribution (`aion diff`)
run_expect 1 "Regression Comparison: ipc_msgsend_fixed vs ipc_msgsend_demo (Expect FAIL)" \
    aion diff traces/ipc_msgsend_fixed.txt traces/ipc_msgsend_demo.txt --thread sensor_high

# 5. Negative Control Workload (Zero False Positives -> PASS [0])
run_expect 0 "Negative Control Suite: Legitimate Producer-Consumer Signaling (Expect PASS)" \
    aion check --config samples/negative_rules.toml traces/negative_cases.txt

echo -e "\n${CYAN}================================================================================${RESET}"
echo -e "${BOLD}Aion Demo Suite Complete:${RESET} ${GREEN}${PASS_COUNT} passed${RESET}, ${RED}${FAIL_COUNT} failed${RESET}"
echo -e "${CYAN}================================================================================${RESET}"

[[ "$FAIL_COUNT" -eq 0 ]]