import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

import click
import yaml

# Try to import rich-based animated UI
try:
    from kernelsmith import cli_ui

    _has_cli_ui = True
except ImportError:
    _has_cli_ui = False
    cli_ui = None

from kernelsmith import __version__
from kernelsmith.codegen.optimize import optimize as optimize_fn
from kernelsmith.codegen.prompt_builder import (
    list_builtin_targets,
    resolve_hardware_profile,
)
from kernelsmith.emulator import emulate
from kernelsmith.harness import KernelsmithHarness, run_kernelsmith_pipeline
from kernelsmith.metrics import collect_metrics
from kernelsmith.toolchain import (
    compile_c_to_elf,
    resolve_toolchain,
    toolchain_available,
)

# ------------------------------------------------------------------
# Auto-Docker helpers for smooth UX (user runs `kernelsmith e2e ...` and it auto-runs in Docker if needed)
# ------------------------------------------------------------------


def _is_docker_available() -> bool:
    return shutil.which("docker") is not None


def _find_docker_image(preferred: list[str] | None = None) -> str | None:
    """Find available kernelsmith docker image, returns image name or None.

    For smooth UX and fast detection on macOS Docker Desktop, we avoid slow `docker images` calls
    by default and return first candidate if docker is available. The docker run will fail quickly
    if image not present, and we handle that with a nice message.
    """
    candidates = preferred or [
        os.getenv("KERNELSMITH_DOCKER_IMAGE", ""),
        "kernelsmith:test",
        "kernelsmith",
        "kernelsmith:latest",
    ]
    # Filter empty
    candidates = [c for c in candidates if c]
    if not candidates:
        return None

    # Fast path: if docker available, return first candidate immediately for smooth UX
    # The actual docker run will validate existence. This avoids slow `docker images -q` on macOS.
    if _is_docker_available():
        # Optionally try a quick check with longer timeout, but don't block too long
        # Try to be helpful: if user set KERNELSMITH_DOCKER_IMAGE, respect it
        explicit = os.getenv("KERNELSMITH_DOCKER_IMAGE")
        if explicit:
            return explicit
        # Return first candidate that likely exists (kernelsmith:test preferred for dev)
        return candidates[0]

    return None


def _build_docker_run_cmd(
    docker_image: str,
    workspace_host: pathlib.Path,
    inner_command: list[str],
    env_vars: list[str] | None = None,
    extra_mounts: list[tuple[pathlib.Path, str]] | None = None,
) -> list[str]:
    """
    Build docker run command that mounts workspace_host as /workspace and runs inner_command.

    inner_command is like ["e2e", "relu", "--target", "cortex-m7", ...]
    """
    env_vars = env_vars or [
        "KERNELSMITH_MODEL_API_KEY",
        "KERNELSMITH_MODEL_API_BASE",
        "KERNELSMITH_DOCKER_IMAGE",
    ]
    # Ensure workspace_host exists and is absolute
    workspace_host = workspace_host.resolve()
    # Use -t for TTY if host is TTY to enable spinner animation, plus -i for interactive
    # For smooth agentic UX, we want animation, so include -t when possible
    tty_flags = []
    try:
        if sys.stdout.isatty():
            tty_flags = ["-t"]
    except Exception:
        pass
    cmd = ["docker", "run", "--rm"] + tty_flags

    # Forward env vars if set
    for ev in env_vars:
        if os.getenv(ev):
            cmd.extend(["-e", ev])

    # Always set inside-docker marker to prevent recursion and force rich animation
    cmd.extend(["-e", "KERNELSMITH_INSIDE_DOCKER=1"])
    cmd.extend(["-e", "KERNELSMITH_FORCE_RICH=1"])

    # Mount workspace
    cmd.extend(["-v", f"{workspace_host}:/workspace"])
    cmd.extend(["-w", "/workspace"])

    # Extra mounts (for absolute paths outside workspace)
    if extra_mounts:
        for host_path, container_path in extra_mounts:
            try:
                host_path = pathlib.Path(host_path).resolve()
                if host_path.exists():
                    cmd.extend(["-v", f"{host_path}:{container_path}"])
            except Exception:
                continue

    cmd.append(docker_image)
    # Use kernelsmith entrypoint - it already handles e2e etc
    cmd.extend(inner_command)
    return cmd


def _run_in_docker_if_needed(
    command_args: list[str],
    workspace_host: pathlib.Path | None = None,
    docker_image: str | None = None,
    force_docker: bool = False,
    auto_docker: bool = True,
) -> bool:
    """
    Attempt to auto-run the given kernelsmith command inside Docker if toolchain missing or force_docker.

    Returns True if Docker was invoked (and this process should exit after), False if not.

    command_args: list like ["e2e", "relu", "--target", "cortex-m7", ...]
    workspace_host: host workspace to mount, defaults to cwd
    docker_image: optional explicit image
    force_docker: if True, always use Docker even if toolchain available
    auto_docker: if False, never auto-use Docker (respects --no-docker)
    """
    if not auto_docker:
        return False

    if not _is_docker_available():
        return False

    # If not forced and toolchain is available, don't use Docker (fast path)
    if not force_docker:
        try:
            from kernelsmith.toolchain import (
                linux_toolchain_available,
                resolve_toolchain,
                toolchain_available,
            )

            # Try to resolve for target if present in args
            target = "cortex-m7"
            if "--target" in command_args or "-t" in command_args:
                try:
                    idx = command_args.index("--target") if "--target" in command_args else command_args.index("-t")
                    target = command_args[idx + 1]
                except Exception:
                    pass
            tc = resolve_toolchain(target)
            has_baremetal = toolchain_available(tc.compiler)
            linux_toolchain_available()
            has_qemu = shutil.which("qemu-arm") or shutil.which("qemu-arm-static")
            if has_baremetal and has_qemu:
                # Toolchain available locally, no need Docker
                return False
        except Exception:
            # If any error resolving, fall through to Docker path
            pass

    # Find docker image
    if docker_image:
        image = docker_image
    else:
        image = _find_docker_image()
        if not image:
            click.secho(
                "Docker available but no kernelsmith image found (tried kernelsmith:test, kernelsmith). "
                "Build one via: docker build -t kernelsmith -f Dockerfile .",
                fg="yellow",
            )
            return False

    workspace_host = workspace_host or pathlib.Path.cwd()

    # Build docker command
    docker_cmd = _build_docker_run_cmd(
        docker_image=image,
        workspace_host=workspace_host,
        inner_command=command_args,
    )

    click.secho("", fg="cyan")
    click.secho(
        f"Toolchain not found locally, auto-running inside Docker image {image}...",
        fg="cyan",
        bold=True,
    )
    click.secho(f"  Host workspace: {workspace_host} -> /workspace in container", fg="cyan")
    click.secho(f"  Docker command: {' '.join(docker_cmd)}", fg="cyan")
    click.secho("", fg="cyan")

    try:
        # Stream output directly to terminal
        result = subprocess.run(docker_cmd)
        # Exit with same code as docker run
        sys.exit(result.returncode)
    except KeyboardInterrupt:
        click.echo("Docker run interrupted", err=True)
        sys.exit(130)
    except Exception as e:
        click.echo(f"Failed to run in Docker: {e}", err=True)
        return False

    return True


# ------------------------------------------------------------------
# Logging helpers for good failure logs and final report
# ------------------------------------------------------------------


def _format_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.1f}s"


def _log_step_start(step_num: int, total: int, name: str, detail: str = ""):
    msg = f"[{step_num}/{total}] {name}..."
    if detail:
        msg += f" {detail}"
    click.secho(msg, fg="cyan")


def _log_step_detail(msg: str):
    click.echo(f"      → {msg}")


def _log_step_success(step_num: int, name: str, duration: float, detail: str = ""):
    dur_str = _format_duration(duration)
    msg = f"      ✓ {name} in {dur_str}"
    if detail:
        msg += f" - {detail}"
    click.secho(msg, fg="green")


def _log_failure_block(
    step_label: str,
    context: str = "",
    error: str = "",
    details: str = "",
    suggestions: str = "",
    artifacts: str = "",
):
    # Try rich first
    if _has_cli_ui:
        try:
            if cli_ui.print_failure_rich(step_label, context, error, details, suggestions, artifacts):
                return
        except Exception:
            pass
    click.secho("", err=True)
    click.secho("=" * 60, fg="red", err=True)
    click.secho(f"  ✗ FAILURE at {step_label}", fg="red", bold=True, err=True)
    click.secho("=" * 60, fg="red", err=True)
    if context:
        click.secho(f"Context: {context}", err=True)
    if error:
        click.secho(f"Error: {error}", fg="red", err=True)
    if details:
        # Truncate details if too long but keep enough for debugging
        truncated = details[:5000] + ("... (truncated)" if len(details) > 5000 else "")
        click.echo(f"Details:\n{truncated}", err=True)
    if suggestions:
        click.secho(f"Suggestions:\n{suggestions}", fg="yellow", err=True)
    if artifacts:
        click.echo(f"Artifacts: {artifacts}", err=True)
    click.secho("=" * 60, fg="red", err=True)
    click.secho("", err=True)


