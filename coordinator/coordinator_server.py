"""GPU Commander Coordinator — central server running on your Mac."""

import asyncio
import base64 as _base64
import hashlib
import hmac as _hmac
import json
import secrets as _secrets
import time
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, Response
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
_SESSION_TTL = 30 * 24 * 3600     # 30 days — JWT expiry

# ---------------------------------------------------------------------------
# JWT (HS256, stdlib only — no PyJWT dependency)
# ---------------------------------------------------------------------------
def _b64url_encode(data: bytes) -> str:
    return _base64.urlsafe_b64encode(data).rstrip(b'=').decode()

def _b64url_decode(s: str) -> bytes:
    pad = 4 - len(s) % 4
    return _base64.urlsafe_b64decode(s + '=' * (pad % 4))

def _jwt_create(payload: dict) -> str:
    header = _b64url_encode(b'{"alg":"HS256","typ":"JWT"}')
    body = _b64url_encode(json.dumps(payload, separators=(',', ':')).encode())
    signing_input = f"{header}.{body}"
    sig = _b64url_encode(_hmac.new(cfg.auth_token.encode(), signing_input.encode(), hashlib.sha256).digest())
    return f"{signing_input}.{sig}"

def _jwt_verify(token: str) -> dict | None:
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return None
        header, body, sig = parts
        signing_input = f"{header}.{body}"
        expected = _b64url_encode(_hmac.new(cfg.auth_token.encode(), signing_input.encode(), hashlib.sha256).digest())
        if not _hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_b64url_decode(body))
        if payload.get('exp', 0) < time.time():
            return None
        return payload
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Server version (written to version.txt by remote.sh at deploy time)
# ---------------------------------------------------------------------------
_VERSION_FILE = Path(__file__).resolve().parent.parent / "version.txt"
_SERVER_VERSION = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "unknown"

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
            "must_change_password": True,
        }
        _save_users(users)
        print(f"\n{'='*52}")
        print(f"  ADMIN CREDENTIALS (first-time setup)")
        print(f"  Username : admin")
        print(f"  Password : {password}")
        print(f"  Change this after first login!")
        print(f"{'='*52}\n")

_bootstrap_admin()

# Paths that don't need authentication
_UNPROTECTED = {"/", "/api/auth/login", "/api/version"}
_UNPROTECTED_PREFIXES = ("/text/", "/audio/", "/tts/", "/ui/", "/static/", "/api/router/")

def _check_request_auth(request: Request) -> dict | None:
    """Return user dict if authenticated, else None."""
    # Agent-to-coordinator calls use X-Agent-Token
    if request.headers.get("X-Agent-Token") == cfg.auth_token:
        return {"username": "_agent", "role": "admin"}
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return _jwt_verify(auth[7:])
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
    if request.url.path in _UNPROTECTED or request.url.path.startswith(_UNPROTECTED_PREFIXES):
        return await call_next(request)
    if not _check_request_auth(request):
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    return await call_next(request)

# Cached GPU status per machine
_gpu_cache: dict[str, dict] = {}
_gpu_cache_ts: dict[str, float] = {}
_machine_online: dict[str, bool] = {}

# Per-machine deploy lock: ensures memory check + task submission are atomic
# so concurrent requests don't both see "enough memory" and both proceed
_deploy_locks: dict[str, asyncio.Lock] = {}

# Idle tracking: "machine:container" -> last_active timestamp
_container_last_active: dict[str, float] = {}
_idle_log: list[dict] = []  # recent auto-stop events

# Deploy ownership: list of {task_id, machine, model, container, username, submitted_at}
_DEPLOY_RECORDS_FILE = Path(__file__).resolve().parent.parent / "deploy_records.json"

def _load_deploy_records() -> list[dict]:
    if _DEPLOY_RECORDS_FILE.exists():
        try:
            return json.loads(_DEPLOY_RECORDS_FILE.read_text())
        except Exception:
            return []
    return []

def _save_deploy_records() -> None:
    _DEPLOY_RECORDS_FILE.write_text(json.dumps(_deploy_records, indent=2))

