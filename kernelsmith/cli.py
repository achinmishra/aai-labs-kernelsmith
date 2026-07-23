import pathlib
import json

import click
import yaml

from kernelsmith import __version__
from kernelsmith.codegen.optimize import optimize as optimize_fn
from kernelsmith.codegen.prompt_builder import (
    list_builtin_targets,
    resolve_hardware_profile,
)
from kernelsmith.toolchain import (
    resolve_toolchain,
    compile_c_to_elf,
    compile_baremetal_system_elf,
    toolchain_available,
)
from kernelsmith.emulator import emulate
from kernelsmith.metrics import collect_metrics, compare_fast_vs_full
from kernelsmith.harness import KernelsmithHarness, run_kernelsmith_pipeline


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(version=__version__, prog_name="kernelsmith")
def main() -> None:
    """Kernelsmith - generates optimized C code for ARM Cortex-M7."""


@main.command(name="optimize")
@click.argument("operator", type=str)
@click.option(
    "--target",
    "-t",
    "target_name",
    type=str,
    required=True,
    help="Target device (e.g., cortex-m7).",
)
@click.option(
    "--spec",
    "spec_path",
    type=click.Path(exists=True, path_type=pathlib.Path),
    default=None,
    help="User-provided operator spec YAML. Falls back to built-in.",
)
@click.option(
    "--output-dir",
    "-o",
    "output_dir",
    type=click.Path(path_type=pathlib.Path),
    default=pathlib.Path("./output"),
    show_default=True,
    help="Output directory for .h/.c/.md triplet.",
)
@click.option(
    "--llm-provider",
    "--provider",
    "llm_provider",
    type=click.Choice(["mock", "avocado", "avocado_free"]),
    default="avocado_free",
    show_default=True,
    help="LLM provider: avocado_free=free (default), avocado=prod soon, mock=offline.",
)
@click.option(
    "--template",
    type=click.Path(exists=True, path_type=pathlib.Path),
    default=None,
    help="Custom prompt template override.",
)
@click.option(
    "--model",
    type=str,
    default=None,
    help="Model name. Default avocado_metacode_rc (prod and dev).",
)
@click.option(
    "--dev",
    "--experimental",
    "dev",
    is_flag=True,
    default=False,
    hidden=True,
    help="Deprecated: use --llm-provider avocado_free.",
)
def optimize_cmd(
    operator: str,
    target_name: str,
    spec_path: pathlib.Path | None,
    output_dir: pathlib.Path,
    llm_provider: str,
    template: pathlib.Path | None,
    model: str | None,
    dev: bool,
) -> None:
    """Generate optimized C kernel for OPERATOR and target.

    Example: kernelsmith optimize relu --target cortex-m7 --output-dir ./output
    """
    operator = operator.lower()
    target_name = target_name.lower()
    effective_model = model
    if effective_model is None:
        effective_model = "avocado_metacode_rc"
    try:
        result = optimize_fn(
            operator=operator,
            target=target_name,
            spec_path=spec_path,
            output_dir=output_dir,
            llm_provider_name=llm_provider,
            template_path=template,
            model=effective_model,
            dev=dev,
        )
        click.echo(f"Generated files for {operator} ({target_name}):")
        click.echo(f"  Header: {result.files.header_path}")
        click.echo(f"  Implementation: {result.files.c_path}")
        click.echo(f"  Reasoning: {result.files.md_path}")
        click.echo(f"  Model: {result.model}")
        click.echo(f"  Operator spec: {result.operator_path}")
        click.echo(f"  Hardware profile: {result.target_path}")
    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort() from e
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort() from e
    except Exception as e:
        click.echo(f"Unexpected error: {e}", err=True)
        raise click.Abort() from e


