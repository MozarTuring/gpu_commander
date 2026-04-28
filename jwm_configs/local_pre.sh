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
