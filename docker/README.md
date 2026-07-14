# Kernelsmith Docker Environment

Docker image provides cross-toolchain compilation per target and QEMU emulation for ARM Cortex-M series, with metrics collection for LLM harness integration.

## Build

```bash
./docker/build.sh
# or
docker build -t kernelsmith:latest .
# or
docker compose build
```

## Verify toolchain

```bash
docker run --rm kernelsmith:latest toolchain-info
```

Expected output includes:
- arm-none-eabi-gcc 12.2+
- arm-linux-gnueabihf-gcc
- qemu-arm, qemu-system-arm
- gdb-multiarch
- Supported targets: cortex-m7, cortex-m4, cortex-m3, cortex-m0

## Quick start

```bash
# Interactive shell with workspace mounted
docker compose --profile shell run --rm kernelsmith-shell

# Inside container:
kernelsmith toolchain-info
kernelsmith list-targets
kernelsmith list-operators

# End-to-end pipeline: LLM generate -> compile -> emulate -> metrics
kernelsmith pipeline relu --target cortex-m7 --llm-provider mock --mode fast

# Benchmark existing kernel
kernelsmith benchmark --kernel reference/naive/relu.c --target-name cortex-m7 --mode fast -o results.json
cat results.json

# Compare fast vs full modes for trade-off reasoning
kernelsmith compare-modes relu --target cortex-m7 --llm-provider mock
```

## Docker Compose usage

```bash
docker compose run --rm kernelsmith pipeline relu --target cortex-m7 --llm-provider mock --mode fast
docker compose run --rm kernelsmith benchmark --kernel reference/naive/relu.c --target-name cortex-m7 --mode full -o /workspace/results.json
docker compose run --rm kernelsmith validate --generated output/ks_relu_cortex_m7.c --reference reference/naive/relu.c --operator examples/operators/relu.yaml --target-name cortex-m7
```

Workspace volume persists at `kernelsmith-workspace` Docker volume, mounted at `/workspace` inside container. Output goes to `/workspace/output`, build artifacts to `/workspace/build`, results to `/workspace/results`.

## Toolchain per target

Hardware profiles in `examples/hardware/*.yaml` or `kernelsmith/hardware_profiles/*.yaml` define:

```yaml
toolchain:
  compiler: arm-none-eabi-gcc
  flags:
    arch: -mcpu=cortex-m7
    fpu: -mfpu=fpv5-sp-d16
    float_abi: -mfloat-abi=hard
    thumb: -mthumb
emulation:
  qemu_user: qemu-arm
  qemu_system: qemu-system-arm
  qemu_machine: mps2-an500
  qemu_cpu: cortex-m7
```

`kernelsmith.toolchain.resolve_toolchain()` reads profile and selects flags. Registry supports cortex-m7, m4, m3, m0 out of box; extend `TARGET_REGISTRY` in `kernelsmith/toolchain/__init__.py` for new targets.

## QEMU modes and metrics trade-offs

**fast mode** (default, `qemu-user`):
- Instruction-accurate via `qemu-arm -d in_asm,exec`
- Counts executed instructions, estimates cycles as time_us * freq
- Fast iteration, good for LLM inner loop, ~10x faster than full
- Use for quick feedback in harness

**full mode** (`qemu-system-arm` simulated):
- Cycle-approximate with pipeline/cache overhead model
- Currently simulates 15% overhead over fast mode to model Cortex-M7 6-stage dual-issue pipeline stalls, ICache/DCache misses
- Future: real semihosting image with DWT_CYCCNT read
- Use for realistic MCU timing reasoning

**auto mode**: selects fast.

Metrics JSON includes:
```json
{
  "metrics": {
    "cycles_estimate": 123456,
    "time_us": 308,
    "instruction_count": 8421,
    "text_bytes": 1240,
    "data_bytes": 64,
    "bss_bytes": 0,
    "total_bytes": 1304
  },
  "tradeoff_note": "..."
}
```

## LLM harness integration

Python API for Metacode harness:

```python
from kernelsmith.harness import KernelsmithHarness, run_kernelsmith_pipeline

# Simple one-shot
result = run_kernelsmith_pipeline(
    operator="relu",
    target="cortex-m7",
    mode="fast",
    llm_provider="avocado_free",
    workspace="/workspace"
)
print(result["metrics"])

# Advanced harness object
h = KernelsmithHarness(target="cortex-m7", mode="fast", workspace="/workspace")
res = h.optimize_and_measure(operator="relu", llm_provider="avocado_free")
print(res.metrics.cycles_estimate, res.metrics.total_bytes)

# Compare modes for reasoning
comparison = h.compare_modes(operator="relu", llm_provider="mock")
# returns fast vs full with ratio and tradeoff_note
```

Environment variables for LLM provider inside Docker:
- `KERNELSMITH_MODEL_API_KEY` or `LLAMA_API_KEY` for avocado_free (default working provider pointing to https://api.llama.com/experimental/compat/openai/v1 )
- Set via `docker run -e KERNELSMITH_MODEL_API_KEY=...` or docker-compose environment.

## Scripts

- `scripts/compile_and_run.sh` — standalone compile + QEMU runner used by CLI benchmark, also callable directly: `docker run --rm kernelsmith compile-test --kernel path --target cortex-m7 --mode fast --output results.json`
- `docker/entrypoint.sh` — toolchain validation dispatch, supports `toolchain-info`, `shell`, `compile-test` subcommands before passing to kernelsmith CLI.

## Extending to new targets

1. Add hardware profile YAML to `kernelsmith/hardware_profiles/` or `examples/hardware/` with toolchain and emulation sections.
2. Add entry to `TARGET_REGISTRY` in `kernelsmith/toolchain/__init__.py` mapping target name to compiler flags and QEMU machine/cpu.
3. Rebuild Docker image; toolchain-info will list new target.

## CI integration

GitHub Actions already builds Docker on PR (see `.github/workflows/ci.yml`). Local test:

```bash
docker build -t kernelsmith:ci .
docker run --rm kernelsmith:ci kernelsmith --help
docker run --rm -v $PWD:/workspace -w /workspace kernelsmith:ci pipeline relu --target cortex-m7 --llm-provider mock --mode fast
```

## Troubleshooting

- `arm-none-eabi-gcc not found`: ensure using kernelsmith Docker image, not host Python venv. Host fallback limited.
- QEMU permission: docker compose sets `seccomp:unconfined` for QEMU user emulation.
- Empty metrics: check `build/` directory for ELF; run with `--mode fast` first.
- LLM provider error: use `--llm-provider mock` for offline CI, or set `KERNELSMITH_MODEL_API_KEY` for avocado_free.
