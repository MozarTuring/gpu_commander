"""GPU Commander Coordinator — central server running on your Mac."""

import asyncio
import json
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

from config import load_config, AppConfig, MachineConfig

cfg: AppConfig = load_config()

app = FastAPI(title="GPU Commander Coordinator")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

# Cached GPU status per machine
_gpu_cache: dict[str, dict] = {}
_gpu_cache_ts: dict[str, float] = {}
_machine_online: dict[str, bool] = {}


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
async def list_running_llm(name: str):
    m = _get_machine(name)
    _require_vllm(m)
    cmd = "docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null || true"
    result = await _agent_request(m, "POST", "/execute", json_body={"command": cmd, "timeout": 10})
    containers = []
    for line in result.get("stdout", "").strip().splitlines():
        parts = line.split("\t")
        containers.append({
            "name": parts[0] if len(parts) > 0 else "",
            "image": parts[1] if len(parts) > 1 else "",
            "status": parts[2] if len(parts) > 2 else "",
            "ports": parts[3] if len(parts) > 3 else "",
        })
    return containers


class LLMDeployRequest(BaseModel):
    model: str  # env file stem, e.g. "qwen3dot5-35b-a3b"


@app.post("/api/machines/{name}/llm/deploy")
async def deploy_llm_model(name: str, req: LLMDeployRequest):
    m = _get_machine(name)
    vd = _require_vllm(m)
    # Override VLLM_SERVED_MODEL_NAME via sed so remote.sh uses the selected model
    cmd = (
        f"cd {vd} && "
        f"sed 's/^export VLLM_SERVED_MODEL_NAME=.*/export VLLM_SERVED_MODEL_NAME=\"{req.model}\"/' remote.sh | bash"
    )
    result = await _agent_request(m, "POST", "/tasks/submit", json_body={"command": cmd})
    return result


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