def _print_e2e_success_report(result_dict: dict, verbose: bool = False, quiet: bool = False):
    """Nice final report printed after successful E2E run - now with rich animated UI."""
    # Try rich animated UI first (agentic coding tool style)
    if _has_cli_ui:
        try:
            cli_ui.print_final_report_rich(result_dict, verbose=verbose, quiet=quiet)
            return
        except Exception:
            pass

    op = result_dict.get("operator", "unknown")
    tgt = result_dict.get("target", "unknown")
    mode = result_dict.get("mode", "fast")
    provider = result_dict.get("llm_provider", "unknown")
    model = result_dict.get("model", "unknown")

    validation = result_dict.get("validation", {}) or {}
    metrics = result_dict.get("metrics", {}) or {}
    emulation = result_dict.get("emulation", {}) or {}
    artifacts = result_dict.get("artifacts", {}) or {}
    timing = result_dict.get("timing", {}) or {}
    steps = result_dict.get("steps", [])

    total_time = timing.get("total", 0)
    # Fallback sum if total missing
    if total_time == 0 and steps:
        total_time = sum(s.get("duration_s", 0) for s in steps)

    click.secho("", fg="green")
    click.secho("=" * 70, fg="green")
    click.secho("  Kernelsmith E2E Report ✓ PASSED", fg="green", bold=True)
    click.secho("=" * 70, fg="green")
    click.echo(f"Operator:       {op}")
    click.echo(f"Target:         {tgt}")
    click.echo(f"Mode:           {mode}")
    click.echo(f"LLM Provider:   {provider} (model={model})")
    click.echo(f"Timing Total:   {_format_duration(total_time)}")
    click.echo("")

    click.secho("Steps:", fg="cyan", bold=True)
    for i, s in enumerate(steps, 1):
        name = s.get("name", f"step{i}")
        dur = s.get("duration_s", 0)
        success = s.get("success", True)
        details = s.get("details", "")
        status_icon = "✓" if success else "✗"
        color = "green" if success else "red"
        click.secho(f"  [{status_icon}] {i}. {name} ({_format_duration(dur)})", fg=color)
        if details and verbose:
            click.echo(f"       {details}")

    click.echo("")
    click.secho("Validation:", fg="cyan", bold=True)
    compile_ok = validation.get("compile_success", validation.get("passed", False))
    # Try to get from validation dict directly if it's ValidationResult.to_dict
    if "passed" in validation:
        validation.get("passed", False)
        failed_step = validation.get("failed_step", "none")
        click.echo(f"  Compile:      {'PASS' if compile_ok else 'FAIL'}")
        # correctness from validation
        corr = validation.get("correctness_passed", validation.get("passed", False))
        click.echo(f"  Correctness:  {'PASS' if corr else 'FAIL'}")
        click.echo(f"  Failed Step:  {failed_step or 'none'}")
        # Counts if available in performance_metrics
        (result_dict.get("validation", {}).get("performance_metrics", {}) if isinstance(result_dict.get("validation"), dict) else {})
        # Actually validation dict may be nested; try to get counts from metrics
        pass_count = metrics.get("validation_pass_count") if metrics else None
        fail_count = metrics.get("validation_fail_count") if metrics else None
        if pass_count is not None or fail_count is not None:
            click.echo(f"  Cases:        {pass_count or '?'} PASS, {fail_count or 0} FAIL")
        details_snippet = validation.get("details", "")[:500]
        if details_snippet:
            click.echo("  Details Snippet:")
            for line in details_snippet.splitlines()[:10]:
                click.echo(f"    {line}")
    else:
        click.echo(f"  Passed: {validation.get('passed', 'unknown')}")

    click.echo("")
    click.secho("Metrics:", fg="cyan", bold=True)
    if metrics:
        text_b = metrics.get("text_bytes", metrics.get("text_bytes", "?"))
        total_b = metrics.get("total_bytes", "?")
        cycles = metrics.get("cycles_estimate", "?")
        time_us = metrics.get("time_us", "?")
        instr = metrics.get("instruction_count", "?")
        click.echo(f"  Size:         text={text_b} total={total_b} bytes")
        click.echo(f"  Cycles:       {cycles} est. ({mode} mode)")
        click.echo(f"  Time:         {time_us} us")
        click.echo(f"  Instr Count:  {instr}")
        if mode == "fast":
            click.echo("  Note:         Full mode ~15% higher for pipeline/cache overhead")
    else:
        click.echo("  Metrics: not available (baremetal compile skipped?)")

    click.echo("")
    click.secho("Emulation:", fg="cyan", bold=True)
    if emulation:
        click.echo(f"  Mode: {emulation.get('mode', mode)}")
        click.echo(f"  Returncode: {emulation.get('returncode', '?')}")
        stdout_snip = emulation.get("stdout", "")[:300]
        if stdout_snip:
            click.echo(f"  Output Snippet: {stdout_snip[:200]}...")
    else:
        click.echo("  Emulation: no data")

    # ------------------------------------------------------------------
    # New: Naive vs Optimized comparison (per user request)
    # ------------------------------------------------------------------
    naive_metrics = result_dict.get("naive_metrics") or {}
    optimized_metrics = result_dict.get("optimized_metrics") or {}
    comparison = result_dict.get("comparison") or {}

    if naive_metrics and optimized_metrics and comparison:
        click.echo("")
        click.secho("=" * 70, fg="magenta")
        click.secho("  Performance Comparison: Naive vs Optimized", fg="magenta", bold=True)
        click.secho("=" * 70, fg="magenta")

        # Prepare table header
        click.echo(f"{'Metric':<20} {'Naive':<15} {'Optimized':<15} {'Gain':<25}")
        click.echo("-" * 70)

        def _format_gain(comp_entry: dict | None, is_size_or_time: bool = True) -> str:
            if not comp_entry:
                return "N/A"
            comp_entry.get("naive", 0)
            comp_entry.get("optimized", 0)
            delta = comp_entry.get("delta", 0)
            delta_pct = comp_entry.get("delta_pct", 0)
            speedup = comp_entry.get("speedup", 1.0)
            improved = comp_entry.get("improved", False)

            if is_size_or_time:
                # For size/time/cycles, lower is better
                if improved:
                    return f"{delta:+.0f} ({delta_pct:+.1f}%) {speedup:.2f}x faster ✓"
                else:
                    return f"{delta:+.0f} ({delta_pct:+.1f}%) {speedup:.2f}x slower"
            else:
                return f"{delta:+.0f} ({delta_pct:+.1f}%)"

        for metric_key in [
            "text_bytes",
            "total_bytes",
            "time_us",
            "cycles_estimate",
            "instruction_count",
        ]:
            comp_entry = comparison.get(metric_key)
            if comp_entry:
                naive_val = comp_entry.get("naive", 0)
                opt_val = comp_entry.get("optimized", 0)
                gain_str = _format_gain(comp_entry, is_size_or_time=True)
                # Color based on improved
                color = "green" if comp_entry.get("improved") else "yellow" if comp_entry.get("delta") == 0 else "red"
                click.secho(f"{metric_key:<20} {naive_val:<15} {opt_val:<15} {gain_str:<25}", fg=color)
            else:
                # Fallback try direct metrics
                n_val = naive_metrics.get(metric_key, "?")
                o_val = optimized_metrics.get(metric_key, "?")
                click.echo(f"{metric_key:<20} {n_val:<15} {o_val:<15} {'N/A':<25}")

        # Summary
        summary = comparison.get("summary", {})
        if summary:
            click.echo("")
            click.secho("Summary:", fg="cyan", bold=True)
            for k, v in summary.items():
                click.echo(f"  {k}: {v}")

        # Additional detailed naive vs optimized metrics
        if verbose:
            click.echo("")
            click.secho("Detailed Naive Metrics:", fg="cyan")
            for k, v in naive_metrics.items():
                if k not in ("benchmark",):
                    click.echo(f"  naive {k}: {v}")
            click.secho("Detailed Optimized Metrics:", fg="cyan")
            for k, v in optimized_metrics.items():
                if k not in ("benchmark",):
                    click.echo(f"  optimized {k}: {v}")

        click.secho("=" * 70, fg="magenta")

    click.echo("")
    click.secho("Artifacts:", fg="cyan", bold=True)
    for k, v in artifacts.items():
        if v:
            click.echo(f"  {k}: {v}")
    generated = result_dict.get("generated", {})
    for k, v in generated.items():
        if v:
            click.echo(f"  Generated {k}: {v}")
    # Also show reference (naive) path
    ref_path = result_dict.get("reference")
    if ref_path:
        click.echo(f"  Reference (naive): {ref_path}")

    results_json = result_dict.get("results_json", "")
    if results_json:
        click.echo(f"  Results JSON: {results_json}")

    click.echo("")
    click.secho(f"Results written to: {results_json}", fg="green")
    click.secho("Status: PASSED ✓ - Ready for benchmark/compare or downstream use", fg="green", bold=True)
    click.secho("=" * 70, fg="green")
    click.secho("", fg="green")


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(version=__version__, prog_name="kernelsmith")
def main() -> None:
    """Kernelsmith - generates optimized C code for ARM Cortex-M7."""


