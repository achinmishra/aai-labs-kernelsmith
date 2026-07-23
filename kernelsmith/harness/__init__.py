"""LLM harness integration for kernelsmith.

Provides:
 - optimize_and_measure: legacy pipeline optimize -> compile -> emulate -> metrics
 - e2e_pipeline: full glue pipeline codegen -> test suite gen -> QEMU validation -> metrics -> results
   with detailed timing and logging support, handling both mock and real LLM providers.

Example usage from Metacode harness:
    from kernelsmith.harness import KernelsmithHarness
    h = KernelsmithHarness(target="cortex-m7", mode="fast")
    result = h.optimize_and_measure(operator="relu", llm_provider="mock")
    print(result.metrics)

    # E2E with validation in QEMU (mock for CI, avocado_free for final)
    result = h.e2e_pipeline(operator="relu", llm_provider="mock", mode="fast")
    print(result.validation_result.passed)
"""

from __future__ import annotations

import json
import pathlib
import time
from dataclasses import dataclass, field
from typing import Any

from kernelsmith.codegen.optimize import optimize as ks_optimize
from kernelsmith.emulator import emulate
from kernelsmith.metrics import KernelMetrics, collect_metrics
from kernelsmith.toolchain import (
    compile_baremetal_system_elf,
    compile_c_to_elf,
    resolve_toolchain,
    toolchain_available,
)


@dataclass
class HarnessResult:
    operator: str
    target: str
    generated_files: Any
    elf_path: pathlib.Path
    metrics: KernelMetrics
    emulation: Any
    compile_info: dict[str, Any]


@dataclass
class E2EStepInfo:
    name: str
    duration_s: float
    success: bool
    details: str = ""


# ------------------------------------------------------------------
# Benchmark driver generation for naive vs optimized comparison
# ------------------------------------------------------------------

BENCHMARK_DRIVER_TEMPLATE = """
#include <stdio.h>
#include <stdint.h>
#include <stddef.h>
#include <sys/time.h>
#include <stdlib.h>
#include <math.h>
{includes}

// Function declaration for kernel under benchmark
{func_declaration}

#ifndef ITERATIONS
#define ITERATIONS 1000
#endif
#ifndef N
#define N 64
#endif

int main() {{
    const int n = N;
    const int iter = ITERATIONS;
    float *input = (float*)malloc(n * sizeof(float));
    float *output = (float*)malloc(n * sizeof(float));
    if (!input || !output) {{
        printf("Allocation failed\\n");
        return 1;
    }}
    for (int i = 0; i < n; i++) {{
        input[i] = (i % 2 == 0) ? (float)i : (float)-i;
        output[i] = 0.0f;
    }}

    struct timeval tv1, tv2;
    // Warmup
    for (int w = 0; w < 10; w++) {{
        {kernel_call};
    }}

    gettimeofday(&tv1, 0);
    for (int it = 0; it < iter; it++) {{
        {kernel_call};
    }}
    gettimeofday(&tv2, 0);

    long us = (tv2.tv_sec - tv1.tv_sec) * 1000000L + (tv2.tv_usec - tv1.tv_usec);
    // Prevent optimization from removing the loop by using output
    float checksum = 0.0f;
    for (int i = 0; i < n; i++) checksum += output[i];

    printf("KERNELSMITH_METRICS_START\\n");
    printf("cycles_estimate: %ld\\n", us * 400); // 400MHz estimate
    printf("time_us: %ld\\n", us);
    printf("iterations: %d\\n", iter);
    printf("n: %d\\n", n);
    printf("checksum: %f\\n", checksum);
    printf("KERNELSMITH_METRICS_END\\n");
    printf("Benchmark done for {kernel_name}, time_us=%ld checksum=%f\\n", us, checksum);

    free(input);
    free(output);
    return 0;
}}
"""


def _generate_benchmark_driver(
    kernel_func: str,
    header_name: str | None = None,
    is_naive: bool = False,
    ref_declaration: str | None = None,
) -> str:
    """
    Generate benchmark driver C code for a kernel.

    - kernel_func: function name to benchmark (e.g., ks_relu_cortex_m7 or relu_f32)
    - header_name: header file to include for optimized kernel (e.g., ks_relu_cortex_m7.h)
    - is_naive: if True, use ref_declaration or generic extern declaration
    - ref_declaration: optional custom extern declaration for naive
    """
    if is_naive:
        # For naive, we need to declare the reference function
        # Try to handle both size_t and int length signatures
        if ref_declaration:
            func_decl = ref_declaration
        else:
            # Default guess: void relu_f32(const float* input, float* output, size_t n)
            func_decl = f"extern void {kernel_func}(const float* input, float* output, size_t n);"
        includes = "#include <stddef.h>"
        kernel_call = f"{kernel_func}(input, output, n)"
    else:
        # Optimized: include header
        if header_name:
            includes = f'#include "{header_name}"'
        else:
            includes = f"extern void {kernel_func}(const float* input, float* output, int length);"
        func_decl = ""  # Header already declares
        kernel_call = f"{kernel_func}(input, output, n)"

    driver = BENCHMARK_DRIVER_TEMPLATE.format(
        includes=includes,
        func_declaration=func_decl,
        kernel_call=kernel_call,
        kernel_name=kernel_func,
    )
    return driver


def _parse_benchmark_metrics_output(stdout: str) -> dict:
    """Parse KERNELSMITH_METRICS_START/END block from benchmark driver output."""
    metrics = {}
    in_block = False
    for line in stdout.splitlines():
        if "KERNELSMITH_METRICS_START" in line:
            in_block = True
            continue
        if "KERNELSMITH_METRICS_END" in line:
            break
        if in_block and ":" in line:
            k, v = line.split(":", 1)
            k = k.strip()
            v = v.strip()
            try:
                # Try to parse as int or float
                if "." in v:
                    metrics[k] = float(v)
                else:
                    metrics[k] = int(v)
            except ValueError:
                metrics[k] = v
    return metrics


