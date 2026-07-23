"""QEMU emulator abstraction supporting fast vs full modes."""

from __future__ import annotations

import dataclasses
import pathlib
import subprocess
import tempfile
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
    qemu_log_path: pathlib.Path | None = None
    qemu_full_log: str | None = None  # optional full log when keep_full_log=True (may be large)


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


def _find_qemu_binary(preferred: str = "qemu-arm") -> str:
    """Find qemu-arm binary with fallback to qemu-arm-static."""
    import shutil

    for cand in [preferred, "qemu-arm-static", "qemu-arm"]:
        if shutil.which(cand):
            return cand
    return preferred


def run_qemu_user(
    elf_path: pathlib.Path,
    qemu_bin: str = "qemu-arm",
    timeout: int = 10,
    extra_args: list[str] | None = None,
    semihosting: bool = False,
    trace: bool = True,
    keep_full_log: bool = False,
    keep_log_path: pathlib.Path | None = None,
) -> EmulationResult:
    log_path = None
    log_text = ""
    full_log_path: pathlib.Path | None = None
    try:
        if trace:
            if keep_log_path:
                log_path = str(keep_log_path)
                keep_log_path.parent.mkdir(parents=True, exist_ok=True)
                # ensure file exists clean
                pathlib.Path(log_path).write_text("")
            else:
                log_fd, log_path = tempfile.mkstemp(prefix="qemu_", suffix=".log")
                import os

                os.close(log_fd)
            cmd = [qemu_bin, "-d", "in_asm,exec", "-D", log_path]
        else:
            cmd = [qemu_bin]

        if semihosting:
            cmd.append("-semihosting")

        if extra_args:
            cmd.extend(extra_args)
        cmd.append(str(elf_path))
        start = time.time()
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        elapsed_us = int((time.time() - start) * 1_000_000)

        if log_path and pathlib.Path(log_path).exists():
            try:
                log_text = pathlib.Path(log_path).read_text(errors="ignore")
            except Exception:
                log_text = ""
            # Decide whether to keep file
            if keep_full_log or keep_log_path:
                full_log_path = pathlib.Path(log_path)
                # If temp file, move to a stable location? Keep as is for caller to use
                # Caller will handle cleanup if needed
            else:
                try:
                    pathlib.Path(log_path).unlink()
                except Exception:
                    pass

        instr_count = log_text.count("\nIN:") if log_text else 0
        metrics = _parse_metrics_output(proc.stdout + proc.stderr)
        cycles = int(metrics.get("cycles_estimate", elapsed_us * 400))
        time_us = int(metrics.get("time_us", elapsed_us))

        mode = "fast"
        if semihosting:
            mode = "fast-semihost"

        return EmulationResult(
            mode=mode,
            cycles_estimate=cycles,
            time_us=time_us,
            instruction_count=instr_count,
            stdout=proc.stdout,
            stderr=proc.stderr,
            returncode=proc.returncode,
            qemu_log=log_text[:2000] if log_text else None,
            qemu_log_path=full_log_path,
            qemu_full_log=log_text if keep_full_log else None,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            f"QEMU binary {qemu_bin} not found. Install qemu-user (apt-get install qemu-user) or run inside kernelsmith Docker image. Error: {e}"
        ) from e
    finally:
        if log_path:
            try:
                if pathlib.Path(log_path).exists():
                    pathlib.Path(log_path).unlink()
            except Exception:
                pass


