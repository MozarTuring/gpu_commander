"""GPU Commander Coordinator — central server running on your Mac."""

import asyncio
import hashlib
import json
import secrets as _secrets
import time
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

from config import load_config, AppConfig, MachineConfig, LLMIdleConfig

cfg: AppConfig = load_config()

app = FastAPI(title="GPU Commander Coordinator")

# ---------------------------------------------------------------------------
# Secrets (HF token etc.) — stored outside git in .secrets.json
# ---------------------------------------------------------------------------
_SECRETS_FILE = Path(__file__).resolve().parent.parent / ".secrets.json"
_hf_token: str = ""

def _load_secrets() -> None:
    global _hf_token
    if _SECRETS_FILE.exists():
        try:
            data = json.loads(_SECRETS_FILE.read_text())
            _hf_token = data.get("hf_token", "")
        except Exception:
            pass

def _save_secrets() -> None:
    _SECRETS_FILE.write_text(json.dumps({"hf_token": _hf_token}))

_load_secrets()

# ---------------------------------------------------------------------------
# User management & authentication
# ---------------------------------------------------------------------------
_USERS_FILE = Path(__file__).resolve().parent.parent / "users.json"
_sessions: dict[str, dict] = {}   # token -> {username, role, expires_at}
_SESSION_TTL = 30 * 24 * 3600     # 30 days

def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()

def _load_users() -> dict:
    if _USERS_FILE.exists():
        try:
            return json.loads(_USERS_FILE.read_text())
        except Exception:
            return {}
    return {}

def _save_users(users: dict) -> None:
    _USERS_FILE.write_text(json.dumps(users, indent=2))

def _bootstrap_admin() -> None:
    users = _load_users()
    if not users:
        password = _secrets.token_urlsafe(12)
        salt = _secrets.token_hex(16)
        users["admin"] = {
            "password_hash": _hash_password(password, salt),
            "salt": salt,
            "role": "admin",
        }
        _save_users(users)
        print(f"\n{'='*52}")
        print(f"  ADMIN CREDENTIALS (first-time setup)")
        print(f"  Username : admin")
        print(f"  Password : {password}")
        print(f"  Change this after first login!")
        print(f"{'='*52}\n")

_bootstrap_admin()

def _get_session(token: str) -> dict | None:
    s = _sessions.get(token)
    if s and s["expires_at"] > time.time():
        return s
    if s:
        del _sessions[token]
    return None

# Paths that don't need authentication
_UNPROTECTED = {"/", "/api/auth/login"}

def _check_request_auth(request: Request) -> dict | None:
    """Return session dict if authenticated, else None."""
    # Backward-compat: agent-to-coordinator calls use X-Agent-Token
    if request.headers.get("X-Agent-Token") == cfg.auth_token:
        return {"username": "_agent", "role": "admin"}
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return _get_session(auth[7:])
    return None

def require_auth(request: Request) -> dict:
    user = _check_request_auth(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user

def require_admin(user: dict = Depends(require_auth)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path in _UNPROTECTED or request.url.path.startswith("/static/"):
        return await call_next(request)
    if not _check_request_auth(request):
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    return await call_next(request)

# Cached GPU status per machine
_gpu_cache: dict[str, dict] = {}
_gpu_cache_ts: dict[str, float] = {}
_machine_online: dict[str, bool] = {}

# Idle tracking: "machine:container" -> last_active timestamp
_container_last_active: dict[str, float] = {}
_idle_log: list[dict] = []  # recent auto-stop events

# Deploy ownership: list of {task_id, machine, model, container, username, submitted_at}
_deploy_records: list[dict] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _headers() -> dict:
    return {"X-Agent-Token": cfg.auth_token}


async def _agent_request(
    machine: MachineConfig,
    method: str,
    path: str,
    json_body: dict | None = None,
    params: dict | None = None,
    timeout: float = 30.0,
) -> dict:
    url = f"{machine.base_url}{path}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.request(
            method, url, headers=_headers(), json=json_body, params=params
        )
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=resp.status_code,
                detail=resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text,
            )
        return resp.json()


