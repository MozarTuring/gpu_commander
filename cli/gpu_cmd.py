#!/usr/bin/env python3
"""GPU Commander CLI — interact with GPU machines from the terminal."""

import json
import sys
import os

import click
import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "coordinator"))
from config import load_config

cfg = load_config()
BASE = f"http://localhost:{cfg.coordinator.port}"


def _client() -> httpx.Client:
    return httpx.Client(base_url=BASE, timeout=600)


def _print_json(data):
    click.echo(json.dumps(data, indent=2))


def _die(msg: str):
    click.secho(msg, fg="red", err=True)
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------
@click.group()
def cli():
    """GPU Commander — manage remote GPU machines."""


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
@cli.command()
@click.argument("machine", required=False)
def status(machine):
    """Show GPU status for one or all machines."""
    with _client() as c:
        if machine:
            resp = c.get(f"/api/machines/{machine}/gpu/status")
            if resp.status_code != 200:
                _die(f"Error: {resp.text}")
            _print_gpu_status(machine, resp.json())
        else:
            resp = c.get("/api/machines")
            if resp.status_code != 200:
                _die(f"Error: {resp.text}")
            for m in resp.json():
                _print_machine_summary(m)


def _print_machine_summary(m):
    name = m["name"]
    state = click.style("ONLINE", fg="green") if m["online"] else click.style("OFFLINE", fg="red")
    click.echo(f"\n{'='*50}")
    click.echo(f"  {name}  [{state}]  {m.get('description','')}")
    click.echo(f"{'='*50}")

    if m.get("gpu_cache") and m["gpu_cache"].get("gpus"):
        _print_gpu_status(name, m["gpu_cache"])
    elif not m["online"]:
        click.secho("  Machine offline", fg="red")


def _print_gpu_status(name, data):
    for g in data.get("gpus", []):
        mem_pct = round(g["memory_used_mib"] / g["memory_total_mib"] * 100) if g["memory_total_mib"] > 0 else 0
        util = g["utilization_gpu_pct"]
        temp = g["temperature_c"]

        util_color = "green" if util < 70 else ("yellow" if util < 90 else "red")
        mem_color = "green" if mem_pct < 70 else ("yellow" if mem_pct < 90 else "red")
        temp_color = "green" if temp < 70 else ("yellow" if temp < 85 else "red")

        click.echo(f"  GPU {g['index']}: {g['name']}")
        click.echo(f"    Util: {click.style(f'{util}%', fg=util_color):>20}  "
                    f"VRAM: {click.style(f'{g['memory_used_mib']}/{g['memory_total_mib']} MiB ({mem_pct}%)', fg=mem_color)}  "
                    f"Temp: {click.style(f'{temp}°C', fg=temp_color)}")
        if g.get("processes"):
            for p in g["processes"]:
                click.echo(f"    └─ PID {p['pid']}  {p['name']}  {p['used_memory_mib']} MiB")


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
@cli.command()
@click.argument("machine")
@click.argument("command")
@click.option("--timeout", "-t", default=300, help="Timeout in seconds")
@click.option("--background", "-bg", is_flag=True, help="Run in background")
@click.option("--workdir", "-w", default=None, help="Working directory on remote")
def run(machine, command, timeout, background, workdir):
    """Run a command on a machine."""
    with _client() as c:
        body = {"command": command, "timeout": timeout, "background": background}
        if workdir:
            body["working_dir"] = workdir
        resp = c.post(f"/api/machines/{machine}/execute", json=body, timeout=timeout + 30)
        if resp.status_code != 200:
            _die(f"Error: {resp.text}")
        result = resp.json()

        if background:
            click.echo(f"Background job started — PID: {result['pid']}")
            click.echo(f"Check with: gpu-cmd job {machine} {result['pid']}")
        else:
            if result.get("stdout"):
                click.echo(result["stdout"], nl=False)
            if result.get("stderr"):
                click.secho(result["stderr"], fg="red", nl=False, err=True)
            raise SystemExit(result.get("exit_code", 0))


