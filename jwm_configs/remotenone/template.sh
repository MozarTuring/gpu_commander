# GPU Commander — remote.sh
# Sourced by meta_script.sh on the remote machine.
# Expects: RUN_DIR_PRE, RUN_PROJ, JWM_COMMIT_ID_L, SERVER_NAME set by meta_script.sh
#
# Local usage:
#   cd /Users/maojingwei/baidu/project/ && source common_tools/meta_script.sh ferragon gpu_commander remote_

# --- remote machine startup ---


# Pick config based on branch suffix: gpu_commander_main -> config.yaml, gpu_commander_dev -> config.dev.yaml
# Write deploy version so coordinator can serve it via /api/version
echo "${JWM_COMMIT_ID_L}" >"${PROJ_DIR}/version.txt"



# Kill old processes (fail if they exist but can't be killed, e.g. owned by another user)
_coord_pids=$(pgrep -f 'python.*coordinator_server' 2>/dev/null || true)
_agent_pids=$(pgrep -f 'python.*agent_server' 2>/dev/null || true)
if [[ -n "$_coord_pids" ]]; then
    pkill -f 'python.*coordinator_server' 2>/dev/null || {
        echo "ERROR: cannot kill coordinator (PIDs: $_coord_pids) — owned by another user?"
        return 1 2>/dev/null
        exit 1
    }
fi
if [[ -n "$_agent_pids" ]]; then
    pkill -f 'python.*agent_server' 2>/dev/null || {
        echo "ERROR: cannot kill agent (PIDs: $_agent_pids) — owned by another user?"
        return 1 2>/dev/null
        exit 1
    }
fi
sleep 1

# Wait for a uvicorn service to start by watching its log.
# Usage: wait_for_startup <name> <log_file>
wait_for_startup() {
    local name="$1" log="$2"
    if timeout 15 tail -f "$log" 2>/dev/null | grep -qm1 -E 'Application startup complete|Error|Traceback'; then
        if grep -q 'Application startup complete' "$log"; then
            echo "${name} is UP"
        else
            echo "ERROR: ${name} failed to start:"
            tail -20 "$log"
            return 1
        fi
    else
        echo "ERROR: ${name} startup timed out:"
        tail -20 "$log" 2>/dev/null || true
        return 1
    fi
}

