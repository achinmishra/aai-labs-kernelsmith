import pathlib
import re
from dataclasses import dataclass


@dataclass
class GeneratedFiles:
    header_path: pathlib.Path
    c_path: pathlib.Path
    md_path: pathlib.Path
    operator: str
    target: str


HEADER_MARKER = "### HEADER"
IMPL_MARKER = "### IMPLEMENTATION"
REASONING_MARKER = "### REASONING"

CODE_FENCE_RE = re.compile(r"```(?:c|C|cpp)?\s*\n?(.*?)\n?```", re.DOTALL)


def strip_code_fences(text: str) -> str:
    def repl(match):
        return match.group(1)

    return CODE_FENCE_RE.sub(repl, text)


def parse_llm_response(response: str) -> tuple[str, str, str]:
    text = strip_code_fences(response).strip()

    if HEADER_MARKER in text and IMPL_MARKER in text and REASONING_MARKER in text:
        try:
            header_part = text.split(HEADER_MARKER)[1].split(IMPL_MARKER)[0].strip()
            impl_part = text.split(IMPL_MARKER)[1].split(REASONING_MARKER)[0].strip()
            reasoning_part = text.split(REASONING_MARKER)[1].strip()
            return header_part, impl_part, reasoning_part
        except IndexError:
            pass

    header = ""
    impl = ""
    reasoning = ""

    if "#ifndef" in text and "#define" in text:
        lines = text.splitlines()
        start_idx = None
        for i, line in enumerate(lines):
            if "#ifndef" in line:
                start_idx = i
                break
        if start_idx is not None:
            end_idx = None
            for j in range(start_idx, len(lines)):
                if "#endif" in lines[j]:
                    end_idx = j + 1
                    break
            if end_idx:
                header = "\n".join(lines[start_idx:end_idx]).strip()
                remaining = "\n".join(lines[:start_idx] + lines[end_idx:]).strip()
                if "#include" in remaining or "void" in remaining:
                    impl = remaining[
                        : remaining.find("##") if "##" in remaining else len(remaining)
                    ].strip()
                reasoning = text

    if not header and not impl:
        if "#include" in text:
            impl = text
            header = (
                "// Header not found in response, impl extracted\n"
                "#ifndef KS_GENERIC_H\n"
                "#define KS_GENERIC_H\n"
                "void ks_generic(const void* input, void* output, int length);\n"
                "#endif"
            )
            reasoning = "# Reasoning not separated\nOriginal response did not contain markers"
        else:
            header = text[:500]
            impl = text
            reasoning = text

    return header.strip(), impl.strip(), reasoning.strip()


def sanitize_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name.lower())


def write_triplet(
    operator: str,
    target: str,
    header_content: str,
    c_content: str,
    md_content: str,
    output_dir: pathlib.Path,
) -> GeneratedFiles:
    output_dir = pathlib.Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    op_safe = sanitize_name(operator)
    tgt_safe = sanitize_name(target)

    base = f"ks_{op_safe}_{tgt_safe}"
    header_path = output_dir / f"{base}.h"
    c_path = output_dir / f"{base}.c"
    md_path = output_dir / f"{base}.md"

    if "ks_" not in header_content and op_safe not in header_content.lower():
        header_content = header_content.replace("ks_generic", base, 1)

    if base not in c_content:
        c_content = c_content.replace("ks_generic", base)

    final_md = f"""# KernelSmith Generation Report: {operator} ({target})

{md_content}

## Inputs Used
- Operator spec: operators/{operator}.yaml
- Hardware profile: hardware_profiles/{target}.yaml
- Model: avocado_metacode_rc

## Generated Files
- Header: {header_path.name}
- Implementation: {c_path.name}
- Reasoning: {md_path.name}
"""

    header_path.write_text(header_content.rstrip() + "\n")
    c_path.write_text(c_content.rstrip() + "\n")
    md_path.write_text(final_md.rstrip() + "\n")

    return GeneratedFiles(
        header_path=header_path,
        c_path=c_path,
        md_path=md_path,
        operator=operator,
        target=target,
    )
