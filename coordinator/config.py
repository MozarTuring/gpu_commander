import os
from pathlib import Path
from dataclasses import dataclass, field

import yaml


@dataclass
class MachineConfig:
    name: str
    host: str
    agent_port: int
    ssh_alias: str
    remote_dir: str
    description: str = ""

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.agent_port}"


@dataclass
class CoordinatorConfig:
    host: str = "0.0.0.0"
    port: int = 9800
    poll_interval: int = 10


@dataclass
class AppConfig:
    coordinator: CoordinatorConfig = field(default_factory=CoordinatorConfig)
    auth_token: str = "gpu-commander-secret-change-me"
    machines: dict[str, MachineConfig] = field(default_factory=dict)


def load_config(config_path: str | None = None) -> AppConfig:
    if config_path is None:
        config_path = os.environ.get(
            "GPU_COMMANDER_CONFIG",
            str(Path(__file__).resolve().parent.parent / "config.yaml"),
        )
    with open(config_path) as f:
        raw = yaml.safe_load(f)

    coord_raw = raw.get("coordinator", {})
    coordinator = CoordinatorConfig(
        host=coord_raw.get("host", "0.0.0.0"),
        port=coord_raw.get("port", 9800),
        poll_interval=coord_raw.get("poll_interval", 10),
    )

    auth_token = raw.get("auth", {}).get("token", "gpu-commander-secret-change-me")

    machines = {}
    for name, m in raw.get("machines", {}).items():
        machines[name] = MachineConfig(
            name=name,
            host=m["host"],
            agent_port=m.get("agent_port", 9850),
            ssh_alias=m.get("ssh_alias", m["host"]),
            remote_dir=m.get("remote_dir", "/tmp/gpu_commander_agent"),
            description=m.get("description", ""),
        )

    return AppConfig(coordinator=coordinator, auth_token=auth_token, machines=machines)