@dataclass
class E2EResult:
    operator: str
    target: str
    mode: str
    llm_provider: str
    model: str
    generated_files: Any
    baremetal_elf: pathlib.Path | None
    validation_elf: pathlib.Path | None
    driver_c_path: pathlib.Path | None
    validation_result: Any  # ValidationResult from validation.harness
    metrics: KernelMetrics | None
    emulation: Any | None
    compile_info_baremetal: dict[str, Any] | None
    compile_info_validation: dict[str, Any] | None
    timing: dict[str, float] = field(default_factory=dict)
    steps: list[E2EStepInfo] = field(default_factory=list)
    output_dir: pathlib.Path | None = None
    results_json_path: pathlib.Path | None = None
    reference_path: pathlib.Path | None = None
    operator_yaml_path: pathlib.Path | None = None
    # New: comparison fields for naive vs optimized
    naive_metrics: dict[str, Any] | None = None
    optimized_metrics: dict[str, Any] | None = None
    comparison: dict[str, Any] | None = None
    naive_elf: pathlib.Path | None = None
    optimized_benchmark_elf: pathlib.Path | None = None
    naive_benchmark_elf: pathlib.Path | None = None
    benchmark_artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        def _maybe_path(p):
            return str(p) if p else None

        # Validation result dict if available
        val_dict = None
        if self.validation_result:
            try:
                val_dict = self.validation_result.to_dict()
            except Exception:
                val_dict = {
                    "passed": getattr(self.validation_result, "passed", False),
                    "details": getattr(self.validation_result, "details", ""),
                }

        return {
            "operator": self.operator,
            "target": self.target,
            "mode": self.mode,
            "llm_provider": self.llm_provider,
            "model": self.model,
            "generated": {
                "header": _maybe_path(self.generated_files.header_path)
                if self.generated_files
                else None,
                "c": _maybe_path(self.generated_files.c_path) if self.generated_files else None,
                "md": _maybe_path(self.generated_files.md_path) if self.generated_files else None,
            },
            "reference": _maybe_path(self.reference_path),
            "operator_yaml": _maybe_path(self.operator_yaml_path),
            "artifacts": {
                "baremetal_elf": _maybe_path(self.baremetal_elf),
                "validation_elf": _maybe_path(self.validation_elf),
                "driver_c": _maybe_path(self.driver_c_path),
                "naive_elf": _maybe_path(self.naive_elf),
                "naive_benchmark_elf": _maybe_path(self.naive_benchmark_elf),
                "optimized_benchmark_elf": _maybe_path(self.optimized_benchmark_elf),
                "output_dir": _maybe_path(self.output_dir),
                **{
                    _maybe_path(k) and k or k: _maybe_path(v) if isinstance(v, pathlib.Path) else v
                    for k, v in (self.benchmark_artifacts or {}).items()
                },
            },
            "validation": val_dict,
            "metrics": self.metrics.to_dict() if self.metrics else None,
            "emulation": {
                "mode": getattr(self.emulation, "mode", self.mode) if self.emulation else self.mode,
                "returncode": getattr(self.emulation, "returncode", -1) if self.emulation else -1,
                "stdout": getattr(self.emulation, "stdout", "")[:2000] if self.emulation else "",
                "stderr": getattr(self.emulation, "stderr", "")[:1000] if self.emulation else "",
                "cycles_estimate": getattr(self.emulation, "cycles_estimate", 0)
                if self.emulation
                else 0,
                "time_us": getattr(self.emulation, "time_us", 0) if self.emulation else 0,
                "instruction_count": getattr(self.emulation, "instruction_count", 0)
                if self.emulation
                else 0,
            },
            "compile": {
                "baremetal": self.compile_info_baremetal,
                "validation": self.compile_info_validation,
            },
            "timing": self.timing,
            "steps": [
                {
                    "name": s.name,
                    "duration_s": s.duration_s,
                    "success": s.success,
                    "details": s.details,
                }
                for s in self.steps
            ],
            "results_json": _maybe_path(self.results_json_path),
            "passed": val_dict.get("passed", False) if val_dict else False,
            # New comparison fields
            "naive_metrics": self.naive_metrics,
            "optimized_metrics": self.optimized_metrics,
            "comparison": self.comparison,
        }