# ---------------------------------------------------------------------------
# job (check background job)
# ---------------------------------------------------------------------------
@cli.command()
@click.argument("machine")
@click.argument("pid", type=int)
def job(machine, pid):
    """Check status of a background job."""
    with _client() as c:
        resp = c.get(f"/api/machines/{machine}/execute/{pid}")
        if resp.status_code != 200:
            _die(f"Error: {resp.text}")
        result = resp.json()
        state = "finished" if result["finished"] else "running"
        click.echo(f"PID {pid} — {click.style(state, fg='green' if result['finished'] else 'yellow')}")
        if result.get("exit_code") is not None:
            click.echo(f"Exit code: {result['exit_code']}")
        if result.get("stdout"):
            click.echo(result["stdout"], nl=False)
        if result.get("stderr"):
            click.secho(result["stderr"], fg="red", nl=False, err=True)


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------
@cli.command()
@click.argument("machine")
@click.argument("command")
@click.option("--gpu", "-g", default=0, help="Required free GPUs to start")
def submit(machine, command, gpu):
    """Submit a command to the task queue."""
    with _client() as c:
        resp = c.post(f"/api/machines/{machine}/tasks/submit",
                       json={"command": command, "gpu_requirement": gpu})
        if resp.status_code != 200:
            _die(f"Error: {resp.text}")
        task = resp.json()
        click.echo(f"Task submitted — ID: {click.style(task['id'], fg='cyan')}")
        click.echo(f"Status: {task['status']}")


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------
@cli.command()
@click.argument("machine")
@click.option("--status", "-s", "filter_status", default=None,
              type=click.Choice(["queued", "running", "completed", "failed", "cancelled"]))
@click.option("--limit", "-n", default=20)
def tasks(machine, filter_status, limit):
    """List tasks on a machine."""
    with _client() as c:
        params = {"limit": limit}
        if filter_status:
            params["status"] = filter_status
        resp = c.get(f"/api/machines/{machine}/tasks", params=params)
        if resp.status_code != 200:
            _die(f"Error: {resp.text}")
        task_list = resp.json()
        if not task_list:
            click.echo("No tasks found.")
            return

        click.echo(f"{'ID':<10} {'Status':<12} {'Command':<50} {'Exit':<6}")
        click.echo("-" * 80)
        for t in task_list:
            status_colors = {
                "queued": "blue", "running": "yellow", "completed": "green",
                "failed": "red", "cancelled": "white",
            }
            s = click.style(t["status"], fg=status_colors.get(t["status"], "white"))
            cmd = t["command"][:48] + ".." if len(t["command"]) > 50 else t["command"]
            ec = str(t["exit_code"]) if t["exit_code"] is not None else "—"
            click.echo(f"{t['id']:<10} {s:<22} {cmd:<50} {ec:<6}")


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------
@cli.command()
@click.argument("machine")
@click.argument("task_id")
def cancel(machine, task_id):
    """Cancel a queued or running task."""
    with _client() as c:
        resp = c.delete(f"/api/machines/{machine}/tasks/{task_id}")
        if resp.status_code != 200:
            _die(f"Error: {resp.text}")
        task = resp.json()
        click.echo(f"Task {task_id}: {task['status']}")


# ---------------------------------------------------------------------------
# machines
# ---------------------------------------------------------------------------
@cli.command()
def machines():
    """List configured machines and their status."""
    with _client() as c:
        resp = c.get("/api/machines")
        if resp.status_code != 200:
            _die(f"Error: {resp.text}")
        for m in resp.json():
            state = click.style("ONLINE", fg="green") if m["online"] else click.style("OFFLINE", fg="red")
            click.echo(f"  {m['name']:<25} {state}  {m.get('description','')}")


if __name__ == "__main__":
    cli()
