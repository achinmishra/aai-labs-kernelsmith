"""LLM harness integration for kernelsmith.

Provides a simple API for main LLM harness to:
 - generate optimized kernel via LLM
 - compile for target toolchain
 - emulate under QEMU (fast or full)
 - return metrics

Example usage from Metacode harness:
    from kernelsmith.harness import KernelsmithHarness
    h = KernelsmithHarness(target="cortex-m7", mode="fast")
    result = h.optimize_and_measure(operator="relu", llm_provider="avocado_free")
    print(result.metrics)
"""

from __future__ import annotations
import pathlib
import tempfile
import json
from dataclasses import dataclass
from typing import Any, Dict

from kernelsmith.codegen.optimize import optimize as ks_optimize
from kernelsmith.toolchain import resolve_toolchain, compile_c_to_elf, toolchain_available
from kernelsmith.emulator import emulate
from kernelsmith.metrics import collect_metrics, KernelMetrics


@dataclass
class HarnessResult:
    operator: str
    target: str
    generated_files: Any
    elf_path: pathlib.Path
    metrics: KernelMetrics
    emulation: Any
    compile_info: Dict[str, Any]


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
        elf_path = self.build_dir / f"{c_path.stem}.elf"
        compile_info = compile_c_to_elf(c_path, elf_path, tc, mode="speed")

        # 4. Emulate under QEMU
        emu_result = emulate(
            elf_path,
            mode=self.mode,
            qemu_user=tc.qemu_user,
            qemu_system=tc.qemu_system,
            machine=tc.qemu_machine,
            cpu=tc.qemu_cpu,
        )

        # 5. Collect metrics
        metrics = collect_metrics(elf_path, emu_result, self.target)

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

    def compare_modes(self, operator: str = "relu", **kwargs) -> Dict[str, Any]:
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


# Convenience function for Metacode LLM harness integration point
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
