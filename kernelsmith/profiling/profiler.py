"""High-level profiler that builds MetricBundle from harness artifacts."""

from __future__ import annotations

import pathlib
import re
from typing import Any

from ..emulator import EmulationResult
from .metrics_bundle import BenchSweepResult, MetricBundle, calc_time_per_elem
from .objdump import get_objdump_stats
from .trace import parse_qemu_log


def build_bundle_from_e2e(
    operator: str,
    target: str,
    mode: str,
    naive_metrics: dict[str, Any],
    opt_metrics: dict[str, Any],
    comparison: dict[str, Any],
    opt_elf: pathlib.Path | None,
    naive_elf: pathlib.Path | None,
    bench_emu_results: dict[int, EmulationResult] | None,
    qemu_log_text: str | None,
    validation_pass: bool,
    validation_details: str = "",
    function_name: str | None = None,
) -> MetricBundle:
    """Construct MetricBundle from existing artifacts."""

    trace_stats = None
    hotspots = []

    if qemu_log_text:
        try:
            from .trace import parse_qemu_log

            trace_stats, hotspots = parse_qemu_log(qemu_log_text)
        except Exception:
            trace_stats = None

    objdump_stats = None
    if opt_elf and opt_elf.exists():
        try:
            objdump_stats = get_objdump_stats(opt_elf, function_name=function_name)
        except Exception:
            objdump_stats = None

    bench_sweep: list[BenchSweepResult] = []
    if bench_emu_results:
        for n, emu_res in bench_emu_results.items():
            # parse benchmark metrics from stdout if available (KERNELSMITH_METRICS_START)
            # but fallback to emu_res.time_us/cycles
            time_us = emu_res.time_us
            cycles = emu_res.cycles_estimate
            instr = emu_res.instruction_count
            # Try to parse from stdout for more accurate n/iter/checksum
            # stdout contains cycles_estimate, time_us, iterations, n, checksum
            parsed_n = n
            iterations = 1000
            checksum = 0.0
            try:
                text = emu_res.stdout
                m_iter = re.search(r"iterations:\s*(\d+)", text)
                m_n = re.search(r"\bn:\s*(\d+)", text)
                m_check = re.search(r"checksum:\s*([-\d\.eE]+)", text)
                if m_iter:
                    iterations = int(m_iter.group(1))
                if m_n:
                    parsed_n = int(m_n.group(1))
                if m_check:
                    checksum = float(m_check.group(1))
            except Exception:
                pass
            pe = calc_time_per_elem(time_us, parsed_n, iterations)
            bench_sweep.append(
                BenchSweepResult(
                    n=parsed_n,
                    time_us=time_us,
                    cycles=cycles,
                    instr=instr,
                    time_per_elem_ns=pe,
                    checksum=checksum,
                )
            )
        bench_sweep.sort(key=lambda b: b.n)

    return MetricBundle(
        operator=operator,
        target=target,
        mode=mode,
        naive_metrics=naive_metrics or {},
        opt_metrics=opt_metrics or {},
        comparison=comparison or {},
        trace_stats=trace_stats,
        hotspots=hotspots,
        objdump_stats=objdump_stats,
        bench_sweep=bench_sweep,
        validation_pass=validation_pass,
        validation_details=validation_details,
    )


def run_bench_sweep(
    driver_template_fn,  # callable that generates driver code for N
    kernel_c: pathlib.Path,
    ref_c: pathlib.Path | None,
    tc,
    include_dirs: list[pathlib.Path],
    build_dir: pathlib.Path,
    ns: list[int] = [11, 16, 64, 256, 1024],
    is_naive: bool = False,
    kernel_func: str = "",
    header_name: str | None = None,
) -> dict[int, EmulationResult]:
    """Run benchmark for multiple N values, return dict N->EmulationResult.

    driver_template_fn is a function (n_value, kernel_func, header_name, is_naive) -> driver_c_string
    """
    from ..emulator import run_validation_elf_qemu
    from ..toolchain import compile_multi_c_to_elf

    results: dict[int, EmulationResult] = {}
    for n in ns:
        try:
            driver_code = driver_template_fn(n, kernel_func, header_name, is_naive)
            driver_path = build_dir / f"bench_sweep_{'naive' if is_naive else 'opt'}_N{n}.c"
            driver_path.write_text(driver_code)
            elf_path = build_dir / f"bench_sweep_{'naive' if is_naive else 'opt'}_N{n}.elf"
            # compile
            compile_multi_c_to_elf(
                sources=[driver_path, kernel_c],
                output_elf=elf_path,
                tc=tc,
                include_dirs=include_dirs,
                mode="speed",
                use_linux=True,
            )
            emu = run_validation_elf_qemu(
                elf_path,
                qemu_bin=tc.qemu_user,
                mode="fast",
                use_linux=True,
                timeout=15,
                keep_full_log=False,
            )
            results[n] = emu
        except Exception as e:
            # produce dummy result on failure
            from ..emulator import EmulationResult

            results[n] = EmulationResult(
                mode="fast",
                cycles_estimate=0,
                time_us=0,
                instruction_count=0,
                stdout=f"bench sweep failed for N={n}: {e}",
                stderr=str(e),
                returncode=1,
            )
    return results
