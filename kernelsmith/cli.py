import pathlib

import click
import yaml

from kernelsmith import __version__
from kernelsmith.codegen.optimize import optimize as optimize_fn
from kernelsmith.codegen.prompt_builder import (
    list_builtin_targets,
)


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
    "--provider",
    type=click.Choice(["mock", "avocado"]),
    default="mock",
    show_default=True,
    help="LLM provider. mock=offline, avocado=Avocado API (needs KERNELSMITH_MODEL_API_KEY).",
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
    default="avocado_metacode_rc",
    show_default=True,
    help="Model name for Avocado provider.",
)
def optimize_cmd(
    operator: str,
    target_name: str,
    spec_path: pathlib.Path | None,
    output_dir: pathlib.Path,
    provider: str,
    template: pathlib.Path | None,
    model: str,
) -> None:
    """Generate optimized C kernel for OPERATOR and target.

    Example: kernelsmith optimize relu --target cortex-m7 --output-dir ./output
    """
    operator = operator.lower()
    target_name = target_name.lower()
    try:
        result = optimize_fn(
            operator=operator,
            target=target_name,
            spec_path=spec_path,
            output_dir=output_dir,
            provider_name=provider,
            template_path=template,
            model=model,
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
    "--provider",
    type=click.Choice(["mock", "avocado"]),
    default="mock",
    show_default=True,
    help="LLM provider.",
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
    default="avocado_metacode_rc",
    show_default=True,
    help="Model name.",
)
def generate_cmd(
    operator: str,
    target_name: str,
    spec_path: pathlib.Path | None,
    output_dir: pathlib.Path,
    provider: str,
    template: pathlib.Path | None,
    model: str,
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
        provider=provider,
        template=template,
        model=model,
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
    required=True,
    help="Hardware profile YAML.",
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
    "--qemu",
    is_flag=True,
    default=False,
    help="Run benchmark under qemu-system-arm.",
)
def benchmark_cmd(
    kernel: pathlib.Path,
    target: pathlib.Path,
    iterations: int,
    qemu: bool,
) -> None:
    """Benchmark a kernel using arm-none-eabi-gcc and QEMU (future)."""
    click.echo("TODO: benchmark command will:")
    click.echo(f"  - Compile kernel {kernel} with arm-none-eabi-gcc for target {target}")
    click.echo("  - If --qemu: launch qemu-system-arm or qemu-user to emulate Cortex-M7")
    click.echo(f"  - Run {iterations} iterations and collect cycles/instructions via QEMU tracing")
    click.echo("  - Compare against reference/naive implementations")
    click.echo("  - Output JSON/CSV performance metrics")


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
def validate_cmd(
    generated: pathlib.Path,
    reference: pathlib.Path,
    operator: pathlib.Path,
    tolerance: float,
) -> None:
    """Validate optimized kernel correctness against reference (future)."""
    click.echo("TODO: validate command will:")
    click.echo(f"  - Compile both generated {generated} and reference {reference}")
    click.echo("    with arm-none-eabi-gcc")
    click.echo(f"  - Load operator spec {operator} to generate random test tensors (via numpy)")
    click.echo(f"  - Run both impls under qemu-user and compare with tolerance {tolerance}")


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


if __name__ == "__main__":
    main()
