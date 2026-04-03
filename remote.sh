# GPU Commander — remote.sh
# Sourced by meta_script.sh on the remote machine.
# Expects: RUN_DIR_PRE, RUN_PROJ set by meta_script.sh
#
# Also handles local pre/after hooks (merged from extra_local.sh):
#   source gpu_commander/remote.sh pre <primary_host>   — patch config before rsync
#   source gpu_commander/remote.sh after <primary_host> — rsync vllm_service, peer deploy, SSH tunnel
#
# Local usage:
#   cd /Users/maojingwei/baidu/project/ && source common_tools/meta_script.sh custodian2ferragon gpu_commander remote_

# --- pre hook (runs locally before rsync) ---
if [[ "$1" == "pre" ]]; then
    _vllm_branch=$(git -C vllm_service rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)
    _branch_suffix="${_remote_proj##*_}"
    if [[ "$_branch_suffix" == "main" ]]; then
        _config_file="gpu_commander/config.yaml"
    elif [[ -f "gpu_commander/config.${_branch_suffix}.yaml" ]]; then
        _config_file="gpu_commander/config.${_branch_suffix}.yaml"
    else
        _config_file="gpu_commander/config.yaml"
    fi
    sed -i '' "s|vllm_service_dir:.*|vllm_service_dir: ${run_dir_pre}/vllm_service_${_vllm_branch}|g" \
        ${_config_file} 2>/dev/null || true
    echo "Patched vllm_service_dir → vllm_service_${_vllm_branch} in ${_config_file}"
    return 0 2>/dev/null || exit 0
fi

# --- after hook (runs locally after remote job launches) ---
if [[ "$1" == "after" ]]; then
    _vllm_branch=$(git -C vllm_service rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)
    _branch_suffix="${_remote_proj##*_}"
    if [[ "$_branch_suffix" == "main" ]]; then
        _config_file="gpu_commander/config.yaml"
    elif [[ -f "gpu_commander/config.${_branch_suffix}.yaml" ]]; then
        _config_file="gpu_commander/config.${_branch_suffix}.yaml"
    else
        _config_file="gpu_commander/config.yaml"
    fi

    echo "Syncing vllm_service to $2 (vllm_service_${_vllm_branch})..."
    rsync -av --exclude-from='common_tools/rsync_exclude.txt' \
        vllm_service/ "$2":${run_dir_pre}/vllm_service_${_vllm_branch}/

    if [[ "$2" == "custodian2ferragon" ]]; then
        _coord_port=$(python3 -c "import yaml; cfg=yaml.safe_load(open('${_config_file}')); print(cfg['coordinator']['port'])")

        for _peer in $(python3 -c "import yaml; cfg=yaml.safe_load(open('${_config_file}')); [print(n) for n in cfg['machines'] if n != 'custodian2ferragon']"); do
            source common_tools/meta_script.sh "$_peer" gpu_commander remote_
        done

        echo "Setting up SSH tunnel to coordinator (port ${_coord_port}, branch: ${_git_branch})..."
        pkill -f "ssh.*-L ${_coord_port}:localhost:${_coord_port}.*custodian2ferragon" 2>/dev/null || true
        lsof -ti :${_coord_port} 2>/dev/null | xargs kill 2>/dev/null || true
        sleep 1
        ssh -o ControlPath=none -f -N -L ${_coord_port}:localhost:${_coord_port} -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes custodian2ferragon
        sleep 1
        if curl -s http://localhost:${_coord_port}/api/machines 2>/dev/null | grep -q 'Not authenticated\|machines'; then
            echo "Tunnel ready: http://localhost:${_coord_port} (${_git_branch})"
        else
            echo "WARNING: SSH tunnel setup failed — nothing responding on port ${_coord_port}"
        fi
    fi
    return 0 2>/dev/null || exit 0
fi

# --- remote machine startup (no arg) ---

if [[ "$1" == "remote" ]]; then
    PROJ_DIR="${RUN_DIR_PRE}/${RUN_PROJ}"
    AGENT_DIR="${PROJ_DIR}/agent"
    VENV_DIR="${PROJ_DIR}/.venv"

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

    # Start agent using the venv python
    cd "${AGENT_DIR}"
    GPU_COMMANDER_TOKEN="${AUTH_TOKEN}" \
        GPU_COMMANDER_AGENT_PORT="${AGENT_PORT}" \
        nohup "${VENV_DIR}/bin/python3" -m uvicorn agent_server:app --host 0.0.0.0 --port "${AGENT_PORT}" \
        >agent.log 2>&1 &
    AGENT_PID=$!
    echo "Agent started — PID: ${AGENT_PID}"

    sleep 3

    # Verify agent
    if curl -sf "http://localhost:${AGENT_PORT}/health" >/dev/null 2>&1; then
        echo "Agent is UP on port ${AGENT_PORT}"
    else
        echo "WARNING: Agent may not have started. Check ${AGENT_DIR}/agent.log"
        tail -20 "${AGENT_DIR}/agent.log" 2>/dev/null || true
    fi

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

    sleep 3
    if curl -sf "http://localhost:${COORDINATOR_PORT}/api/machines" >/dev/null 2>&1; then
        echo "Coordinator is UP on port ${COORDINATOR_PORT}"
    else
        echo "WARNING: Coordinator may not have started. Check ${PROJ_DIR}/coordinator.log"
        tail -20 "${PROJ_DIR}/coordinator.log" 2>/dev/null || true
    fi
fi

if false; then

    cd /Users/maojingwei/baidu/project/ && source common_tools/meta_script.sh custodian2ferragon gpu_commander remote_

    _coord_port=9800 && ssh -o ControlPath=none -f -N -L ${_coord_port}:localhost:${_coord_port} -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes custodian2ferragon
fi
