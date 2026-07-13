import pathlib
from dataclasses import dataclass

from .llm_client import LLMProvider, get_provider
from .output_writer import GeneratedFiles, parse_llm_response, write_triplet
from .prompt_builder import build_prompt, resolve_hardware_profile, resolve_operator_spec


@dataclass
class OptimizeResult:
    files: GeneratedFiles
    prompt: str
    raw_response: str
    model: str
    operator_path: pathlib.Path
    target_path: pathlib.Path


def optimize(
    operator: str,
    target: str,
    spec_path: pathlib.Path | None = None,
    output_dir: pathlib.Path = pathlib.Path("./output"),
    llm_provider_name: str = "avocado",
    template_path: pathlib.Path | None = None,
    model: str = "avocado_metacode_rc",
    provider_name: str | None = None,
    experimental: bool = False,
    dev: bool = False,
) -> OptimizeResult:
    if provider_name is not None and llm_provider_name == "avocado":
        llm_provider_name = provider_name

    is_experimental = experimental or dev

    if model is None:
        model = "aws-claude-4-8-opus-aai" if is_experimental else "avocado_metacode_rc"
    elif is_experimental and model == "avocado_metacode_rc":
        model = "aws-claude-4-8-opus-aai"

    operator_path = resolve_operator_spec(operator, custom_spec=spec_path)
    target_path = resolve_hardware_profile(target)

    prompt = build_prompt(operator_path, target_path, template_path=template_path)

    provider: LLMProvider = get_provider(
        llm_provider_name, model=model, experimental=is_experimental
    )

    raw_response = provider.generate(prompt)

    header_content, c_content, reasoning_content = parse_llm_response(raw_response)

    files = write_triplet(
        operator=operator,
        target=target,
        header_content=header_content,
        c_content=c_content,
        md_content=reasoning_content,
        output_dir=output_dir,
    )

    actual_model = getattr(provider, "MODEL", model) if hasattr(provider, "MODEL") else model
    if llm_provider_name == "mock":
        actual_model = "avocado_metacode_rc (mock)"

    return OptimizeResult(
        files=files,
        prompt=prompt,
        raw_response=raw_response,
        model=actual_model,
        operator_path=operator_path,
        target_path=target_path,
    )
