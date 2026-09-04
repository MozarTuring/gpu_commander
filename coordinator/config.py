from pathlib import Path
from dataclasses import dataclass, field

import yaml


@dataclass
class MachineConfig:
    name: str
    host: str
    ssh_alias: str
    remote_dir: str
    agent_port: int = 9850
    description: str = ""
    vllm_service_dir: str = ""

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.agent_port}"


@dataclass
class CoordinatorConfig:
    host: str = "0.0.0.0"
    port: int = 9800
    poll_interval: int = 10
    host_machine: str = ""


@dataclass
class LLMIdleConfig:
    timeout_hours: float = 2.0
    check_interval_hours: float = 0.5
    last_active_interval: int = 30  # seconds between last-active updates for display


@dataclass
class AppConfig:
    coordinator: CoordinatorConfig = field(default_factory=CoordinatorConfig)
    auth_token: str = "gpu-commander-secret-change-me"
    machines: dict[str, MachineConfig] = field(default_factory=dict)
    llm_idle: LLMIdleConfig = field(default_factory=LLMIdleConfig)


def load_config(config_path: str | None = None) -> AppConfig:
    if config_path is None:
        config_path = str(Path(__file__).resolve().parent.parent / "config.yaml")
    with open(config_path) as f:
        raw = yaml.safe_load(f)

    coord_raw = raw.get("coordinator", {})
    coordinator = CoordinatorConfig(
        host=coord_raw.get("host", "0.0.0.0"),
        port=coord_raw.get("port", 9800),
        poll_interval=coord_raw.get("poll_interval", 10),
        host_machine=coord_raw.get("host_machine", ""),
    )

    auth_token = raw.get("auth", {}).get("token", "gpu-commander-secret-change-me")

    machines = {}
    for name, m in raw.get("machines", {}).items():
        machines[name] = MachineConfig(
            name=name,
            host=m["host"],
            agent_port=m.get("agent_port", raw.get("agent_port", 9850)),
            ssh_alias=m.get("ssh_alias", m["host"]),
            remote_dir=m.get("remote_dir", "/tmp/gpu_commander_agent"),
            description=m.get("description", ""),
            vllm_service_dir=m.get("vllm_service_dir", ""),
        )

    idle_raw = raw.get("llm_idle", {})
    llm_idle = LLMIdleConfig(
        timeout_hours=idle_raw.get("timeout_hours", 2.0),
        check_interval_hours=idle_raw.get("check_interval_hours", 0.5),
        last_active_interval=idle_raw.get("last_active_interval", 30),
    )

    return AppConfig(coordinator=coordinator, auth_token=auth_token, machines=machines, llm_idle=llm_idle)
