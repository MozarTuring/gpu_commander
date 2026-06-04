"""In-memory task queue with JSON persistence and background execution."""

import asyncio
import json
import os
import signal
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Task:
    id: str
    command: str
    status: TaskStatus = TaskStatus.QUEUED
    created_at: float = 0.0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    pid: Optional[int] = None
    gpu_requirement: int = 0  # min free GPUs needed to start

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d


class TaskQueue:
    def __init__(self, persistence_dir: str = os.path.join(os.path.expanduser("~"), "tmp", "gpu_commander_tasks")):
        self._tasks: dict[str, Task] = {}
        self._running_procs: dict[str, asyncio.subprocess.Process] = {}
        self._readopt_tasks: list[Task] = []
        self._persistence_dir = Path(persistence_dir)
        self._persistence_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._worker_task: Optional[asyncio.Task] = None
        self._max_concurrent = int(os.environ.get("GPU_CMD_MAX_CONCURRENT", "2"))
        self._load_tasks()

    def _persistence_file(self) -> Path:
        return self._persistence_dir / "tasks.json"

    def _load_tasks(self) -> None:
        p = self._persistence_file()
        if not p.exists():
            return
        try:
            with open(p) as f:
                data = json.load(f)
            for d in data:
                task = Task(
                    id=d["id"],
                    command=d["command"],
                    status=TaskStatus(d["status"]),
                    created_at=d.get("created_at", 0),
                    started_at=d.get("started_at"),
                    finished_at=d.get("finished_at"),
                    exit_code=d.get("exit_code"),
                    stdout=d.get("stdout", ""),
                    stderr=d.get("stderr", ""),
                    pid=d.get("pid"),
                    gpu_requirement=d.get("gpu_requirement", 0),
                )
                if task.status == TaskStatus.RUNNING:
                    if task.pid and self._is_pid_alive(task.pid):
                        # Process still running — re-adopt it
                        self._readopt_tasks.append(task)
                    else:
                        task.status = TaskStatus.FAILED
                        task.stderr += "\n[Agent restarted — task marked as failed]"
                        task.finished_at = time.time()
                self._tasks[task.id] = task
        except (json.JSONDecodeError, KeyError):
            pass

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def _save_tasks(self) -> None:
        data = [t.to_dict() for t in self._tasks.values()]
        with open(self._persistence_file(), "w") as f:
            json.dump(data, f, indent=2)

    def start_worker(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_loop())
        # Re-adopt tasks whose PIDs survived the agent restart
        for task in self._readopt_tasks:
            asyncio.create_task(self._monitor_pid(task))
        self._readopt_tasks.clear()

    async def _monitor_pid(self, task: Task) -> None:
        """Poll a surviving PID until it exits, then update task status."""
        while self._is_pid_alive(task.pid):
            await asyncio.sleep(5)
        # Process exited — check exit code via waitpid
        try:
            _, wait_status = os.waitpid(task.pid, os.WNOHANG)
            task.exit_code = os.WEXITSTATUS(wait_status) if os.WIFEXITED(wait_status) else -1
        except ChildProcessError:
            task.exit_code = 0  # not our child, assume success if it ran to completion
        task.status = TaskStatus.COMPLETED if task.exit_code == 0 else TaskStatus.FAILED
        task.finished_at = time.time()
        async with self._lock:
            self._save_tasks()

    async def _worker_loop(self) -> None:
        while True:
            await asyncio.sleep(2)
            async with self._lock:
                running_count = sum(
                    1 for t in self._tasks.values() if t.status == TaskStatus.RUNNING
                )
                if running_count >= self._max_concurrent:
                    continue
                for task in sorted(self._tasks.values(), key=lambda t: t.created_at):
                    if task.status == TaskStatus.QUEUED:
                        await self._run_task(task)
                        break

    async def _run_task(self, task: Task) -> None:
        task.status = TaskStatus.RUNNING
        task.started_at = time.time()
        self._save_tasks()

        proc = await asyncio.create_subprocess_shell(
            task.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        task.pid = proc.pid
        self._running_procs[task.id] = proc
        self._save_tasks()

        asyncio.create_task(self._wait_for_task(task, proc))

    async def _wait_for_task(
        self, task: Task, proc: asyncio.subprocess.Process
    ) -> None:
        try:
            async def _read_stream(stream, attr):
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    setattr(task, attr, getattr(task, attr) + line.decode(errors="replace"))

            await asyncio.gather(
                _read_stream(proc.stdout, "stdout"),
                _read_stream(proc.stderr, "stderr"),
            )
            await proc.wait()
            task.exit_code = proc.returncode
            task.status = (
                TaskStatus.COMPLETED if proc.returncode == 0 else TaskStatus.FAILED
            )
        except asyncio.CancelledError:
            task.status = TaskStatus.CANCELLED
        finally:
            task.finished_at = time.time()
            self._running_procs.pop(task.id, None)
            async with self._lock:
                self._save_tasks()

    async def submit(self, command: str, gpu_requirement: int = 0) -> Task:
        task = Task(
            id=str(uuid.uuid4())[:8],
            command=command,
            created_at=time.time(),
            gpu_requirement=gpu_requirement,
        )
        async with self._lock:
            self._tasks[task.id] = task
            self._save_tasks()
        return task

    async def cancel(self, task_id: str) -> Task | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            if task.status == TaskStatus.QUEUED:
                task.status = TaskStatus.CANCELLED
                task.finished_at = time.time()
                self._save_tasks()
            elif task.status == TaskStatus.RUNNING:
                proc = self._running_procs.get(task_id)
                if proc:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except (ProcessLookupError, OSError):
                        proc.terminate()
                task.status = TaskStatus.CANCELLED
                task.finished_at = time.time()
                self._save_tasks()
            return task

    def list_tasks(
        self, status: Optional[TaskStatus] = None, limit: int = 50
    ) -> list[dict]:
        tasks = sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)
        if status is not None:
            tasks = [t for t in tasks if t.status == status]
        return [t.to_dict() for t in tasks[:limit]]

    def get_task(self, task_id: str) -> dict | None:
        task = self._tasks.get(task_id)
        return task.to_dict() if task else None