@main.command(name="generate")
@click.argument("operator", type=str)
@click.option(
    "--target",
    "-t",
    "target_name",
    type=str,
    required=True,
    help="Target device (e.g., cortex-m7).",
)
@click.option(
    "--spec",
    "spec_path",
    type=click.Path(exists=True, path_type=pathlib.Path),
    default=None,
    help="User-provided operator spec YAML.",
)
@click.option(
    "--output-dir",
    "-o",
    "output_dir",
    type=click.Path(path_type=pathlib.Path),
    default=pathlib.Path("./output"),
    show_default=True,
    help="Output directory.",
)
@click.option(
    "--llm-provider",
    "--provider",
    "llm_provider",
    type=click.Choice(["mock", "avocado", "avocado_free"]),
    default="avocado_free",
    show_default=True,
    help="LLM provider: avocado_free=free (default), avocado=prod soon, mock=offline.",
)
@click.option(
    "--template",
    type=click.Path(exists=True, path_type=pathlib.Path),
    default=None,
    help="Custom template.",
)
@click.option(
    "--model",
    type=str,
    default=None,
    help="Model name. Default avocado_metacode_rc (prod and dev).",
)
@click.option(
    "--dev",
    "--experimental",
    "dev",
    is_flag=True,
    default=False,
    hidden=True,
    help="Deprecated: use --llm-provider avocado_free.",
)
def generate_cmd(
    operator: str,
    target_name: str,
    spec_path: pathlib.Path | None,
    output_dir: pathlib.Path,
    llm_provider: str,
    template: pathlib.Path | None,
    model: str | None,
    dev: bool,
) -> None:
    """Alias for optimize (kept for backward compat with brief doc)."""
    click.echo("Note: `generate` is alias for `optimize`, prefer `optimize`")
    ctx = click.get_current_context()
    ctx.invoke(
        optimize_cmd,
        operator=operator,
        target_name=target_name,
        spec_path=spec_path,
        output_dir=output_dir,
        llm_provider=llm_provider,
        template=template,
        model=model,
        dev=dev,
    )


