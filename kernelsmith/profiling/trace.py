"""QEMU log parsing for instruction mix, hotspot, timeline hints."""

from __future__ import annotations

import collections
import dataclasses
import pathlib
import re
from typing import Any


@dataclasses.dataclass
class TraceStats:
    total_in_blocks: int  # number of IN: blocks = translated blocks
    total_instr_estimated: int  # IN: count proxy
    branch_count: int
    fpu_count: int
    vmaxnm_count: int
    vsel_count: int
    vcmp_count: int
    vldr_vstr_count: int
    load_store_count: int
    vmov_count: int
    # derived
    branch_density: float = 0.0
    fpu_density: float = 0.0
    mem_density: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class Hotspot:
    pc: str
    count: int
    asm_snippet: str = ""


# Regex patterns for QEMU -d in_asm,exec log
_IN_RE = re.compile(r"^IN:\s+")
_PC_RE = re.compile(r"0x[0-9a-fA-F]{4,16}")
_BRANCH_RE = re.compile(r"\b(b(?:eq|ne|gt|lt|ge|le|hi|ls)?|bl|bx|cbz|cbnz|tbb|tbh)\b", re.IGNORECASE)
_FPU_RE = re.compile(r"\bv\w*(?:f32|f16|f64|s32|u32)\b|vmaxnm|vsel|vcmp|vmul|vadd|vsub|vmov", re.IGNORECASE)
_VMAXNM_RE = re.compile(r"vmaxnm", re.IGNORECASE)
_VSEL_RE = re.compile(r"vsel", re.IGNORECASE)
_VCMP_RE = re.compile(r"vcmp", re.IGNORECASE)
_VLDR_VSTR_RE = re.compile(r"\bvldr|\bvstr|\bvldm|\bvstm", re.IGNORECASE)
_LDST_RE = re.compile(r"\b(ldr|str|ldm|stm|ldrb|strb|ldrh|strh|ldmia|stmia)\b", re.IGNORECASE)
_VMOV_RE = re.compile(r"\bvmov\b", re.IGNORECASE)
# exec trace:  Trace 0x... [....]  or  0x....:  <asm>
_EXEC_PC_RE = re.compile(r"(?:Trace|0x)([0-9a-fA-F]{4,16})")


def parse_qemu_log(log_text: str) -> tuple[TraceStats, list[Hotspot]]:
    """Parse QEMU log text (full log, not truncated) into TraceStats + hotspots."""
    total_in = 0
    branch = 0
    fpu = 0
    vmaxnm = 0
    vsel = 0
    vcmp = 0
    vldr_vstr = 0
    ldst = 0
    vmov = 0

    pc_counter: collections.Counter[str] = collections.Counter()
    pc_to_asm: dict[str, str] = {}

    for line in log_text.splitlines():
        if _IN_RE.search(line):
            total_in += 1
            # Try to capture PC of block from next lines? QEMU IN: block header often has pc: 0x...
            m = _PC_RE.search(line)
            if m:
                pc_counter[m.group(0)] += 1
            continue

        low = line.lower()
        # hotspot via exec trace PC counting - lines that contain 0x...
        # heuristic: count every occurrence of hex addr in exec lines as hot PC hit
        if "0x" in low and ("out:" not in low):  # avoid OUT: exec noise? keep simple
            for pm in _PC_RE.finditer(line):
                pc_counter[pm.group(0)] += 1
                if pm.group(0) not in pc_to_asm:
                    pc_to_asm[pm.group(0)] = line.strip()[:200]

        if _BRANCH_RE.search(line):
            branch += 1
        if _FPU_RE.search(line):
            fpu += 1
        if _VMAXNM_RE.search(line):
            vmaxnm += 1
        if _VSEL_RE.search(line):
            vsel += 1
        if _VCMP_RE.search(line):
            vcmp += 1
        if _VLDR_VSTR_RE.search(line):
            vldr_vstr += 1
            ldst += 1
        elif _LDST_RE.search(line):
            ldst += 1
        if _VMOV_RE.search(line):
            vmov += 1

    total_instr_proxy = total_in if total_in else max(branch + fpu + ldst, 1)
    # Ensure at least some total to compute density
    denom = max(total_instr_proxy, 1)

    stats = TraceStats(
        total_in_blocks=total_in,
        total_instr_estimated=total_instr_proxy,
        branch_count=branch,
        fpu_count=fpu,
        vmaxnm_count=vmaxnm,
        vsel_count=vsel,
        vcmp_count=vcmp,
        vldr_vstr_count=vldr_vstr,
        load_store_count=ldst,
        vmov_count=vmov,
        branch_density=branch / denom,
        fpu_density=fpu / denom,
        mem_density=ldst / denom,
    )

    # Top 10 hotspots
    hotspots = [Hotspot(pc=pc, count=cnt, asm_snippet=pc_to_asm.get(pc, "")) for pc, cnt in pc_counter.most_common(10)]

    return stats, hotspots


def parse_qemu_log_file(log_path: pathlib.Path) -> tuple[TraceStats, list[Hotspot]]:
    """Read file and parse."""
    try:
        text = log_path.read_text(errors="ignore")
    except Exception:
        text = ""
    return parse_qemu_log(text)