def _get_machine(name: str) -> MachineConfig:
    m = cfg.machines.get(name)
    if m is None:
        raise HTTPException(status_code=404, detail=f"Machine '{name}' not found")
    return m


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    index = WEB_DIR / "index.html"
    return HTMLResponse(index.read_text())


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    username: str
    password: str

@app.post("/api/auth/login")
async def login(req: LoginRequest):
    users = _load_users()
    user = users.get(req.username)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    expected = _hash_password(req.password, user["salt"])
    if expected != user["password_hash"]:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = _secrets.token_urlsafe(32)
    _sessions[token] = {
        "username": req.username,
        "role": user["role"],
        "expires_at": time.time() + _SESSION_TTL,
    }
    return {"token": token, "username": req.username, "role": user["role"]}

@app.post("/api/auth/logout")
async def logout(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        _sessions.pop(auth[7:], None)
    return {"ok": True}

@app.get("/api/auth/me")
async def me(user: dict = Depends(require_auth)):
    users = _load_users()
    u = users.get(user["username"], {})
    stellar = u.get("stellar_account", "")
    hf = u.get("hf_token", "")
    return {
        "username": user["username"],
        "role": user["role"],
        "stellar_account": stellar,
        "hf_token_set": bool(hf),
        "setup_required": not stellar,
    }


class UserProfileRequest(BaseModel):
    stellar_account: Optional[str] = None
    hf_token: Optional[str] = None

@app.post("/api/auth/profile")
async def update_profile(req: UserProfileRequest, user: dict = Depends(require_auth)):
    users = _load_users()
    username = user["username"]
    if req.stellar_account is not None:
        users[username]["stellar_account"] = req.stellar_account.strip()
    if req.hf_token is not None:
        users[username]["hf_token"] = req.hf_token.strip()
    _save_users(users)
    # Refresh session data
    stellar = users[username].get("stellar_account", "")
    hf = users[username].get("hf_token", "")
    return {"ok": True, "stellar_account": stellar, "hf_token_set": bool(hf), "setup_required": not stellar}


# ---------------------------------------------------------------------------
# User management (admin only)
# ---------------------------------------------------------------------------
class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "user"  # "user" or "admin"

class ChangePasswordRequest(BaseModel):
    new_password: str

@app.get("/api/admin/users")
async def list_users(_: dict = Depends(require_admin)):
    users = _load_users()
    return [{"username": u, "role": d["role"]} for u, d in users.items()]

@app.post("/api/admin/users")
async def create_user(req: CreateUserRequest, _: dict = Depends(require_admin)):
    if req.role not in ("user", "admin"):
        raise HTTPException(status_code=400, detail="role must be 'user' or 'admin'")
    users = _load_users()
    if req.username in users:
        raise HTTPException(status_code=409, detail=f"User '{req.username}' already exists")
    salt = _secrets.token_hex(16)
    users[req.username] = {
        "password_hash": _hash_password(req.password, salt),
        "salt": salt,
        "role": req.role,
    }
    _save_users(users)
    return {"username": req.username, "role": req.role}

@app.delete("/api/admin/users/{username}")
async def delete_user(username: str, current: dict = Depends(require_admin)):
    if username == current["username"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    users = _load_users()
    if username not in users:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")
    del users[username]
    _save_users(users)
    # Invalidate any active sessions for this user
    for token, s in list(_sessions.items()):
        if s["username"] == username:
            del _sessions[token]
    return {"deleted": username}

@app.post("/api/admin/users/{username}/password")
async def reset_password(username: str, req: ChangePasswordRequest, _: dict = Depends(require_admin)):
    users = _load_users()
    if username not in users:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")
    salt = _secrets.token_hex(16)
    users[username]["password_hash"] = _hash_password(req.new_password, salt)
    users[username]["salt"] = salt
    _save_users(users)
    for token, s in list(_sessions.items()):
        if s["username"] == username:
            del _sessions[token]
    return {"ok": True}

@app.post("/api/auth/change-password")
async def change_own_password(req: ChangePasswordRequest, user: dict = Depends(require_auth)):
    users = _load_users()
    username = user["username"]
    if username not in users:
        raise HTTPException(status_code=404, detail="User not found")
    salt = _secrets.token_hex(16)
    users[username]["password_hash"] = _hash_password(req.new_password, salt)
    users[username]["salt"] = salt
    _save_users(users)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Machines
# ---------------------------------------------------------------------------
@app.get("/api/machines")
async def list_machines():
    machines = []
    for name, m in cfg.machines.items():
        machines.append({
            "name": name,
            "host": m.host,
            "agent_port": m.agent_port,
            "description": m.description,
            "vllm_service_dir": m.vllm_service_dir,
            "online": _machine_online.get(name, False),
            "gpu_cache": _gpu_cache.get(name),
            "gpu_cache_age": time.time() - _gpu_cache_ts.get(name, 0)
            if name in _gpu_cache_ts
            else None,
        })
    return machines


@app.get("/api/machines/{name}/health")
async def machine_health(name: str):
    m = _get_machine(name)
    try:
        result = await _agent_request(m, "GET", "/health", timeout=5)
        _machine_online[name] = True
        return result
    except Exception:
        _machine_online[name] = False
        raise HTTPException(status_code=502, detail=f"Agent on {name} unreachable")


# ---------------------------------------------------------------------------
# GPU Status
# ---------------------------------------------------------------------------
@app.get("/api/machines/{name}/gpu/status")
async def machine_gpu_status(name: str, refresh: bool = False):
    m = _get_machine(name)
    cache_age = time.time() - _gpu_cache_ts.get(name, 0)
    if not refresh and name in _gpu_cache and cache_age < cfg.coordinator.poll_interval:
        return _gpu_cache[name]

    try:
        result = await _agent_request(m, "GET", "/gpu/status", timeout=15)
        _gpu_cache[name] = result
        _gpu_cache_ts[name] = time.time()
        _machine_online[name] = True
        return result
    except Exception:
        _machine_online[name] = False
        if name in _gpu_cache:
            return {**_gpu_cache[name], "_stale": True}
        raise HTTPException(status_code=502, detail=f"Agent on {name} unreachable")


# ---------------------------------------------------------------------------
# Command Execution
# ---------------------------------------------------------------------------
class ExecuteRequest(BaseModel):
    command: str
    timeout: int = 300
    background: bool = False
    working_dir: Optional[str] = None


@app.post("/api/machines/{name}/execute")
async def execute_on_machine(name: str, req: ExecuteRequest):
    m = _get_machine(name)
    return await _agent_request(
        m,
        "POST",
        "/execute",
        json_body=req.model_dump(),
        timeout=req.timeout + 10,
    )


@app.get("/api/machines/{name}/execute/{pid}")
async def get_background_job(name: str, pid: int):
    m = _get_machine(name)
    return await _agent_request(m, "GET", f"/execute/{pid}")


# ---------------------------------------------------------------------------
# Task Queue
# ---------------------------------------------------------------------------
class TaskSubmitRequest(BaseModel):
    command: str
    gpu_requirement: int = 0


@app.post("/api/machines/{name}/tasks/submit")
async def submit_task(name: str, req: TaskSubmitRequest):
    m = _get_machine(name)
    return await _agent_request(m, "POST", "/tasks/submit", json_body=req.model_dump())


@app.get("/api/machines/{name}/tasks")
async def list_tasks(name: str, status: Optional[str] = None, limit: int = 50):
    m = _get_machine(name)
    params = {"limit": limit}
    if status:
        params["status"] = status
    return await _agent_request(m, "GET", "/tasks", params=params)


@app.get("/api/machines/{name}/tasks/{task_id}")
async def get_task(name: str, task_id: str):
    m = _get_machine(name)
    return await _agent_request(m, "GET", f"/tasks/{task_id}")


@app.delete("/api/machines/{name}/tasks/{task_id}")
async def cancel_task(name: str, task_id: str):
    m = _get_machine(name)
    return await _agent_request(m, "DELETE", f"/tasks/{task_id}")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class HFTokenRequest(BaseModel):
    token: str

@app.get("/api/settings")
async def get_settings():
    masked = f"hf_{'*' * 16}{_hf_token[-4:]}" if _hf_token else ""
    return {"hf_token_set": bool(_hf_token), "hf_token_masked": masked}

@app.post("/api/settings/hf-token")
async def set_hf_token(req: HFTokenRequest):
    global _hf_token
    _hf_token = req.token.strip()
    _save_secrets()
    masked = f"hf_{'*' * 16}{_hf_token[-4:]}" if _hf_token else ""
    return {"hf_token_set": bool(_hf_token), "hf_token_masked": masked}

@app.delete("/api/settings/hf-token")
async def delete_hf_token():
    global _hf_token
    _hf_token = ""
    _save_secrets()
    return {"hf_token_set": False}


# ---------------------------------------------------------------------------
# LLM Services
# ---------------------------------------------------------------------------

_LIST_MODELS_CMD = """python3 -c "
import os, json
d = '{vllm_dir}'
models = []
for f in sorted(os.listdir(d)):
    if not f.endswith('.env'):
        continue
    name = f[:-4]
    if not name:
        continue
    env = {{}}
    with open(os.path.join(d, f)) as fp:
        for line in fp:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    models.append({{'name': name, 'model': env.get('VLLM_MODEL', ''), 'port': env.get('VLLM_PORT', '8000'), 'served_name': env.get('VLLM_SERVED_MODEL_NAME', name), 'which_gpu': int(env.get('VLLM_WHICH_GPU', '0')), 'memory_utilization': float(env.get('VLLM_GPU_MEMORY_UTILIZATION', '0.85'))}})
print(json.dumps(models))
" """


def _require_vllm(m: MachineConfig) -> str:
    if not m.vllm_service_dir:
        raise HTTPException(status_code=404, detail=f"No vllm_service_dir configured for {m.name}")
    return m.vllm_service_dir


@app.get("/api/machines/{name}/llm/models")
async def list_llm_models(name: str):
    m = _get_machine(name)
    vd = _require_vllm(m)
    cmd = _LIST_MODELS_CMD.format(vllm_dir=vd)
    result = await _agent_request(m, "POST", "/execute", json_body={"command": cmd, "timeout": 10})
    try:
        return json.loads(result["stdout"])
    except Exception:
        raise HTTPException(status_code=500, detail=f"Failed to parse models: {result.get('stdout')}")


@app.get("/api/machines/{name}/llm/running")
async def list_running_llm(name: str, user: dict = Depends(require_auth)):
    m = _get_machine(name)
    _require_vllm(m)
    cmd = "docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null || true"
    result = await _agent_request(m, "POST", "/execute", json_body={"command": cmd, "timeout": 10})
    containers = []
    for line in result.get("stdout", "").strip().splitlines():
        parts = line.split("\t")
        cname = parts[0] if len(parts) > 0 else ""
        owner = next((r["username"] for r in _deploy_records if r["machine"] == name and r["container"] == cname), None)
        containers.append({
            "name": cname,
            "image": parts[1] if len(parts) > 1 else "",
            "status": parts[2] if len(parts) > 2 else "",
            "ports": parts[3] if len(parts) > 3 else "",
            "owner": owner,
        })
    return containers


class LLMStopRequest(BaseModel):
    container: str  # exact container name, e.g. "qwen3dot5-35b-a3b-vllm-1"

@app.post("/api/machines/{name}/llm/stop")
async def stop_llm_container(name: str, req: LLMStopRequest, user: dict = Depends(require_auth)):
    m = _get_machine(name)
    _require_vllm(m)
    container = req.container
    if not container or any(c in container for c in [';', '&', '|', '$', '`']):
        raise HTTPException(status_code=400, detail="Invalid container name")
    if user["role"] != "admin":
        owner = next((r["username"] for r in _deploy_records if r["machine"] == name and r["container"] == container), None)
        if owner != user["username"]:
            raise HTTPException(status_code=403, detail="You can only stop containers you deployed")
    cmd = f"docker stop {container} && docker rm {container}"
    result = await _agent_request(m, "POST", "/execute", json_body={"command": cmd, "timeout": 30})
    if result.get("exit_code", 0) != 0:
        raise HTTPException(status_code=500, detail=result.get("stderr", "Failed to stop container"))
    return {"stopped": container}


class LLMDeployRequest(BaseModel):
    model: str           # env file stem, e.g. "qwen3dot5-35b-a3b"
    which_gpu: Optional[int] = None  # override VLLM_WHICH_GPU if provided


@app.get("/api/llm/my-tasks")
async def get_my_deploy_tasks(user: dict = Depends(require_auth)):
    username = user["username"]
    records = [r for r in _deploy_records if user["role"] == "admin" or r["username"] == username]
    result = []
    for r in records:
        m = cfg.machines.get(r["machine"])
        status = "unknown"
        if m:
            try:
                task = await _agent_request(m, "GET", f"/tasks/{r['task_id']}", timeout=5)
                status = task.get("status", "unknown")
            except Exception:
                pass
        result.append({**r, "task_status": status})
    return result


@app.post("/api/machines/{name}/llm/deploy")
async def deploy_llm_model(name: str, req: LLMDeployRequest, user: dict = Depends(require_auth)):
    m = _get_machine(name)
    vd = _require_vllm(m)

    # Fetch model config to check memory requirement
    list_cmd = _LIST_MODELS_CMD.format(vllm_dir=vd)
    list_result = await _agent_request(m, "POST", "/execute", json_body={"command": list_cmd, "timeout": 10})
    try:
        all_models = json.loads(list_result["stdout"])
    except Exception:
        all_models = []
    model_cfg = next((x for x in all_models if x["name"] == req.model), None)

    which_gpu = req.which_gpu if req.which_gpu is not None else (model_cfg.get("which_gpu", 0) if model_cfg else 0)
    required_mib = None
    if model_cfg:
        gpu_status = _gpu_cache.get(name)
        if gpu_status:
            gpus = gpu_status.get("gpus", [])
            if which_gpu < len(gpus):
                mem_util = model_cfg.get("memory_utilization", 0.85)
                required_mib = round(mem_util * gpus[which_gpu]["memory_total_mib"])

    users = _load_users()
    user_hf = users.get(user["username"], {}).get("hf_token", "")
    effective_hf = user_hf or _hf_token
    if not effective_hf:
        raise HTTPException(
            status_code=400,
            detail="No HuggingFace token configured. Set your HF token in your profile before deploying.",
        )

    sed_exprs = f's/^export VLLM_SERVED_MODEL_NAME=.*/export VLLM_SERVED_MODEL_NAME="{req.model}"/'
    sed_exprs += f'; s|docker compose -p|export VLLM_WHICH_GPU={which_gpu}\\n    docker compose -p|'
    sed_exprs += f'; s/^export HUGGING_FACE_HUB_TOKEN=.*/export HUGGING_FACE_HUB_TOKEN="{effective_hf}"/'
    sed_exprs += f'; s/^export HF_TOKEN=.*/export HF_TOKEN="{effective_hf}"/'

    # Prepend a memory-wait loop so the task queues until GPU is free
    if required_mib is not None:
        wait_loop = (
            f'echo "Waiting for GPU {which_gpu} to have {required_mib} MiB free..."; '
            f'while true; do '
            f'  _free=$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits -i {which_gpu} | tr -d " "); '
            f'  if [ "${{_free}}" -ge {required_mib} ]; then '
            f'    echo "GPU memory ready: ${{_free}} MiB free"; break; '
            f'  fi; '
            f'  echo "Need {required_mib} MiB, have ${{_free}} MiB — retrying in 60s..."; sleep 60; '
            f'done && '
        )
    else:
        wait_loop = ""
    cmd = f"{wait_loop}cd {vd} && sed '{sed_exprs}' remote.sh | bash"
    result = await _agent_request(m, "POST", "/tasks/submit", json_body={"command": cmd})
    container_name = f"{req.model}-vllm-1"
    _deploy_records.append({
        "task_id": result["id"],
        "machine": name,
        "model": req.model,
        "container": container_name,
        "username": user["username"],
        "submitted_at": time.time(),
    })
    return result


# ---------------------------------------------------------------------------
# Idle auto-stop
# ---------------------------------------------------------------------------

@app.get("/api/llm/idle-status")
async def get_idle_status():
    now = time.time()
    timeout_s = cfg.llm_idle.timeout_hours * 3600
    return {
        "timeout_hours": cfg.llm_idle.timeout_hours,
        "check_interval_hours": cfg.llm_idle.check_interval_hours,
        "containers": {
            key: {
                "idle_seconds": round(now - ts),
                "idle_minutes": round((now - ts) / 60),
                "will_stop_in_seconds": max(0, round(timeout_s - (now - ts))),
            }
            for key, ts in _container_last_active.items()
        },
        "recent_stops": _idle_log[-20:],
    }


async def _check_idle_containers():
    now = time.time()
    timeout_s = cfg.llm_idle.timeout_hours * 3600
    check_minutes = max(1, int(cfg.llm_idle.check_interval_hours * 60))

    for machine_name, m in cfg.machines.items():
        if not m.vllm_service_dir or not _machine_online.get(machine_name):
            continue

        # List only vllm containers (image vllm-rtx5090:latest)
        list_cmd = "docker ps --filter 'ancestor=vllm-rtx5090:latest' --format '{{.Names}}' 2>/dev/null || true"
        try:
            res = await _agent_request(m, "POST", "/execute", json_body={"command": list_cmd, "timeout": 10})
            containers = [l.strip() for l in res.get("stdout", "").splitlines() if l.strip()]
        except Exception:
            continue

        for container in containers:
            key = f"{machine_name}:{container}"

            # Check for recent inference activity via "Finished request" in logs
            activity_cmd = f"docker logs --since {check_minutes}m {container} 2>&1 | grep -c 'Finished request' || echo 0"
            try:
                res = await _agent_request(m, "POST", "/execute", json_body={"command": activity_cmd, "timeout": 10})
                count = int(res.get("stdout", "0").strip())
            except Exception:
                count = 0

            if count > 0 or key not in _container_last_active:
                _container_last_active[key] = now

            idle_s = now - _container_last_active[key]
            if idle_s >= timeout_s:
                stop_cmd = f"docker stop {container} && docker rm {container}"
                try:
                    await _agent_request(m, "POST", "/execute", json_body={"command": stop_cmd, "timeout": 30})
                    event = {"time": now, "machine": machine_name, "container": container, "idle_hours": round(idle_s / 3600, 2)}
                    _idle_log.append(event)
                    del _container_last_active[key]
                    print(f"[idle-stop] {machine_name}:{container} idle {idle_s/3600:.1f}h — stopped")
                except Exception as e:
                    print(f"[idle-stop] failed to stop {machine_name}:{container}: {e}")


async def _idle_checker_loop():
    check_interval_s = cfg.llm_idle.check_interval_hours * 3600
    while True:
        await asyncio.sleep(check_interval_s)
        await _check_idle_containers()


# ---------------------------------------------------------------------------
# Background poller
# ---------------------------------------------------------------------------
async def _poll_gpu_status():
    while True:
        for name, m in cfg.machines.items():
            try:
                result = await _agent_request(m, "GET", "/gpu/status", timeout=10)
                _gpu_cache[name] = result
                _gpu_cache_ts[name] = time.time()
                _machine_online[name] = True
            except Exception:
                _machine_online[name] = False
        await asyncio.sleep(cfg.coordinator.poll_interval)


@app.on_event("startup")
async def startup():
    asyncio.create_task(_poll_gpu_status())
    asyncio.create_task(_idle_checker_loop())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=cfg.coordinator.host,
        port=cfg.coordinator.port,
    )