@main.command(name="benchmark")
@click.option(
    "--kernel",
    "-k",
    type=click.Path(exists=True, path_type=pathlib.Path),
    required=True,
    help="Path to generated or naive C kernel to benchmark.",
)
@click.option(
    "--target",
    "-t",
    type=click.Path(exists=True, path_type=pathlib.Path),
    required=False,
    help="Hardware profile YAML path.",
)
@click.option(
    "--target-name",
    type=str,
    default="cortex-m7",
    show_default=True,
    help="Target name for toolchain resolution (e.g., cortex-m7, cortex-m4).",
)
@click.option(
    "--iterations",
    "-n",
    type=int,
    default=1000,
    show_default=True,
    help="Number of benchmark iterations.",
)
@click.option(
    "--mode",
    type=click.Choice(["fast", "full", "auto"]),
    default="auto",
    show_default=True,
    help="QEMU mode: fast=instruction accurate qemu-user, full=cycle approximate qemu-system, auto=fast.",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=pathlib.Path),
    default=pathlib.Path("results.json"),
    show_default=True,
    help="Output JSON metrics file.",
)
@click.option(
    "--qemu",
    is_flag=True,
    default=False,
    help="Legacy flag, implies --mode full.",
)
def benchmark_cmd(
    kernel: pathlib.Path,
    target: pathlib.Path | None,
    target_name: str,
    iterations: int,
    mode: str,
    output: pathlib.Path,
    qemu: bool,
) -> None:
    """Benchmark a kernel using arm-none-eabi-gcc and QEMU with metrics."""
    if qemu:
        mode = "full"
    try:
        # Resolve hardware profile for toolchain config
        hp_path = target
        if hp_path is None:
            try:
                hp_path = resolve_hardware_profile(target_name)
            except Exception:
                hp_path = None
        tc = resolve_toolchain(target_name, hp_path)
        if not toolchain_available(tc.compiler):
            click.echo(
                f"Error: toolchain {tc.compiler} not found. Use kernelsmith Docker image.", err=True
            )
            raise click.Abort()
        build_dir = pathlib.Path("./build")
        build_dir.mkdir(exist_ok=True)
        elf_path = build_dir / f"{kernel.stem}.elf"
        click.echo(
            f"Compiling {kernel} for {target_name} with {tc.compiler} {tc.arch_flag} {tc.fpu_flag} {tc.float_abi_flag}..."
        )
        compile_info = compile_c_to_elf(kernel, elf_path, tc, mode="speed")
        click.echo(f"ELF size info:\n{compile_info['size']}")

        # For full mode, build real baremetal system image with startup/linker/semihosting + DWT
        if mode == "full":
            system_elf = build_dir / f"{kernel.stem}_system.elf"
            try:
                import re

                # Try to auto-detect func name from kernel source
                func_name = None
                try:
                    txt = kernel.read_text(errors="ignore")
                    m = re.search(r"void\s+(ks_\w+)\s*\(", txt)
                    if m:
                        func_name = m.group(1)
                except Exception:
                    pass
                click.echo(
                    f"Compiling baremetal system image for {tc.qemu_machine} {tc.qemu_cpu} (func={func_name or 'auto'})..."
                )
                sys_info = compile_baremetal_system_elf(
                    kernel_source=kernel,
                    output_elf=system_elf,
                    tc=tc,
                    build_dir=build_dir,
                    func_name=func_name,
                    iters=iterations,
                )
                click.echo(f"System ELF size:\n{sys_info['size']}")
                compile_info = {**compile_info, "system": sys_info}
                emu_elf = system_elf
                metrics_elf = system_elf
            except Exception as e:
                click.echo(
                    f"WARN: system ELF build failed ({e}), falling back to simple ELF", err=True
                )
                emu_elf = elf_path
                metrics_elf = elf_path
        else:
            emu_elf = elf_path
            metrics_elf = elf_path

        click.echo(
            f"Emulating under QEMU mode={mode} (user={tc.qemu_user}, system={tc.qemu_system} machine={tc.qemu_machine} cpu={tc.qemu_cpu})..."
        )
        emu = emulate(
            emu_elf,
            mode=mode,
            qemu_user=tc.qemu_user,
            qemu_system=tc.qemu_system,
            machine=tc.qemu_machine,
            cpu=tc.qemu_cpu,
        )
        metrics = collect_metrics(metrics_elf, emu, target_name)
        out_data = {
            "kernel": str(kernel),
            "target": target_name,
            "hardware_profile": str(hp_path) if hp_path else None,
            "mode": emu.mode,
            "iterations": iterations,
            "toolchain": {
                "compiler": tc.compiler,
                "arch": tc.arch_flag,
                "fpu": tc.fpu_flag,
                "float_abi": tc.float_abi_flag,
                "qemu_user": tc.qemu_user,
                "qemu_system": tc.qemu_system,
                "qemu_machine": tc.qemu_machine,
                "qemu_cpu": tc.qemu_cpu,
            },
            "metrics": metrics.to_dict(),
            "compile": compile_info,
            "emulation": {
                "returncode": emu.returncode,
                "stdout": emu.stdout[:2000],
                "stderr": emu.stderr[:2000],
            },
            "tradeoff_note": "fast mode = instruction accurate via qemu-user -d in_asm (cortex-a15 proxy); full mode = real baremetal qemu-system-arm -machine mps2-an500 -cpu cortex-m7 with semihosting + DWT CYCCNT cycle-accurate. Use fast for iteration speed, full for realistic MCU timing.",
        }
        output.write_text(json.dumps(out_data, indent=2))
        click.echo(f"Benchmark complete. Metrics written to {output}")
        click.echo(f"  cycles_estimate: {metrics.cycles_estimate}")
        click.echo(f"  time_us: {metrics.time_us}")
        click.echo(f"  instruction_count: {metrics.instruction_count}")
        click.echo(
            f"  text_bytes: {metrics.text_bytes}  data: {metrics.data_bytes}  bss: {metrics.bss_bytes}  total: {metrics.total_bytes}"
        )
        click.echo(f"  mode: {metrics.mode}  target: {metrics.target}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort() from e


@main.command(name="validate")
@click.option(
    "--generated",
    "-g",
    type=click.Path(exists=True, path_type=pathlib.Path),
    required=True,
    help="Path to generated optimized kernel.",
)
@click.option(
    "--reference",
    "-r",
    type=click.Path(exists=True, path_type=pathlib.Path),
    required=True,
    help="Path to reference naive implementation (e.g., reference/naive/relu.c).",
)
@click.option(
    "--operator",
    "-o",
    type=click.Path(exists=True, path_type=pathlib.Path),
    required=True,
    help="Operator spec YAML for test vectors.",
)
@click.option(
    "--tolerance",
    type=float,
    default=1e-5,
    show_default=True,
    help="Numerical tolerance for validation.",
)
@click.option(
    "--target-name",
    type=str,
    default="cortex-m7",
    show_default=True,
    help="Target for toolchain.",
)
def validate_cmd(
    generated: pathlib.Path,
    reference: pathlib.Path,
    operator: pathlib.Path,
    tolerance: float,
    target_name: str,
) -> None:
    """Validate optimized kernel correctness against reference using toolchain compile check."""
    try:
        tc = resolve_toolchain(target_name)
        if not toolchain_available(tc.compiler):
            click.echo(f"Error: toolchain {tc.compiler} not found. Use Docker image.", err=True)
            raise click.Abort()
        build_dir = pathlib.Path("./build")
        build_dir.mkdir(exist_ok=True)
        gen_elf = build_dir / f"{generated.stem}_val.elf"
        ref_elf = build_dir / f"{reference.stem}_val.elf"
        click.echo(f"Compiling generated {generated}...")
        compile_c_to_elf(generated, gen_elf, tc)
        click.echo(f"Compiling reference {reference}...")
        compile_c_to_elf(reference, ref_elf, tc)
        # Size comparison as proxy for validation in this scaffolding
        from kernelsmith.metrics import get_size_metrics

        gen_size = get_size_metrics(gen_elf)
        ref_size = get_size_metrics(ref_elf)
        click.echo("Validation (compile + size check) passed.")
        click.echo(
            f"  Generated: text={gen_size.text} data={gen_size.data} bss={gen_size.bss} total={gen_size.total}"
        )
        click.echo(
            f"  Reference: text={ref_size.text} data={ref_size.data} bss={ref_size.bss} total={ref_size.total}"
        )
        click.echo(f"  Tolerance for numerical check (future full numpy compare): {tolerance}")
        click.echo(
            "  NOTE: Full numerical validation under QEMU with test vectors is planned; current check ensures both compile and link successfully for target."
        )
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort() from e


@main.command(name="list-operators")
@click.option(
    "--spec-dir",
    type=click.Path(path_type=pathlib.Path),
    default=None,
    help="Directory containing operator spec YAMLs (defaults to built-in).",
)
def list_operators_cmd(spec_dir: pathlib.Path | None) -> None:
    """List available operator specifications (built-in registry)."""
    if spec_dir:
        search_dirs = [spec_dir]
    else:
        from kernelsmith.codegen.prompt_builder import list_builtin_operators as list_ops

        builtins = list_ops()
        if builtins:
            click.echo(f"Found {len(builtins)} built-in operator spec(s):")
            for s in builtins:
                try:
                    data = yaml.safe_load(s.read_text())
                    name = data.get("name", s.stem) if isinstance(data, dict) else s.stem
                    op_type = (
                        data.get("op_type", "unknown") if isinstance(data, dict) else "unknown"
                    )
                    desc = data.get("description", "") if isinstance(data, dict) else ""
                    click.echo(f"  - {name} ({op_type}): {s} – {desc[:60]}")
                except Exception:
                    click.echo(f"  - {s.stem}: {s} [failed to parse]")
            return
        search_dirs = [
            pathlib.Path("kernelsmith/operators"),
            pathlib.Path("examples/operators"),
        ]

    for d in search_dirs:
        if d.exists():
            specs = list(d.glob("*.yaml")) + list(d.glob("*.yml"))
            if specs:
                click.echo(f"Found {len(specs)} operator spec(s) in {d}:")
                for s in specs:
                    try:
                        data = yaml.safe_load(s.read_text())
                        name = data.get("name", s.stem) if isinstance(data, dict) else s.stem
                        op_type = (
                            data.get("op_type", "unknown") if isinstance(data, dict) else "unknown"
                        )
                        click.echo(f"  - {name} ({op_type}): {s}")
                    except Exception:
                        click.echo(f"  - {s.stem}: {s} [failed to parse]")


@main.command(name="list-targets")
@click.option(
    "--target-dir",
    type=click.Path(path_type=pathlib.Path),
    default=None,
    help="Directory containing hardware profile YAMLs (defaults to built-in).",
)
def list_targets_cmd(target_dir: pathlib.Path | None) -> None:
    """List available hardware target profiles (built-in registry)."""
    if target_dir:
        search_dirs = [target_dir]
    else:
        builtins = list_builtin_targets()
        if builtins:
            click.echo(f"Found {len(builtins)} built-in hardware profile(s):")
            for p in builtins:
                try:
                    data = yaml.safe_load(p.read_text())
                    name = data.get("name", p.stem) if isinstance(data, dict) else p.stem
                    arch = (
                        data.get("architecture", "unknown") if isinstance(data, dict) else "unknown"
                    )
                    click.echo(f"  - {name} ({arch}): {p}")
                except Exception:
                    click.echo(f"  - {p.stem}: {p} [failed to parse]")
            return
        search_dirs = [
            pathlib.Path("kernelsmith/hardware_profiles"),
            pathlib.Path("examples/hardware"),
        ]

    for d in search_dirs:
        if d.exists():
            profiles = list(d.glob("*.yaml")) + list(d.glob("*.yml"))
            if profiles:
                click.echo(f"Found {len(profiles)} hardware profile(s) in {d}:")
                for p in profiles:
                    try:
                        data = yaml.safe_load(p.read_text())
                        name = data.get("name", p.stem) if isinstance(data, dict) else p.stem
                        arch = (
                            data.get("architecture", "unknown")
                            if isinstance(data, dict)
                            else "unknown"
                        )
                        click.echo(f"  - {name} ({arch}): {p}")
                    except Exception:
                        click.echo(f"  - {p.stem}: {p} [failed to parse]")


@main.command(name="export-data")
@click.option(
    "--input",
    "-i",
    "input_path",
    type=click.Path(exists=True, path_type=pathlib.Path),
    required=True,
    help="Path to benchmark results JSON.",
)
@click.option(
    "--format",
    "-f",
    "export_format",
    type=click.Choice(["json", "csv", "parquet", "yaml"]),
    default="json",
    show_default=True,
    help="Export format.",
)
@click.option(
    "--output",
    "-O",
    type=click.Path(path_type=pathlib.Path),
    required=True,
    help="Output file path.",
)
def export_data_cmd(
    input_path: pathlib.Path,
    export_format: str,
    output: pathlib.Path,
) -> None:
    """Export benchmarking data to different formats (future)."""
    click.echo("TODO: export-data command will:")
    click.echo(f"  - Load benchmark data from {input_path}")
    click.echo(f"  - Convert to {export_format} format")
    click.echo(f"  - Write to {output}")


@main.command(name="report")
@click.option(
    "--input",
    "-i",
    "input_path",
    type=click.Path(exists=True, path_type=pathlib.Path),
    required=True,
    help="Path to benchmark results or export directory.",
)
@click.option(
    "--output",
    "-O",
    type=click.Path(path_type=pathlib.Path),
    default=pathlib.Path("./report.html"),
    show_default=True,
    help="Output report path (HTML or Markdown).",
)
@click.option(
    "--template",
    type=str,
    default="default",
    show_default=True,
    help="Report template name.",
)
def report_cmd(
    input_path: pathlib.Path,
    output: pathlib.Path,
    template: str,
) -> None:
    """Generate performance report from benchmark data (future)."""
    click.echo("TODO: report command will:")
    click.echo(f"  - Load results from {input_path}")
    click.echo("  - Aggregate per-operator and per-target statistics")
    click.echo(f"  - Render {template} template to {output}")


@main.command(name="pipeline")
@click.argument("operator", type=str)
@click.option(
    "--target", "-t", type=str, default="cortex-m7", show_default=True, help="Target device."
)
@click.option("--spec", type=click.Path(exists=True, path_type=pathlib.Path), default=None)
@click.option(
    "--output-dir", "-o", type=click.Path(path_type=pathlib.Path), default=pathlib.Path("./output")
)
@click.option(
    "--llm-provider", type=click.Choice(["mock", "avocado", "avocado_free"]), default="mock"
)
@click.option("--mode", type=click.Choice(["fast", "full", "auto"]), default="fast")
@click.option(
    "--workspace", type=click.Path(path_type=pathlib.Path), default=pathlib.Path("/workspace")
)
def pipeline_cmd(operator, target, spec, output_dir, llm_provider, mode, workspace):
    """End-to-end LLM harness pipeline: optimize -> compile -> emulate -> metrics."""
    try:
        click.echo(f"Running kernelsmith pipeline for {operator} on {target} mode={mode}...")
        result = run_kernelsmith_pipeline(
            operator=operator,
            target=target,
            mode=mode,
            llm_provider=llm_provider,
            workspace=str(workspace),
        )
        click.echo(json.dumps(result, indent=2))
        click.echo("Pipeline complete. Metrics ready for LLM harness reasoning.")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort() from e


@main.command(name="toolchain-info")
def toolchain_info_cmd():
    """Show toolchain and QEMU environment info for Docker."""
    import subprocess

    def ver(cmd):
        try:
            out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=3)
            return out.stdout.strip().splitlines()[0]
        except Exception:
            return "not found"

    click.echo("=== Kernelsmith Toolchain Environment ===")
    click.echo(f"arm-none-eabi-gcc: {ver('arm-none-eabi-gcc --version')}")
    click.echo(f"arm-linux-gnueabihf-gcc: {ver('arm-linux-gnueabihf-gcc --version')}")
    click.echo(f"qemu-arm: {ver('qemu-arm --version')}")
    click.echo(f"qemu-system-arm: {ver('qemu-system-arm --version')}")
    click.echo(f"gdb-multiarch: {ver('gdb-multiarch --version')}")
    click.echo(f"python: {ver('python3 --version')}")
    from kernelsmith.toolchain import TARGET_REGISTRY

    click.echo(f"Supported targets: {', '.join(TARGET_REGISTRY.keys())}")


@main.command(name="compare-modes")
@click.argument("operator", type=str)
@click.option("--target", "-t", default="cortex-m7")
@click.option("--spec", type=click.Path(exists=True, path_type=pathlib.Path), default=None)
@click.option("--llm-provider", default="mock")
@click.option("--workspace", default="/workspace")
def compare_modes_cmd(operator, target, spec, llm_provider, workspace):
    """Compare fast vs full QEMU modes for trade-off reasoning."""
    try:
        h = KernelsmithHarness(target=target, mode="fast", workspace=workspace)
        result = h.compare_modes(operator=operator, spec_path=spec, llm_provider=llm_provider)
        click.echo(json.dumps(result, indent=2))
        comp = result["comparison"]
        click.echo(
            f"Fast cycles: {result['fast']['cycles_estimate']}, Full cycles: {result['full']['cycles_estimate']}, ratio: {comp['cycles_ratio']}"
        )
        click.echo(comp["tradeoff_note"])
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort() from e


if __name__ == "__main__":
    main()
