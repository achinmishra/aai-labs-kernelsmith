"""Metrics collection for compiled kernels: cycles, memory, size."""

from __future__ import annotations
import pathlib
import subprocess
import json
import dataclasses
from typing import Dict, Any


@dataclasses.dataclass
class SizeMetrics:
    text: int
    data: int
    bss: int
    total: int
    dec_hex: str


def get_size_metrics(elf_path: pathlib.Path) -> SizeMetrics:
    try:
        proc = subprocess.run(
            ["arm-none-eabi-size", "-A", "-d", str(elf_path)],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        # fallback to default size format parsing
        out = proc.stdout
    except Exception:
        try:
            proc = subprocess.run(
                ["arm-none-eabi-size", str(elf_path)], capture_output=True, text=True, timeout=5
            )
            lines = proc.stdout.strip().splitlines()
            if len(lines) >= 2:
                parts = lines[1].split()
                text, data, bss = int(parts[0]), int(parts[1]), int(parts[2])
                total = text + data + bss
                dec = parts[3] if len(parts) > 3 else str(total)
                return SizeMetrics(text, data, bss, total, dec)
        except Exception:
            pass
        return SizeMetrics(0, 0, 0, 0, "0")
    # Parse -A output as fallback robust
    text = data = bss = 0
    for line in out.splitlines():
        if ".text" in line:
            try:
                text += int(line.split()[1])
            except:
                pass
        if ".data" in line:
            try:
                data += int(line.split()[1])
            except:
                pass
        if ".bss" in line:
            try:
                bss += int(line.split()[1])
            except:
                pass
    total = text + data + bss
    return SizeMetrics(text, data, bss, total, str(total))


@dataclasses.dataclass
class KernelMetrics:
    cycles_estimate: int
    time_us: int
    instruction_count: int
    text_bytes: int
    data_bytes: int
    bss_bytes: int
    total_bytes: int
    mode: str
    target: str

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    def to_json(self, path: pathlib.Path):
        path.write_text(json.dumps(self.to_dict(), indent=2))


def collect_metrics(elf_path: pathlib.Path, emulation_result, target: str) -> KernelMetrics:
    size = get_size_metrics(elf_path)
    return KernelMetrics(
        cycles_estimate=getattr(emulation_result, "cycles_estimate", 0),
        time_us=getattr(emulation_result, "time_us", 0),
        instruction_count=getattr(emulation_result, "instruction_count", 0),
        text_bytes=size.text,
        data_bytes=size.data,
        bss_bytes=size.bss,
        total_bytes=size.total,
        mode=getattr(emulation_result, "mode", "unknown"),
        target=target,
    )


def compare_fast_vs_full(fast_metrics: KernelMetrics, full_metrics: KernelMetrics) -> dict:
    return {
        "cycles_ratio": (full_metrics.cycles_estimate / fast_metrics.cycles_estimate)
        if fast_metrics.cycles_estimate
        else None,
        "time_ratio": (full_metrics.time_us / fast_metrics.time_us)
        if fast_metrics.time_us
        else None,
        "instr_same": fast_metrics.instruction_count == full_metrics.instruction_count,
        "tradeoff_note": (
            "fast=instruction-accurate via qemu-user -d in_asm (cortex-a15 proxy, no DWT); "
            "full=real baremetal qemu-system-arm -machine mps2-an500 -cpu cortex-m7 "
            "semihosting + DWT CYCCNT cycle-accurate measurement"
        ),
    }
