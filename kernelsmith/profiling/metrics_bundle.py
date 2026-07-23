"""Metric bundle that combines naive vs opt + trace + objdump + bench sweep for feedback loop."""

from __future__ import annotations

import dataclasses
import pathlib
from typing import Any

from .objdump import ObjdumpStats
from .trace import Hotspot, TraceStats


@dataclasses.dataclass
class BenchSweepResult:
    n: int
    time_us: int
    cycles: int
    instr: int
    time_per_elem_ns: float
    checksum: float = 0.0


@dataclasses.dataclass
class MetricBundle:
    operator: str
    target: str
    mode: str
    naive_metrics: dict[str, Any]  # from harness benchmark: text_bytes, total_bytes, time_us, cycles_estimate, instruction_count
    opt_metrics: dict[str, Any]
    comparison: dict[str, Any]  # with delta, speedup, improved per key
    trace_stats: TraceStats | None = None
    hotspots: list[Hotspot] = dataclasses.field(default_factory=list)
    objdump_stats: ObjdumpStats | None = None
    bench_sweep: list[BenchSweepResult] = dataclasses.field(default_factory=list)
    timeline: dict[str, Any] = dataclasses.field(default_factory=dict)
    validation_pass: bool = True
    validation_details: str = ""
    extra: dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator": self.operator,
            "target": self.target,
            "mode": self.mode,
            "naive": self.naive_metrics,
            "optimized": self.opt_metrics,
            "comparison": self.comparison,
            "trace": self.trace_stats.to_dict() if self.trace_stats else None,
            "hotspots": [dataclasses.asdict(h) for h in self.hotspots[:5]],
            "objdump": self.objdump_stats.to_dict() if self.objdump_stats else None,
            "bench_sweep": [dataclasses.asdict(b) for b in self.bench_sweep],
            "timeline": self.timeline,
            "validation_pass": self.validation_pass,
        }

    def summary_str(self) -> str:
        comp = self.comparison

        def _gain(k):
            v = comp.get(k, {})
            return f"{k}: naive={v.get('naive', '?')} opt={v.get('optimized', '?')} speedup={v.get('speedup', 1.0):.2f}x {'✓' if v.get('improved') else '✗'}"

        lines = [
            f"Operator {self.operator} target {self.target} mode {self.mode} validation {'PASS' if self.validation_pass else 'FAIL'}",
            _gain("time_us"),
            _gain("cycles_estimate"),
            _gain("instruction_count"),
            _gain("text_bytes"),
        ]
        if self.trace_stats:
            ts = self.trace_stats
            lines.append(
                f"Trace: IN_blocks={ts.total_in_blocks} branches={ts.branch_count} ({ts.branch_density:.1%}) fpu={ts.fpu_count} ({ts.fpu_density:.1%}) vmaxnm={ts.vmaxnm_count} mem={ts.load_store_count} ({ts.mem_density:.1%})"
            )
        if self.objdump_stats:
            od = self.objdump_stats
            lines.append(
                f"Objdump: total={od.total_instr} fpu={od.fpu_instr} vmaxnm={od.vmaxnm_instr} unroll~{od.estimated_unroll} tail={od.has_tail} branchless={od.has_branchless_pattern}"
            )
        if self.bench_sweep:
            lines.append("BenchSweep N->time_per_elem:")
            for b in self.bench_sweep:
                lines.append(f"  N={b.n} time={b.time_us}us per_elem={b.time_per_elem_ns:.1f}ns instr={b.instr}")
        return "\n".join(lines)


def calc_time_per_elem(time_us: int, n: int, iterations: int) -> float:
    if n <= 0 or iterations <= 0:
        return 0.0
    # ns per element
    return (time_us * 1000.0) / (n * iterations) if n * iterations else 0.0
