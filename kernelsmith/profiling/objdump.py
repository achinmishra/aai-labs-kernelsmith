"""Objdump based static analysis for Cortex-M kernels."""

from __future__ import annotations

import dataclasses
import pathlib
import re
import subprocess
from typing import Any


@dataclasses.dataclass
class ObjdumpStats:
    total_instr: int
    branch_instr: int
    fpu_instr: int
    vmaxnm_instr: int
    vsel_instr: int
    load_store_instr: int
    vldr_vstr_instr: int
    mov_instr: int
    estimated_unroll: int  # heuristic 1,2,4,8
    has_tail: bool
    loop_count: int
    func_size_bytes: int
    bloat_vs_naive: float = 0.0  # if provided
    has_branchless_pattern: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


_BRANCH_RE = re.compile(r"\b(b(eq|ne|gt|lt|ge|le|cs|cc|mi|pl|vs|vc|hi|ls)?|bl|bx|cbz|cbnz)\b", re.IGNORECASE)
_FPU_RE = re.compile(r"v.*f32|v.*f16|vmaxnm|vsel|vcmp|vmul|vadd|vsub", re.IGNORECASE)
_VMAXNM_RE = re.compile(r"vmaxnm", re.IGNORECASE)
_VSEL_RE = re.compile(r"vsel", re.IGNORECASE)
_LDR_RE = re.compile(r"\b(vldr|vstr|ldr|str|ldm|stm)\b", re.IGNORECASE)
_VLDR_RE = re.compile(r"\bvldr|\bvstr", re.IGNORECASE)
_LOOP_RE = re.compile(r"^\s*[0-9a-fA-F]+:\s+.*\b(b\w*)\s+.*<.*loop", re.IGNORECASE)

# Heuristic for unroll: look for repeated load pattern
# e.g., 4 consecutive vldr/ldr with increasing offsets


def _run_objdump(elf_path: pathlib.Path, function_name: str | None = None) -> str:
    # Try arm-none-eabi-objdump, fallback to generic objdump, then llvm-objdump
    for tool in ["arm-none-eabi-objdump", "arm-linux-gnueabihf-objdump", "objdump"]:
        try:
            cmd = [tool, "-d", "-C"]
            if function_name:
                # Try to disassemble only function if possible, but not all versions support -j trick
                # We'll filter later
                pass
            cmd.append(str(elf_path))
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout
        except Exception:
            continue
    return ""


def _try_nm_size(elf_path: pathlib.Path, func_name: str) -> int:
    for tool in ["arm-none-eabi-nm", "nm"]:
        try:
            proc = subprocess.run([tool, "-S", str(elf_path)], capture_output=True, text=True, timeout=5)
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    if func_name in line:
                        parts = line.split()
                        # typical nm -S: addr size type name
                        for p in parts:
                            try:
                                # size often hex
                                if p.startswith("0"):
                                    continue
                                # try hex parse
                                size = int(p, 16)
                                if 0 < size < 100000:
                                    return size
                            except Exception:
                                pass
        except Exception:
            continue
    return 0


def get_objdump_stats(elf_path: pathlib.Path, function_name: str | None = None) -> ObjdumpStats:
    """Static analysis of ELF to get instruction mix."""
    dump = _run_objdump(elf_path, function_name)

    if not dump:
        return ObjdumpStats(
            total_instr=0,
            branch_instr=0,
            fpu_instr=0,
            vmaxnm_instr=0,
            vsel_instr=0,
            load_store_instr=0,
            vldr_vstr_instr=0,
            mov_instr=0,
            estimated_unroll=1,
            has_tail=False,
            loop_count=0,
            func_size_bytes=0,
        )

    # If function name provided, filter dump to that function's disassembly section
    if function_name:
        # Find function label
        lines = dump.splitlines()
        filtered = []
        in_func = False
        func_start_re = re.compile(rf"<{re.escape(function_name)}>:")
        next_func_re = re.compile(r"^Disassembly of section|^[\da-fA-F]+\s+<.*>:")
        for line in lines:
            if func_start_re.search(line):
                in_func = True
                filtered.append(line)
                continue
            if in_func:
                # stop at next function or empty+next label
                if re.search(rf"^[\da-fA-F]+\s+<", line) and function_name not in line:
                    # Heuristic: if we see a new <func>: and we already collected some instr lines, break after some?
                    # For simplicity, break when we see new function label after having collected at least 5 instrs
                    if len(filtered) > 5:
                        # Check if this is a new function in same file
                        if "<" in line and ">:" in line and function_name not in line:
                            # could be next func; stop if we have loop detected
                            # continue a bit to capture tail? We'll break
                            pass
                filtered.append(line)
                # limit to avoid huge dump leaking into other funcs - cap at 500 lines after start
                if len(filtered) > 800:
                    break
        if filtered:
            dump = "\n".join(filtered)

    total = 0
    branch = 0
    fpu = 0
    vmaxnm = 0
    vsel = 0
    ldst = 0
    vldr = 0
    mov = 0
    loop_cnt = 0

    # For unroll heuristic, count consecutive identical patterns
    load_seq = 0
    max_load_seq = 0

    for line in dump.splitlines():
        # objdump line typically: address: opcode mnemonic args
        # skip headers
        if not re.search(r"^\s*[0-9a-fA-F]+:\s+", line):
            continue
        total += 1
        low = line.lower()
        if _BRANCH_RE.search(low):
            branch += 1
            if "loop" in low or "b " in low or "bne" in low or "bge" in low:
                # heuristic for loop branch
                if total < 200:  # near start loops
                    loop_cnt += 1
        if _FPU_RE.search(low):
            fpu += 1
        if _VMAXNM_RE.search(low):
            vmaxnm += 1
        if _VSEL_RE.search(low):
            vsel += 1
        if _LDR_RE.search(low):
            ldst += 1
        if _VLDR_RE.search(low):
            vldr += 1
        if "mov" in low:
            mov += 1

        # unroll detection: count consecutive loads
        if "ldr" in low or "vldr" in low:
            load_seq += 1
            max_load_seq = max(max_load_seq, load_seq)
        else:
            if "add" in low or "sub" in low:
                # still part of unrolled body, don't reset immediately
                pass
            else:
                load_seq = 0

    # estimate unroll from max consecutive loads (clamp to pow2-ish)
    if max_load_seq >= 8:
        unroll = 8
    elif max_load_seq >= 4:
        unroll = 4
    elif max_load_seq >= 2:
        unroll = 2
    else:
        unroll = 1

    # Tail detection via C source presence is better, but via objdump heuristic:
    # look for remainder handling - small loop after main - two branches with different loop bounds
    has_tail = loop_cnt >= 2 or branch > 4  # crude

    has_branchless = vmaxnm > 0 or vsel > 0

    func_size = _try_nm_size(elf_path, function_name) if function_name else 0
    if func_size == 0:
        # estimate via total * 2 or 4 bytes per instr Thumb2 mixed 16/32
        func_size = total * 4

    return ObjdumpStats(
        total_instr=total,
        branch_instr=branch,
        fpu_instr=fpu,
        vmaxnm_instr=vmaxnm,
        vsel_instr=vsel,
        load_store_instr=ldst,
        vldr_vstr_instr=vldr,
        mov_instr=mov,
        estimated_unroll=unroll,
        has_tail=has_tail,
        loop_count=loop_cnt,
        func_size_bytes=func_size,
        has_branchless_pattern=has_branchless,
    )
