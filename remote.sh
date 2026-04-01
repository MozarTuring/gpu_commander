# GPU Commander — remote.sh
# Sourced by meta_script.sh on the remote machine.
# Expects: RUN_DIR_PRE, RUN_PROJ set by meta_script.sh
#
# Installs deps, kills any old agent, and starts the new agent daemon.
#
# Local usage:
#   cd /Users/maojingwei/baidu/project/ && source common_tools/meta_script.sh custodian2ferragon gpu_commander remote_

PROJ_DIR="${RUN_DIR_PRE}/${RUN_PROJ}"
AGENT_DIR="${PROJ_DIR}/agent"
VENV_DIR="${PROJ_DIR}/.venv"
CONFIG_FILE="${PROJ_DIR}/config.yaml"

# Create/reuse venv and install deps
if [[ ! -d "${VENV_DIR}" ]]; then
    echo "Creating virtualenv at ${VENV_DIR}..."
    python3 -m venv "${VENV_DIR}"
fi
source "${VENV_DIR}/bin/activate"

pip install -q fastapi 'uvicorn[standard]' pyyaml 2>&1 | tail -3

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

# Kill old agent
pkill -f 'uvicorn.*agent_server' 2>/dev/null || true
sleep 1

# Start agent using the venv python
cd "${AGENT_DIR}"
GPU_COMMANDER_TOKEN="${AUTH_TOKEN}" \
GPU_COMMANDER_AGENT_PORT="${AGENT_PORT}" \
nohup "${VENV_DIR}/bin/python3" -m uvicorn agent_server:app --host 0.0.0.0 --port "${AGENT_PORT}" \
    > agent.log 2>&1 &
AGENT_PID=$!
echo "Agent started — PID: ${AGENT_PID}"

sleep 3

# Verify
if curl -sf "http://localhost:${AGENT_PORT}/health" >/dev/null 2>&1; then
    echo "Agent is UP on port ${AGENT_PORT}"
else
    echo "WARNING: Agent may not have started. Check ${AGENT_DIR}/agent.log"
    tail -20 "${AGENT_DIR}/agent.log" 2>/dev/null || true
fi
