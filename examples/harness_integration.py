"""
Example LLM harness integration for kernelsmith.
Demonstrates Metacode LLM generating optimized C kernel, compiling per target
toolchain, emulating under QEMU, collecting metrics for fast vs full trade-off.
"""

import json
import pathlib

from kernelsmith.harness import KernelsmithHarness, run_kernelsmith_pipeline


def main():
    workspace = pathlib.Path("/workspace")
    workspace.mkdir(exist_ok=True)

    print("=== Kernelsmith LLM Harness Example ===")
    print("Target: cortex-m7, mode: fast, provider: mock (offline)")

    # Simple pipeline
    result = run_kernelsmith_pipeline(
        operator="relu",
        target="cortex-m7",
        mode="fast",
        llm_provider="mock",
        workspace=str(workspace),
    )
    print("\n--- Pipeline result ---")
    print(json.dumps(result, indent=2))

    # Advanced harness with compare modes
    h = KernelsmithHarness(target="cortex-m7", mode="fast", workspace=workspace)
    comparison = h.compare_modes(operator="relu", llm_provider="mock")
    print("\n--- Fast vs Full comparison ---")
    print(json.dumps(comparison, indent=2))

    fast = comparison["fast"]
    full = comparison["full"]
    print("\nReasoning for LLM harness:")
    print(f"  Fast mode cycles: {fast['cycles_estimate']}, instr: {fast['instruction_count']}, time_us: {fast['time_us']}")
    print(f"  Full mode cycles: {full['cycles_estimate']}, time_us: {full['time_us']}")
    print(f"  Ratio full/fast: {comparison['comparison']['cycles_ratio']:.2f}")
    print(f"  Text size: {fast['text_bytes']} bytes, total: {fast['total_bytes']} bytes")
    print(f"  Trade-off: {comparison['comparison']['tradeoff_note']}")

    print(
        "\nLLM harness can now reason: fast run gives instruction accuracy "
        "for quick iteration, full run gives cycle accuracy with ~15pct "
        "overhead modeling pipeline stalls. Choose based on latency budget."
    )


if __name__ == "__main__":
    main()