def run_validation_elf_qemu(
    elf_path: pathlib.Path,
    qemu_bin: str = "qemu-arm",
    mode: QemuMode = "fast",
    use_linux: bool = True,
    timeout: int = 15,
    extra_args: list[str] | None = None,
    keep_full_log: bool = False,
    keep_log_path: pathlib.Path | None = None,
) -> EmulationResult:
    """
    Run validation suite ELF (driver+ref+gen) under QEMU.

    - use_linux=True: ELF compiled with arm-linux-gnueabihf-gcc, run as Linux user ELF (no semihosting needed)
    - use_linux=False: ELF compiled with arm-none-eabi-gcc + rdimon, needs -semihosting
    - mode fast: with instruction tracing via -d in_asm,exec (for metrics)
    - mode full: simulate 15% overhead on top of fast result (or run without tracing)
    """
    qemu_actual = _find_qemu_binary(qemu_bin)

    # Build extra args for Linux: set QEMU_LD_PREFIX via -L flag to find ld-linux-armhf.so.3
    # In Docker image, libs are at /usr/arm-linux-gnueabihf/lib/
    qemu_extra = list(extra_args) if extra_args else []
    if use_linux:
        # Check common prefix locations
        for prefix in ["/usr/arm-linux-gnueabihf", "/usr"]:
            if pathlib.Path(prefix).exists():
                # Only add -L if not already in extra
                if not any(a == "-L" for a in qemu_extra):
                    qemu_extra.extend(["-L", prefix])
                    break

    # For full mode, we still run fast then apply overhead factor (as original design)
    # but with distinction for validation suite (which includes driver overhead)
    if mode == "full":
        # Run without heavy tracing for full simulation, then apply overhead
        try:
            fast_res = run_qemu_user(
                elf_path,
                qemu_bin=qemu_actual,
                timeout=timeout,
                extra_args=qemu_extra,
                semihosting=not use_linux,
                trace=False,
                keep_full_log=keep_full_log,
                keep_log_path=keep_log_path,
            )
        except Exception:
            fast_res = EmulationResult("fast", 0, 0, 0, "", "fallback", 1, None, None, None)

        # Try with tracing if no instr count yet and we want some metrics
        if fast_res.instruction_count == 0 and fast_res.returncode == 0:
            try:
                traced = run_qemu_user(
                    elf_path,
                    qemu_bin=qemu_actual,
                    timeout=timeout,
                    extra_args=qemu_extra,
                    semihosting=not use_linux,
                    trace=True,
                    keep_full_log=keep_full_log,
                    keep_log_path=keep_log_path,
                )
                if traced.instruction_count > 0:
                    fast_res.instruction_count = traced.instruction_count
                    fast_res.qemu_log = traced.qemu_log
                    fast_res.qemu_log_path = traced.qemu_log_path
                    fast_res.qemu_full_log = traced.qemu_full_log
            except Exception:
                pass

        cycles = int(fast_res.cycles_estimate * 1.15) if fast_res.cycles_estimate else 0
        time_us = int(fast_res.time_us * 1.15) if fast_res.time_us else 0
        return EmulationResult(
            mode="full-sim",
            cycles_estimate=cycles,
            time_us=time_us,
            instruction_count=fast_res.instruction_count,
            stdout=fast_res.stdout,
            stderr=fast_res.stderr + "\n[full mode simulated 15% overhead for pipeline/cache]",
            returncode=fast_res.returncode,
            qemu_log=fast_res.qemu_log,
            qemu_log_path=fast_res.qemu_log_path,
            qemu_full_log=fast_res.qemu_full_log,
        )

    # fast or auto
    # For fast mode, we want tracing for instruction count
    trace_enabled = True
    # For validation suite, tracing generates huge log, but we keep it limited (qemu handles)
    try:
        res = run_qemu_user(
            elf_path,
            qemu_bin=qemu_actual,
            timeout=timeout,
            extra_args=qemu_extra,
            semihosting=not use_linux,
            trace=trace_enabled,
            keep_full_log=keep_full_log,
            keep_log_path=keep_log_path,
        )
        return res
    except subprocess.TimeoutExpired as e:
        # Build partial result with timeout info
        stdout = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        raise RuntimeError(
            f"QEMU execution timed out after {timeout}s for {elf_path}. "
            f"Possible infinite loop in generated kernel. "
            f"Partial STDOUT: {stdout[:1000]}\nSTDERR: {stderr[:1000]}"
        ) from e


def run_qemu_system(
    elf_path: pathlib.Path,
    machine: str = "mps2-an500",
    cpu: str = "cortex-m7",
    qemu_bin: str = "qemu-system-arm",
    timeout: int = 15,
    keep_full_log: bool = False,
    keep_log_path: pathlib.Path | None = None,
) -> EmulationResult:
    """
    Full system emulation with semihosting. For Cortex-M baremetal, we approximate.
    Real cycle-accurate mode would require proper board image and semihosting setup.
    Here we simulate trade-off: run fast mode then apply 1.15x overhead factor
    to model pipeline stalls and cache misses.
    """
    # Attempt fast user mode as baseline
    try:
        fast = run_qemu_user(elf_path, qemu_bin="qemu-arm", timeout=timeout, keep_full_log=keep_full_log, keep_log_path=keep_log_path)
    except Exception:
        fast = EmulationResult("fast", 0, 0, 0, "", "fallback", 1, None, None, None)
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
        qemu_log_path=fast.qemu_log_path,
        qemu_full_log=fast.qemu_full_log,
    )


def emulate(
    elf_path: pathlib.Path,
    mode: QemuMode = "auto",
    qemu_user: str = "qemu-arm",
    qemu_system: str = "qemu-system-arm",
    machine: str = "mps2-an500",
    cpu: str = "cortex-m7",
    semihosting: bool = False,
    use_linux: bool = False,
    keep_full_log: bool = False,
    keep_log_path: pathlib.Path | None = None,
) -> EmulationResult:
    """
    Unified emulate entry point.
    - For baremetal kernel-only ELF (old behavior): use run_qemu_user / run_qemu_system
    - For validation suite ELF: set use_linux=True or semihosting=True
    """
    if use_linux or semihosting:
        return run_validation_elf_qemu(
            elf_path, qemu_bin=qemu_user, mode=mode, use_linux=use_linux, timeout=15, keep_full_log=keep_full_log, keep_log_path=keep_log_path
        )

    if mode == "fast" or mode == "auto":
        return run_qemu_user(elf_path, qemu_bin=qemu_user, semihosting=semihosting, keep_full_log=keep_full_log, keep_log_path=keep_log_path)
    elif mode == "full":
        return run_qemu_system(elf_path, machine=machine, cpu=cpu, qemu_bin=qemu_system, keep_full_log=keep_full_log, keep_log_path=keep_log_path)
    else:
        raise ValueError(f"Unknown mode {mode}")


def parse_validation_output(stdout: str) -> dict:
    """Parse validation driver stdout for PASS/FAIL counts and details."""
    pass_count = stdout.count("PASS ")
    fail_count = stdout.count("FAIL ")
    all_passed = "All" in stdout and "validation tests passed" in stdout and fail_count == 0
    failed_cases = []
    passed_cases = []
    for line in stdout.splitlines():
        if line.strip().startswith("FAIL "):
            failed_cases.append(line.strip())
        elif line.strip().startswith("PASS "):
            passed_cases.append(line.strip())
    return {
        "pass_count": pass_count,
        "fail_count": fail_count,
        "all_passed": all_passed,
        "failed_cases": failed_cases,
        "passed_cases": passed_cases,
    }
