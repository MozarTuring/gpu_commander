# Extra local steps when deploying gpu_commander.
# Sourced by meta_script.sh — has access to $1 (primary host), $run_dir_pre, $last_commit, $run_id.

# Sync vllm_service to the primary host
echo "Syncing vllm_service to $1..."
rsync -av --exclude-from='common_tools/rsync_exclude.txt' \
    vllm_service/ "$1":${run_dir_pre}/vllm_service/

# Deploy to peer machines and set up SSH tunnel — only when primary is ferragon to avoid recursion
if [[ "$1" == "custodian2ferragon" ]]; then
    for _peer in $(python3 -c "import yaml; cfg=yaml.safe_load(open('gpu_commander/config.yaml')); [print(n) for n in cfg['machines'] if n != 'custodian2ferragon']"); do
        source common_tools/meta_script.sh "$_peer" gpu_commander remote_
    done

    echo "Setting up SSH tunnel to coordinator (port 9800)..."
    pkill -f "ssh.*-L 9800:localhost:9800.*custodian2ferragon" 2>/dev/null || true
    # Kill any local process holding port 9800 (e.g. stale local coordinator)
    lsof -ti :9800 2>/dev/null | xargs kill 2>/dev/null || true
    sleep 1
    ssh -f -N -L 9800:localhost:9800 \
        -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
        -o ExitOnForwardFailure=yes custodian2ferragon
    sleep 1
    if curl -sf http://localhost:9800/api/machines >/dev/null 2>&1 || \
       curl -s http://localhost:9800/api/machines 2>/dev/null | grep -q 'Not authenticated'; then
        echo "Tunnel ready: http://localhost:9800"
    else
        echo "WARNING: SSH tunnel setup failed — nothing responding on port 9800"
    fi
fi
