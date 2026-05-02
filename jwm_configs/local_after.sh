# _vllm_branch=$(git -C vllm_service rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)
# _branch_suffix="${_remote_proj##*_}"
# if [[ "$_branch_suffix" == "main" ]]; then
#     _config_file="gpu_commander/config.yaml"
# elif [[ -f "gpu_commander/config.${_branch_suffix}.yaml" ]]; then
#     _config_file="gpu_commander/config.${_branch_suffix}.yaml"
# else
#     _config_file="gpu_commander/config.yaml"
# fi
#
# echo "Syncing vllm_service to $2 (vllm_service_${_vllm_branch})..."
# rsync -av --exclude-from='common_tools/rsync_exclude.txt' \
#     vllm_service/ "$2":${run_dir_pre}/vllm_service_${_vllm_branch}/
#

last_commit=$(sync_and_commit_repo vllm_service)
if [[ "$2" == *"ferragon" ]]; then
    _coord_port=$(python3 -c "import yaml; cfg=yaml.safe_load(open('${_config_file}')); print(cfg['coordinator']['port'])")

    for _peer in $(python3 -c "import yaml; cfg=yaml.safe_load(open('${_config_file}')); [print(n) for n in cfg['machines'] if n != 'ferragon']"); do
        source common_tools/meta_script.sh /Users/maojingwei/baidu/project/gpu_commander/jwm_configs/remotenone.sh "$_peer"
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