_deploy_records: list[dict] = _load_deploy_records()


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
    token = _jwt_create({
        "username": req.username,
        "role": user["role"],
        "iat": int(time.time()),
        "exp": int(time.time()) + _SESSION_TTL,
    })
    return {"token": token, "username": req.username, "role": user["role"]}

@app.post("/api/auth/logout")
async def logout():
    # JWT is stateless — client simply discards the token
    return {"ok": True}

@app.get("/api/version")
async def version():
    return {"version": _SERVER_VERSION}

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
        "setup_required": not stellar or u.get("must_change_password", False),
        "must_change_password": u.get("must_change_password", False),
    }


class UserProfileRequest(BaseModel):
    stellar_account: Optional[str] = None
    hf_token: Optional[str] = None
    new_password: Optional[str] = None

@app.post("/api/auth/profile")
async def update_profile(req: UserProfileRequest, user: dict = Depends(require_auth)):
    users = _load_users()
    username = user["username"]
    if req.stellar_account is not None:
        users[username]["stellar_account"] = req.stellar_account.strip()
    if req.hf_token is not None:
        users[username]["hf_token"] = req.hf_token.strip()
    if req.new_password:
        salt = _secrets.token_hex(16)
        users[username]["password_hash"] = _hash_password(req.new_password, salt)
        users[username]["salt"] = salt
        users[username]["must_change_password"] = False
    _save_users(users)
    stellar = users[username].get("stellar_account", "")
    hf = users[username].get("hf_token", "")
    return {"ok": True, "stellar_account": stellar, "hf_token_set": bool(hf),
            "setup_required": not stellar or users[username].get("must_change_password", False)}


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
        "must_change_password": True,
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

@app.post("/api/settings/idle-timeout")
async def set_idle_timeout(request: Request, user: dict = Depends(require_admin)):
    body = await request.json()
    hours = float(body["timeout_hours"])
    cfg.llm_idle.timeout_hours = hours
    # Persist to config.yaml
    import yaml as _yaml
    with open(_cfg_path) as f:
        raw = _yaml.safe_load(f)
    raw.setdefault("llm_idle", {})["timeout_hours"] = hours
    with open(_cfg_path, "w") as f:
        _yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
    return {"timeout_hours": hours}


@app.delete("/api/settings/hf-token")
async def delete_hf_token():
    global _hf_token
    _hf_token = ""
    _save_secrets()
    return {"hf_token_set": False}


# ---------------------------------------------------------------------------
# LLM Services
# ---------------------------------------------------------------------------

def _read_llm_models(vllm_dir: str) -> list[dict]:
    """List models from llm_services/{category}/{model}/ structure."""
    import os as _os, re as _re, yaml as _yaml
    models = []
    services_dir = _os.path.join(vllm_dir, "llm_services")
    if not _os.path.isdir(services_dir):
        return models
    for category in sorted(_os.listdir(services_dir)):
        category_dir = _os.path.join(services_dir, category)
        if not _os.path.isdir(category_dir) or category.startswith("."):
            continue
        for dir_name in sorted(_os.listdir(category_dir)):
            model_dir = _os.path.join(category_dir, dir_name)
            compose_path = _os.path.join(model_dir, "docker-compose.yml")
            if not _os.path.isfile(compose_path):
                continue
            mem_util = None
            env_file = _os.path.join(model_dir, ".env")
            if _os.path.isfile(env_file):
                try:
                    with open(env_file) as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith("GPU_MEMORY_UTILIZATION="):
                                mem_util = float(line.split("=", 1)[1])
                                break
                except (ValueError, IOError):
                    pass
            hf_model = ""
            model_type = "other"
            try:
                with open(compose_path) as f:
                    compose = _yaml.safe_load(f)
                svc = next(iter((compose.get("services") or {}).values()), {})
                cmd = str(svc.get("command", ""))
                env_list = [e for e in svc.get("environment", []) if isinstance(e, str)]
                if "vllm serve" in cmd:
                    model_type = "vllm"
                    m = _re.search(r'vllm serve\s+(\S+)', cmd)
                    hf_model = m.group(1) if m else ""
                elif any("WHISPER_MODEL" in e for e in env_list):
                    model_type = "whisper"
                    for e in env_list:
                        if e.startswith("WHISPER_MODEL="):
                            hf_model = e.split("=", 1)[1]
                            break
                else:
                    hf_model = str(svc.get("image", ""))
            except Exception:
                pass
            models.append({
                "name": dir_name,
                "model": hf_model,
                "memory_utilization": mem_util,
                "type": model_type,
                "category": category,
                "container_name": dir_name,
                "compose_dir": f"{category}/{dir_name}",
                "compose_service": "",
            })
    return models


