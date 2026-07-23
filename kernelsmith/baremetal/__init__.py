"""Baremetal support files for MPS2-AN500 Cortex-M7 QEMU system emulation."""

import pathlib

TEMPLATE_DIR = pathlib.Path(__file__).parent

STARTUP_ASM = TEMPLATE_DIR / "startup_mps2_an500.s"
LINKER_LD = TEMPLATE_DIR / "linker_mps2_an500.ld"
SYSCALLS_C = TEMPLATE_DIR / "syscalls.c"
HARNESS_C = TEMPLATE_DIR / "harness.c"


def get_baremetal_sources():
    return {
        "startup": STARTUP_ASM,
        "linker": LINKER_LD,
        "syscalls": SYSCALLS_C,
        "harness": HARNESS_C,
    }


def baremetal_sources_exist() -> bool:
    return all(p.exists() for p in get_baremetal_sources().values())
