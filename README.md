# kernelsmith

CLI tool that generates optimized C code for **ARM Cortex-M7**.

> GitHub: [`codimango/aai-labs-kernelsmith`](https://github.com/codimango/aai-labs-kernelsmith)

## Features (planned)

- Parse operator specs (YAML) and hardware profiles
- Generate optimized C kernels with Cortex-M7 awareness (FPU fpv5-sp-d16, DSP, M-Profile)
- Compile with `arm-none-eabi-gcc` and benchmark under `qemu-system-arm` / `qemu-user`
- Validate numerical correctness vs naive reference implementations
- Export benchmarking data and generate reports

## Project Structure

```
kernelsmith/
├── kernelsmith/           # pip-installable package
│   ├── __init__.py
│   └── cli.py             # Click CLI with stub commands
├── examples/
│   ├── operators/         # Operator specs (YAML)
│   │   └── relu.yaml
│   └── hardware/          # Hardware profiles (YAML)
│       └── cortex-m7.yaml
├── reference/
│   └── naive/
│       └── relu.c         # Naive reference C implementations
├── tests/
│   └── test_smoke.py      # Smoke tests: YAML load, ref files, CLI
├── .github/workflows/
│   └── ci.yml             # lint + pytest on push, Docker build on PR
├── Dockerfile             # arm-none-eabi-gcc + qemu-user + qemu-system-arm
├── pyproject.toml
├── requirements.txt       # pinned prod deps (==)
├── requirements-dev.txt   # pinned dev deps
└── .pre-commit-config.yaml
```

## Installation

Requires Python 3.10+.

```bash
pip install -e .
# or
pip install -r requirements.txt
pip install -e .
```

Dev setup:

```bash
pip install -r requirements-dev.txt
pip install -e .
pre-commit install
```

## CLI Usage

```bash
kernelsmith --help
kernelsmith list-operators
kernelsmith list-targets

kernelsmith optimize --operator examples/operators/relu.yaml --target examples/hardware/cortex-m7.yaml --output ./generated
kernelsmith benchmark --kernel generated/relu.c --target examples/hardware/cortex-m7.yaml --iterations 1000 --qemu
kernelsmith validate --generated generated/relu_opt.c --reference reference/naive/relu.c --operator examples/operators/relu.yaml
kernelsmith export-data --input results.json --format csv --output results.csv
kernelsmith report --input results.json --output report.html
```

All commands currently print TODO messages describing future behavior (initial scaffolding).

## Docker

Builds with ARM toolchain:

```bash
docker build -t kernelsmith .
docker run --rm kernelsmith --help
docker run --rm kernelsmith list-operators
docker run --rm -v $PWD:/app -w /app kernelsmith optimize --operator examples/operators/relu.yaml --target examples/hardware/cortex-m7.yaml
```

Toolchain verification:

```bash
docker run --rm kernelsmith arm-none-eabi-gcc --version
docker run --rm kernelsmith qemu-system-arm --version
docker run --rm kernelsmith qemu-arm --version
```

## CI

GitHub Actions (`.github/workflows/ci.yml`):

- **On every push** to main/master: `ruff check`, `ruff format --check`, `pytest`
- **On PR**: Docker build verification + toolchain checks inside image

Local:

```bash
ruff check .
ruff format --check .
pytest -v
```

## Dependencies

Pinned with `==`:

- prod: `click==8.1.8`, `pyyaml==6.0.2`, `numpy==1.26.4`, `jinja2==3.1.5`
- dev: `pytest==8.3.4`, `ruff==0.9.6`, `pre-commit==4.0.1`

## Examples

- Operator spec: `examples/operators/relu.yaml`
- Hardware profile: `examples/hardware/cortex-m7.yaml`
- Naive C: `reference/naive/relu.c`

## License

MIT
