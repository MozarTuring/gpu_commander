# Extra local steps when deploying gpu_commander.
# Sourced by meta_script.sh — has access to $1 (primary host), $run_dir_pre, $last_commit, $run_id, _git_branch, _remote_proj.

# Sync vllm_service to the primary host
echo "Syncing vllm_service to $1..."
rsync -av --exclude-from='common_tools/rsync_exclude.txt' \
    vllm_service/ "$1":${run_dir_pre}/vllm_service/

# Deploy to peer machines and set up SSH tunnel — only when primary is ferragon to avoid recursion
if [[ "$1" == "custodian2ferragon" ]]; then
    # Derive coordinator port from branch: main=9800, dev=9801, other branches=9802+
    # Config file defines the port — read it directly
    _branch_suffix="${_remote_proj##*_}"
    if [[ "$_branch_suffix" == "main" ]]; then
        _config_file="gpu_commander/config.yaml"
    elif [[ -f "gpu_commander/config.${_branch_suffix}.yaml" ]]; then
        _config_file="gpu_commander/config.${_branch_suffix}.yaml"
    else
        _config_file="gpu_commander/config.yaml"
    fi
    _coord_port=$(python3 -c "import yaml; cfg=yaml.safe_load(open('${_config_file}')); print(cfg['coordinator']['port'])")

    for _peer in $(python3 -c "import yaml; cfg=yaml.safe_load(open('${_config_file}')); [print(n) for n in cfg['machines'] if n != 'custodian2ferragon']"); do
        source common_tools/meta_script.sh "$_peer" gpu_commander remote_
    done

    echo "Setting up SSH tunnel to coordinator (port ${_coord_port}, branch: ${_git_branch})..."
    pkill -f "ssh.*-L ${_coord_port}:localhost:${_coord_port}.*custodian2ferragon" 2>/dev/null || true
    # Kill any local process holding this port (e.g. stale local coordinator)
    lsof -ti :${_coord_port} 2>/dev/null | xargs kill 2>/dev/null || true
    sleep 1
    # Use ControlPath=none so this tunnel has its own independent SSH connection.
    # Without it, the tunnel shares the mux master — if the mux is disrupted
    # (e.g. a failed port-forward attempt kills the master), all tunnels on it die.
    ssh -o ControlPath=none -f -N -L ${_coord_port}:localhost:${_coord_port} \
        -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
        -o ExitOnForwardFailure=yes custodian2ferragon
    sleep 1
    if curl -s http://localhost:${_coord_port}/api/machines 2>/dev/null | grep -q 'Not authenticated\|machines'; then
        echo "Tunnel ready: http://localhost:${_coord_port} (${_git_branch})"
    else
        echo "WARNING: SSH tunnel setup failed — nothing responding on port ${_coord_port}"
    fi
fi
