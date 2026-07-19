# GPU Commander — remote.sh
# Sourced by meta_script.sh on the remote machine.
# Expects: RUN_DIR_PRE, RUN_PROJ, JWM_COMMIT_ID_L, SERVER_NAME set by meta_script.sh
#
# Local usage:
#   cd /Users/maojingwei/baidu/project/ && source common_tools/meta_script.sh ferragon gpu_commander remote_

# --- remote machine startup ---

JWM_SERVER_NAME=ferragon

PROJ_DIR="${RUN_DIR_HOME}/project_remote_jwm/${RUN_PROJ}"
AGENT_DIR="${PROJ_DIR}/agent"

# Pick config based on branch suffix: gpu_commander_main -> config.yaml, gpu_commander_dev -> config.dev.yaml
_branch_suffix="${RUN_PROJ##*_}" # everything after last underscore
CONFIG_FILE="${PROJ_DIR}/config.yaml"
echo "Using config: ${CONFIG_FILE}"

# Write deploy version so coordinator can serve it via /api/version
echo "${JWM_COMMIT_ID_L}" >"${PROJ_DIR}/version.txt"

pip install -q fastapi 'uvicorn[standard]' pyyaml httpx python-multipart 2>&1 | tail -3

echo "Agent dir:  ${AGENT_DIR}"

# Kill old processes (fail if they exist but can't be killed, e.g. owned by another user)
_coord_pids=$(pgrep -f 'python.*coordinator_server' 2>/dev/null || true)
_agent_pids=$(pgrep -f 'python.*agent_server' 2>/dev/null || true)
if [[ -n "$_coord_pids" ]]; then
    pkill -f 'python.*coordinator_server' 2>/dev/null || {
        echo "ERROR: cannot kill coordinator (PIDs: $_coord_pids) — owned by another user?"
        return 1 2>/dev/null
        exit 1
    }
fi
if [[ -n "$_agent_pids" ]]; then
    pkill -f 'python.*agent_server' 2>/dev/null || {
        echo "ERROR: cannot kill agent (PIDs: $_agent_pids) — owned by another user?"
        return 1 2>/dev/null
        exit 1
    }
fi
sleep 1

# Wait for a uvicorn service to start by watching its log.
# Usage: wait_for_startup <name> <log_file>
wait_for_startup() {
    local name="$1" log="$2"
    if timeout 15 tail -f "$log" 2>/dev/null | grep -qm1 -E 'Application startup complete|Error|Traceback'; then
        if grep -q 'Application startup complete' "$log"; then
            echo "${name} is UP"
        else
            echo "ERROR: ${name} failed to start:"
            tail -20 "$log"
            return 1
        fi
    else
        echo "ERROR: ${name} startup timed out:"
        tail -20 "$log" 2>/dev/null || true
        return 1
    fi
}

# Start agent using the venv python
cd "${AGENT_DIR}"
>agent.log
GPU_COMMANDER_CONFIG="${CONFIG_FILE}" \
    nohup "python3" agent_server.py \
    >agent.log 2>&1 &
AGENT_PID=$!
echo "Agent started — PID: ${AGENT_PID}"

wait_for_startup "Agent" agent.log || {
    return 1 2>/dev/null
    exit 1
}

if [[ ${JWM_SERVER_NAME} == "ferragon" ]]; then
    cd "${PROJ_DIR}/coordinator"
    GPU_COMMANDER_CONFIG="${CONFIG_FILE}" \
        nohup "python3" coordinator_server.py \
        >"${PROJ_DIR}/coordinator.log" 2>&1 &
    echo "Coordinator started — PID: $!"

    wait_for_startup "Coordinator" "${PROJ_DIR}/coordinator.log" || {
        return 1 2>/dev/null
        exit 1
    }
fi
