import abc
import os
import time


class LLMProvider(abc.ABC):
    @abc.abstractmethod
    def generate(self, prompt: str) -> str:
        raise NotImplementedError


MOCK_RESPONSE_RELU = """### HEADER
#ifndef KS_RELU_CORTEX_M7_H
#define KS_RELU_CORTEX_M7_H

#include <stdint.h>
#include <stddef.h>

void ks_relu_cortex_m7(const float* input, float* output, int length);

#endif // KS_RELU_CORTEX_M7_H

### IMPLEMENTATION
#include "ks_relu_cortex_m7.h"

void ks_relu_cortex_m7(const float* input, float* output, int length) {
    int i = 0;
    int unrolled = length & ~3;
    for (; i < unrolled; i += 4) {
        float x0 = input[i];
        float x1 = input[i+1];
        float x2 = input[i+2];
        float x3 = input[i+3];
        output[i]   = x0 > 0.0f ? x0 : 0.0f;
        output[i+1] = x1 > 0.0f ? x1 : 0.0f;
        output[i+2] = x2 > 0.0f ? x2 : 0.0f;
        output[i+3] = x3 > 0.0f ? x3 : 0.0f;
    }
    for (; i < length; ++i) {
        float x = input[i];
        output[i] = x > 0.0f ? x : 0.0f;
    }
}

### REASONING
# KernelSmith Generation Report: relu (cortex-m7)
## Optimization Reasoning
- Used 4x loop unrolling to reduce branch overhead and enable dual-issue
  on Cortex-M7 6-stage pipeline
- Branchless conditional via ternary to avoid pipeline flush, could use VMAXNM.F32 with FPU
- In-place capable, no extra buffer allocation, respects 8KB stack budget
- Tail loop handles remaining elements when length % 4 != 0

## Hardware Considerations
- Target: ARM Cortex-M7, 512KB SRAM, 16KB ICache/DCache, fpv5-sp-d16 FPU, DSP extensions
- No dynamic allocation, Thumb2, hard float ABI
- Single-precision FPU available

## Trade-offs
- Code size increased ~40 bytes due to unrolling: acceptable within 256KB code budget
- Unrolled loop requires tail handling

## Expected Execution
- After this reasoning and optimization, expected execution:
  ~25% fewer cycles vs naive due to reduced branches and dual-issue;
  throughput improves for length >=16; stack <32 bytes (no alloc, only counters);
  correctness preserved for zeros, negatives, mixed edge cases;
  handles tail when length %4 !=0; in-place safe.
  Speedup holds when input aligned to 4 bytes and length >=4;
  fallback tail ensures correctness otherwise.

## Inputs Used
- Operator spec: operators/relu.yaml
- Hardware profile: hardware_profiles/cortex_m7.yaml
- Model: avocado_metacode_rc (mock)
"""


class MockProvider(LLMProvider):
    def __init__(self, response: str | None = None, **kwargs):
        _ = kwargs
        self._response = response or MOCK_RESPONSE_RELU

    def generate(self, prompt: str) -> str:
        _ = prompt
        return self._response