def _require_vllm(m: MachineConfig) -> str:
    if not m.vllm_service_dir:
        raise HTTPException(status_code=404, detail=f"No vllm_service_dir configured for {m.name}")
    return m.vllm_service_dir


@app.get("/api/machines/{name}/llm/models")
async def list_llm_models(name: str):
    m = _get_machine(name)
    vd = _require_vllm(m)
    return _read_llm_models(vd)


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
    # Unregister from model routes
    rec = next((r for r in reversed(_deploy_records) if r["machine"] == name and r["container"] == container), None)
    if rec:
        _unregister_route(rec["model"], name)
    return {"stopped": container}


class LLMDeployRequest(BaseModel):
    model: str           # env file stem, e.g. "qwen3dot5-35b-a3b"
    force_build: bool = False  # rebuild Docker image before starting


@app.get("/api/llm/my-tasks")
async def get_my_deploy_tasks(user: dict = Depends(require_auth)):
    records = list(_deploy_records)  # all users' tasks visible to everyone

    # Fetch running containers once per machine (only for machines that have completed tasks)
    machines_needed = {r["machine"] for r in records}
    running_by_machine: dict[str, list[dict]] = {}
    for mname in machines_needed:
        m = cfg.machines.get(mname)
        if not m or not _machine_online.get(mname):
            continue
        try:
            cmd = "docker ps -a --format '{{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null || true"
            res = await _agent_request(m, "POST", "/execute", json_body={"command": cmd, "timeout": 10})
            running_by_machine[mname] = []
            for line in res.get("stdout", "").strip().splitlines():
                parts = line.split("\t")
                running_by_machine[mname].append({
                    "name": parts[0] if len(parts) > 0 else "",
                    "status": parts[1] if len(parts) > 1 else "",
                    "ports": parts[2] if len(parts) > 2 else "",
                })
        except Exception:
            pass

    # Fetch all tasks per machine in one call instead of per-record
    tasks_by_machine: dict[str, dict[str, str]] = {}  # machine -> {task_id: status}
    for mname in machines_needed:
        m = cfg.machines.get(mname)
        if not m or not _machine_online.get(mname):
            continue
        try:
            all_tasks = await _agent_request(m, "GET", "/tasks", params={"limit": 200}, timeout=10)
            tasks_by_machine[mname] = {t["id"]: t["status"] for t in all_tasks}
        except Exception:
            pass

    result = []
    for r in records:
        task_status = tasks_by_machine.get(r["machine"], {}).get(r["task_id"], "unknown")
        entry = {**r, "task_status": task_status}
        # For completed tasks, enrich with live Docker status and ports
        if task_status == "completed" and r.get("container"):
            containers = running_by_machine.get(r["machine"], [])
            match = next((c for c in containers if c["name"] == r["container"]), None)
            if match:
                entry["container_status"] = match["status"]
                entry["container_ports"] = match["ports"]
            elif r["machine"] in running_by_machine:
                # Task completed but container gone — user stopped it
                entry["container_status"] = "stopped"
        result.append(entry)
    return result


