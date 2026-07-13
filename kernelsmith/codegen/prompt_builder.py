import pathlib
from typing import Any

import yaml
from jinja2 import Template


def load_yaml(path: pathlib.Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"YAML {path} must be a dict, got {type(data)}")
    return data


def load_template(template_path: pathlib.Path | None = None) -> Template:
    if template_path is None:
        template_path = pathlib.Path(__file__).parent.parent / "templates" / "optimize_kernel.txt"
    if not template_path.exists():
        alt = pathlib.Path(__file__).parent.parent / "templates" / "generate_kernel.txt"
        if alt.exists():
            template_path = alt
        else:
            raise FileNotFoundError(f"Template not found: {template_path}")
    text = template_path.read_text()
    return Template(text)


def load_naive_code(operator_data: dict[str, Any], base_dir: pathlib.Path | None = None) -> str:
    ref = operator_data.get("reference", {})
    c_file = ref.get("c_file")
    if not c_file:
        return "// No naive reference found"
    c_path = pathlib.Path(c_file)
    if not c_path.is_absolute():
        if base_dir:
            candidates = [
                base_dir / c_path,
                base_dir / "reference" / "naive" / c_path.name
                if c_path.name
                else base_dir / c_path,
                pathlib.Path(__file__).parent.parent.parent / c_path,
                pathlib.Path.cwd() / c_path,
            ]
            for cand in candidates:
                if cand.exists():
                    c_path = cand
                    break
        else:
            repo_root = pathlib.Path(__file__).parent.parent.parent
            if (repo_root / c_path).exists():
                c_path = repo_root / c_path
    if c_path.exists():
        return c_path.read_text()
    return f"// Naive reference {c_file} not found at {c_path}"


def build_prompt(
    operator_path: pathlib.Path,
    hardware_path: pathlib.Path,
    template_path: pathlib.Path | None = None,
    extra_context: dict[str, Any] | None = None,
) -> str:
    operator_data = load_yaml(operator_path)
    hardware_data = load_yaml(hardware_path)

    repo_root = operator_path.parents[2] if len(operator_path.parents) >= 3 else pathlib.Path.cwd()
    naive_code = load_naive_code(operator_data, base_dir=repo_root)

    template = load_template(template_path)

    context = {
        "operator": operator_data,
        "target": hardware_data,
        "hardware": hardware_data,
        "naive_code": naive_code,
    }
    if extra_context:
        context.update(extra_context)

    rendered = template.render(**context)

    if "{{" in rendered or "}}" in rendered:
        raise ValueError("Template rendering left unresolved placeholders")

    return rendered


def list_builtin_operators() -> list[pathlib.Path]:
    builtin_dir = pathlib.Path(__file__).parent.parent / "operators"
    if not builtin_dir.exists():
        fallback = pathlib.Path(__file__).parent.parent.parent / "examples" / "operators"
        if fallback.exists():
            builtin_dir = fallback
    if not builtin_dir.exists():
        return []
    return sorted(list(builtin_dir.glob("*.yaml")) + list(builtin_dir.glob("*.yml")))


def list_builtin_targets() -> list[pathlib.Path]:
    builtin_dir = pathlib.Path(__file__).parent.parent / "hardware_profiles"
    if not builtin_dir.exists():
        fallback = pathlib.Path(__file__).parent.parent.parent / "examples" / "hardware"
        if fallback.exists():
            builtin_dir = fallback
    if not builtin_dir.exists():
        return []
    return sorted(list(builtin_dir.glob("*.yaml")) + list(builtin_dir.glob("*.yml")))


def resolve_operator_spec(
    operator_name: str, custom_spec: pathlib.Path | None = None
) -> pathlib.Path:
    if custom_spec:
        if not custom_spec.exists():
            raise FileNotFoundError(f"Custom spec not found: {custom_spec}")
        return custom_spec

    builtin_dir = pathlib.Path(__file__).parent.parent / "operators"
    candidates = [
        builtin_dir / f"{operator_name}.yaml",
        builtin_dir / f"{operator_name}.yml",
    ]
    for cand in candidates:
        if cand.exists():
            return cand

    fallback_dir = pathlib.Path(__file__).parent.parent.parent / "examples" / "operators"
    for cand in [fallback_dir / f"{operator_name}.yaml", fallback_dir / f"{operator_name}.yml"]:
        if cand.exists():
            return cand

    available = [p.stem for p in list_builtin_operators()]
    avail_str = ", ".join(available) or "none"
    raise FileNotFoundError(
        f"No spec found for {operator_name}. Provide --spec. Available: {avail_str}"
    )


def resolve_hardware_profile(target_name: str) -> pathlib.Path:
    builtin_dir = pathlib.Path(__file__).parent.parent / "hardware_profiles"
    candidates = [
        builtin_dir / f"{target_name}.yaml",
        builtin_dir / f"{target_name}.yml",
        builtin_dir / f"{target_name.replace('-', '_')}.yaml",
        builtin_dir / f"{target_name.replace('-', '_')}.yml",
    ]
    for cand in candidates:
        if cand.exists():
            return cand

    fallback_dir = pathlib.Path(__file__).parent.parent.parent / "examples" / "hardware"
    for cand in [
        fallback_dir / f"{target_name}.yaml",
        fallback_dir / f"{target_name}.yml",
        fallback_dir / f"{target_name.replace('-', '_')}.yaml",
    ]:
        if cand.exists():
            return cand

    available = [p.stem for p in list_builtin_targets()]
    raise FileNotFoundError(
        f"Unknown target {target_name}. Available: {', '.join(available) or 'none'}"
    )
