# Extra local steps when deploying gpu_commander.
# Sourced by meta_script.sh — has access to $1 (primary host), $run_dir_pre, $last_commit, $run_id.

# Sync vllm_service to the primary host
echo "Syncing vllm_service to $1..."
rsync -av --delete-after --exclude-from='common_tools/rsync_exclude.txt' \
    vllm_service/ "$1":${run_dir_pre}/vllm_service/

# Deploy to peer machines — only when primary is ferragon to avoid recursion
if [[ "$1" == "custodian2ferragon" ]]; then
    source common_tools/meta_script.sh custodian2greatrawr gpu_commander remote_
fi
