"""GPU Commander Agent — FastAPI daemon running on each GPU machine."""

import asyncio
import platform
import time
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

from gpu_monitor import get_gpu_status
from task_queue import TaskQueue

AUTH_TOKEN: str = ""
AGENT_PORT: int = 9850

app = FastAPI(title="GPU Commander Agent")
task_queue = TaskQueue()


def verify_token(x_agent_token: str = Header(default="")):
    if x_agent_token != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "hostname": platform.node(),
        "timestamp": time.time(),
    }


# ---------------------------------------------------------------------------
# GPU Status
# ---------------------------------------------------------------------------
@app.get("/gpu/status")
async def gpu_status(x_agent_token: str = Header(default="")):
    verify_token(x_agent_token)
    try:
        status = get_gpu_status()
        return status.to_dict()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Command Execution
# ---------------------------------------------------------------------------
class ExecuteRequest(BaseModel):
    command: str
    timeout: int = 300
    background: bool = False
    working_dir: Optional[str] = None


class BackgroundJob:
    def __init__(self, proc: asyncio.subprocess.Process, command: str):
        self.proc = proc
        self.command = command
        self.started_at = time.time()
        self.stdout_chunks: list[str] = []
        self.stderr_chunks: list[str] = []
        self.finished = False
        self.exit_code: int | None = None


_background_jobs: dict[int, BackgroundJob] = {}


@app.post("/execute")
async def execute(req: ExecuteRequest, x_agent_token: str = Header(default="")):
    verify_token(x_agent_token)

    kwargs = {}
    if req.working_dir:
        kwargs["cwd"] = req.working_dir

    if req.background:
        proc = await asyncio.create_subprocess_shell(
            req.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        job = BackgroundJob(proc, req.command)
        _background_jobs[proc.pid] = job
        asyncio.create_task(_collect_background(job))
        return {"pid": proc.pid, "status": "running", "command": req.command}

    try:
        proc = await asyncio.create_subprocess_shell(
            req.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=req.timeout
        )
        return {
            "exit_code": proc.returncode,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
        }
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(status_code=408, detail="Command timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def _collect_background(job: BackgroundJob):
    stdout, stderr = await job.proc.communicate()
    job.stdout_chunks.append(stdout.decode(errors="replace"))
    job.stderr_chunks.append(stderr.decode(errors="replace"))
    job.finished = True
    job.exit_code = job.proc.returncode


@app.get("/execute/{pid}")
async def get_background_job(pid: int, x_agent_token: str = Header(default="")):
    verify_token(x_agent_token)
    job = _background_jobs.get(pid)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "pid": pid,
        "command": job.command,
        "finished": job.finished,
        "exit_code": job.exit_code,
        "stdout": "".join(job.stdout_chunks),
        "stderr": "".join(job.stderr_chunks),
        "running_for_seconds": time.time() - job.started_at,
    }


# ---------------------------------------------------------------------------
# Task Queue
# ---------------------------------------------------------------------------
class TaskSubmitRequest(BaseModel):
    command: str
    gpu_requirement: int = 0


@app.post("/tasks/submit")
async def submit_task(
    req: TaskSubmitRequest, x_agent_token: str = Header(default="")
):
    verify_token(x_agent_token)
    task = await task_queue.submit(req.command, req.gpu_requirement)
    return task.to_dict()


@app.get("/tasks")
async def list_tasks(
    status: Optional[str] = None,
    limit: int = 50,
    x_agent_token: str = Header(default=""),
):
    verify_token(x_agent_token)
    from task_queue import TaskStatus

    ts = TaskStatus(status) if status else None
    return task_queue.list_tasks(status=ts, limit=limit)


@app.get("/tasks/{task_id}")
async def get_task(task_id: str, x_agent_token: str = Header(default="")):
    verify_token(x_agent_token)
    task = task_queue.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.delete("/tasks/{task_id}")
async def cancel_task(task_id: str, x_agent_token: str = Header(default="")):
    verify_token(x_agent_token)
    task = await task_queue.cancel(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task.to_dict()


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup():
    task_queue.start_worker()


if __name__ == "__main__":
    import argparse
    import uvicorn

    _default_config = str(Path(__file__).resolve().parent.parent / "config.yaml")

    parser = argparse.ArgumentParser(description="GPU Commander Agent")
    parser.add_argument(
        "--config", "-c",
        default=_default_config,
        help="Path to config.yaml (default: %(default)s)",
    )
    args = parser.parse_args()

    with open(args.config) as _f:
        _cfg = yaml.safe_load(_f)
    AUTH_TOKEN = _cfg.get("auth", {}).get("token", "gpu-commander-secret-change-me")
    AGENT_PORT = _cfg.get("agent_port", 9850)

    uvicorn.run(app, host="0.0.0.0", port=AGENT_PORT)