class AvocadoProvider(LLMProvider):
    MODEL = "avocado_metacode_rc"
    EXPERIMENTAL_MODEL = "avocado_metacode_rc"
    ENV_VAR = "KERNELSMITH_MODEL_API_KEY"
    ENV_VAR_EXPERIMENTAL = "LLAMA_API_KEY"
    BASE_URL = "https://api.ai.meta.com/v1"
    EXPERIMENTAL_BASE_URL = "https://api.llama.com/experimental/compat/openai/v1"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        max_retries: int = 3,
        timeout: float = 60.0,
        experimental: bool = False,
        dev: bool = False,
    ):
        self.experimental = experimental or dev

        if self.experimental:
            self.api_key = (
                api_key or os.getenv(self.ENV_VAR_EXPERIMENTAL) or os.getenv(self.ENV_VAR)
            )
            env_hint = f"{self.ENV_VAR_EXPERIMENTAL} or {self.ENV_VAR}"
            default_model = self.EXPERIMENTAL_MODEL
            default_base = self.EXPERIMENTAL_BASE_URL
        else:
            self.api_key = api_key or os.getenv(self.ENV_VAR)
            env_hint = self.ENV_VAR
            default_model = self.MODEL
            default_base = self.BASE_URL

        if not self.api_key:
            primary_env = env_hint.split(" or ")[0]
            raise RuntimeError(
                f"{env_hint} not set. "
                f"Add it to your OS environment: "
                f'export {primary_env}="your_key" (bash/zsh) or '
                f'setx {primary_env} "your_key" (Windows). '
                f"For dev, set {self.ENV_VAR_EXPERIMENTAL} or {self.ENV_VAR}. "
                f"See README.md LLM Configuration."
            )
        self.model = model or default_model
        if self.experimental and model is None:
            self.model = default_model
        self.base_url = base_url or default_base
        self.max_retries = max_retries
        self.timeout = timeout

    def generate(self, prompt: str) -> str:
        try:
            from openai import OpenAI
        except ImportError as e:
            raise RuntimeError(
                "openai package not installed. Install with: pip install openai==1.30.5"
            ) from e

        for k in [
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "http_proxy",
            "https_proxy",
            "ALL_PROXY",
            "all_proxy",
            "NO_PROXY",
            "no_proxy",
        ]:
            os.environ.pop(k, None)

        try:
            import httpx

            http_client = httpx.Client(trust_env=False, timeout=self.timeout)
            client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                http_client=http_client,
            )
        except Exception:
            client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=self.timeout)

        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                if hasattr(client, "responses"):
                    response = client.responses.create(
                        model=self.model,
                        input=prompt,
                        max_output_tokens=4096,
                        temperature=1.0,
                        top_p=1.0,
                    )
                else:
                    response = client.chat.completions.create(
                        model=self.model,
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=4096,
                        temperature=1.0,
                        top_p=1.0,
                    )
                text = self._extract_text(response)
                if text:
                    return text
                raise RuntimeError("Empty response from LLM")
            except Exception as exc:
                err_str = str(exc).lower()
                if (
                    "model_not_found" in err_str
                    or "model was not found" in err_str
                    or "404" in err_str
                ):
                    try:
                        models = client.models.list()
                        avail = [m.id for m in models.data[:20]]
                        avail_str = ", ".join(avail) if avail else "none listed"
                    except Exception:
                        avail_str = "could not list (check API key and base_url)"
                    raise RuntimeError(
                        f"Model '{self.model}' not found at {self.base_url}. "
                        f"Available (first 20): {avail_str}. "
                        f"Try --model <available> or check {self.ENV_VAR} / "
                        f"{self.ENV_VAR_EXPERIMENTAL} and base_url. "
                        f"For dev: --dev uses {self.EXPERIMENTAL_BASE_URL} "
                        f"with model {self.EXPERIMENTAL_MODEL}. "
                        f"Original error: {exc}"
                    ) from exc
                last_exc = exc
                if attempt < self.max_retries - 1:
                    time.sleep(2**attempt)
                    continue
                raise
        raise RuntimeError(f"LLM call failed after {self.max_retries} retries") from last_exc

    @staticmethod
    def _extract_text(response) -> str:
        if hasattr(response, "output_text") and response.output_text:
            return response.output_text
        if hasattr(response, "output"):
            parts = []
            for item in getattr(response, "output", []):
                if hasattr(item, "content"):
                    for c in item.content:
                        if hasattr(c, "text"):
                            parts.append(c.text)
                        elif hasattr(c, "output_text"):
                            parts.append(c.output_text)
            if parts:
                return "\n".join(parts)
        if hasattr(response, "choices") and response.choices:
            first = response.choices[0]
            if hasattr(first, "message") and hasattr(first.message, "content"):
                return first.message.content or ""
            if hasattr(first, "text"):
                return first.text or ""
        if hasattr(response, "content"):
            return response.content or ""
        return str(response)


def get_provider(name: str = "mock", **kwargs) -> LLMProvider:
    name = name.lower()
    if name == "mock":
        return MockProvider(**kwargs)
    if name in ("avocado", "avocado_metacode_rc", "metacode", "muse"):
        return AvocadoProvider(**kwargs)
    if name in ("avocado_free", "avocado-free", "free"):
        kwargs.setdefault("experimental", True)
        return AvocadoProvider(**kwargs)
    raise ValueError(f"Unknown LLM provider: {name}. Available: mock, avocado, avocado_free")
