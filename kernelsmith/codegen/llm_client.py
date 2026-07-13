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
    ENV_VAR = "KERNELSMITH_MODEL_API_KEY"
    BASE_URL = "https://api.ai.meta.com/v1"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        max_retries: int = 3,
        timeout: float = 60.0,
    ):
        self.api_key = api_key or os.getenv(self.ENV_VAR)
        if not self.api_key:
            raise RuntimeError(
                f"{self.ENV_VAR} not set. "
                f"Add it to your OS environment: "
                f'export {self.ENV_VAR}="your_key" (bash/zsh) or '
                f'setx {self.ENV_VAR} "your_key" (Windows). '
                f"See README.md LLM Configuration."
            )
        self.model = model or self.MODEL
        self.base_url = base_url or self.BASE_URL
        self.max_retries = max_retries
        self.timeout = timeout

    def generate(self, prompt: str) -> str:
        try:
            from openai import OpenAI
        except ImportError as e:
            raise RuntimeError(
                "openai package not installed. Install with: pip install openai==1.30.5"
            ) from e

        client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=self.timeout)

        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = client.responses.create(
                    model=self.model,
                    input=prompt,
                    max_output_tokens=4096,
                    temperature=1.0,
                    top_p=1.0,
                )
                text = self._extract_text(response)
                if text:
                    return text
                raise RuntimeError("Empty response from LLM")
            except Exception as exc:
                last_exc = exc
                if attempt < self.max_retries - 1:
                    time.sleep(2**attempt)
                    continue
                raise
        raise RuntimeError(f"LLM call failed after {self.max_retries} retries") from last_exc

    @staticmethod
    def _extract_text(response) -> str:
        if hasattr(response, "output_text"):
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
        return str(response)


def get_provider(name: str = "mock", **kwargs) -> LLMProvider:
    name = name.lower()
    if name == "mock":
        return MockProvider(**kwargs)
    if name in ("avocado", "avocado_metacode_rc", "metacode", "muse"):
        return AvocadoProvider(**kwargs)
    raise ValueError(f"Unknown LLM provider: {name}. Available: mock, avocado")
