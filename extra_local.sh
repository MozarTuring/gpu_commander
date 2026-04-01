# Synced before gpu_commander deploys — keeps vllm_service up to date on remote.
# Runs in the context of meta_script.sh; $1 = remote host, $run_dir_pre = remote base dir.
rsync -av --delete-after --exclude-from='common_tools/rsync_exclude.txt' \
    vllm_service/ "$1":${run_dir_pre}/vllm_service/
