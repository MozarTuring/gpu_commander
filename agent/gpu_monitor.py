"""Parse nvidia-smi output into structured GPU status data."""

import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict


@dataclass
class GpuProcess:
    pid: int
    name: str
    used_memory_mib: int


@dataclass
class GpuInfo:
    index: int
    name: str
    uuid: str
    temperature_c: int
    fan_speed_pct: int | None
    utilization_gpu_pct: int
    utilization_memory_pct: int
    memory_total_mib: int
    memory_used_mib: int
    memory_free_mib: int
    power_draw_w: float | None
    power_limit_w: float | None
    processes: list[GpuProcess] = field(default_factory=list)


@dataclass
class GpuStatus:
    hostname: str
    driver_version: str
    cuda_version: str
    gpu_count: int
    gpus: list[GpuInfo] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_int(text: str | None, default: int = 0) -> int:
    if text is None:
        return default
    cleaned = text.strip().split()[0]
    try:
        return int(cleaned)
    except ValueError:
        return default


def _parse_float(text: str | None, default: float | None = None) -> float | None:
    if text is None:
        return default
    cleaned = text.strip().split()[0]
    try:
        return float(cleaned)
    except ValueError:
        return default


def get_gpu_status() -> GpuStatus:
    """Run nvidia-smi -x -q and parse the XML output."""
    result = subprocess.run(
        ["nvidia-smi", "-x", "-q"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"nvidia-smi failed: {result.stderr}")

    root = ET.fromstring(result.stdout)

    hostname = root.findtext("hostname", "unknown")
    driver_version = root.findtext("driver_version", "unknown")
    cuda_version = root.findtext("cuda_version", "unknown")

    gpus: list[GpuInfo] = []
    for i, gpu_el in enumerate(root.findall("gpu")):
        temp_el = gpu_el.find("temperature")
        util_el = gpu_el.find("utilization")
        mem_el = gpu_el.find("fb_memory_usage")
        power_el = gpu_el.find("gpu_power_readings") or gpu_el.find("power_readings")

        processes: list[GpuProcess] = []
        procs_el = gpu_el.find("processes")
        if procs_el is not None:
            for pi in procs_el.findall("process_info"):
                processes.append(GpuProcess(
                    pid=_parse_int(pi.findtext("pid")),
                    name=pi.findtext("process_name", "unknown"),
                    used_memory_mib=_parse_int(pi.findtext("used_memory")),
                ))

        gpus.append(GpuInfo(
            index=i,
            name=gpu_el.findtext("product_name", "unknown"),
            uuid=gpu_el.get("id", gpu_el.findtext("uuid", "unknown")),
            temperature_c=_parse_int(
                temp_el.findtext("gpu_temp") if temp_el is not None else None
            ),
            fan_speed_pct=_parse_int(gpu_el.findtext("fan_speed")) if gpu_el.findtext("fan_speed") else None,
            utilization_gpu_pct=_parse_int(
                util_el.findtext("gpu_util") if util_el is not None else None
            ),
            utilization_memory_pct=_parse_int(
                util_el.findtext("memory_util") if util_el is not None else None
            ),
            memory_total_mib=_parse_int(
                mem_el.findtext("total") if mem_el is not None else None
            ),
            memory_used_mib=_parse_int(
                mem_el.findtext("used") if mem_el is not None else None
            ),
            memory_free_mib=_parse_int(
                mem_el.findtext("free") if mem_el is not None else None
            ),
            power_draw_w=_parse_float(
                power_el.findtext("power_draw") if power_el is not None else None
            ),
            power_limit_w=_parse_float(
                power_el.findtext("power_limit") if power_el is not None else None
            ),
            processes=processes,
        ))

    return GpuStatus(
        hostname=hostname,
        driver_version=driver_version,
        cuda_version=cuda_version,
        gpu_count=len(gpus),
        gpus=gpus,
    )