@app.post("/api/machines/{name}/llm/deploy")
async def deploy_llm_model(name: str, req: LLMDeployRequest, user: dict = Depends(require_auth)):
    m = _get_machine(name)
    vd = _require_vllm(m)

    # Serialize deploys per machine: prevents two concurrent requests both
    # seeing "enough memory" and racing to deploy on the same GPU
    if name not in _deploy_locks:
        _deploy_locks[name] = asyncio.Lock()
    async with _deploy_locks[name]:

        # Fetch model config
        all_models = _read_llm_models(vd)
        model_cfg = next((x for x in all_models if x["name"] == req.model), None)
        is_whisper = model_cfg.get("type") == "whisper" if model_cfg else False

        users = _load_users()
        user_hf = users.get(user["username"], {}).get("hf_token", "")
        effective_hf = user_hf or _hf_token
        if not effective_hf:
            raise HTTPException(
                status_code=400,
                detail="No HuggingFace token configured. Set your HF token in your profile before deploying.",
            )

        gpu_status = _gpu_cache.get(name)
        gpus = gpu_status.get("gpus", []) if gpu_status else []

        if is_whisper:
            # Whisper: no GPU selection or memory check needed
            which_gpu = 0
            memory_insufficient = False
            wait_loop = ""
        else:
            # Search all machines for the best available GPU (no ongoing deploy + most free memory)
            best = None  # (machine_name, machine_obj, gpu_idx, free_mib, vllm_dir)
            machines_to_check = [(name, m, vd)]  # prefer the requested machine first
            for alt_name, alt_m in cfg.machines.items():
                if alt_name != name and alt_m.vllm_service_dir and _machine_online.get(alt_name):
                    alt_vd = alt_m.vllm_service_dir
                    machines_to_check.append((alt_name, alt_m, alt_vd))

            for mname, mobj, mvd in machines_to_check:
                mgpus = (_gpu_cache.get(mname) or {}).get("gpus", [])
                if not mgpus:
                    continue
                # Find reserved GPUs on this machine
                reserved = set()
                try:
                    list_cmd = "docker ps --format '{{.Names}}\t{{.Status}}' 2>/dev/null || true"
                    res = await _agent_request(mobj, "POST", "/execute", json_body={"command": list_cmd, "timeout": 10})
                    for line in res.get("stdout", "").splitlines():
                        parts = line.strip().split("\t", 1)
                        if len(parts) < 2:
                            continue
                        cname, cstatus = parts[0].strip(), parts[1].strip()
                        if "health: starting" in cstatus:
                            rec = next((r for r in reversed(_deploy_records) if r["machine"] == mname and r["container"] == cname), None)
                            if rec and "which_gpu" in rec:
                                reserved.add(int(rec["which_gpu"]))
                except Exception:
                    pass
                for rec in _deploy_records:
                    if rec["machine"] != mname or "which_gpu" not in rec:
                        continue
                    try:
                        task = await _agent_request(mobj, "GET", f"/tasks/{rec['task_id']}", timeout=5)
                        if task.get("status") in ("queued", "running"):
                            reserved.add(int(rec["which_gpu"]))
                    except Exception:
                        pass

                for i, gpu in enumerate(mgpus):
                    if i in reserved:
                        continue
                    free = gpu["memory_free_mib"]
                    if best is None or free > best[3]:
                        best = (mname, mobj, i, free, mvd)

            if best is None:
                raise HTTPException(status_code=409, detail="All GPUs on all machines have ongoing deploys. Wait for them to finish.")

            name, m, which_gpu, _, vd = best
            gpus = (_gpu_cache.get(name) or {}).get("gpus", [])

            required_mib = None
            if model_cfg and model_cfg.get("memory_utilization") and which_gpu < len(gpus):
                required_mib = round(model_cfg["memory_utilization"] * gpus[which_gpu]["memory_total_mib"])
            free_mib = gpus[which_gpu]["memory_free_mib"] if which_gpu < len(gpus) else None
            memory_insufficient = required_mib is not None and free_mib is not None and free_mib < required_mib
            if memory_insufficient:
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

        compose_dir = model_cfg.get("compose_dir", req.model) if model_cfg else req.model
        compose_service = model_cfg.get("compose_service", "") if model_cfg else ""
        force_build_export = 'export FORCE_BUILD=1 && ' if req.force_build else ''
        exports = (
            f'{force_build_export}'
            f'export VLLM_SERVED_MODEL_NAME="{req.model}" && '
            f'export MODEL_DIR="{compose_dir}" && '
            f'export COMPOSE_SERVICE="{compose_service}" && '
            f'export VLLM_WHICH_GPU={which_gpu} && '
            f'export HUGGING_FACE_HUB_TOKEN="{effective_hf}" && '
            f'export HF_TOKEN="{effective_hf}"'
        )
        import os as _os
        _run_dir_pre = str(_os.path.dirname(vd))
        _vllm_proj = str(_os.path.basename(vd))
        cmd = (
            f"{wait_loop}{exports} && "
            f"export GPU_CMD_MACHINE_NAME={name} && "
            f"export GPU_CMD_COORDINATOR_URL=http://{'localhost' if name == cfg.coordinator.host_machine else cfg.machines[cfg.coordinator.host_machine].description}:{cfg.coordinator.port} && "
            f"cd {_run_dir_pre} && "
            f"bash -c 'source common_tools/meta_script.sh localmachine {_vllm_proj} remote_docker_compose'"
        )
        result = await _agent_request(m, "POST", "/tasks/submit", json_body={"command": cmd})
        result["memory_insufficient"] = memory_insufficient
        container_name = model_cfg.get("container_name", req.model) if model_cfg else req.model
        category = model_cfg.get("category", "text") if model_cfg else "text"
        _deploy_records.append({
            "task_id": result["id"],
            "machine": name,
            "model": req.model,
            "container": container_name,
            "category": category,
            "username": user["username"],
            "submitted_at": time.time(),
            "which_gpu": which_gpu,
            "hf_token": effective_hf,
            "compose_dir": compose_dir,
            "compose_service": compose_service,
            "vllm_service_dir": vd,
        })
        _save_deploy_records()
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

        known = {r["container"] for r in _deploy_records if r["machine"] == machine_name}
        if not known:
            continue
        list_cmd = "docker ps --format '{{.Names}}\t{{.Status}}' 2>/dev/null || true"
        try:
            res = await _agent_request(m, "POST", "/execute", json_body={"command": list_cmd, "timeout": 10})
            running = set()
            not_yet_healthy = set()
            for line in res.get("stdout", "").splitlines():
                parts = line.strip().split("\t", 1)
                if not parts or not parts[0].strip():
                    continue
                cname = parts[0].strip()
                status = parts[1].strip() if len(parts) > 1 else ""
                running.add(cname)
                if "health: starting" in status or "health: unhealthy" in status:
                    not_yet_healthy.add(cname)
            containers = known & running
        except Exception:
            continue

        for container in containers:
            key = f"{machine_name}:{container}"

            # Check for recent inference activity via "Finished request" in logs
            # Count HTTP requests excluding health/metrics endpoints (works for any web service)
            activity_cmd = f"docker logs --since {check_minutes}m {container} 2>&1 | grep -E '\"(GET|POST|PUT) /' | grep -vE '/(health|metrics|ping|status)' | wc -l"
            try:
                res = await _agent_request(m, "POST", "/execute", json_body={"command": activity_cmd, "timeout": 10})
                count = int(res.get("stdout", "0").strip())
            except Exception:
                count = 0

            if container in not_yet_healthy:
                # Container is starting/restarting — reset timer so it starts fresh when healthy
                _container_last_active.pop(key, None)
                continue
            if key not in _container_last_active or count > 0:
                _container_last_active[key] = now

            idle_s = now - _container_last_active[key]
            if idle_s >= timeout_s:
                stop_cmd = f"docker stop {container} && docker rm {container}"
                try:
                    await _agent_request(m, "POST", "/execute", json_body={"command": stop_cmd, "timeout": 30})
                    event = {"time": now, "machine": machine_name, "container": container, "idle_hours": round(idle_s / 3600, 2)}
                    _idle_log.append(event)
                    del _container_last_active[key]
                    rec = next((r for r in reversed(_deploy_records) if r["machine"] == machine_name and r["container"] == container), None)
                    if rec:
                        _unregister_route(rec["model"], machine_name)
                    print(f"[idle-stop] {machine_name}:{container} idle {idle_s/3600:.1f}h — stopped")
                except Exception as e:
                    print(f"[idle-stop] failed to stop {machine_name}:{container}: {e}")


