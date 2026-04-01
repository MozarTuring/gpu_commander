# GPU Commander — remote.sh
# Sourced by meta_script.sh on the remote machine.
# Expects: RUN_DIR_PRE, RUN_PROJ set by meta_script.sh
#
# Installs deps, kills any old agent, and starts the new agent daemon.
#
# Local usage:
#   cd /Users/maojingwei/baidu/project/ && source common_tools/meta_script.sh custodian2ferragon gpu_commander remote_

AGENT_DIR="${RUN_DIR_PRE}/${RUN_PROJ}/agent"
CONFIG_FILE="${RUN_DIR_PRE}/${RUN_PROJ}/config.yaml"

# Read agent_port from config.yaml for this machine, fallback to 9850
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

# Install deps
pip install -q fastapi 'uvicorn[standard]' pyyaml 2>/dev/null ||
    pip3 install -q fastapi 'uvicorn[standard]' pyyaml 2>/dev/null || true

# Kill old agent
pkill -f 'uvicorn.*agent_server' 2>/dev/null || true
sleep 1

# Start agent
cd "${AGENT_DIR}"
GPU_COMMANDER_TOKEN="${AUTH_TOKEN}" \
GPU_COMMANDER_AGENT_PORT="${AGENT_PORT}" \
nohup python3 -m uvicorn agent_server:app --host 0.0.0.0 --port "${AGENT_PORT}" \
    > agent.log 2>&1 &
AGENT_PID=$!
echo "Agent started — PID: ${AGENT_PID}"

sleep 2

# Verify
if curl -sf "http://localhost:${AGENT_PORT}/health" >/dev/null 2>&1; then
    echo "Agent is UP on port ${AGENT_PORT}"
else
    echo "WARNING: Agent may not have started. Check ${AGENT_DIR}/agent.log"
    tail -20 "${AGENT_DIR}/agent.log" 2>/dev/null || true
fi
