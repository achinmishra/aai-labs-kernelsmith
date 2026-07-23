"""Enhanced QEMU profiling for kernelsmith - Task 1: profiling infra.

Provides:
- TraceStats, Hotspot from QEMU log
- ObjdumpStats from arm-none-eabi-objdump
- MetricBundle that rolls up naive vs opt + trace + objdump + bench sweep
"""

from .metrics_bundle import MetricBundle
from .objdump import ObjdumpStats, get_objdump_stats
from .trace import Hotspot, TraceStats, parse_qemu_log, parse_qemu_log_file

__all__ = [
    "MetricBundle",
    "ObjdumpStats",
    "get_objdump_stats",
    "TraceStats",
    "Hotspot",
    "parse_qemu_log",
    "parse_qemu_log_file",
]