async def _update_last_active():
    """Fast loop: update _container_last_active for display without stopping containers."""
    now = time.time()
    check_minutes = max(1, cfg.llm_idle.last_active_interval // 60 + 1)
    for machine_name, m in cfg.machines.items():
        if not m.vllm_service_dir or not _machine_online.get(machine_name):
            continue
        known = {r["container"] for r in _deploy_records if r["machine"] == machine_name}
        if not known:
            continue
        list_cmd = "docker ps --format '{{.Names}}\t{{.Status}}' 2>/dev/null || true"
        try:
            res = await _agent_request(m, "POST", "/execute", json_body={"command": list_cmd, "timeout": 10})
            running = set()
            not_yet_healthy = set()
            for line in res.get("stdout", "").splitlines():
                parts = line.strip().split("\t", 1)
                if not parts or not parts[0].strip():
                    continue
                cname = parts[0].strip()
                status = parts[1].strip() if len(parts) > 1 else ""
                running.add(cname)
                if "health: starting" in status or "health: unhealthy" in status:
                    not_yet_healthy.add(cname)
            containers = known & running
        except Exception:
            continue
        for container in containers:
            key = f"{machine_name}:{container}"
            # Count HTTP requests excluding health/metrics endpoints (works for any web service)
            activity_cmd = f"docker logs --since {check_minutes}m {container} 2>&1 | grep -E '\"(GET|POST|PUT) /' | grep -vE '/(health|metrics|ping|status)' | wc -l"
            try:
                res = await _agent_request(m, "POST", "/execute", json_body={"command": activity_cmd, "timeout": 10})
                count = int(res.get("stdout", "0").strip())
            except Exception:
                count = 0
            if container in not_yet_healthy:
                _container_last_active.pop(key, None)
                # Stop crash-looping containers that failed due to insufficient GPU memory
                oom_cmd = f"docker logs --tail 30 {container} 2>&1 | grep -c 'is less than desired GPU memory utilization' || echo 0"
                try:
                    oom_res = await _agent_request(m, "POST", "/execute", json_body={"command": oom_cmd, "timeout": 10})
                    if int(oom_res.get("stdout", "0").strip()) > 0:
                        await _agent_request(m, "POST", "/execute", json_body={"command": f"docker rm -f {container}", "timeout": 30})
                        print(f"[oom-stop] {machine_name}:{container} removed — insufficient GPU memory")
                except Exception:
                    pass
                continue
            if key not in _container_last_active or count > 0:
                _container_last_active[key] = now


async def _last_active_loop():
    while True:
        await asyncio.sleep(cfg.llm_idle.last_active_interval)
        await _update_last_active()


async def _idle_checker_loop():
    check_interval_s = cfg.llm_idle.check_interval_hours * 3600
    while True:
        await asyncio.sleep(check_interval_s)
        await _check_idle_containers()


# ---------------------------------------------------------------------------
# LLM Router — OpenAI-compatible proxy
# ---------------------------------------------------------------------------
# model_routes.json: { "model_name": [{"machine": "...", "host": "...", "port": 8007}, ...] }
import os as _os
_cfg_path = _os.environ.get("GPU_COMMANDER_CONFIG", str(Path(__file__).resolve().parent.parent / "config.yaml"))
_routes_file = Path(_cfg_path).parent / "model_routes.json"
_routes_cache: dict[str, list[dict]] = {}
_routes_mtime: float = 0
_routes_rr_idx: dict[str, int] = {}


def _save_routes_file(routes: dict):
    """Write routes to model_routes.json."""
    with open(_routes_file, "w") as f:
        json.dump(routes, f, indent=2)


def _read_routes_file() -> dict:
    """Read routes from model_routes.json."""
    try:
        with open(_routes_file) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


@app.post("/api/router/register")
async def register_route(request: Request):
    """Called by meta_script after container becomes healthy."""
    body = await request.json()
    model, machine, port = body["model"], body["machine"], body["port"]
    route_type = body.get("type", "api")  # "api", "website", or "both"
    category = body.get("category", "text")
    routes = _read_routes_file()
    routes.setdefault(model, [])
    routes[model] = [e for e in routes[model] if e["machine"] != machine]
    routes[model].append({"machine": machine, "port": port, "type": route_type, "category": category})
    _save_routes_file(routes)
    global _routes_mtime
    _routes_mtime = 0
    print(f"[router] registered {model} ({category}/{route_type}) -> {machine}:{port}")
    return {"status": "ok"}


@app.post("/api/router/unregister")
async def unregister_route_api(request: Request):
    """Called when a container is stopped."""
    body = await request.json()
    model, machine = body["model"], body["machine"]
    _unregister_route(model, machine)
    return {"status": "ok"}


def _load_routes() -> dict[str, list[dict]]:
    """Load routes from file, using mtime cache to avoid re-reading."""
    global _routes_cache, _routes_mtime
    try:
        mt = _routes_file.stat().st_mtime
        if mt != _routes_mtime:
            with open(_routes_file) as f:
                raw = json.load(f)
            resolved = {}
            for model, endpoints in raw.items():
                resolved[model] = []
                for ep in endpoints:
                    m = cfg.machines.get(ep["machine"])
                    host = m.host if m else ep.get("host", "localhost")
                    resolved[model].append({
                        "machine": ep["machine"], "host": host, "port": ep["port"],
                        "type": ep.get("type", "api"), "category": ep.get("category", "text"),
                    })
            _routes_cache = resolved
            _routes_mtime = mt
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return _routes_cache


def _unregister_route(model_name: str, machine_name: str):
    """Remove a model endpoint from model_routes.json."""
    try:
        routes = json.load(open(_routes_file)) if _routes_file.exists() else {}
        if model_name in routes:
            routes[model_name] = [e for e in routes[model_name] if e["machine"] != machine_name]
            if not routes[model_name]:
                del routes[model_name]
            with open(_routes_file, "w") as f:
                json.dump(routes, f, indent=2)
            print(f"[router] unregistered {model_name} on {machine_name}")
    except Exception:
        pass


def _get_model_endpoint(model_name: str, kind: str = "api", category: str | None = None) -> dict:
    """Pick an endpoint for a model using round-robin.
    kind: 'api', 'website', or 'api-other'.
    category: if set, also filter by category (text/audio/ui)."""
    routes = _load_routes()
    all_endpoints = routes.get(model_name) or []
    endpoints = [e for e in all_endpoints if e.get("type", "api") == kind]
    if category:
        endpoints = [e for e in endpoints if e.get("category", "text") == category]
    if not endpoints:
        detail = f"Model '{model_name}' is not running as {kind}"
        if category:
            detail += f" in category '{category}'"
        detail += " on any machine"
        raise HTTPException(status_code=404, detail=detail)
    key = f"{model_name}:{kind}:{category or '*'}"
    idx = _routes_rr_idx.get(key, 0) % len(endpoints)
    _routes_rr_idx[key] = idx + 1
    return endpoints[idx]


# ---------------------------------------------------------------------------
# Category-prefixed routes: /{category}/v1/...
# ---------------------------------------------------------------------------

_VALID_CATEGORIES = ("text", "audio", "tts", "ui")


@app.get("/{category}/v1/models")
async def router_list_models_by_category(category: str):
    if category not in _VALID_CATEGORIES:
        raise HTTPException(status_code=404, detail=f"Unknown category '{category}'")
    routes = _load_routes()
    data = []
    for name, eps in routes.items():
        cat_eps = [e for e in eps if e.get("category", "text") == category]
        if cat_eps:
            data.append({"id": name, "object": "model", "owned_by": "vllm",
                         "category": category,
                         "endpoints": [f"{e['host']}:{e['port']}" for e in cat_eps]})
    return {"object": "list", "data": data}


@app.post("/{category}/v1/chat/completions")
async def router_cat_chat_completions(category: str, request: Request):
    body = await request.json()
    model = body.get("model")
    if not model:
        raise HTTPException(status_code=400, detail="Missing 'model' field")
    ep = _get_model_endpoint(model, category=category)
    return await _proxy_request(f"http://{ep['host']}:{ep['port']}/v1/chat/completions", body, stream=body.get("stream", False))


@app.post("/{category}/v1/completions")
async def router_cat_completions(category: str, request: Request):
    body = await request.json()
    model = body.get("model")
    if not model:
        raise HTTPException(status_code=400, detail="Missing 'model' field")
    ep = _get_model_endpoint(model, category=category)
    return await _proxy_request(f"http://{ep['host']}:{ep['port']}/v1/completions", body, stream=body.get("stream", False))


@app.post("/{category}/v1/embeddings")
async def router_cat_embeddings(category: str, request: Request):
    body = await request.json()
    model = body.get("model")
    if not model:
        raise HTTPException(status_code=400, detail="Missing 'model' field")
    ep = _get_model_endpoint(model, category=category)
    return await _proxy_request(f"http://{ep['host']}:{ep['port']}/v1/embeddings", body)


@app.post("/{category}/v1/images/generations")
async def router_cat_images_generations(category: str, request: Request):
    body = await request.json()
    model = body.get("model")
    if not model:
        raise HTTPException(status_code=400, detail="Missing 'model' field")
    ep = _get_model_endpoint(model, category=category)
    return await _proxy_request(f"http://{ep['host']}:{ep['port']}/v1/images/generations", body)


@app.post("/{category}/v1/audio/transcriptions")
async def router_cat_audio_transcriptions(category: str, request: Request):
    form = await request.form()
    model = form.get("model")
    if not model:
        raise HTTPException(status_code=400, detail="Missing 'model' field")
    ep = _get_model_endpoint(str(model), category=category)
    return await _forward_multipart(ep, form)


@app.post("/{category}/v1/audio/speech")
async def router_cat_audio_speech(category: str, request: Request):
    body = await request.json()
    model = body.get("model")
    if not model:
        raise HTTPException(status_code=400, detail="Missing 'model' field")
    ep = _get_model_endpoint(model, category=category)
    url = f"http://{ep['host']}:{ep['port']}/v1/audio/speech"
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(url, json=body)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers={k: v for k, v in resp.headers.items()
                     if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")},
        )


# ---------------------------------------------------------------------------
# UI proxy: /ui/{model}/...
# ---------------------------------------------------------------------------

@app.get("/ui/{model}")
async def router_ui_redirect(model: str):
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/ui/{model}/", status_code=308)


@app.api_route("/ui/{model}/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def router_ui_proxy(model: str, request: Request, path: str = ""):
    ep = _get_model_endpoint(model, kind="website", category="ui")
    return await _proxy_path(ep, request, path)


# ---------------------------------------------------------------------------
# Proxy helpers
# ---------------------------------------------------------------------------

async def _proxy_path(ep: dict, request: Request, path: str):
    """Forward an arbitrary request path to the endpoint."""
    target_path = f"/{path}" if path else "/"
    if request.url.query:
        target_path += f"?{request.url.query}"
    url = f"http://{ep['host']}:{ep['port']}{target_path}"
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
    async with httpx.AsyncClient(timeout=300, follow_redirects=False) as client:
        resp = await client.request(request.method, url, content=body, headers=headers)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers={k: v for k, v in resp.headers.items() if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")},
        )


async def _forward_multipart(ep: dict, form):
    """Forward multipart form data (audio transcriptions etc.)."""
    url = f"http://{ep['host']}:{ep['port']}/v1/audio/transcriptions"
    files = {}
    data = {}
    for key, value in form.items():
        if hasattr(value, "read"):
            content = await value.read()
            files[key] = (value.filename, content, value.content_type)
        else:
            data[key] = str(value)
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(url, files=files, data=data)
        return JSONResponse(content=resp.json(), status_code=resp.status_code)


async def _proxy_request(url: str, body: dict, stream: bool = False):
    if stream:
        async def _stream():
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream("POST", url, json=body) as resp:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
        return StreamingResponse(_stream(), media_type="text/event-stream")
    else:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(url, json=body)
            return JSONResponse(content=resp.json(), status_code=resp.status_code)


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
    asyncio.create_task(_last_active_loop())


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
