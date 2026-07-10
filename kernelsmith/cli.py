import pathlib

import click
import yaml

from kernelsmith import __version__


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(version=__version__, prog_name="kernelsmith")
def main() -> None:
    """Kernelsmith - generates optimized C code for ARM Cortex-M7."""


@main.command(name="optimize")
@click.option(
    "--operator",
    "-o",
    "operator_spec",
    type=click.Path(exists=True, path_type=pathlib.Path),
    required=True,
    help="Path to operator spec YAML (e.g., examples/operators/relu.yaml).",
)
@click.option(
    "--target",
    "-t",
    "target_spec",
    type=click.Path(exists=True, path_type=pathlib.Path),
    required=True,
    help="Path to hardware profile YAML (e.g., examples/hardware/cortex-m7.yaml).",
)
@click.option(
    "--output",
    "-O",
    "output_dir",
    type=click.Path(path_type=pathlib.Path),
    default=pathlib.Path("./generated"),
    show_default=True,
    help="Output directory for generated C code.",
)
@click.option(
    "--template",
    type=click.Path(path_type=pathlib.Path),
    default=None,
    help="Optional Jinja2 template override.",
)
def optimize_cmd(
    operator_spec: pathlib.Path,
    target_spec: pathlib.Path,
    output_dir: pathlib.Path,
    template: pathlib.Path | None,
) -> None:
    """Generate optimized C kernel for given operator and target."""
    click.echo("TODO: optimize command will:")
    click.echo(f"  - Parse operator spec: {operator_spec}")
    click.echo(f"  - Parse hardware profile: {target_spec}")
    click.echo(
        "  - Apply hardware-aware optimization passes for Cortex-M7 "
        "(DSP, FPU, M-Profile Vector Extension)"
    )
    if template:
        click.echo(f"  - Render with custom Jinja2 template: {template}")
    else:
        click.echo("  - Render with built-in Jinja2 templates")
    click.echo(f"  - Emit optimized C code to: {output_dir}")
    click.echo("  - Future: autotune tiling, loop unrolling, SIMD intrinsics")


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
    """Benchmark a kernel using arm-none-eabi-gcc and QEMU."""
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
    """Validate optimized kernel correctness against reference."""
    click.echo("TODO: validate command will:")
    click.echo(f"  - Compile both generated {generated} and reference {reference}")
    click.echo("    with arm-none-eabi-gcc")
    click.echo(f"  - Load operator spec {operator} to generate random test tensors (via numpy)")
    click.echo(
        f"  - Run both implementations under qemu-user and compare outputs "
        f"with tolerance {tolerance}"
    )
    click.echo("  - Report mismatches and numerical error statistics")


@main.command(name="list-operators")
@click.option(
    "--spec-dir",
    type=click.Path(exists=True, path_type=pathlib.Path),
    default=pathlib.Path("examples/operators"),
    show_default=True,
    help="Directory containing operator spec YAMLs.",
)
def list_operators_cmd(spec_dir: pathlib.Path) -> None:
    """List available operator specifications."""
    click.echo("TODO: list-operators command will:")
    click.echo(f"  - Scan {spec_dir} for YAML operator specs")
    click.echo("  - Parse each spec and display name, op_type, description, inputs/outputs")
    if spec_dir.exists():
        specs = list(spec_dir.glob("*.yaml")) + list(spec_dir.glob("*.yml"))
        if specs:
            click.echo(f"\nFound {len(specs)} operator spec(s):")
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
        else:
            click.echo(f"\nNo specs found in {spec_dir}, but scanning logic is ready.")
    else:
        click.echo(f"  (spec dir {spec_dir} does not exist yet)")


@main.command(name="list-targets")
@click.option(
    "--target-dir",
    type=click.Path(exists=True, path_type=pathlib.Path),
    default=pathlib.Path("examples/hardware"),
    show_default=True,
    help="Directory containing hardware profile YAMLs.",
)
def list_targets_cmd(target_dir: pathlib.Path) -> None:
    """List available hardware target profiles."""
    click.echo("TODO: list-targets command will:")
    click.echo(f"  - Scan {target_dir} for hardware profile YAMLs")
    click.echo("  - Parse each profile and display architecture, CPU, features, memory")
    if target_dir.exists():
        profiles = list(target_dir.glob("*.yaml")) + list(target_dir.glob("*.yml"))
        if profiles:
            click.echo(f"\nFound {len(profiles)} hardware profile(s):")
            for p in profiles:
                try:
                    data = yaml.safe_load(p.read_text())
                    name = data.get("name", p.stem) if isinstance(data, dict) else p.stem
                    arch = (
                        data.get("architecture", "unknown") if isinstance(data, dict) else "unknown"
                    )
                    click.echo(f"  - {name} ({arch}): {p}")
                except Exception:
                    click.echo(f"  - {p.stem}: {p} [failed to parse]")
        else:
            click.echo(f"\nNo profiles found in {target_dir}, but scanning logic is ready.")
    else:
        click.echo(f"  (target dir {target_dir} does not exist yet)")


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
    """Export benchmarking data to different formats."""
    click.echo("TODO: export-data command will:")
    click.echo(f"  - Load benchmark data from {input_path}")
    click.echo(f"  - Convert to {export_format} format with numpy/pandas processing")
    click.echo(f"  - Write filtered/aggregated dataset to {output}")
    click.echo("  - Support dataset splitting for ML-driven cost modeling")


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
    """Generate performance report from benchmark data."""
    click.echo("TODO: report command will:")
    click.echo(f"  - Load results from {input_path}")
    click.echo("  - Aggregate per-operator and per-target statistics using numpy")
    click.echo(f"  - Render {template} Jinja2 template to {output}")
    click.echo("  - Include tables, speedup vs naive, cycle counts, code size")
    click.echo("  - Future: embed flame graphs, Roofline model plots")


if __name__ == "__main__":
    main()