# --- SWE-bench task generation (single CLI group per user request) ---
# Only `swe-bench` group is kept; `task` alias removed.


def _register_task_commands(group):
    """Register draft and assemble commands on a click group."""

    @group.command(name="draft")
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
        "--naive",
        "naive_path",
        type=click.Path(exists=True, path_type=pathlib.Path),
        required=True,
        help="Path to naive baseline C file.",
    )
    @click.option(
        "--gold",
        "gold_path",
        type=click.Path(exists=True, path_type=pathlib.Path),
        required=True,
        help="Path to gold optimized C file (human-verified).",
    )
    @click.option(
        "--measured-costs",
        "costs_path",
        type=click.Path(exists=True, path_type=pathlib.Path),
        default=None,
        help="JSON with naive_cost, gold_cost, cost_metric (W2 fallback).",
    )
    @click.option(
        "--kernelsmith-repo",
        "ks_repo",
        type=click.Path(exists=True, path_type=pathlib.Path),
        default=None,
        help="Path to kernelsmith repo root (for operator/HW YAML discovery).",
    )
    @click.option(
        "--approve",
        is_flag=True,
        default=False,
        help="Auto-approve human authorship gate (CI mode).",
    )
    @click.option(
        "--output-dir",
        "-o",
        "output_dir",
        type=click.Path(path_type=pathlib.Path),
        default=pathlib.Path("./staging"),
        show_default=True,
        help="Staging output directory.",
    )
    @click.option(
        "--template",
        "template_path",
        type=click.Path(exists=True, path_type=pathlib.Path),
        default=None,
        help="Custom instruction.md.j2 template override.",
    )
    def draft_cmd(
        operator: str,
        target_name: str,
        naive_path: pathlib.Path,
        gold_path: pathlib.Path,
        costs_path: pathlib.Path | None,
        ks_repo: pathlib.Path | None,
        approve: bool,
        output_dir: pathlib.Path,
        template_path: pathlib.Path | None,
    ) -> None:
        """Draft instruction.md from deterministic template + costs."""
        from kernelsmith.taskgen.draft import draft_pipeline

        rc = draft_pipeline(
            operator=operator,
            target=target_name,
            naive_path=naive_path,
            gold_path=gold_path,
            output_dir=output_dir,
            measured_costs_path=costs_path,
            kernelsmith_repo=ks_repo,
            approve=approve,
            template_path=template_path,
        )
        sys.exit(rc)

    @group.command(name="assemble")
    @click.option(
        "--staging",
        "staging_dir",
        type=click.Path(exists=True, path_type=pathlib.Path),
        required=True,
        help="Staging dir from draft (contains instruction.md + .taskgen-meta.json).",
    )
    @click.option(
        "--naive",
        "naive_path",
        type=click.Path(exists=True, path_type=pathlib.Path),
        required=True,
        help="Path to naive baseline C file.",
    )
    @click.option(
        "--gold",
        "gold_path",
        type=click.Path(exists=True, path_type=pathlib.Path),
        required=True,
        help="Path to gold optimized C file.",
    )
    @click.option(
        "--operator",
        "operator_name",
        type=str,
        required=True,
        help="Operator name (e.g., relu).",
    )
    @click.option(
        "--target",
        "target_name",
        type=str,
        required=True,
        help="Target device (e.g., cortex-m7).",
    )
    @click.option(
        "--kernelsmith-repo",
        "ks_repo",
        type=click.Path(path_type=pathlib.Path),
        default=None,
        help="Path to kernelsmith repo root.",
    )
    @click.option(
        "--task-repo",
        "task_repo",
        type=click.Path(path_type=pathlib.Path),
        required=True,
        help="Path to codimango task repo (or temp dir for dry-run).",
    )
    @click.option(
        "--task-name",
        "task_name",
        type=str,
        default=None,
        help="Task name, e.g., codimango/kernelsmith-relu-cortex-m7-v1. Auto-generated if omitted.",
    )
    @click.option(
        "--force",
        is_flag=True,
        default=False,
        help="Overwrite existing task folder.",
    )
    @click.option(
        "--push",
        is_flag=True,
        default=False,
        help="Push base commit to remote (use only for real submit).",
    )
    def assemble_cmd(
        staging_dir: pathlib.Path,
        naive_path: pathlib.Path,
        gold_path: pathlib.Path,
        operator_name: str,
        target_name: str,
        ks_repo: pathlib.Path | None,
        task_repo: pathlib.Path,
        task_name: str | None,
        force: bool,
        push: bool,
    ) -> None:
        """Assemble codimango task folder from staging + naive/gold."""
        from kernelsmith.taskgen.assemble import assemble_pipeline

        if task_name is None:
            op_norm = operator_name.replace("-", "_").lower()
            tgt_dash = target_name.lower()
            task_name = f"kernelsmith-{op_norm}-{tgt_dash}-v1"

        rc = assemble_pipeline(
            staging_dir=staging_dir,
            naive_path=naive_path,
            gold_path=gold_path,
            operator=operator_name,
            target=target_name,
            task_repo=task_repo,
            task_name=task_name,
            kernelsmith_repo=ks_repo,
            force=force,
            push=push,
        )
        sys.exit(rc)


@main.group(name="swe-bench")
def swe_bench_group() -> None:
    """SWE-bench task generation pipeline (draft + assemble)."""


