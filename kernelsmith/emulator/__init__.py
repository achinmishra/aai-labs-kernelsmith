"""QEMU emulator abstraction supporting fast vs full modes."""

from __future__ import annotations
import pathlib
import subprocess
import json
import tempfile
import dataclasses
import time
from typing import Literal

QemuMode = Literal["fast", "full", "auto"]


@dataclasses.dataclass
class EmulationResult:
    mode: str
    cycles_estimate: int
    time_us: int
    instruction_count: int
    stdout: str
    stderr: str
    returncode: int
    qemu_log: str | None = None


def _parse_metrics_output(text: str) -> dict:
    metrics = {}
    in_block = False
    for line in text.splitlines():
        if "KERNELSMITH_METRICS_START" in line:
            in_block = True
            continue
        if "KERNELSMITH_METRICS_END" in line:
            break
        if in_block and ":" in line:
            k, v = line.split(":", 1)
            metrics[k.strip()] = v.strip()
    return metrics


def run_qemu_user(
    elf_path: pathlib.Path,
    qemu_bin: str = "qemu-arm",
    timeout: int = 10,
    extra_args: list[str] | None = None,
) -> EmulationResult:
    log_fd, log_path = tempfile.mkstemp(prefix="qemu_", suffix=".log")
    import os

    os.close(log_fd)
    cmd = [qemu_bin, "-d", "in_asm,exec", "-D", log_path]
    if extra_args:
        cmd.extend(extra_args)
    cmd.append(str(elf_path))
    start = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    elapsed_us = int((time.time() - start) * 1_000_000)
    log_text = (
        pathlib.Path(log_path).read_text(errors="ignore") if pathlib.Path(log_path).exists() else ""
    )
    instr_count = log_text.count("\nIN:")
    metrics = _parse_metrics_output(proc.stdout + proc.stderr)
    cycles = int(metrics.get("cycles_estimate", elapsed_us * 400))  # assume 400MHz if not provided
    time_us = int(metrics.get("time_us", elapsed_us))
    try:
        pathlib.Path(log_path).unlink()
    except Exception:
        pass
    return EmulationResult(
        mode="fast",
        cycles_estimate=cycles,
        time_us=time_us,
        instruction_count=instr_count,
        stdout=proc.stdout,
        stderr=proc.stderr,
        returncode=proc.returncode,
        qemu_log=log_text[:2000],
    )


def run_qemu_system(
    elf_path: pathlib.Path,
    machine: str = "mps2-an500",
    cpu: str = "cortex-m7",
    qemu_bin: str = "qemu-system-arm",
    timeout: int = 15,
) -> EmulationResult:
    """
    Full system emulation with semihosting. For Cortex-M baremetal, we approximate.
    Real cycle-accurate mode would require proper board image and semihosting setup.
    Here we simulate trade-off: run fast mode then apply 1.15x overhead factor to model pipeline stalls, cache misses.
    """
    # Attempt fast user mode as baseline
    try:
        fast = run_qemu_user(elf_path, qemu_bin="qemu-arm", timeout=timeout)
    except Exception:
        fast = EmulationResult("fast", 0, 0, 0, "", "fallback", 1)
    # Simulate full system overhead
    cycles = int(fast.cycles_estimate * 1.15) if fast.cycles_estimate else 0
    time_us = int(fast.time_us * 1.15) if fast.time_us else 0
    return EmulationResult(
        mode="full-sim",
        cycles_estimate=cycles,
        time_us=time_us,
        instruction_count=fast.instruction_count,
        stdout=fast.stdout,
        stderr=fast.stderr + "\n[full mode simulated 15% overhead for pipeline/cache]",
        returncode=fast.returncode,
        qemu_log=fast.qemu_log,
    )


def emulate(
    elf_path: pathlib.Path,
    mode: QemuMode = "auto",
    qemu_user: str = "qemu-arm",
    qemu_system: str = "qemu-system-arm",
    machine: str = "mps2-an500",
    cpu: str = "cortex-m7",
) -> EmulationResult:
    if mode == "fast" or mode == "auto":
        return run_qemu_user(elf_path, qemu_bin=qemu_user)
    elif mode == "full":
        return run_qemu_system(elf_path, machine=machine, cpu=cpu, qemu_bin=qemu_system)
    else:
        raise ValueError(f"Unknown mode {mode}")
