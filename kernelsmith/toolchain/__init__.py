"""Toolchain abstraction per target hardware profile."""

from __future__ import annotations

import dataclasses
import pathlib
import subprocess
from typing import Any

import yaml


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


TARGET_REGISTRY: dict[str, dict[str, Any]] = {
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


def resolve_toolchain(target_name: str, hardware_profile_path: pathlib.Path | None = None) -> ToolchainConfig:
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


LINUX_COMPILER = "arm-linux-gnueabihf-gcc"


def toolchain_available(compiler: str) -> bool:
    try:
        subprocess.run([compiler, "--version"], capture_output=True, check=True, timeout=5)
        return True
    except Exception:
        return False


def linux_toolchain_available() -> bool:
    return toolchain_available(LINUX_COMPILER)


def get_validation_compile_flags(
    tc: ToolchainConfig, use_linux: bool = False, use_semihosting: bool = False, use_semihost: bool = False, **kwargs
) -> list[str]:
    # Backward compat: test_e2e_qemu calls use_semihost=True (old name), we accept both use_semihost and use_semihosting
    if use_semihost:
        use_semihosting = True
    # Also allow use_semihost passed via kwargs
    if kwargs.get("use_semihost"):
        use_semihosting = True
    """Get compile flags for validation suite ELF (not baremetal -nostdlib).

    Linux path: prefers arm-linux-gnueabihf-gcc with -mcpu=cortex-a15 + neon + hard float
    Semihosting path: arm-none-eabi-gcc with --specs=rdimon.specs + -lc -lrdimon

    Decision #4: memory_bytes from QEMU output requires validation ELF compilation,
    which needs full libc (not -nostdlib). This is prerequisite for evolutionary evaluator.
    """
    if use_linux and linux_toolchain_available():
        # ARMv7-A Linux flags — works for generic C (relu), may not support M-specific asm
        # Documented limitation: if LLM generates M-specific intrinsics, need semihosting fallback
        return [
            "-O2",
            "-g",
            "-mcpu=cortex-a15",
            "-mfpu=neon",
            "-mfloat-abi=hard",
        ]
    elif use_semihosting:
        flags = [
            tc.arch_flag,
            tc.thumb_flag,
            "-O2",
            "-g",
            "--specs=rdimon.specs",
            "-lc",
            "-lrdimon",
        ]
        if tc.fpu_flag:
            flags.append(tc.fpu_flag)
        if tc.float_abi_flag:
            flags.append(tc.float_abi_flag)
        flags.extend(["-ffunction-sections", "-fdata-sections"])
        return [f for f in flags if f]
    else:
        # Host fallback or baremetal with rdimon if no linux toolchain
        # Still need libc for driver, so use rdimon.specs
        flags = [
            tc.arch_flag,
            tc.thumb_flag,
            "-O2",
            "-g",
        ]
        if tc.fpu_flag:
            flags.append(tc.fpu_flag)
        if tc.float_abi_flag:
            flags.append(tc.float_abi_flag)
        # For validation we need libc, so avoid -nostdlib, use rdimon if available
        return [f for f in flags if f]


def compile_multi_c_to_elf(
    sources: list[pathlib.Path],
    output_elf: pathlib.Path,
    tc: ToolchainConfig,
    include_dirs: list[pathlib.Path] | None = None,
    mode: str = "speed",
    use_linux: bool = False,
    use_semihosting: bool = False,
) -> dict:
    """Compile multiple C sources into one ELF for QEMU validation suite.

    This is the key new API for evolutionary evaluator (and QEMU E2E glue).

    Args:
        sources: List of C files e.g., [driver.c, reference/naive/relu.c, generated.c]
        output_elf: Output ELF path
        tc: Toolchain config
        include_dirs: -I include dirs (e.g., output/, reference/naive/)
        mode: optimization mode for flags (speed/size/debug) — currently unused for validation, uses -O2
        use_linux: prefer arm-linux-gnueabihf-gcc if available
        use_semihosting: use semihosting rdimon.specs if linux not available

    Returns:
        compile_info dict with command, stdout, stderr, size, elf path

    Raises:
        RuntimeError on compile failure with detailed info for failure blocks
    """
    # Choose compiler
    compiler = LINUX_COMPILER if (use_linux and linux_toolchain_available()) else tc.compiler

    if not toolchain_available(compiler):
        raise RuntimeError(
            f"Compiler {compiler} not found. Use kernelsmith Docker image or install gcc-arm-none-eabi / gcc-arm-linux-gnueabihf. "
            f"Toolchain {tc.compiler} also checked."
        )

    output_elf.parent.mkdir(parents=True, exist_ok=True)

    flags = get_validation_compile_flags(tc, use_linux=use_linux, use_semihosting=use_semihosting)

    cmd = [compiler] + flags
    if include_dirs:
        for inc in include_dirs:
            cmd.extend(["-I", str(inc)])
    cmd.extend([str(s) for s in sources])
    cmd.extend(["-lm", "-o", str(output_elf)])

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Validation suite compile failed:\n"
            f"Command: {' '.join(cmd)}\n"
            f"Sources: {sources}\n"
            f"Compiler: {compiler} (use_linux={use_linux}, semihost={use_semihosting})\n"
            f"STDERR:\n{proc.stderr}\n"
            f"STDOUT:\n{proc.stdout}"
        )

    # Size metrics via appropriate size tool
    size_tool = "arm-none-eabi-size" if "arm-none-eabi" in compiler else "size"
    try:
        size_proc = subprocess.run([size_tool, str(output_elf)], capture_output=True, text=True, timeout=5)
        size_output = size_proc.stdout.strip()
    except Exception:
        size_output = "size tool not available"

    return {
        "command": " ".join(cmd),
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "size": size_output,
        "elf": str(output_elf),
        "compiler": compiler,
        "use_linux": use_linux,
        "use_semihosting": use_semihosting,
    }


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
        raise RuntimeError(f"Compiler {tc.compiler} not found in PATH. Ensure Docker image has toolchain.")
    cmd = [tc.compiler] + get_compile_flags(tc, mode) + ["-o", str(output_elf), str(source)]
    if extra_sources:
        cmd.extend(str(s) for s in extra_sources)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"Compile failed: {proc.stderr}\n{proc.stdout}")
    # size analysis
    size_proc = subprocess.run(["arm-none-eabi-size", str(output_elf)], capture_output=True, text=True)
    size_output = size_proc.stdout.strip()
    return {
        "command": " ".join(cmd),
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "size": size_output,
        "elf": str(output_elf),
    }

