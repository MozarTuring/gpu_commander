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
echo "SERVER_NAME, ${SERVER_NAME}"
if [[ "${SERVER_NAME}" == *"ferragon" ]]; then
    _coord_port=$(python3 -c "import yaml; cfg=yaml.safe_load(open('${_config_file}')); print(cfg['coordinator']['port'])")

    for _peer in $(python3 -c "import yaml; cfg=yaml.safe_load(open('${_config_file}')); [print(n) for n in cfg['machines'] if n != 'ferragon']"); do
        echo "$_peer" >>/Users/maojingwei/baidu/project/gpu_commander/jwm_configs/remotenone.sh
        bash common_tools/meta_script.sh /Users/maojingwei/baidu/project/gpu_commander/jwm_configs/remotenone.sh
    done
else
    sed -i '' '$d' /Users/maojingwei/baidu/project/gpu_commander/jwm_configs/remotenone.sh
fi
return 0 2>/dev/null || exit 0