_register_task_commands(swe_bench_group)


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
    """Generate optimized C kernel for OPERATOR and target."""
    operator = operator.lower()
    target_name = target_name.lower()
    effective_model = model
    if effective_model is None:
        effective_model = "avocado_metacode_rc"
    try:
        _log_step_start(
            1,
            1,
            f"Generating optimized kernel for {operator} on {target_name} via LLM (provider={llm_provider})",
        )
        start = time.time()
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
        dur = time.time() - start
        _log_step_success(1, "Codegen", dur, f"{result.files.header_path.name}, {result.files.c_path.name}")
        click.echo(f"Generated files for {operator} ({target_name}):")
        click.echo(f"  Header: {result.files.header_path}")
        click.echo(f"  Implementation: {result.files.c_path}")
        click.echo(f"  Reasoning: {result.files.md_path}")
        click.echo(f"  Model: {result.model}")
        click.echo(f"  Operator spec: {result.operator_path}")
        click.echo(f"  Hardware profile: {result.target_path}")
    except FileNotFoundError as e:
        _log_failure_block(
            "Codegen",
            context=f"operator={operator} target={target_name} provider={llm_provider}",
            error=str(e),
            details="Operator spec or hardware profile not found",
            suggestions="Run kernelsmith list-operators / list-targets to see available, or provide --spec",
        )
        raise click.Abort() from e
    except RuntimeError as e:
        err_str = str(e)
        suggestion = ""
        if "KERNELSMITH_MODEL_API_KEY" in err_str or "api_key" in err_str.lower():
            suggestion = "Set KERNELSMITH_MODEL_API_KEY env var or use --llm-provider mock for offline test"
        _log_failure_block(
            "Codegen",
            context=f"operator={operator} target={target_name} provider={llm_provider}",
            error=err_str,
            suggestions=suggestion,
        )
        raise click.Abort() from e
    except Exception as e:
        _log_failure_block("Codegen", context=f"operator={operator} target={target_name}", error=str(e))
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
    help="QEMU mode: fast=instruction accurate, full=cycle approx, auto=fast.",
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
@click.option(
    "--verbose",
    is_flag=True,
    default=False,
    help="Verbose logging (compiler commands, full output).",
)
@click.option(
    "--docker/--no-docker",
    "use_docker",
    is_flag=True,
    default=None,
    help="Auto Docker fallback for smooth UX. Default auto.",
)
@click.option("--docker-image", type=str, default=None, help="Docker image for auto fallback.")
def benchmark_cmd(
    kernel: pathlib.Path,
    target: pathlib.Path | None,
    target_name: str,
    iterations: int,
    mode: str,
    output: pathlib.Path,
    qemu: bool,
    verbose: bool,
    use_docker: bool | None,
    docker_image: str | None,
) -> None:
    """Benchmark a kernel using arm-none-eabi-gcc and QEMU with metrics."""

    # Auto-Docker smooth UX for benchmark (needs toolchain)
    should_docker = False
    if use_docker is None:
        try:
            from kernelsmith.toolchain import resolve_toolchain as _rt
            from kernelsmith.toolchain import toolchain_available as _ta

            _tc = _rt(target_name, target)
            if not _ta(_tc.compiler):
                should_docker = True
        except Exception:
            should_docker = True
    elif use_docker is True:
        should_docker = True

    if should_docker and not os.getenv("KERNELSMITH_INSIDE_DOCKER") and _is_docker_available():
        img = docker_image or _find_docker_image()
        if img:
            inner = [
                "benchmark",
                "--kernel",
                f"/workspace/{kernel}" if str(kernel).startswith("/") else f"/workspace/{kernel}",
                "--target-name",
                target_name,
                "--mode",
                mode,
                "--output",
                f"/workspace/{output}",
            ]
            if verbose:
                inner.append("--verbose")
            inner.append("--no-docker")
            host_pwd = pathlib.Path.cwd()
            docker_cmd = _build_docker_run_cmd(img, host_pwd, inner)
            click.secho(
                f"Toolchain missing locally → auto-running benchmark inside Docker {img}...",
                fg="cyan",
                bold=True,
            )
            result = subprocess.run(docker_cmd)
            sys.exit(result.returncode)
    if qemu:
        mode = "full"
    try:
        _log_step_start(1, 3, f"Resolving toolchain for {target_name}")
        start = time.time()
        hp_path = target
        if hp_path is None:
            try:
                hp_path = resolve_hardware_profile(target_name)
            except Exception:
                hp_path = None
        tc = resolve_toolchain(target_name, hp_path)
        if not toolchain_available(tc.compiler):
            _log_failure_block(
                "Toolchain Resolution",
                context=f"target={target_name} compiler={tc.compiler}",
                error=f"Toolchain {tc.compiler} not found",
                suggestions=("Use Docker: docker run --rm -v $PWD:/workspace -w /workspace kernelsmith benchmark ..."),
            )
            raise click.Abort()
        _log_step_success(1, "Toolchain Resolution", time.time() - start, f"compiler={tc.compiler}")

        _log_step_start(2, 3, f"Compiling {kernel} for {target_name}")
        start = time.time()
        build_dir = pathlib.Path("./build")
        build_dir.mkdir(exist_ok=True)
        elf_path = build_dir / f"{kernel.stem}.elf"
        flags = f"{tc.arch_flag} {tc.fpu_flag} {tc.float_abi_flag}"
        if verbose:
            _log_step_detail(f"Compiler: {tc.compiler} {flags}")
        compile_info = compile_c_to_elf(kernel, elf_path, tc, mode="speed")
        _log_step_success(
            2,
            "Compilation",
            time.time() - start,
            f"ELF {elf_path} size={compile_info['size'][:100]}",
        )

        _log_step_start(3, 3, f"Emulating under QEMU mode={mode}")
        start = time.time()
        emu = emulate(
            elf_path,
            mode=mode,
            qemu_user=tc.qemu_user,
            qemu_system=tc.qemu_system,
            machine=tc.qemu_machine,
            cpu=tc.qemu_cpu,
        )
        _log_step_success(3, "QEMU Emulation", time.time() - start, f"mode={emu.mode} returncode={emu.returncode}")
        if verbose:
            _log_step_detail(f"STDOUT: {emu.stdout[:500]}")
            _log_step_detail(f"STDERR: {emu.stderr[:500]}")

        metrics = collect_metrics(elf_path, emu, target_name)
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
            "tradeoff_note": (
                "fast=instruction accurate via qemu-user -d; "
                "full=cycle approx via qemu-system with 15pct overhead; "
                "use fast for iteration, full for realistic timing"
            ),
        }
        output.write_text(json.dumps(out_data, indent=2))
        click.echo(f"Benchmark complete. Metrics written to {output}")
        click.echo(f"  cycles_estimate: {metrics.cycles_estimate}")
        click.echo(f"  time_us: {metrics.time_us}")
        click.echo(f"  instruction_count: {metrics.instruction_count}")
        click.echo(f"  text={metrics.text_bytes} data={metrics.data_bytes} bss={metrics.bss_bytes} total={metrics.total_bytes}")
        click.echo(f"  mode: {metrics.mode}  target: {metrics.target}")
    except Exception as e:
        _log_failure_block("Benchmark", context=f"kernel={kernel} target={target_name} mode={mode}", error=str(e))
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
    default=None,
    show_default=True,
    help="Numerical tolerance for validation. Defaults to precision-based tolerance if not set.",
)
@click.option(
    "--precision",
    type=click.Choice(["fp32", "fp16", "bf16", "int8", "int16", "q15", "q31"]),
    default="fp32",
    show_default=True,
    help="Precision for tolerance selection.",
)
@click.option(
    "--target",
    "-t",
    type=str,
    default="cortex-m7",
    show_default=True,
    help="Hardware target for vector generation.",
)
@click.option(
    "--target-name",
    type=str,
    default=None,
    hidden=True,
    help="Deprecated alias for --target.",
)
@click.option(
    "--qemu",
    is_flag=True,
    default=False,
    help="Run validation inside QEMU (requires cross-toolchain and qemu-arm, use Docker image).",
)
@click.option(
    "--mode",
    type=click.Choice(["fast", "full", "auto"]),
    default="fast",
    show_default=True,
    help="QEMU mode when --qemu is set.",
)
@click.option(
    "--use-linux",
    is_flag=True,
    default=True,
    help="Use arm-linux-gnueabihf-gcc for QEMU validation (default, simpler).",
)
@click.option(
    "--output",
    type=click.Path(path_type=pathlib.Path),
    default=None,
    help="Output JSON file with validation results + metrics (for QEMU mode).",
)
@click.option(
    "--verbose",
    is_flag=True,
    default=False,
    help="Verbose logging (full compiler output, driver output).",
)
@click.option(
    "--docker/--no-docker",
    "use_docker",
    is_flag=True,
    default=None,
    help="Auto Docker fallback. Default auto when --qemu and toolchain missing.",
)
@click.option("--docker-image", type=str, default=None, help="Docker image for auto fallback.")
def validate_cmd(
    generated: pathlib.Path,
    reference: pathlib.Path,
    operator: pathlib.Path,
    tolerance: float | None,
    precision: str,
    target: str,
    target_name: str | None,
    qemu: bool,
    mode: str,
    use_linux: bool,
    output: pathlib.Path | None,
    verbose: bool,
    use_docker: bool | None,
    docker_image: str | None,
) -> None:
    """Validate optimized kernel correctness against reference."""
    effective_target = target_name or target

    # Auto-Docker for QEMU validation if toolchain missing
    if qemu:
        should_docker = False
        if use_docker is None:
            try:
                from kernelsmith.toolchain import (
                    linux_toolchain_available as _lta,
                )
                from kernelsmith.toolchain import (
                    resolve_toolchain as _rt,
                )
                from kernelsmith.toolchain import (
                    toolchain_available as _ta,
                )

                _tc = _rt(effective_target)
                if not _ta(_tc.compiler) and not _lta():
                    should_docker = True
            except Exception:
                should_docker = True
        elif use_docker is True:
            should_docker = True

        if should_docker and not os.getenv("KERNELSMITH_INSIDE_DOCKER") and _is_docker_available():
            img = docker_image or _find_docker_image()
            if img:
                # Build inner validate command
                inner = [
                    "validate",
                    "--generated",
                    f"/workspace/{generated}" if not str(generated).startswith("/workspace") else str(generated),
                    "--reference",
                    f"/workspace/{reference}" if not str(reference).startswith("/workspace") else str(reference),
                    "--operator",
                    f"/workspace/{operator}" if not str(operator).startswith("/workspace") else str(operator),
                    "--target",
                    effective_target,
                    "--precision",
                    precision,
                    "--qemu",
                    "--mode",
                    mode,
                ]
                if use_linux:
                    inner.append("--use-linux")
                if tolerance is not None:
                    inner.extend(["--tolerance", str(tolerance)])
                if output:
                    inner.extend(["--output", f"/workspace/{output}"])
                if verbose:
                    inner.append("--verbose")
                inner.append("--no-docker")
                host_pwd = pathlib.Path.cwd()
                docker_cmd = _build_docker_run_cmd(img, host_pwd, inner)
                click.secho(
                    f"Toolchain missing locally → auto-running validate --qemu inside Docker {img}...",
                    fg="cyan",
                    bold=True,
                )
                result = subprocess.run(docker_cmd)
                sys.exit(result.returncode)

    if qemu:
        # QEMU path
        try:
            from kernelsmith.toolchain import (
                linux_toolchain_available,
                resolve_toolchain,
                toolchain_available,
            )
            from kernelsmith.validation.harness import validate_from_paths_qemu

            _log_step_start(1, 3, f"Resolving toolchain for QEMU validation target={effective_target}")
            start = time.time()
            tc = resolve_toolchain(effective_target)
            has_baremetal = toolchain_available(tc.compiler)
            has_linux = linux_toolchain_available()
            if not has_baremetal and not has_linux:
                _log_failure_block(
                    "Toolchain Resolution",
                    context=f"target={effective_target} compiler={tc.compiler} linux=arm-linux-gnueabihf-gcc",
                    error="No cross-toolchain found",
                    suggestions="Use Docker image: docker run --rm -v $PWD:/workspace -w /workspace kernelsmith validate --qemu ...\n"
                    "Or install: apt-get install gcc-arm-none-eabi gcc-arm-linux-gnueabihf qemu-user",
                )
                raise click.Abort()
            _log_step_success(
                1,
                "Toolchain Resolution",
                time.time() - start,
                f"baremetal={has_baremetal} linux={has_linux} qemu={tc.qemu_user}",
            )

            _log_step_start(2, 3, f"Compiling validation suite for QEMU (mode={mode}, use_linux={use_linux})")
            start = time.time()
            result = validate_from_paths_qemu(
                generated_c=generated,
                reference_c=reference,
                operator_yaml=operator,
                tolerance=tolerance,
                precision=precision,
                target=effective_target,
                mode=mode,
                use_linux=use_linux,
                workspace=pathlib.Path("./build"),
            )
            _log_step_success(
                2,
                "QEMU Compilation & Execution",
                time.time() - start,
                f"compile={result.compile_success} correctness={result.correctness_passed}",
            )

            # Output structured result
            _log_step_start(3, 3, "Collecting results")
            click.echo(f"Operator: {result.operator}")
            click.echo(f"Generated: {result.generated_path}")
            click.echo(f"Reference: {result.reference_path}")
            click.echo(f"Compile success: {result.compile_success}")
            click.echo(f"Correctness passed: {result.correctness_passed}")
            click.echo(f"Safety passed: {result.safety_passed} (skipped for QEMU)")
            click.echo(f"Failed step: {result.failed_step or 'none'}")
            click.echo("Details:")
            click.echo(result.details[:5000] if not verbose else result.details)
            click.echo("Performance metrics (QEMU):")
            for k, v in result.performance_metrics.items():
                if verbose or k not in ("compile_info", "emulation"):
                    click.echo(f"  {k}: {str(v)[:500]}")

            if output:
                output.write_text(json.dumps(result.to_dict(), indent=2))
                click.echo(f"Results JSON written to {output}")

            if result.passed:
                click.secho(
                    f"Validation PASSED for {result.operator} under QEMU mode={mode}",
                    fg="green",
                    bold=True,
                )
            else:
                _log_failure_block(
                    "Validation Correctness (QEMU)",
                    context=f"operator={result.operator} target={effective_target} mode={mode} use_linux={use_linux}",
                    error=f"Validation FAILED at {result.failed_step}",
                    details=result.details,
                    suggestions="Inspect generated C for out-of-bounds, NaN handling, or incorrect logic.\n"
                    "Try host validation first: kernelsmith validate --generated ... --reference ... --operator ... (without --qemu)\n"
                    "Or try different LLM provider: --llm-provider mock vs avocado_free",
                    artifacts=f"Generated: {result.generated_path}, Build dir: ./build/",
                )
                raise click.Abort()

        except click.Abort:
            raise
        except Exception as e:
            _log_failure_block(
                "QEMU Validation",
                context=f"generated={generated} reference={reference} operator={operator} target={effective_target} mode={mode}",
                error=str(e),
                suggestions="Use --verbose for more details, ensure Docker image has toolchain",
            )
            raise click.Abort() from e

    else:
        # Host path (original)
        try:
            from kernelsmith.validation.harness import validate_from_paths
        except ImportError as e:
            click.echo(f"Error importing validation harness: {e}", err=True)
            raise click.Abort() from e

        _log_step_start(1, 2, f"Running host validation for {operator.stem}")
        start = time.time()
        result = validate_from_paths(
            generated_c=generated,
            reference_c=reference,
            operator_yaml=operator,
            tolerance=tolerance,
            precision=precision,
            target=effective_target,
        )
        _log_step_success(1, "Host Validation", time.time() - start)

        click.echo(f"Operator: {result.operator}")
        click.echo(f"Generated: {result.generated_path}")
        click.echo(f"Reference: {result.reference_path}")
        click.echo(f"Compile success: {result.compile_success}")
        click.echo(f"Correctness passed: {result.correctness_passed}")
        click.echo(f"Safety passed: {result.safety_passed}")
        click.echo(f"Failed step: {result.failed_step or 'none'}")
        click.echo("Details:")
        click.echo(result.details[:5000] if not verbose else result.details)
        click.echo("Performance metrics:")
        for k, v in result.performance_metrics.items():
            click.echo(f"  {k}: {v}")

        if output:
            output.write_text(json.dumps(result.to_dict(), indent=2))
            click.echo(f"Results JSON written to {output}")

        if result.passed:
            click.secho(f"Validation PASSED for {result.operator}", fg="green", bold=True)
        else:
            _log_failure_block(
                "Host Validation",
                context=f"operator={result.operator} target={effective_target}",
                error=f"Validation FAILED at {result.failed_step}",
                details=result.details,
                suggestions="Check generated C syntax, ensure reference function signature matches, try different tolerance",
            )
            raise click.Abort()


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
                    op_type = data.get("op_type", "unknown") if isinstance(data, dict) else "unknown"
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
                        op_type = data.get("op_type", "unknown") if isinstance(data, dict) else "unknown"
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
                    arch = data.get("architecture", "unknown") if isinstance(data, dict) else "unknown"
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
                        arch = data.get("architecture", "unknown") if isinstance(data, dict) else "unknown"
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
@click.option("--target", "-t", type=str, default="cortex-m7", show_default=True, help="Target device.")
@click.option("--spec", type=click.Path(exists=True, path_type=pathlib.Path), default=None)
@click.option("--output-dir", "-o", type=click.Path(path_type=pathlib.Path), default=pathlib.Path("./output"))
@click.option("--llm-provider", type=click.Choice(["mock", "avocado", "avocado_free"]), default="mock")
@click.option("--mode", type=click.Choice(["fast", "full", "auto"]), default="fast")
@click.option("--workspace", type=click.Path(path_type=pathlib.Path), default=pathlib.Path("/workspace"))
@click.option(
    "--validate",
    is_flag=True,
    default=False,
    help="Run validation suite under QEMU after codegen (full E2E).",
)
@click.option(
    "--precision",
    type=click.Choice(["fp32", "fp16", "bf16", "int8", "int16", "q15", "q31"]),
    default="fp32",
    show_default=True,
    help="Precision for validation when --validate is set.",
)
@click.option(
    "--use-linux",
    is_flag=True,
    default=True,
    help="Use arm-linux-gnueabihf-gcc for QEMU validation (default).",
)
@click.option(
    "--output-json",
    type=click.Path(path_type=pathlib.Path),
    default=None,
    help="Output JSON file for results (when --validate).",
)
@click.option(
    "--verbose",
    is_flag=True,
    default=False,
    help="Verbose logging.",
)
@click.option(
    "--docker/--no-docker",
    "use_docker",
    is_flag=True,
    default=None,
    help="Auto Docker fallback for QEMU validation. Default auto when --validate and toolchain missing.",
)
@click.option("--docker-image", type=str, default=None, help="Docker image for auto fallback.")
def pipeline_cmd(
    operator,
    target,
    spec,
    output_dir,
    llm_provider,
    mode,
    workspace,
    validate,
    precision,
    use_linux,
    output_json,
    verbose,
    use_docker,
    docker_image,
):
    """End-to-end LLM harness pipeline: optimize -> compile -> emulate -> metrics."""
    # Auto-Docker for validation path
    if validate:
        should_docker = False
        if use_docker is None:
            try:
                from kernelsmith.toolchain import (
                    linux_toolchain_available as _lta,
                )
                from kernelsmith.toolchain import (
                    resolve_toolchain as _rt,
                )
                from kernelsmith.toolchain import (
                    toolchain_available as _ta,
                )

                _tc = _rt(target)
                if not _ta(_tc.compiler) and not _lta():
                    should_docker = True
            except Exception:
                should_docker = True
        elif use_docker is True:
            should_docker = True

        if should_docker and not os.getenv("KERNELSMITH_INSIDE_DOCKER") and _is_docker_available():
            img = docker_image or _find_docker_image()
            if img:
                inner = [
                    "pipeline",
                    operator,
                    "--target",
                    target,
                    "--llm-provider",
                    llm_provider,
                    "--mode",
                    mode,
                    "--validate",
                    "--precision",
                    precision,
                    "--workspace",
                    "/workspace",
                ]
                if use_linux:
                    inner.append("--use-linux")
                if output_json:
                    inner.extend(["--output-json", f"/workspace/{output_json}"])
                if verbose:
                    inner.append("--verbose")
                inner.append("--no-docker")
                host_pwd = pathlib.Path.cwd()
                docker_cmd = _build_docker_run_cmd(img, host_pwd, inner)
                click.secho(
                    f"Toolchain missing locally → auto-running pipeline --validate inside Docker {img}...",
                    fg="cyan",
                    bold=True,
                )
                result = subprocess.run(docker_cmd)
                sys.exit(result.returncode)

    try:
        if validate:
            click.echo(f"Running kernelsmith pipeline WITH validation for {operator} on {target} mode={mode}...")
            from kernelsmith.harness import KernelsmithHarness

            h = KernelsmithHarness(target=target, mode=mode, workspace=workspace)
            result = h.e2e_pipeline(
                operator=operator,
                spec_path=spec,
                llm_provider=llm_provider,
                precision=precision,
                use_linux=use_linux,
                output_json=output_json,
            )
            result_dict = result.to_dict()
            if verbose:
                click.echo(json.dumps(result_dict, indent=2))
            _print_e2e_success_report(result_dict, verbose=verbose)
            click.echo("Pipeline WITH validation complete.")
        else:
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
        _log_failure_block(
            "Pipeline",
            context=f"operator={operator} target={target} mode={mode} provider={llm_provider} validate={validate}",
            error=str(e),
            suggestions="Try --llm-provider mock for offline, or check KERNELSMITH_MODEL_API_KEY, or use Docker image for toolchain",
        )
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