class KernelsmithHarness:
    def __init__(
        self,
        target: str = "cortex-m7",
        mode: str = "fast",
        workspace: pathlib.Path | str | None = None,
    ):
        self.target = target
        self.mode = mode
        self.workspace = pathlib.Path(workspace or "/workspace")
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.build_dir = self.workspace / "build"
        self.build_dir.mkdir(exist_ok=True)
        self.output_dir = self.workspace / "output"
        self.output_dir.mkdir(exist_ok=True)
        self.results_dir = self.workspace / "results"
        self.results_dir.mkdir(exist_ok=True)

    # ------------------------------------------------------------------
    # Legacy pipeline (kept for backward compat)
    # ------------------------------------------------------------------
    def optimize_and_measure(
        self,
        operator: str = "relu",
        spec_path: pathlib.Path | None = None,
        llm_provider: str = "mock",
        model: str | None = None,
        template_path: pathlib.Path | None = None,
    ) -> HarnessResult:
        # 1. LLM generate optimized kernel
        opt_result = ks_optimize(
            operator=operator,
            target=self.target,
            spec_path=spec_path,
            output_dir=self.output_dir,
            llm_provider_name=llm_provider,
            template_path=template_path,
            model=model or "avocado_metacode_rc",
        )
        c_path = opt_result.files.c_path
        # 2. Resolve toolchain per target hardware profile
        tc = resolve_toolchain(self.target, opt_result.target_path)
        if not toolchain_available(tc.compiler):
            raise RuntimeError(
                f"Toolchain {tc.compiler} not available. Run inside kernelsmith Docker image."
            )

        # 3. Compile generated C to ELF
        # For fast mode we use simple ELF (size metrics).
        # For full mode we need true baremetal system image with startup/linker/semihosting
        # so that qemu-system-arm -machine mps2-an500 -cpu cortex-m7 can boot it.
        elf_path = self.build_dir / f"{c_path.stem}.elf"
        compile_info = compile_c_to_elf(c_path, elf_path, tc, mode="speed")

        if self.mode == "full":
            # Build real baremetal system ELF for DWT cycle-accurate emulation
            system_elf_path = self.build_dir / f"{c_path.stem}_system.elf"
            try:
                import re

                func_name = None
                try:
                    hdr_text = opt_result.files.header_path.read_text(errors="ignore")
                    m = re.search(r"void\s+(ks_\w+)\s*\(", hdr_text)
                    if m:
                        func_name = m.group(1)
                except Exception:
                    pass
                if not func_name:
                    func_name = f"ks_{operator}_{self.target.replace('-', '_')}"
                system_compile_info = compile_baremetal_system_elf(
                    kernel_source=c_path,
                    output_elf=system_elf_path,
                    tc=tc,
                    build_dir=self.build_dir,
                    func_name=func_name,
                )
                emu_elf = system_elf_path
                compile_info = {**compile_info, "system": system_compile_info}
                elf_path_for_metrics = system_elf_path
            except Exception as e:
                emu_elf = elf_path
                elf_path_for_metrics = elf_path
                compile_info["system_compile_error"] = str(e)
        else:
            emu_elf = elf_path
            elf_path_for_metrics = elf_path

        # 4. Emulate under QEMU
        emu_result = emulate(
            emu_elf,
            mode=self.mode,
            qemu_user=tc.qemu_user,
            qemu_system=tc.qemu_system,
            machine=tc.qemu_machine,
            cpu=tc.qemu_cpu,
        )

        # 5. Collect metrics
        metrics = collect_metrics(elf_path_for_metrics, emu_result, self.target)

        # 6. Save results JSON for harness consumption
        result_json = self.results_dir / f"{operator}_{self.target}_{self.mode}.json"
        payload = {
            "operator": operator,
            "target": self.target,
            "mode": self.mode,
            "generated": {
                "header": str(opt_result.files.header_path),
                "c": str(opt_result.files.c_path),
                "md": str(opt_result.files.md_path),
            },
            "elf": str(elf_path),
            "metrics": metrics.to_dict(),
            "compile": compile_info,
            "emulation": {
                "mode": emu_result.mode,
                "returncode": emu_result.returncode,
                "stdout_snippet": emu_result.stdout[:500],
            },
        }
        result_json.write_text(json.dumps(payload, indent=2))

        return HarnessResult(
            operator=operator,
            target=self.target,
            generated_files=opt_result.files,
            elf_path=elf_path,
            metrics=metrics,
            emulation=emu_result,
            compile_info=compile_info,
        )

    # ------------------------------------------------------------------
    # E2E pipeline: codegen + test suite gen + QEMU validation + metrics
    # ------------------------------------------------------------------
    def e2e_pipeline(
        self,
        operator: str = "relu",
        spec_path: pathlib.Path | None = None,
        llm_provider: str = "avocado_free",
        model: str | None = None,
        template_path: pathlib.Path | None = None,
        precision: str = "fp32",
        use_linux: bool = True,
        output_json: pathlib.Path | None = None,
        progress_callback: Any | None = None,
    ) -> E2EResult:
        """
        Full E2E glue pipeline with timing and detailed steps.

        Steps:
          1. Codegen via LLM (mock or avocado_free)
          2. Test suite generation (driver.c) via validation.harness
          3a. Baremetal compile for size metrics
          3b. Validation suite compile for QEMU (linux or semihost)
          4. QEMU execution of validation suite
          5. Metrics collection + results JSON

        Supports both mock (fast CI, no key) and real LLM (final validation, needs KERNELSMITH_MODEL_API_KEY).

        Raises RuntimeError with detailed context on failure for CLI to print nice logs.
        """
        steps: list[E2EStepInfo] = []
        timing: dict[str, float] = {}
        total_start = time.time()

        # Helper to notify progress (for agentic UI)
        def _notify(event: str, step_name: str, payload: dict | None = None):
            if progress_callback:
                try:
                    progress_callback(event, step_name, payload or {})
                except Exception:
                    pass

        # Helper to record step
        def _record_step(name: str, start: float, success: bool, details: str = "") -> E2EStepInfo:
            dur = time.time() - start
            timing[name] = dur
            info = E2EStepInfo(name=name, duration_s=dur, success=success, details=details)
            steps.append(info)
            # Notify success/failure
            if success:
                _notify("success", name, {"duration": dur, "details": details})
            else:
                _notify("failure", name, {"duration": dur, "details": details})
            return info

        # Step 1: Codegen
        _notify(
            "start",
            "codegen",
            {"operator": operator, "target": self.target, "provider": llm_provider},
        )
        step1_start = time.time()
        try:
            opt_result = ks_optimize(
                operator=operator,
                target=self.target,
                spec_path=spec_path,
                output_dir=self.output_dir,
                llm_provider_name=llm_provider,
                template_path=template_path,
                model=model or "avocado_metacode_rc",
            )
            c_path = opt_result.files.c_path
            h_path = opt_result.files.header_path
            md_path = opt_result.files.md_path
            actual_model = getattr(opt_result, "model", model or "avocado_metacode_rc")
            _record_step(
                "codegen",
                step1_start,
                True,
                f"Generated {h_path.name} ({h_path.stat().st_size} B), {c_path.name} ({c_path.stat().st_size} B)",
            )
        except Exception as e:
            _record_step("codegen", step1_start, False, str(e))
            # Check for API key missing
            err_msg = str(e)
            if (
                "KERNELSMITH_MODEL_API_KEY" in err_msg
                or "api_key" in err_msg.lower()
                or "model_not_found" in err_msg.lower()
            ):
                raise RuntimeError(
                    f"Codegen failed (provider={llm_provider}): {e}\n"
                    f"Suggestion: Set KERNELSMITH_MODEL_API_KEY env var or use --llm-provider mock for offline test"
                ) from e
            raise RuntimeError(
                f"Codegen failed (operator={operator}, target={self.target}, provider={llm_provider}): {e}\n"
                f"Try --llm-provider mock to verify pipeline wiring"
            ) from e

        # Resolve toolchain early to fail fast if missing
        try:
            tc = resolve_toolchain(self.target, opt_result.target_path)
        except Exception as e:
            _record_step("toolchain_resolve", time.time(), False, str(e))
            raise RuntimeError(f"Failed to resolve toolchain for target {self.target}: {e}") from e

        # Check toolchain availability for QEMU path (warn but don't fail yet, validate step will fail with nice message)
        baremetal_available = toolchain_available(tc.compiler)
        from kernelsmith.toolchain import linux_toolchain_available

        linux_available = linux_toolchain_available()

        # Step 2: Test suite generation (driver.c)
        step2_start = time.time()
        _notify("start", "test_suite_gen", {"operator": operator, "target": self.target})
        driver_c_path: pathlib.Path | None = None
        reference_path: pathlib.Path | None = None
        operator_yaml_path: pathlib.Path | None = None
        try:
            from kernelsmith.validation.harness import generate_driver_for_operator

            # Load operator YAML path (built-in)
            if spec_path:
                operator_yaml_path = pathlib.Path(spec_path)
            else:
                # Try built-in location
                builtin_ops = list(
                    (pathlib.Path(__file__).parent.parent / "operators").glob(f"{operator}.yaml")
                )
                if builtin_ops:
                    operator_yaml_path = builtin_ops[0]
                else:
                    operator_yaml_path = pathlib.Path(f"kernelsmith/operators/{operator}.yaml")

            # Generate driver via harness
            # This function loads spec and creates driver_c string + ref paths
            drv_info = generate_driver_for_operator(
                operator, target=self.target, precision=precision
            )
            driver_c_content = drv_info["driver_c"]
            ref_c_path_raw = drv_info["ref_c_path"]
            gen_h_path_raw = drv_info["gen_header_path"]
            gen_c_path_raw = drv_info["gen_c_path"]

            # Resolve reference path absolute
            ref_path_candidate = pathlib.Path(ref_c_path_raw)
            if not ref_path_candidate.is_absolute():
                # Try relative to workspace root or repo root
                # Look up from harness package root
                repo_root = pathlib.Path(__file__).resolve().parents[2]
                potential = repo_root / ref_c_path_raw
                if potential.exists():
                    ref_path_candidate = potential
                else:
                    # Search common locations
                    for base in [self.workspace, pathlib.Path.cwd(), repo_root]:
                        p = base / ref_c_path_raw
                        if p.exists():
                            ref_path_candidate = p
                            break

            reference_path = (
                ref_path_candidate if ref_path_candidate.exists() else pathlib.Path(ref_c_path_raw)
            )
            driver_c_path = self.build_dir / f"driver_{operator}_{self.target}.c"
            driver_c_path.write_text(driver_c_content)

            # Count edge cases for logging
            import yaml as _yaml

            spec_dict = {}
            if operator_yaml_path and operator_yaml_path.exists():
                try:
                    spec_dict = _yaml.safe_load(operator_yaml_path.read_text())
                except Exception:
                    pass
            # If spec_path provided, load that
            if spec_path and pathlib.Path(spec_path).exists():
                try:
                    spec_dict = _yaml.safe_load(pathlib.Path(spec_path).read_text())
                except Exception:
                    pass

            # Best effort count
            edge_cases = (
                spec_dict.get("test_vectors", {}).get("edge_cases", []) if spec_dict else []
            )
            shapes = (
                spec_dict.get("test_vectors", {}).get("generate", {}).get("shapes", [])
                if spec_dict
                else []
            )

            _record_step(
                "test_suite_gen",
                step2_start,
                True,
                f"Driver {driver_c_path.name} ({len(driver_c_content)} B), edge_cases={edge_cases}, shapes={shapes}, ref={reference_path}",
            )
        except Exception as e:
            _record_step("test_suite_gen", step2_start, False, str(e))
            raise RuntimeError(
                f"Test suite generation failed for operator={operator}, target={self.target}: {e}\n"
                f"Check operator YAML {operator_yaml_path} and template "
                f"kernelsmith/validation/templates/operator_validation.c.j2"
            ) from e

        # Step 3a: Baremetal compile for size metrics (with fallback for GC'd ELF)
        step3a_start = time.time()
        _notify("start", "baremetal_compile", {"target": self.target})
        baremetal_elf: pathlib.Path | None = None
        compile_info_baremetal: dict | None = None
        try:
            if not baremetal_available:
                _record_step(
                    "baremetal_compile",
                    step3a_start,
                    False,
                    f"Compiler {tc.compiler} not found, skipping baremetal size metrics",
                )
                baremetal_elf = None
            else:
                baremetal_elf = self.build_dir / f"{c_path.stem}_baremetal.elf"
                compile_info_baremetal = compile_c_to_elf(c_path, baremetal_elf, tc, mode="speed")

                # Fallback size measurement: if ELF text=0 due to --gc-sections removing unreferenced function,
                # compile to object and measure .o size
                try:
                    from kernelsmith.metrics import get_size_metrics

                    size_m = get_size_metrics(baremetal_elf)
                    if size_m.text == 0 and size_m.data == 0 and size_m.bss == 0:
                        # Try object file size
                        import subprocess

                        obj_path = self.build_dir / f"{c_path.stem}_baremetal.o"
                        # Compile to object only, without --gc-sections and without -nostdlib GC
                        compile_cmd = [
                            tc.compiler,
                            tc.arch_flag,
                            tc.thumb_flag,
                            tc.fpu_flag if tc.fpu_flag else "",
                            tc.float_abi_flag if tc.float_abi_flag else "",
                            "-O2",
                            "-c",
                            str(c_path),
                            "-o",
                            str(obj_path),
                        ]
                        compile_cmd = [c for c in compile_cmd if c]
                        subprocess.run(compile_cmd, capture_output=True, timeout=10)
                        if obj_path.exists():
                            # Get size of .o file via wc and via size tool (approx)
                            obj_size = obj_path.stat().st_size
                            compile_info_baremetal["fallback_obj"] = str(obj_path)
                            compile_info_baremetal["fallback_obj_size"] = obj_size
                            # Also try to get text size via size tool on .o
                            try:
                                size_obj_proc = subprocess.run(
                                    ["arm-none-eabi-size", str(obj_path)],
                                    capture_output=True,
                                    text=True,
                                    timeout=5,
                                )
                                if size_obj_proc.stdout:
                                    compile_info_baremetal["size_obj"] = (
                                        size_obj_proc.stdout.strip()
                                    )
                            except Exception:
                                pass
                except Exception:
                    pass

                _record_step(
                    "baremetal_compile",
                    step3a_start,
                    True,
                    f"Baremetal ELF {baremetal_elf.name} size={compile_info_baremetal.get('size', '')[:200]}",
                )
        except Exception as e:
            # Baremetal compile failure is non-fatal for validation, but record
            _record_step("baremetal_compile", step3a_start, False, str(e))
            # We don't abort, but keep baremetal_elf as None
            compile_info_baremetal = {"error": str(e), "compiler": tc.compiler}
            baremetal_elf = None

        # Step 3b + 4: Validation compile + QEMU execution via validation.harness QEMU path
        step3b_start = time.time()
        _notify("start", "validation_compile_and_run", {"mode": self.mode, "target": self.target})
        validation_elf: pathlib.Path | None = None
        compile_info_validation: dict | None = None
        validation_result = None
        emulation_result = None
        metrics: KernelMetrics | None = None

        try:
            from kernelsmith.validation.harness import validate_from_paths_qemu

            # Determine operator YAML for validation
            # Use spec_path if provided and it's YAML, else try to find builtin YAML
            op_yaml_for_validation: pathlib.Path
            if spec_path and pathlib.Path(spec_path).suffix in (".yaml", ".yml"):
                op_yaml_for_validation = pathlib.Path(spec_path)
            else:
                # Search builtin operators
                candidate_paths = [
                    pathlib.Path(__file__).parent.parent / f"operators/{operator}.yaml",
                    pathlib.Path(f"kernelsmith/operators/{operator}.yaml"),
                    pathlib.Path(f"examples/operators/{operator}.yaml"),
                ]
                found = None
                for cand in candidate_paths:
                    if cand.exists():
                        found = cand
                        break
                if found:
                    op_yaml_for_validation = found
                else:
                    # Fallback to operator_yaml_path from earlier
                    op_yaml_for_validation = operator_yaml_path or pathlib.Path(
                        f"kernelsmith/operators/{operator}.yaml"
                    )

            # If reference_path not exists, try to resolve via operator YAML
            if not reference_path or not reference_path.exists():
                try:
                    import yaml as _yaml2

                    if op_yaml_for_validation.exists():
                        sd = _yaml2.safe_load(op_yaml_for_validation.read_text())
                        ref_file = sd.get("reference", {}).get("c_file", "")
                        if ref_file:
                            repo_root = pathlib.Path(__file__).resolve().parents[2]
                            potential = repo_root / ref_file
                            if potential.exists():
                                reference_path = potential
                except Exception:
                    pass

            # Validation ELF path
            validation_elf = self.build_dir / f"validation_{operator}_{self.target}.elf"

            # Use workspace build dir for temp artifacts to keep logs
            # validate_from_paths_qemu will compile and run
            validation_result = validate_from_paths_qemu(
                generated_c=c_path,
                reference_c=reference_path,
                operator_yaml=op_yaml_for_validation,
                tolerance=None,
                precision=precision,
                target=self.target,
                mode=self.mode,
                use_linux=use_linux if use_linux is not None else linux_available,
                workspace=self.build_dir,
            )

            # Extract compile info and emulation from validation_result.performance_metrics
            perf_metrics = getattr(validation_result, "performance_metrics", {}) or {}
            compile_info_validation = perf_metrics.get("compile_info")
            # Emulation result stored in perf_metrics["emulation"] or we need to reconstruct?
            # The harness returns ValidationResult with details, but emulation result is not directly
            # stored as object, only in perf_metrics. We'll try to get it from validation_result if needed.
            # For E2E we want emulation object; we can reconstruct minimal or use what validate_from_paths_qemu produced.
            # Since validate_from_paths_qemu doesn't return emulation object directly, we will parse from perf_metrics.
            # We'll store emulation as a simple object from perf_metrics for reporting.

            # For simplicity, create a mock emulation result from perf_metrics if available
            # The actual EmulationResult was inside _correctness_step_qemu; we lost it after conversion to ValidationResult.
            # So we will re-run parsing: we have details in validation_result.details and perf_metrics.
            # To have full object, we could instead call lower-level functions here, but for now use perf_metrics.

            # Build metrics using baremetal elf and emulation result if we have it
            # We need to try to get emulation_result from validation_result if stored
            # Let's attempt to retrieve via attribute if we stored earlier (we didn't)
            # As fallback, we will use perf_metrics to build KernelMetrics via collect_metrics if baremetal exists
            # If we have baremetal and we want cycles from perf_metrics, we can build metrics manually.
            # Actually, we can create emulation_result placeholder with cycles from perf_metrics.

            from kernelsmith.emulator import EmulationResult as _ER

            emu_mode = perf_metrics.get("mode", self.mode)
            emu_cycles = perf_metrics.get("cycles_estimate", 0)
            emu_time = perf_metrics.get("time_us", 0)
            emu_instr = perf_metrics.get("instruction_count", 0)
            emu_rc = (
                perf_metrics.get("emulation", {}).get("returncode", 0)
                if isinstance(perf_metrics.get("emulation"), dict)
                else 0
            )
            emu_stdout = validation_result.details if validation_result else ""

            emulation_result = _ER(
                mode=emu_mode,
                cycles_estimate=emu_cycles,
                time_us=emu_time,
                instruction_count=emu_instr,
                stdout=emu_stdout,
                stderr="",
                returncode=emu_rc,
            )

            # Now collect real metrics using baremetal ELF and emulation result if baremetal exists
            if baremetal_elf and baremetal_elf.exists():
                try:
                    metrics = collect_metrics(baremetal_elf, emulation_result, self.target)
                    # Fallback: if text is 0 due to --gc-sections removing unreferenced function,
                    # use fallback object size from compile_info_baremetal if available
                    if metrics and metrics.text_bytes == 0 and metrics.total_bytes == 0:
                        fallback_size = None
                        if compile_info_baremetal:
                            fallback_size = compile_info_baremetal.get("fallback_obj_size")
                            # Also try to parse size_obj output
                            size_obj_str = compile_info_baremetal.get("size_obj", "")
                            if size_obj_str and fallback_size is None:
                                # Try to parse first line after header for .text size
                                try:
                                    lines = size_obj_str.strip().splitlines()
                                    if len(lines) >= 2:
                                        parts = lines[-1].split()
                                        # For .o, size output may have text, data, bss etc
                                        # Use first number as text approx
                                        if parts[0].isdigit():
                                            fallback_size = int(parts[0])
                                except Exception:
                                    pass
                        if fallback_size:
                            # Override metrics with fallback
                            from dataclasses import replace

                            metrics = replace(
                                metrics,
                                text_bytes=fallback_size,
                                total_bytes=fallback_size,
                            )
                except Exception:
                    metrics = None
            else:
                # No baremetal, metrics from perf only - try to build from emulation + fallback
                metrics = None
                # If we have fallback size, create a minimal KernelMetrics
                try:
                    if compile_info_baremetal and compile_info_baremetal.get("fallback_obj_size"):
                        from kernelsmith.metrics import KernelMetrics as _KM

                        fb_size = compile_info_baremetal["fallback_obj_size"]
                        metrics = _KM(
                            cycles_estimate=emulation_result.cycles_estimate
                            if emulation_result
                            else 0,
                            time_us=emulation_result.time_us if emulation_result else 0,
                            instruction_count=emulation_result.instruction_count
                            if emulation_result
                            else 0,
                            text_bytes=fb_size,
                            data_bytes=0,
                            bss_bytes=0,
                            total_bytes=fb_size,
                            mode=self.mode,
                            target=self.target,
                        )
                except Exception:
                    metrics = None

            _record_step(
                "validation_compile_and_run",
                step3b_start,
                validation_result.passed if validation_result else False,
                f"Validation ELF {validation_elf.name if validation_elf else 'unknown'}, "
                f"compile={'ok' if validation_result and validation_result.compile_success else 'fail'}, "
                f"correctness={'PASS' if validation_result and validation_result.correctness_passed else 'FAIL'}",
            )

            # If validation failed, still continue to reporting but mark as failure for final
            if validation_result and not validation_result.passed:
                # Don't raise yet, let final report handle, but record timing
                pass

        except Exception as e:
            _record_step("validation_compile_and_run", step3b_start, False, str(e))
            # Build failure ValidationResult if not already
            if validation_result is None:
                from kernelsmith.validation.harness import ValidationResult as _VR

                validation_result = _VR(
                    passed=False,
                    failed_step="compile",
                    compile_success=False,
                    correctness_passed=False,
                    safety_passed=False,
                    performance_metrics={"error": str(e), "compile_info": compile_info_validation},
                    details=f"E2E validation compile/run failed:\n{e}",
                    operator=operator,
                    generated_path=str(c_path) if "c_path" in locals() else "",
                    reference_path=str(reference_path) if reference_path else "",
                )
            # Re-raise as RuntimeError with detailed context for CLI to print failure block
            raise RuntimeError(
                f"Validation QEMU step failed (operator={operator}, target={self.target}, "
                f"use_linux={use_linux}, mode={self.mode}): {e}\n"
                f"Check toolchain: {tc.compiler} available={baremetal_available}, "
                f"linux {linux_toolchain_available()}, qemu={tc.qemu_user}\n"
                f"Sources: driver={driver_c_path}, ref={reference_path}, gen={c_path}\n"
                f"See build dir {self.build_dir} for artifacts"
            ) from e

        # ------------------------------------------------------------------
        # Step 4: Naive vs Optimized benchmark comparison (new per user request)
        # ------------------------------------------------------------------
        # This step runs both naive and optimized kernels through same QEMU benchmark
        # harness and produces side-by-side metrics + gain.
        naive_metrics: dict[str, Any] | None = None
        optimized_metrics: dict[str, Any] | None = None
        comparison: dict[str, Any] | None = None
        naive_elf: pathlib.Path | None = None
        optimized_benchmark_elf: pathlib.Path | None = None
        naive_benchmark_elf: pathlib.Path | None = None
        benchmark_artifacts: dict[str, Any] = {}
        # For size comparison
        naive_baremetal_elf: pathlib.Path | None = None
        naive_compile_info: dict[str, Any] | None = None

        # Only run comparison if we have reference and generated and toolchain available
        compare_start = time.time()
        _notify("start", "benchmark_comparison", {"operator": operator})
        try:
            # Determine reference function name from operator YAML
            ref_func_name = "relu_f32"  # default fallback
            try:
                import yaml as _yaml_cmp

                # Try to load spec for ref_func
                if operator_yaml_path and operator_yaml_path.exists():
                    spec_cmp = _yaml_cmp.safe_load(operator_yaml_path.read_text())
                    ref_func_name = spec_cmp.get("reference", {}).get("function", ref_func_name)
                elif spec_path and pathlib.Path(spec_path).exists():
                    spec_cmp = _yaml_cmp.safe_load(pathlib.Path(spec_path).read_text())
                    ref_func_name = spec_cmp.get("reference", {}).get("function", ref_func_name)
            except Exception:
                pass

            # Determine generated function name from header
            gen_func_name = c_path.stem  # fallback
            try:
                import re as _re_cmp

                header_text = h_path.read_text() if "h_path" in locals() and h_path.exists() else ""
                m = _re_cmp.search(r"void\s+(ks_\w+)\s*\(\s*const\s+float", header_text)
                if m:
                    gen_func_name = m.group(1)
            except Exception:
                pass

            # Helper to get size for a kernel file (baremetal)
            def _get_size_for_file(kernel_c_path: pathlib.Path, name: str):
                try:
                    if not baremetal_available:
                        return None, None
                    # Compile to object for fallback size
                    import subprocess

                    obj_path = self.build_dir / f"{name}_baremetal.o"
                    elf_path = self.build_dir / f"{name}_baremetal.elf"
                    # Compile to object
                    compile_cmd_obj = [
                        tc.compiler,
                        tc.arch_flag,
                        tc.thumb_flag,
                        tc.fpu_flag if tc.fpu_flag else "",
                        tc.float_abi_flag if tc.float_abi_flag else "",
                        "-O2",
                        "-c",
                        str(kernel_c_path),
                        "-o",
                        str(obj_path),
                    ]
                    compile_cmd_obj = [c for c in compile_cmd_obj if c]
                    subprocess.run(compile_cmd_obj, capture_output=True, timeout=10)
                    obj_size = obj_path.stat().st_size if obj_path.exists() else 0

                    # Compile to ELF via compile_c_to_elf (may GC, but we have obj fallback)
                    try:
                        c_info = compile_c_to_elf(kernel_c_path, elf_path, tc, mode="speed")
                    except Exception as e:
                        c_info = {"error": str(e)}

                    # Try to get size via metrics module
                    size_metrics = None
                    try:
                        from kernelsmith.metrics import get_size_metrics

                        if elf_path.exists():
                            size_metrics = get_size_metrics(elf_path)
                    except Exception:
                        pass

                    text_bytes = 0
                    total_bytes = 0
                    if size_metrics:
                        text_bytes = size_metrics.text if size_metrics.text != 0 else obj_size
                        total_bytes = size_metrics.total if size_metrics.total != 0 else obj_size
                    else:
                        text_bytes = obj_size
                        total_bytes = obj_size

                    return {
                        "text_bytes": text_bytes,
                        "total_bytes": total_bytes,
                        "obj_size": obj_size,
                        "elf": str(elf_path),
                        "compile_info": c_info,
                    }, elf_path
                except Exception as e:
                    return {"error": str(e)}, None

            # Size for naive and optimized
            naive_size_info, naive_baremetal_elf = _get_size_for_file(
                reference_path, f"naive_{operator}"
            )
            opt_size_info, _ = _get_size_for_file(c_path, f"opt_{operator}")

            # Generate benchmark drivers
            bench_naive_c = self.build_dir / f"benchmark_naive_{operator}.c"
            bench_opt_c = self.build_dir / f"benchmark_opt_{operator}.c"

            # For naive: if reference file is C, we need to declare function
            naive_driver_code = _generate_benchmark_driver(
                kernel_func=ref_func_name,
                is_naive=True,
            )
            bench_naive_c.write_text(naive_driver_code)

            # For optimized: include header
            header_name_for_opt = h_path.name if "h_path" in locals() and h_path.exists() else None
            opt_driver_code = _generate_benchmark_driver(
                kernel_func=gen_func_name,
                header_name=header_name_for_opt,
                is_naive=False,
            )
            bench_opt_c.write_text(opt_driver_code)

            benchmark_artifacts["benchmark_naive_c"] = str(bench_naive_c)
            benchmark_artifacts["benchmark_opt_c"] = str(bench_opt_c)

            # Compile and run benchmarks under QEMU (linux user mode)
            def _compile_and_run_benchmark(
                driver_c: pathlib.Path, kernel_c: pathlib.Path, name: str
            ):
                try:
                    from kernelsmith.emulator import run_validation_elf_qemu
                    from kernelsmith.toolchain import compile_multi_c_to_elf

                    bench_elf = self.build_dir / f"bench_{name}_{operator}.elf"
                    # Include dirs: for optimized need output dir for header, for naive need ref parent
                    inc_dirs = []
                    if not driver_c.name.startswith("benchmark_naive"):
                        # Optimized: include output dir for header
                        inc_dirs.append(self.output_dir)
                    else:
                        # Naive: include reference parent if needed
                        if reference_path:
                            inc_dirs.append(reference_path.parent)

                    # Compile
                    c_info = compile_multi_c_to_elf(
                        sources=[driver_c, kernel_c],
                        output_elf=bench_elf,
                        tc=tc,
                        include_dirs=inc_dirs,
                        mode="speed",
                        use_linux=True,
                    )

                    # Run under QEMU
                    emu_res = run_validation_elf_qemu(
                        bench_elf,
                        qemu_bin=tc.qemu_user,
                        mode=self.mode,
                        use_linux=True,
                        timeout=15,
                    )

                    # Parse benchmark metrics from stdout
                    bench_metrics = _parse_benchmark_metrics_output(emu_res.stdout)
                    # Also get cycles/time from emulation result as fallback
                    bench_metrics["emulation_cycles"] = emu_res.cycles_estimate
                    bench_metrics["emulation_time_us"] = emu_res.time_us
                    bench_metrics["emulation_instr"] = emu_res.instruction_count
                    bench_metrics["returncode"] = emu_res.returncode
                    bench_metrics["stdout"] = emu_res.stdout[:1000]
                    bench_metrics["elf"] = str(bench_elf)
                    bench_metrics["compile_info"] = c_info

                    return bench_metrics, bench_elf, c_info, emu_res
                except Exception as e:
                    return {"error": str(e)}, None, {"error": str(e)}, None

            # Run naive benchmark
            naive_bench_metrics, naive_benchmark_elf, naive_bench_compile_info, naive_emu = (
                _compile_and_run_benchmark(bench_naive_c, reference_path, "naive")
            )
            # Run optimized benchmark
            opt_bench_metrics, optimized_benchmark_elf, opt_bench_compile_info, opt_emu = (
                _compile_and_run_benchmark(bench_opt_c, c_path, "optimized")
            )

            # Build naive_metrics and optimized_metrics dicts combining size + benchmark
            naive_metrics = {
                "text_bytes": naive_size_info.get("text_bytes", 0)
                if isinstance(naive_size_info, dict)
                else 0,
                "total_bytes": naive_size_info.get("total_bytes", 0)
                if isinstance(naive_size_info, dict)
                else 0,
                "benchmark": naive_bench_metrics,
                "time_us": naive_bench_metrics.get(
                    "time_us", naive_bench_metrics.get("emulation_time_us", 0)
                ),
                "cycles_estimate": naive_bench_metrics.get(
                    "cycles_estimate", naive_bench_metrics.get("emulation_cycles", 0)
                ),
                "instruction_count": naive_bench_metrics.get("emulation_instr", 0),
                "checksum": naive_bench_metrics.get("checksum", 0),
            }
            optimized_metrics = {
                "text_bytes": opt_size_info.get("text_bytes", 0)
                if isinstance(opt_size_info, dict)
                else 0,
                "total_bytes": opt_size_info.get("total_bytes", 0)
                if isinstance(opt_size_info, dict)
                else 0,
                "benchmark": opt_bench_metrics,
                "time_us": opt_bench_metrics.get(
                    "time_us", opt_bench_metrics.get("emulation_time_us", 0)
                ),
                "cycles_estimate": opt_bench_metrics.get(
                    "cycles_estimate", opt_bench_metrics.get("emulation_cycles", 0)
                ),
                "instruction_count": opt_bench_metrics.get("emulation_instr", 0),
                "checksum": opt_bench_metrics.get("checksum", 0),
            }

            # Compute comparison / gain
            def _calc_gain(naive_val, opt_val):
                if naive_val == 0:
                    return {
                        "delta": opt_val - naive_val,
                        "delta_pct": 0,
                        "speedup": 1.0,
                        "improved": opt_val < naive_val,
                    }
                delta = opt_val - naive_val
                delta_pct = (delta / naive_val) * 100 if naive_val != 0 else 0
                speedup = naive_val / opt_val if opt_val != 0 else 1.0
                (
                    opt_val < naive_val
                    if "bytes" in str(naive_val)
                    or "time" in str(naive_val)
                    or "cycles" in str(naive_val)
                    else opt_val > naive_val
                )
                # For size/time/cycles, lower is better
                return {
                    "naive": naive_val,
                    "optimized": opt_val,
                    "delta": delta,
                    "delta_pct": delta_pct,
                    "speedup": speedup,
                    "improved": delta < 0
                    if "bytes" in str(naive_val)
                    or "time" in str(naive_val)
                    or "cycles" in str(naive_val)
                    or isinstance(naive_val, int | float)
                    else False,
                }

            # Generic gain calc for key metrics
            comparison = {}
            for key in [
                "text_bytes",
                "total_bytes",
                "time_us",
                "cycles_estimate",
                "instruction_count",
            ]:
                n_val = naive_metrics.get(key, 0)
                o_val = optimized_metrics.get(key, 0)
                if isinstance(n_val, int | float) and isinstance(o_val, int | float):
                    delta = o_val - n_val
                    delta_pct = (delta / n_val * 100) if n_val != 0 else 0
                    speedup = (n_val / o_val) if o_val != 0 else 1.0
                    # For these metrics, lower is better (size, time, cycles)
                    improved = o_val < n_val
                    comparison[key] = {
                        "naive": n_val,
                        "optimized": o_val,
                        "delta": delta,
                        "delta_pct": delta_pct,
                        "speedup": speedup,
                        "improved": improved,
                    }

            # Overall summary
            comparison["summary"] = {
                "size_improved": comparison.get("text_bytes", {}).get("improved", False),
                "time_improved": comparison.get("time_us", {}).get("improved", False),
                "cycles_improved": comparison.get("cycles_estimate", {}).get("improved", False),
                "overall_gain": "Optimized is faster"
                if comparison.get("time_us", {}).get("improved", False)
                else "Naive is faster or equal",
            }

            _record_step(
                "benchmark_comparison",
                compare_start,
                True,
                (
                    f"Naive vs Optimized: "
                    f"naive_time={naive_metrics.get('time_us')}us "
                    f"opt_time={optimized_metrics.get('time_us')}us "
                    f"speedup={comparison.get('time_us', {}).get('speedup', 1):.2f}x"
                ),
            )

        except Exception as e:
            _record_step("benchmark_comparison", compare_start, False, f"Comparison failed: {e}")
            # Don't fail entire pipeline if comparison fails, just log
            naive_metrics = {"error": str(e)}
            optimized_metrics = {"error": str(e)}
            comparison = {"error": str(e), "summary": {"overall_gain": f"Comparison failed: {e}"}}

        # Step 5: Finalize metrics and results JSON
        step5_start = time.time()
        _notify("start", "finalize_and_write_json", {"operator": operator})
        try:
            total_dur = time.time() - total_start
            timing["total"] = total_dur

            # Determine results JSON path
            if output_json:
                results_json_path = pathlib.Path(output_json)
            else:
                results_json_path = (
                    self.results_dir
                    / f"e2e_{operator}_{self.target}_{self.mode}_{llm_provider}.json"
                )
            results_json_path.parent.mkdir(parents=True, exist_ok=True)

            # Build E2EResult with comparison
            e2e_result = E2EResult(
                operator=operator,
                target=self.target,
                mode=self.mode,
                llm_provider=llm_provider,
                model=actual_model if "actual_model" in locals() else (model or "unknown"),
                generated_files=opt_result.files if "opt_result" in locals() else None,
                baremetal_elf=baremetal_elf,
                validation_elf=validation_elf,
                driver_c_path=driver_c_path,
                validation_result=validation_result,
                metrics=metrics,
                emulation=emulation_result,
                compile_info_baremetal=compile_info_baremetal,
                compile_info_validation=compile_info_validation,
                timing=timing,
                steps=steps,
                output_dir=self.output_dir,
                results_json_path=results_json_path,
                reference_path=reference_path,
                operator_yaml_path=operator_yaml_path,
                naive_metrics=naive_metrics if "naive_metrics" in locals() else None,
                optimized_metrics=optimized_metrics if "optimized_metrics" in locals() else None,
                comparison=comparison if "comparison" in locals() else None,
                naive_elf=naive_baremetal_elf if "naive_baremetal_elf" in locals() else None,
                naive_benchmark_elf=naive_benchmark_elf
                if "naive_benchmark_elf" in locals()
                else None,
                optimized_benchmark_elf=optimized_benchmark_elf
                if "optimized_benchmark_elf" in locals()
                else None,
                benchmark_artifacts=benchmark_artifacts
                if "benchmark_artifacts" in locals()
                else {},
            )

            # Write JSON
            results_json_path.write_text(json.dumps(e2e_result.to_dict(), indent=2))

            # Also write to standard results_dir for harness consumption
            standard_path = self.results_dir / f"e2e_{operator}_{self.target}_{self.mode}.json"
            if standard_path != results_json_path:
                try:
                    standard_path.write_text(json.dumps(e2e_result.to_dict(), indent=2))
                except Exception:
                    pass

            _record_step("finalize_and_write_json", step5_start, True, f"Wrote {results_json_path}")

            return e2e_result

        except Exception as e:
            _record_step("finalize_and_write_json", step5_start, False, str(e))
            raise RuntimeError(f"Finalize step failed: {e}") from e

    def compare_modes(self, operator: str = "relu", **kwargs) -> dict[str, Any]:
        """Run both fast and full to reason about trade-offs."""
        fast_h = KernelsmithHarness(target=self.target, mode="fast", workspace=self.workspace)
        full_h = KernelsmithHarness(target=self.target, mode="full", workspace=self.workspace)
        fast_res = fast_h.optimize_and_measure(operator=operator, **kwargs)
        full_res = full_h.optimize_and_measure(operator=operator, **kwargs)
        from kernelsmith.metrics import compare_fast_vs_full

        comp = compare_fast_vs_full(fast_res.metrics, full_res.metrics)
        return {
            "fast": fast_res.metrics.to_dict(),
            "full": full_res.metrics.to_dict(),
            "comparison": comp,
        }


