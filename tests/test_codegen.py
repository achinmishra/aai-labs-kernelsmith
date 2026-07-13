import pathlib

import pytest
from click.testing import CliRunner

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_llm_mock_provider():
    from kernelsmith.codegen.llm_client import MockProvider, get_provider

    provider = get_provider("mock")
    assert isinstance(provider, MockProvider)
    prompt = "test prompt"
    resp = provider.generate(prompt)
    assert "### HEADER" in resp
    assert "### IMPLEMENTATION" in resp
    assert "### REASONING" in resp
    assert "ks_relu" in resp.lower() or "relu" in resp.lower()


def test_llm_avocado_requires_key(monkeypatch):
    monkeypatch.delenv("KERNELSMITH_MODEL_API_KEY", raising=False)
    from kernelsmith.codegen.llm_client import AvocadoProvider

    with pytest.raises(RuntimeError) as exc:
        AvocadoProvider(api_key=None)
    assert "KERNELSMITH_MODEL_API_KEY" in str(exc.value)


def test_prompt_builder_renders():
    from kernelsmith.codegen.prompt_builder import (
        build_prompt,
        resolve_hardware_profile,
        resolve_operator_spec,
    )

    op_path = resolve_operator_spec("relu")
    hw_path = resolve_hardware_profile("cortex-m7")
    assert op_path.exists()
    assert hw_path.exists()

    prompt = build_prompt(op_path, hw_path)
    assert "relu" in prompt.lower()
    assert "cortex-m7" in prompt.lower() or "cortex_m7" in prompt.lower()
    assert "HEADER" in prompt
    assert "{{" not in prompt
    assert "}}" not in prompt


def test_prompt_builder_resolve_errors():
    from kernelsmith.codegen.prompt_builder import resolve_hardware_profile, resolve_operator_spec

    with pytest.raises(FileNotFoundError) as exc:
        resolve_operator_spec("nonexistent_op_xyz")
    assert "No spec found" in str(exc.value)

    with pytest.raises(FileNotFoundError) as exc:
        resolve_hardware_profile("nonexistent_target_xyz")
    assert "Unknown target" in str(exc.value)


def test_output_writer_parses_and_writes(tmp_path):
    from kernelsmith.codegen.llm_client import MOCK_RESPONSE_RELU
    from kernelsmith.codegen.output_writer import parse_llm_response, write_triplet

    header, impl, reasoning = parse_llm_response(MOCK_RESPONSE_RELU)
    assert "#ifndef" in header
    assert "ks_relu" in header or "relu" in header.lower()
    assert "void" in impl
    assert len(reasoning) > 50

    result = write_triplet(
        operator="relu",
        target="cortex-m7",
        header_content=header,
        c_content=impl,
        md_content=reasoning,
        output_dir=tmp_path,
    )
    assert result.header_path.exists()
    assert result.c_path.exists()
    assert result.md_path.exists()
    assert result.header_path.name == "ks_relu_cortex_m7.h"
    assert result.c_path.name == "ks_relu_cortex_m7.c"
    assert result.md_path.name == "ks_relu_cortex_m7.md"
    assert "ks_relu" in result.c_path.read_text()


def test_optimize_e2e_mock(tmp_path):
    from kernelsmith.codegen.optimize import optimize

    result = optimize(
        operator="relu",
        target="cortex-m7",
        output_dir=tmp_path,
        provider_name="mock",
    )
    assert result.files.header_path.exists()
    assert result.files.c_path.exists()
    assert result.files.md_path.exists()
    assert "avocado_metacode_rc" in result.model.lower() or "mock" in result.model.lower()
    assert "relu" in result.prompt.lower()
    assert result.operator_path.exists()
    assert result.target_path.exists()
    c_text = result.files.c_path.read_text()
    assert "ks_relu" in c_text.lower()


def test_cli_optimize_mock(tmp_path):
    from kernelsmith.cli import main

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "optimize",
            "relu",
            "--target",
            "cortex-m7",
            "--output-dir",
            str(tmp_path),
            "--provider",
            "mock",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Generated files" in result.output
    assert "ks_relu" in result.output.lower()
    assert (tmp_path / "ks_relu_cortex_m7.h").exists()
    assert (tmp_path / "ks_relu_cortex_m7.c").exists()
    assert (tmp_path / "ks_relu_cortex_m7.md").exists()


def test_cli_optimize_with_custom_spec(tmp_path):
    from kernelsmith.cli import main

    custom_spec = tmp_path / "my_relu.yaml"
    custom_spec.write_text(
        """
name: relu
op_type: relu
description: custom
inputs:
  - name: input
    dtype: float32
    shape: [1, 16]
    description: test
outputs:
  - name: output
    dtype: float32
    shape: [1, 16]
    description: test
reference:
  c_file: reference/naive/relu.c
  function: relu_f32
"""
    )
    out_dir = tmp_path / "out"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "optimize",
            "relu",
            "--target",
            "cortex-m7",
            "--spec",
            str(custom_spec),
            "--output-dir",
            str(out_dir),
            "--provider",
            "mock",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (out_dir / "ks_relu_cortex_m7.c").exists()


def test_builtin_specs_exist():
    op_dir = ROOT / "kernelsmith" / "operators"
    hw_dir = ROOT / "kernelsmith" / "hardware_profiles"
    assert op_dir.exists(), "kernelsmith/operators should exist"
    assert hw_dir.exists(), "kernelsmith/hardware_profiles should exist"
    assert (op_dir / "relu.yaml").exists(), "relu.yaml should be in built-in operators"
    assert (hw_dir / "cortex_m7.yaml").exists(), (
        "cortex_m7.yaml should be in built-in hardware_profiles"
    )