# ------------------------------------------------------------------
# New E2E command: codegen + test suite gen + QEMU execution + results
# ------------------------------------------------------------------
@main.command(name="e2e")
@click.argument("operator", type=str)
@click.option("--target", "-t", type=str, default="cortex-m7", show_default=True, help="Target device.")
@click.option(
    "--spec",
    type=click.Path(exists=True, path_type=pathlib.Path),
    default=None,
    help="Custom operator spec YAML.",
)
@click.option(
    "--output-dir",
    "-o",
    "output_dir",
    type=click.Path(path_type=pathlib.Path),
    default=pathlib.Path("./output"),
    show_default=True,
    help="Output directory for generated .h/.c/.md triplet.",
)
@click.option(
    "--llm-provider",
    type=click.Choice(["mock", "avocado", "avocado_free"]),
    default="avocado_free",
    show_default=True,
    help="LLM provider: avocado_free=real (default, final), mock=offline CI.",
)
@click.option(
    "--model",
    type=str,
    default=None,
    help="Model name, default avocado_metacode_rc.",
)
@click.option("--mode", type=click.Choice(["fast", "full", "auto"]), default="fast", show_default=True)
@click.option(
    "--workspace",
    type=click.Path(path_type=pathlib.Path),
    default=pathlib.Path("."),
    show_default=True,
    help="Workspace dir containing build/ and results/ subdirs, default current dir for host dev.",
)
@click.option(
    "--precision",
    type=click.Choice(["fp32", "fp16", "bf16", "int8", "int16", "q15", "q31"]),
    default="fp32",
    show_default=True,
    help="Precision for validation.",
)
@click.option(
    "--use-linux/--use-semihost",
    "use_linux",
    is_flag=True,
    default=True,
    show_default=True,
    help="Use arm-linux-gnueabihf-gcc (linux) vs arm-none-eabi with semihosting.",
)
@click.option(
    "--results",
    "--output-json",
    "output_json",
    type=click.Path(path_type=pathlib.Path),
    default=None,
    help="Output JSON file for combined E2E results.",
)
@click.option(
    "--verbose",
    is_flag=True,
    default=False,
    help="Verbose logging (full compiler commands, driver output).",
)
@click.option(
    "--quiet",
    is_flag=True,
    default=False,
    help="Quiet mode, only final JSON and report.",
)
@click.option(
    "--docker/--no-docker",
    "use_docker",
    is_flag=True,
    default=None,
    help="Force Docker or disable auto Docker fallback. Default auto: if toolchain missing locally, auto-run inside Docker for smooth UX.",
)
@click.option(
    "--docker-image",
    type=str,
    default=None,
    help="Docker image to use for auto fallback (default auto-detect kernelsmith:test, kernelsmith, env KERNELSMITH_DOCKER_IMAGE).",
)
def e2e_cmd(
    operator,
    target,
    spec,
    output_dir,
    llm_provider,
    model,
    mode,
    workspace,
    precision,
    use_linux,
    output_json,
    verbose,
    quiet,
    use_docker,
    docker_image,
):
    """
    End-to-end pipeline: codegen -> test suite generation -> QEMU execution -> results.

    Code gen should generate code, test suit should add tests using the code and then these
    tests should be run in QEMU and finally results should be outputed.

    Default LLM provider is avocado_free (real) for final validation, but mock can be used for CI fast path:

      \b
      Mock (CI/fast):  kernelsmith e2e relu --target cortex-m7
        --llm-provider mock --mode fast
      Real (Final):  KERNELSMITH_MODEL_API_KEY=... kernelsmith e2e relu
        --target cortex-m7 --llm-provider avocado_free --mode fast
      Docker Mock:  docker run --rm -v $PWD:/workspace -w /workspace
        kernelsmith e2e relu --target cortex-m7 --llm-provider mock --mode fast
      Docker Real:  docker run --rm -e KERNELSMITH_MODEL_API_KEY
        -v $PWD:/workspace -w /workspace kernelsmith e2e relu
        --target cortex-m7 --llm-provider avocado_free --mode fast
    """
    try:
        operator = operator.lower()
        target = target.lower()

        # ------------------------------------------------------------------
        # Auto-Docker smooth UX: if toolchain missing locally, run inside Docker
        # ------------------------------------------------------------------
        # Determine if we should auto-run in Docker for smooth UX
        # use_docker=None means auto, True means force, False means never
        should_auto_docker = False
        if use_docker is None:
            # Auto mode: check if toolchain missing
            try:
                from kernelsmith.toolchain import (
                    linux_toolchain_available as _lta,
                )
                from kernelsmith.toolchain import (
                    qemu_available as _qa,
                )
                from kernelsmith.toolchain import (
                    resolve_toolchain as _rt,
                )
                from kernelsmith.toolchain import (
                    toolchain_available as _ta,
                )

                _tc = _rt(target)
                _has_bare = _ta(_tc.compiler)
                _has_linux = _lta()
                _has_qemu = _qa(_tc.qemu_user)
                if not _has_bare and not _has_linux or not _has_qemu:
                    should_auto_docker = True
            except Exception:
                should_auto_docker = True  # If resolve fails, try Docker
        elif use_docker is True:
            should_auto_docker = True

        if should_auto_docker and not os.getenv("KERNELSMITH_INSIDE_DOCKER"):
            # Prevent recursion: set env var inside Docker
            if _is_docker_available():
                img = docker_image or _find_docker_image()
                if img:
                    # Build inner command args (same as user invoked, but force no-docker inside to avoid loop)
                    inner_cmd = [
                        "e2e",
                        operator,
                        "--target",
                        target,
                        "--llm-provider",
                        llm_provider,
                        "--mode",
                        mode,
                        "--precision",
                        precision,
                        "--workspace",
                        "/workspace",
                        "--output-dir",
                        "/workspace/output",
                    ]
                    if model:
                        inner_cmd.extend(["--model", model])
                    if not use_linux:
                        inner_cmd.append("--use-semihost")
                    if output_json:
                        # Translate output_json to container path if it's inside cwd
                        try:
                            out_path = pathlib.Path(output_json).resolve()
                            cwd = pathlib.Path.cwd().resolve()
                            if cwd in out_path.parents or out_path.parent == cwd:
                                rel = out_path.relative_to(cwd)
                                inner_cmd.extend(["--results", f"/workspace/{rel}"])
                            else:
                                inner_cmd.extend(["--results", f"/workspace/{out_path.name}"])
                        except Exception:
                            inner_cmd.extend(["--results", str(output_json)])
                    if spec:
                        try:
                            spec_path = pathlib.Path(spec).resolve()
                            cwd = pathlib.Path.cwd().resolve()
                            if cwd in spec_path.parents or spec_path.parent == cwd:
                                rel = spec_path.relative_to(cwd)
                                inner_cmd.extend(["--spec", f"/workspace/{rel}"])
                            else:
                                # Need extra mount for spec outside cwd - add as extra mount
                                inner_cmd.extend(["--spec", f"/tmp/spec/{spec_path.name}"])
                        except Exception:
                            pass
                    if verbose:
                        inner_cmd.append("--verbose")
                    if quiet:
                        inner_cmd.append("--quiet")
                    # Always add --no-docker inside to prevent recursion
                    inner_cmd.append("--no-docker")

                    workspace_host = pathlib.Path.cwd()
                    # Check for extra mounts needed (spec outside cwd)
                    extra_mounts = []
                    if spec:
                        try:
                            sp = pathlib.Path(spec).resolve()
                            cwd = pathlib.Path.cwd().resolve()
                            if cwd not in sp.parents and sp.parent != cwd:
                                extra_mounts.append((sp.parent, "/tmp/spec"))
                        except Exception:
                            pass

                    docker_cmd = _build_docker_run_cmd(
                        docker_image=img,
                        workspace_host=workspace_host,
                        inner_command=inner_cmd,
                        extra_mounts=extra_mounts if extra_mounts else None,
                    )
                    click.secho("", fg="cyan")
                    click.secho(
                        f"Toolchain not found locally → auto-running inside Docker image {img} for smooth UX...",
                        fg="cyan",
                        bold=True,
                    )
                    click.secho(f"  Host workspace: {workspace_host} -> /workspace", fg="cyan")
                    click.secho(f"  Docker command: {' '.join(docker_cmd)}", fg="cyan")
                    click.secho(
                        "  (Use --no-docker to disable auto-Docker, or --docker-image to specify image)",
                        fg="cyan",
                    )
                    click.secho("", fg="cyan")

                    # Set env to prevent recursion inside container
                    env = os.environ.copy()
                    env["KERNELSMITH_INSIDE_DOCKER"] = "1"
                    try:
                        result = subprocess.run(docker_cmd, env=env)
                        sys.exit(result.returncode)
                    except KeyboardInterrupt:
                        click.echo("Docker run interrupted", err=True)
                        sys.exit(130)
                    except Exception as e:
                        click.echo(
                            f"Failed to run in Docker: {e}, falling back to local execution",
                            err=True,
                        )
                        # Continue to local execution fallback
                else:
                    if use_docker is True:
                        # User explicitly forced Docker but no image found
                        click.secho(
                            "Docker forced via --docker but no kernelsmith image found. Build one via: docker build -t kernelsmith -f Dockerfile .",
                            fg="red",
                            err=True,
                        )
                        raise click.Abort()

        if not quiet:
            click.secho(
                f"Running Kernelsmith E2E pipeline for {operator} on {target} mode={mode} provider={llm_provider}",
                fg="cyan",
                bold=True,
            )
            click.echo(f"  Target: {target} | Mode: {mode} | Provider: {llm_provider} | Precision: {precision} | Use Linux: {use_linux}")
            click.echo("")

        # Timing overall
        total_start = time.time()

        # Use harness e2e_pipeline which already has step timing and detailed error messages
        # But we also add outer step logs for nice CLI
        from kernelsmith.harness import KernelsmithHarness
        from kernelsmith.toolchain import (
            linux_toolchain_available,
            qemu_available,
            toolchain_available,
        )

        # Pre-check toolchain with animated spinner (agentic tool style)
        tc_check_start = time.time()
        toolchain_check_ctx = None
        try:
            if _has_cli_ui and not quiet:
                toolchain_check_ctx = cli_ui.AnimatedStep(
                    "Checking toolchain and QEMU availability",
                    name="Toolchain & QEMU Check",
                    total_steps=5,
                    step_num=1,
                    quiet=quiet,
                )
                toolchain_check_ctx.__enter__()
            elif not quiet:
                _log_step_start(1, 5, "Checking toolchain and QEMU availability")

            from kernelsmith.toolchain import resolve_toolchain

            tc = resolve_toolchain(target)
            has_baremetal = toolchain_available(tc.compiler)
            has_linux = linux_toolchain_available()
            has_qemu = qemu_available(tc.qemu_user)
            if not has_baremetal and not has_linux:
                if toolchain_check_ctx:
                    toolchain_check_ctx.failure("No toolchain found")
                    toolchain_check_ctx.__exit__(None, None, None)
                _log_failure_block(
                    "Toolchain Check [1/5]",
                    context=f"target={target} compiler={tc.compiler} linux=arm-linux-gnueabihf-gcc qemu={tc.qemu_user}",
                    error="No cross-toolchain found (both baremetal and linux missing)",
                    suggestions="Install toolchain: apt-get install gcc-arm-none-eabi gcc-arm-linux-gnueabihf qemu-user\n"
                    "Or use Docker: docker run --rm -v $PWD:/workspace -w /workspace kernelsmith e2e ...",
                )
                raise click.Abort()
            if not has_qemu:
                if toolchain_check_ctx:
                    toolchain_check_ctx.failure("QEMU not found")
                    toolchain_check_ctx.__exit__(None, None, None)
                _log_failure_block(
                    "QEMU Check [1/5]",
                    context=f"qemu={tc.qemu_user}",
                    error="QEMU binary not found",
                    suggestions="Install qemu-user: apt-get install qemu-user qemu-system-arm\nOr use Docker image",
                )
                raise click.Abort()

            if toolchain_check_ctx:
                toolchain_check_ctx.success(f"baremetal={has_baremetal} linux={has_linux} qemu={has_qemu}")
                toolchain_check_ctx.__exit__(None, None, None)
            elif not quiet:
                _log_step_success(
                    1,
                    "Toolchain & QEMU Check",
                    time.time() - tc_check_start,
                    f"baremetal={has_baremetal} linux={has_linux} qemu={has_qemu}",
                )
        except click.Abort:
            if toolchain_check_ctx:
                try:
                    toolchain_check_ctx.__exit__(None, None, None)
                except Exception:
                    pass
            raise
        except Exception as e:
            if toolchain_check_ctx:
                try:
                    toolchain_check_ctx.failure(str(e)[:100])
                    toolchain_check_ctx.__exit__(None, None, None)
                except Exception:
                    pass
            if "Aborted" not in str(e):
                _log_failure_block("Toolchain Check [1/5]", error=str(e))
            raise click.Abort() from e

        # Now run actual e2e pipeline with animated summary (agentic style) - now with live progress for each sub-step
        try:
            h = KernelsmithHarness(target=target, mode=mode, workspace=workspace)
            h.output_dir = pathlib.Path(output_dir)
            h.output_dir.mkdir(parents=True, exist_ok=True)

            if not quiet:
                click.echo("")
                if _has_cli_ui:
                    cli_ui.print_e2e_header(operator, target, mode, llm_provider, precision, use_linux, quiet=quiet)
                else:
                    click.secho("Starting E2E Pipeline Steps (detailed logs from harness):", fg="cyan")
                click.echo("")

            # Use live progress for agentic tool style animation
            live_progress = None
            progress_callback = None
            if _has_cli_ui and not quiet:
                live_progress = cli_ui.E2ELiveProgress(quiet=quiet)
                live_progress.__enter__()
                progress_callback = live_progress.callback

            result = h.e2e_pipeline(
                operator=operator,
                spec_path=spec,
                llm_provider=llm_provider,
                model=model,
                precision=precision,
                use_linux=use_linux,
                output_json=output_json,
                progress_callback=progress_callback,
            )

            if live_progress:
                live_progress.__exit__(None, None, None)

            result_dict = result.to_dict()

            # Print final nice report
            if not quiet:
                _print_e2e_success_report(result_dict, verbose=verbose)
            else:
                # Quiet mode: just print JSON path and basic status
                click.echo(f"E2E PASSED for {operator} - results: {result.results_json_path}")

            # Also output JSON path for scripting
            if output_json:
                click.echo(f"Combined results JSON: {output_json}")
            else:
                click.echo(f"Combined results JSON: {result.results_json_path}")

            total_dur = time.time() - total_start
            if not quiet:
                click.secho(f"Total E2E time: {_format_duration(total_dur)}", fg="green")

        except RuntimeError as e:
            if "live_progress" in locals() and live_progress:
                try:
                    live_progress.__exit__(None, None, None)
                except Exception:
                    pass
            err_str = str(e)
            _log_failure_block(
                "E2E Pipeline",
                context=f"operator={operator} target={target} mode={mode} provider={llm_provider} precision={precision}",
                error=err_str,
                suggestions="For mock fast path: --llm-provider mock\n"
                "For real LLM final: set KERNELSMITH_MODEL_API_KEY and use --llm-provider avocado_free\n"
                "For toolchain issues: use Docker image\n"
                "Use --verbose for full compiler/QEMU output",
                artifacts=f"Check build dir: {workspace}/build and output dir: {output_dir}",
            )
            raise click.Abort() from e
        except Exception as e:
            if "live_progress" in locals() and live_progress:
                try:
                    live_progress.__exit__(None, None, None)
                except Exception:
                    pass
            _log_failure_block(
                "E2E Pipeline (Unexpected)",
                context=f"operator={operator} target={target} mode={mode} provider={llm_provider}",
                error=str(e),
                suggestions="Try --llm-provider mock, --verbose, or check logs",
            )
            raise click.Abort() from e

    except click.Abort:
        raise
    except Exception as e:
        _log_failure_block("E2E Command", error=str(e))
        raise click.Abort() from e