# Convenience functions for Metacode LLM harness integration point
def run_kernelsmith_pipeline(
    operator: str,
    target: str = "cortex-m7",
    mode: str = "fast",
    llm_provider: str = "avocado_free",
    workspace: str = "/workspace",
) -> dict:
    h = KernelsmithHarness(target=target, mode=mode, workspace=workspace)
    res = h.optimize_and_measure(operator=operator, llm_provider=llm_provider)
    return {
        "metrics": res.metrics.to_dict(),
        "elf": str(res.elf_path),
        "generated_c": str(res.generated_files.c_path),
        "generated_h": str(res.generated_files.header_path),
    }


def run_kernelsmith_e2e_pipeline(
    operator: str,
    target: str = "cortex-m7",
    mode: str = "fast",
    llm_provider: str = "avocado_free",
    workspace: str = "/workspace",
    precision: str = "fp32",
    use_linux: bool = True,
    output_json: str | None = None,
) -> dict:
    """
    E2E pipeline convenience: codegen + test suite gen + QEMU validation + metrics.

    Default llm_provider=avocado_free for final real LLM validation, but accepts mock for CI fast path.
    """
    h = KernelsmithHarness(target=target, mode=mode, workspace=workspace)
    out_path = pathlib.Path(output_json) if output_json else None
    res = h.e2e_pipeline(
        operator=operator,
        llm_provider=llm_provider,
        precision=precision,
        use_linux=use_linux,
        output_json=out_path,
    )
    return res.to_dict()