def get_baremetal_compile_flags(tc: ToolchainConfig, mode: str = "speed") -> list[str]:
    """Flags for true baremetal system image with semihosting."""
    opt_map = {"speed": "-O3", "size": "-Os", "debug": "-O0 -g", "default": "-O2"}
    opt = opt_map.get(mode, "-O2")
    flags = [
        tc.arch_flag,
        tc.thumb_flag,
        opt,
        "-g",
        "-ffunction-sections",
        "-fdata-sections",
        "-nostartfiles",
        "-specs=nosys.specs",
        "-Wl,--gc-sections",
        "-Wl,-u,_printf_float",
    ]
    if tc.fpu_flag:
        flags.append(tc.fpu_flag)
    if tc.float_abi_flag:
        flags.append(tc.float_abi_flag)
    return [f for f in flags if f]


def compile_baremetal_system_elf(
    kernel_source: pathlib.Path,
    output_elf: pathlib.Path,
    tc: ToolchainConfig,
    build_dir: pathlib.Path,
    func_name: str | None = None,
    iters: int = 1000,
    length: int = 64,
    mode: str = "speed",
) -> dict:
    """
    Compile a true baremetal QEMU system image:
      startup_mps2_an500.s + syscalls.c + harness.c + kernel.c -> ELF
    Uses linker script for mps2-an500 and semihosting syscalls.
    """
    if not toolchain_available(tc.compiler):
        raise RuntimeError(
            f"Compiler {tc.compiler} not found in PATH. Ensure Docker image has toolchain."
        )

    from kernelsmith.baremetal import get_baremetal_sources

    sources = get_baremetal_sources()
    for k, p in sources.items():
        if not p.exists():
            raise RuntimeError(f"Baremetal source missing: {p} ({k})")

    build_dir.mkdir(parents=True, exist_ok=True)

    startup = sources["startup"]
    syscalls = sources["syscalls"]
    harness_tmpl = sources["harness"]
    linker = sources["linker"]

    harness_src = build_dir / "baremetal_harness.c"
    if harness_tmpl.exists():
        harness_src.write_text(harness_tmpl.read_text())
    else:
        harness_src = harness_tmpl

    func_define = []
    if func_name:
        func_define.append(f"-DKERNELSMITH_FUNC={func_name}")
    func_define.extend(
        [f"-DKERNELSMITH_ITERS={iters}", f"-DKERNELSMITH_LEN={length}"]
    )

    flags = get_baremetal_compile_flags(tc, mode)

    cmd = [
        tc.compiler,
        *flags,
        *func_define,
        f"-T{linker}",
        "-o",
        str(output_elf),
        str(startup),
        str(syscalls),
        str(harness_src),
        str(kernel_source),
        "-lm",
        f"-Wl,-Map,{output_elf.with_suffix('.map')}",
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Baremetal compile failed: {' '.join(cmd)}\n"
            f"STDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
        )

    size_proc = subprocess.run(
        ["arm-none-eabi-size", str(output_elf)], capture_output=True, text=True
    )
    size_output = size_proc.stdout.strip() if size_proc.returncode == 0 else ""

    return {
        "command": " ".join(cmd),
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "size": size_output,
        "elf": str(output_elf),
        "linker": str(linker),
        "startup": str(startup),
        "syscalls": str(syscalls),
        "harness": str(harness_src),
    }
