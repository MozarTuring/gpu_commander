# GPU Commander — remote.sh
# Sourced by meta_script.sh on the remote machine.
# Expects: RUN_DIR_PRE, RUN_PROJ, JWM_COMMIT_ID_L, SERVER_NAME set by meta_script.sh
#
# Local usage:
#   cd /Users/maojingwei/baidu/project/ && source common_tools/meta_script.sh custodian2ferragon gpu_commander remote_

# --- remote machine startup ---

PROJ_DIR="${RUN_DIR_PRE}/${RUN_PROJ}"
AGENT_DIR="${PROJ_DIR}/agent"
VENV_DIR="${RUN_DIR_PRE}/.venvs/gpu_commander"

# Pick config based on branch suffix: gpu_commander_main -> config.yaml, gpu_commander_dev -> config.dev.yaml
_branch_suffix="${RUN_PROJ##*_}" # everything after last underscore
if [[ "$_branch_suffix" == "main" ]]; then
    CONFIG_FILE="${PROJ_DIR}/config.yaml"
elif [[ -f "${PROJ_DIR}/config.${_branch_suffix}.yaml" ]]; then
    CONFIG_FILE="${PROJ_DIR}/config.${_branch_suffix}.yaml"
else
    CONFIG_FILE="${PROJ_DIR}/config.yaml"
fi
echo "Using config: ${CONFIG_FILE}"

# Write deploy version so coordinator can serve it via /api/version
echo "${JWM_COMMIT_ID_L}" >"${PROJ_DIR}/version.txt"

# Create/reuse venv and install deps
if [[ ! -f "${VENV_DIR}/bin/activate" ]]; then
    echo "Creating virtualenv at ${VENV_DIR}..."
    rm -rf "${VENV_DIR}"
    python3 -m venv "${VENV_DIR}"
fi
source "${VENV_DIR}/bin/activate"

pip install -q fastapi 'uvicorn[standard]' pyyaml httpx 2>&1 | tail -3

AGENT_PORT=$(python3 -c "
import yaml
with open('${CONFIG_FILE}') as f:
    cfg = yaml.safe_load(f)
for name, m in cfg.get('machines', {}).items():
    if name == '${SERVER_NAME}' or m.get('ssh_alias') == '${SERVER_NAME}':
        print(m.get('agent_port', 9850))
        break
else:
    print(9850)
" 2>/dev/null || echo 9850)

AUTH_TOKEN=$(python3 -c "
import yaml
with open('${CONFIG_FILE}') as f:
    cfg = yaml.safe_load(f)
print(cfg.get('auth', {}).get('token', 'gpu-commander-secret-change-me'))
" 2>/dev/null || echo "gpu-commander-secret-change-me")

echo "Agent dir:  ${AGENT_DIR}"
echo "Agent port: ${AGENT_PORT}"

# Kill coordinator first so it can't submit stale tasks to the new agent
pkill -f 'uvicorn.*coordinator_server' 2>/dev/null || true
# Kill old agent
pkill -f 'uvicorn.*agent_server' 2>/dev/null || true
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
GPU_COMMANDER_TOKEN="${AUTH_TOKEN}" \
    GPU_COMMANDER_AGENT_PORT="${AGENT_PORT}" \
    nohup "${VENV_DIR}/bin/python3" -m uvicorn agent_server:app --host 0.0.0.0 --port "${AGENT_PORT}" \
    >agent.log 2>&1 &
AGENT_PID=$!
echo "Agent started — PID: ${AGENT_PID}"

wait_for_startup "Agent" agent.log || { return 1 2>/dev/null; exit 1; }

# Start coordinator (only on the designated coordinator host)
COORDINATOR_HOST=$(python3 -c "
import yaml
with open('${CONFIG_FILE}') as f:
    cfg = yaml.safe_load(f)
print(cfg.get('coordinator', {}).get('host_machine', ''))
" 2>/dev/null || echo "")

if [[ -z "${COORDINATOR_HOST}" || "${SERVER_NAME}" != "${COORDINATOR_HOST}" ]]; then
    echo "Skipping coordinator (not the coordinator host)"
    return 0 2>/dev/null || exit 0
fi

COORDINATOR_PORT=$(python3 -c "
import yaml
with open('${CONFIG_FILE}') as f:
    cfg = yaml.safe_load(f)
print(cfg.get('coordinator', {}).get('port', 9800))
" 2>/dev/null || echo 9800)

echo "Coordinator port: ${COORDINATOR_PORT}"

cd "${PROJ_DIR}/coordinator"
GPU_COMMANDER_CONFIG="${CONFIG_FILE}" \
    nohup "${VENV_DIR}/bin/python3" -m uvicorn coordinator_server:app --host 0.0.0.0 --port "${COORDINATOR_PORT}" \
    >"${PROJ_DIR}/coordinator.log" 2>&1 &
echo "Coordinator started — PID: $!"

wait_for_startup "Coordinator" "${PROJ_DIR}/coordinator.log" || { return 1 2>/dev/null; exit 1; }

if false; then
    cd /Users/maojingwei/baidu/project/ && source common_tools/meta_script.sh custodian2ferragon gpu_commander remote_

    _coord_port=9800 && ssh -o ControlPath=none -f -N -L ${_coord_port}:localhost:${_coord_port} -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes custodian2ferragon
fi