@main.command(name="profile")
@click.option(
    "--generated",
    "-g",
    type=click.Path(exists=True, path_type=pathlib.Path),
    required=True,
    help="Path to generated optimized kernel C file.",
)
@click.option(
    "--reference",
    "-r",
    type=click.Path(exists=True, path_type=pathlib.Path),
    required=True,
    help="Path to reference naive C file.",
)
@click.option(
    "--operator",
    "-o",
    type=click.Path(exists=True, path_type=pathlib.Path),
    required=True,
    help="Operator spec YAML.",
)
@click.option("--target", "-t", type=str, default="cortex-m7", help="Target hardware.")
@click.option("--mode", type=click.Choice(["fast", "full"]), default="fast", help="QEMU mode.")
@click.option("--output", type=click.Path(path_type=pathlib.Path), default=None, help="Output JSON for profiling bundle.")
@click.option("--verbose", is_flag=True, default=False, help="Verbose logs.")
def profile_cmd(
    generated: pathlib.Path,
    reference: pathlib.Path,
    operator: pathlib.Path,
    target: str,
    mode: str,
    output: pathlib.Path | None,
    verbose: bool,
) -> None:
    """Enhanced profiling: size + QEMU trace + objdump + bench sweep timeline."""
    try:
        click.secho(f"Profiling {generated} on target {target} mode {mode}", fg="cyan", bold=True)
        from kernelsmith.profiling.metrics_bundle import BenchSweepResult, calc_time_per_elem
        from kernelsmith.profiling.objdump import get_objdump_stats
        from kernelsmith.profiling.profiler import build_bundle_from_e2e
        from kernelsmith.profiling.trace import parse_qemu_log
        from kernelsmith.toolchain import compile_c_to_elf, compile_multi_c_to_elf, resolve_toolchain
        from kernelsmith.validation.harness import validate_from_paths_qemu
        from kernelsmith.metrics import get_size_metrics
        from kernelsmith.emulator import run_validation_elf_qemu

        # Local sweep driver template (Task1 standalone, no dependency on refine module)
        BENCH_SWEEP_TPL = """
#include <stdio.h>
#include <stdint.h>
#include <stddef.h>
#include <sys/time.h>
#include <stdlib.h>
#include <math.h>
{includes}
{func_declaration}
int main() {{
    const int n = {n};
    const int iter = {iterations};
    float *input = (float*)malloc(n * sizeof(float));
    float *output = (float*)malloc(n * sizeof(float));
    if (!input || !output) {{ printf("Allocation failed\\n"); return 1; }}
    for (int i = 0; i < n; i++) {{ input[i] = (i % 2 == 0) ? (float)i : (float)-i; output[i]=0; }}
    struct timeval tv1, tv2;
    for (int w=0; w<5; w++) {{ {kernel_call}; }}
    gettimeofday(&tv1, 0);
    for (int it=0; it<iter; it++) {{ {kernel_call}; }}
    gettimeofday(&tv2, 0);
    long us = (tv2.tv_sec - tv1.tv_sec)*1000000L + (tv2.tv_usec - tv1.tv_usec);
    float checksum=0; for(int i=0;i<n;i++) checksum+=output[i];
    printf("KERNELSMITH_METRICS_START\\n");
    printf("cycles_estimate: %ld\\n", us*400);
    printf("time_us: %ld\\n", us);
    printf("iterations: %d\\n", iter);
    printf("n: %d\\n", n);
    printf("checksum: %f\\n", checksum);
    printf("KERNELSMITH_METRICS_END\\n");
    printf("TIMELINE N=%d time_us=%ld checksum=%f\\n", n, us, checksum);
    free(input); free(output); return 0;
}}
"""

        def _gen_sweep_driver(n: int, kernel_func: str, header_name: str | None, is_naive: bool, iterations: int = 500) -> str:
            if is_naive:
                func_decl = f"extern void {kernel_func}(const float* input, float* output, size_t n);"
                includes = "#include <stddef.h>"
                kcall = f"{kernel_func}(input, output, n)"
            else:
                includes = f'#include "{header_name}"' if header_name else ""
                func_decl = "" if header_name else f"extern void {kernel_func}(const float* input, float* output, int length);"
                kcall = f"{kernel_func}(input, output, n)"
            return BENCH_SWEEP_TPL.format(
                includes=includes,
                func_declaration=func_decl,
                n=n,
                iterations=iterations,
                kernel_call=kcall,
            )

        build_dir = pathlib.Path("./build")
        build_dir.mkdir(exist_ok=True)

        val_result = validate_from_paths_qemu(
            generated_c=generated,
            reference_c=reference,
            operator_yaml=operator,
            target=target,
            mode=mode,
            use_linux=True,
            workspace=build_dir,
        )

        tc = resolve_toolchain(target)
        func_name = f"ks_{operator.stem}_{target.replace('-', '_')}"
        try:
            import re

            h_candidate = generated.with_suffix(".h")
            if h_candidate.exists():
                ht = h_candidate.read_text()
                m = re.search(r"void\s+(ks_\w+)\s*\(", ht)
                if m:
                    func_name = m.group(1)
        except Exception:
            pass

        sweep_ns = [11, 16, 64, 256, 1024]
        sweep_results = {}
        include_dirs = [generated.parent, reference.parent]
        for n in sweep_ns:
            try:
                driver_code = _gen_sweep_driver(n, func_name, generated.with_suffix(".h").name, False, iterations=500)
                driver_path = build_dir / f"profile_sweep_opt_N{n}.c"
                driver_path.write_text(driver_code)
                elf_path = build_dir / f"profile_sweep_opt_N{n}.elf"
                compile_multi_c_to_elf(
                    sources=[driver_path, generated],
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
                    keep_full_log=True,
                )
                sweep_results[n] = emu
                if verbose:
                    click.echo(f"  N={n} time={emu.time_us}us instr={emu.instruction_count} rc={emu.returncode}")
            except Exception as e:
                click.echo(f"  Sweep N={n} failed: {e}")

        try:
            opt_elf_size_path = build_dir / f"profile_opt_size.elf"
            compile_c_to_elf(generated, opt_elf_size_path, tc, mode="speed")
            opt_size = get_size_metrics(opt_elf_size_path)
            naive_elf_path = build_dir / f"profile_naive_size.elf"
            compile_c_to_elf(reference, naive_elf_path, tc, mode="speed")
            naive_size = get_size_metrics(naive_elf_path)
        except Exception:
            opt_size = naive_size = None

        opt_metrics = {}
        if sweep_results and 64 in sweep_results:
            emu64 = sweep_results[64]
            opt_metrics = {
                "text_bytes": opt_size.text if opt_size else 0,
                "total_bytes": opt_size.total if opt_size else 0,
                "time_us": emu64.time_us,
                "cycles_estimate": emu64.cycles_estimate,
                "instruction_count": emu64.instruction_count,
            }
        naive_metrics = {
            "text_bytes": naive_size.text if naive_size else 0,
            "total_bytes": naive_size.total if naive_size else 0,
            "time_us": 0,
            "cycles_estimate": 0,
            "instruction_count": 0,
        }

        comparison = {}
        for k in ["text_bytes", "total_bytes", "time_us", "cycles_estimate", "instruction_count"]:
            nv = naive_metrics.get(k, 0)
            ov = opt_metrics.get(k, 0)
            if isinstance(nv, (int, float)) and isinstance(ov, (int, float)) and nv:
                comparison[k] = {
                    "naive": nv,
                    "optimized": ov,
                    "delta": ov - nv,
                    "delta_pct": (ov - nv) / nv * 100 if nv else 0,
                    "speedup": nv / ov if ov else 1.0,
                    "improved": ov < nv,
                }

        qemu_full_log = None
        for n in [64, 16, 256, 11]:
            if n in sweep_results and sweep_results[n].qemu_full_log:
                qemu_full_log = sweep_results[n].qemu_full_log
                break

        bundle = build_bundle_from_e2e(
            operator=operator.stem,
            target=target,
            mode=mode,
            naive_metrics=naive_metrics,
            opt_metrics=opt_metrics,
            comparison=comparison,
            opt_elf=opt_elf_size_path if "opt_elf_size_path" in locals() else None,
            naive_elf=naive_elf_path if "naive_elf_path" in locals() else None,
            bench_emu_results=sweep_results,
            qemu_log_text=qemu_full_log,
            validation_pass=val_result.passed,
            validation_details=val_result.details,
            function_name=func_name,
        )

        click.echo("\n=== Profiling Bundle (Task 1) ===")
        click.echo(bundle.summary_str())

        if output:
            data = {
                "bundle": bundle.to_dict(),
                "validation": val_result.to_dict(),
            }
            output.write_text(json.dumps(data, indent=2))
            click.echo(f"\nProfiling JSON written to {output}")

    except Exception as e:
        _log_failure_block("Profile", error=str(e))
        raise click.Abort() from e


if __name__ == "__main__":
    main()
