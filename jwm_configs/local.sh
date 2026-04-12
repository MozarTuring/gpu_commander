# GPU Commander — local.sh
# Sourced by meta_script.sh on the local machine.
#   source gpu_commander/jwm_configs/local.sh pre <primary_host>   — patch config before rsync
#   source gpu_commander/jwm_configs/local.sh after <primary_host> — rsync vllm_service, peer deploy, SSH tunnel
#
# Usage:
#   cd /Users/maojingwei/baidu/project/ && source common_tools/meta_script.sh ferragon gpu_commander remote_

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

    if [[ "$2" == "ferragon" ]]; then
        _coord_port=$(python3 -c "import yaml; cfg=yaml.safe_load(open('${_config_file}')); print(cfg['coordinator']['port'])")

        for _peer in $(python3 -c "import yaml; cfg=yaml.safe_load(open('${_config_file}')); [print(n) for n in cfg['machines'] if n != 'ferragon']"); do
            source common_tools/meta_script.sh "$_peer" gpu_commander remote_
        done

        echo "Setting up SSH tunnel to coordinator (port ${_coord_port}, branch: ${_git_branch})..."
        pkill -f "ssh.*-L ${_coord_port}:localhost:${_coord_port}.*ferragon" 2>/dev/null || true
        lsof -ti :${_coord_port} 2>/dev/null | xargs kill 2>/dev/null || true
        sleep 1
        ssh -o ControlPath=none -f -N -L ${_coord_port}:localhost:${_coord_port} -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes ferragon
        sleep 1
        if curl -s http://localhost:${_coord_port}/api/machines 2>/dev/null | grep -q 'Not authenticated\|machines'; then
            echo "Tunnel ready: http://localhost:${_coord_port} (${_git_branch})"
        else
            echo "WARNING: SSH tunnel setup failed — nothing responding on port ${_coord_port}"
        fi
    fi
    return 0 2>/dev/null || exit 0
fi
