"""Toolchain abstraction per target hardware profile."""

from __future__ import annotations
import pathlib
import subprocess
import dataclasses
import yaml
from typing import Dict, Any


@dataclasses.dataclass
class ToolchainConfig:
    compiler: str
    arch_flag: str
    fpu_flag: str
    float_abi_flag: str
    thumb_flag: str
    optimize_flag: str
    linker_spec: str
    extra_flags: list[str]
    qemu_user: str
    qemu_system: str
    qemu_machine: str
    qemu_cpu: str


TARGET_REGISTRY: Dict[str, Dict[str, Any]] = {
    "cortex-m7": {
        "compiler": "arm-none-eabi-gcc",
        "arch": "-mcpu=cortex-m7",
        "fpu": "-mfpu=fpv5-sp-d16",
        "float_abi": "-mfloat-abi=hard",
        "thumb": "-mthumb",
        "qemu_user": "qemu-arm",
        "qemu_system": "qemu-system-arm",
        "qemu_machine": "mps2-an500",
        "qemu_cpu": "cortex-m7",
    },
    "cortex-m4": {
        "compiler": "arm-none-eabi-gcc",
        "arch": "-mcpu=cortex-m4",
        "fpu": "-mfpu=fpv4-sp-d16",
        "float_abi": "-mfloat-abi=hard",
        "thumb": "-mthumb",
        "qemu_user": "qemu-arm",
        "qemu_system": "qemu-system-arm",
        "qemu_machine": "mps2-an385",
        "qemu_cpu": "cortex-m4",
    },
    "cortex-m3": {
        "compiler": "arm-none-eabi-gcc",
        "arch": "-mcpu=cortex-m3",
        "fpu": "",
        "float_abi": "",
        "thumb": "-mthumb",
        "qemu_user": "qemu-arm",
        "qemu_system": "qemu-system-arm",
        "qemu_machine": "mps2-an385",
        "qemu_cpu": "cortex-m3",
    },
    "cortex-m0": {
        "compiler": "arm-none-eabi-gcc",
        "arch": "-mcpu=cortex-m0",
        "fpu": "",
        "float_abi": "",
        "thumb": "-mthumb",
        "qemu_user": "qemu-arm",
        "qemu_system": "qemu-system-arm",
        "qemu_machine": "mps2-an385",
        "qemu_cpu": "cortex-m0",
    },
}


def load_hardware_profile(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text())


def resolve_toolchain(
    target_name: str, hardware_profile_path: pathlib.Path | None = None
) -> ToolchainConfig:
    target_key = target_name.lower().replace("_", "-")
    base = TARGET_REGISTRY.get(target_key, TARGET_REGISTRY["cortex-m7"])

    if hardware_profile_path and hardware_profile_path.exists():
        hp = load_hardware_profile(hardware_profile_path)
        tc = hp.get("toolchain", {})
        flags = tc.get("flags", {})
        emu = hp.get("emulation", {})
        base = {
            **base,
            "compiler": tc.get("compiler", base["compiler"]),
            "arch": flags.get("arch", base["arch"]),
            "fpu": flags.get("fpu", base["fpu"]),
            "float_abi": flags.get("float_abi", base["float_abi"]),
            "thumb": flags.get("thumb", base["thumb"]),
            "qemu_user": emu.get("qemu_user", base["qemu_user"]),
            "qemu_system": emu.get("qemu_system", base["qemu_system"]),
            "qemu_machine": emu.get("qemu_machine", base["qemu_machine"]),
            "qemu_cpu": emu.get("qemu_cpu", base["qemu_cpu"]),
        }

    return ToolchainConfig(
        compiler=base["compiler"],
        arch_flag=base["arch"],
        fpu_flag=base["fpu"],
        float_abi_flag=base["float_abi"],
        thumb_flag=base["thumb"],
        optimize_flag="-O2",
        linker_spec="nosys.specs",
        extra_flags=["-Wl,--gc-sections"],
        qemu_user=base["qemu_user"],
        qemu_system=base["qemu_system"],
        qemu_machine=base["qemu_machine"],
        qemu_cpu=base["qemu_cpu"],
    )


def toolchain_available(compiler: str) -> bool:
    try:
        subprocess.run([compiler, "--version"], capture_output=True, check=True, timeout=5)
        return True
    except Exception:
        return False


def get_compile_flags(tc: ToolchainConfig, mode: str = "speed") -> list[str]:
    opt_map = {"speed": "-O3", "size": "-Os", "debug": "-O0 -g", "default": "-O2"}
    opt = opt_map.get(mode, "-O2")
    flags = [
        tc.arch_flag,
        tc.thumb_flag,
        opt,
        "-ffunction-sections",
        "-fdata-sections",
        "-nostdlib",
        f"-specs={tc.linker_spec}",
    ]
    if tc.fpu_flag:
        flags.append(tc.fpu_flag)
    if tc.float_abi_flag:
        flags.append(tc.float_abi_flag)
    flags.extend(tc.extra_flags)
    return [f for f in flags if f]


def compile_c_to_elf(
    source: pathlib.Path,
    output_elf: pathlib.Path,
    tc: ToolchainConfig,
    extra_sources: list[pathlib.Path] | None = None,
    mode: str = "speed",
) -> dict:
    if not toolchain_available(tc.compiler):
        raise RuntimeError(
            f"Compiler {tc.compiler} not found in PATH. Ensure Docker image has toolchain."
        )
    cmd = [tc.compiler] + get_compile_flags(tc, mode) + ["-o", str(output_elf), str(source)]
    if extra_sources:
        cmd.extend(str(s) for s in extra_sources)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"Compile failed: {proc.stderr}\n{proc.stdout}")
    # size analysis
    size_proc = subprocess.run(
        ["arm-none-eabi-size", str(output_elf)], capture_output=True, text=True
    )
    size_output = size_proc.stdout.strip()
    return {
        "command": " ".join(cmd),
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "size": size_output,
        "elf": str(output_elf),
    }
