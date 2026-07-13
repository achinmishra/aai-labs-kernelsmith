import pathlib
import subprocess
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXAMPLES_OP = ROOT / "examples" / "operators"
EXAMPLES_HW = ROOT / "examples" / "hardware"
REF_DIR = ROOT / "reference" / "naive"


def test_operator_specs_exist_and_load():
    ops = list(EXAMPLES_OP.glob("*.yaml")) + list(EXAMPLES_OP.glob("*.yml"))
    assert len(ops) >= 1, f"No operator specs found in {EXAMPLES_OP}"
    for spec_path in ops:
        data = yaml.safe_load(spec_path.read_text())
        assert isinstance(data, dict)
        assert "name" in data
        assert "op_type" in data
        assert "description" in data


def test_relu_operator_spec_content():
    relu_path = EXAMPLES_OP / "relu.yaml"
    assert relu_path.exists(), f"Expected {relu_path} to exist"
    data = yaml.safe_load(relu_path.read_text())
    assert data["name"] == "relu"
    assert data["op_type"] == "relu"
    assert "inputs" in data
    assert "outputs" in data
    assert len(data["inputs"]) >= 1
    assert len(data["outputs"]) >= 1
    assert data["reference"]["c_file"] is not None


def test_hardware_profiles_exist_and_load():
    hws = list(EXAMPLES_HW.glob("*.yaml")) + list(EXAMPLES_HW.glob("*.yml"))
    assert len(hws) >= 1, f"No hardware profiles found in {EXAMPLES_HW}"
    for hw_path in hws:
        data = yaml.safe_load(hw_path.read_text())
        assert isinstance(data, dict)
        assert "name" in data
        assert "architecture" in data
        assert "cpu" in data


def test_cortex_m7_profile_content():
    cm7_path = EXAMPLES_HW / "cortex-m7.yaml"
    assert cm7_path.exists(), f"Expected {cm7_path} to exist"
    data = yaml.safe_load(cm7_path.read_text())
    assert data["name"] == "cortex-m7"
    assert data["cpu"]["core"] == "Cortex-M7"
    assert data["fpu"]["present"] is True
    assert "toolchain" in data
    assert data["toolchain"]["compiler"] == "arm-none-eabi-gcc"
    assert "memory" in data


def test_reference_files_exist():
    assert REF_DIR.exists(), f"Reference dir {REF_DIR} missing"
    relu_c = REF_DIR / "relu.c"
    assert relu_c.exists(), f"Expected {relu_c}"
    content = relu_c.read_text()
    assert "relu_f32" in content
    assert "void" in content
    assert len(content) > 100


def test_reference_c_has_expected_functions():
    relu_c = REF_DIR / "relu.c"
    text = relu_c.read_text()
    expected_funcs = ["relu_f32", "relu_f32_inplace", "relu_i8", "relu_i16"]
    for fn in expected_funcs:
        assert fn in text, f"Function {fn} not found in relu.c"


def test_cli_importable_and_version():
    import kernelsmith

    assert hasattr(kernelsmith, "__version__")
    from kernelsmith.cli import main

    assert main is not None


def test_cli_help():
    result = subprocess.run(
        [sys.executable, "-m", "kernelsmith.cli", "--help"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0
    assert (
        "kernelsmith" in result.stdout.lower()
        or "kernelsmith" in result.stderr.lower()
        or "Usage" in result.stdout
    )


def test_cli_commands_registered():
    from click.testing import CliRunner

    from kernelsmith.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    for cmd in [
        "optimize",
        "benchmark",
        "validate",
        "list-operators",
        "list-targets",
        "export-data",
        "report",
    ]:
        assert cmd in result.output, f"Command {cmd} missing from CLI help"


def test_cli_stub_commands_print_todo():
    from click.testing import CliRunner

    from kernelsmith.cli import main

    runner = CliRunner()

    res = runner.invoke(main, ["list-operators"])
    assert res.exit_code == 0
    assert "relu" in res.output.lower() or "operator" in res.output.lower()

    res = runner.invoke(main, ["list-targets"])
    assert res.exit_code == 0
    assert "cortex-m7" in res.output.lower() or "cortex" in res.output.lower()

    for cmd in ["optimize", "benchmark", "validate", "export-data", "report"]:
        res = runner.invoke(main, [cmd, "--help"])
        assert res.exit_code == 0

    import inspect

    from kernelsmith import cli as cli_module

    src = inspect.getsource(cli_module)
    assert "TODO" in src or "benchmark" in src.lower()
