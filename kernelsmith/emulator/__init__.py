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
    timeout: int = 20,
    extra_log: bool = False,
) -> EmulationResult:
    """
    True full system emulation for Cortex-M baremetal.
    Uses qemu-system-arm -machine <machine> -cpu <cpu> -semihosting -kernel <elf>
    Requires ELF built with startup_mps2_an500.s + linker.ld + syscalls.c (semihosting).

    If ELF was not built as baremetal system image (e.g., legacy fast ELF),
    falls back to simulated overhead path for backward compat.
    """
    import os

    # Heuristic: baremetal ELF built with our linker has isr_vector; but we attempt real QEMU first
    log_path = None
    log_text = ""
    if extra_log:
        fd, log_path = tempfile.mkstemp(prefix="qemu_sys_", suffix=".log")
        os.close(fd)

    cmd = [
        qemu_bin,
        "-machine",
        machine,
        "-cpu",
        cpu,
        "-m",
        "16M",
        "-nographic",
        "-semihosting",
        "-semihosting-config",
        "enable=on,target=native",
        "-monitor",
        "none",
        "-serial",
        "none",
        "-kernel",
        str(elf_path),
    ]
    if extra_log and log_path:
        cmd += ["-d", "in_asm,exec", "-D", log_path]

    start = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        elapsed_us = int((time.time() - start) * 1_000_000)
        if log_path and pathlib.Path(log_path).exists():
            try:
                log_text = pathlib.Path(log_path).read_text(errors="ignore")
            except Exception:
                log_text = ""
        instr_count = log_text.count("\nIN:") if log_text else 0
        metrics = _parse_metrics_output(proc.stdout + proc.stderr)
        # If we got no metrics, this ELF is likely not a baremetal semihosting image
        # -> fallback to fast-sim path (preserves backward compat for old artifacts)
        if not metrics:
            # Try to distinguish: QEMU system may have failed to boot legacy ELF
            # Fall back to simulated overhead using fast path
            try:
                fast = run_qemu_user(
                    elf_path,
                    qemu_bin="qemu-arm",
                    timeout=timeout,
                )
                cycles = (
                    int(fast.cycles_estimate * 1.15) if fast.cycles_estimate else elapsed_us * 400
                )
                time_us = int(fast.time_us * 1.15) if fast.time_us else elapsed_us
                return EmulationResult(
                    mode="full-sim-fallback",
                    cycles_estimate=cycles,
                    time_us=time_us,
                    instruction_count=fast.instruction_count,
                    stdout=proc.stdout + "\n" + fast.stdout,
                    stderr=proc.stderr
                    + "\n"
                    + fast.stderr
                    + "\n[full mode fallback: ELF not baremetal, simulated 15% overhead]",
                    returncode=fast.returncode,
                    qemu_log=log_text[:4000] + "\n" + (fast.qemu_log or "")[:2000],
                )
            except Exception as e:
                # No qemu-user either
                return EmulationResult(
                    mode="full-failed",
                    cycles_estimate=elapsed_us * 400,
                    time_us=elapsed_us,
                    instruction_count=instr_count,
                    stdout=proc.stdout,
                    stderr=proc.stderr + f"\n[full mode failed, no fallback: {e}]",
                    returncode=proc.returncode,
                    qemu_log=log_text[:4000],
                )

        cycles = int(metrics.get("cycles_estimate", metrics.get("cycles_avg", elapsed_us * 400)))
        # If cycles_avg present, cycles_estimate is total; normalize
        time_us = int(metrics.get("time_us", elapsed_us))
        # Prefer cycles_estimate as total, but ensure at least elapsed
        if cycles == 0:
            cycles = elapsed_us * 400

        return EmulationResult(
            mode="full",
            cycles_estimate=cycles,
            time_us=time_us,
            instruction_count=instr_count,
            stdout=proc.stdout,
            stderr=proc.stderr,
            returncode=proc.returncode,
            qemu_log=log_text[:4000],
        )
    except subprocess.TimeoutExpired as te:
        # QEMU hung - kill and return what we have
        out = (
            (te.stdout or b"").decode(errors="ignore")
            if isinstance(te.stdout, (bytes, bytearray))
            else (te.stdout or "")
        )
        err = (
            (te.stderr or b"").decode(errors="ignore")
            if isinstance(te.stderr, (bytes, bytearray))
            else (te.stderr or "")
        )
        elapsed_us = int((time.time() - start) * 1_000_000)
        if log_path and pathlib.Path(log_path).exists():
            try:
                log_text = pathlib.Path(log_path).read_text(errors="ignore")
            except Exception:
                pass
        metrics = _parse_metrics_output(out + err)
        cycles = (
            int(metrics.get("cycles_estimate", elapsed_us * 400)) if metrics else elapsed_us * 400
        )
        time_us = int(metrics.get("time_us", elapsed_us)) if metrics else elapsed_us
        return EmulationResult(
            mode="full-timeout",
            cycles_estimate=cycles,
            time_us=time_us,
            instruction_count=log_text.count("\nIN:") if log_text else 0,
            stdout=out,
            stderr=err + f"\n[timeout after {timeout}s]",
            returncode=-1,
            qemu_log=log_text[:4000],
        )
    finally:
        if log_path:
            try:
                pathlib.Path(log_path).unlink(missing_ok=True)
            except Exception:
                pass


def run_qemu_system_legacy(
    elf_path: pathlib.Path,
    machine: str = "mps2-an500",
    cpu: str = "cortex-m7",
    qemu_bin: str = "qemu-system-arm",
    timeout: int = 15,
) -> EmulationResult:
    """Legacy simulated full mode - kept for reference."""
    try:
        fast = run_qemu_user(elf_path, qemu_bin="qemu-arm", timeout=timeout)
    except Exception:
        fast = EmulationResult("fast", 0, 0, 0, "", "fallback", 1)
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
