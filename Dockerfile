FROM python:3.10-slim

LABEL maintainer="kernelsmith"
LABEL description="kernelsmith - optimized C codegen for ARM Cortex-M7 with arm-none-eabi-gcc and QEMU"

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/root/.local/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc-arm-none-eabi \
    binutils-arm-none-eabi \
    libnewlib-arm-none-eabi \
    libstdc++-arm-none-eabi-newlib \
    qemu-user \
    qemu-system-arm \
    qemu-system-common \
    build-essential \
    git \
    curl \
    make \
    python3-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN arm-none-eabi-gcc --version && \
    qemu-arm --version && \
    qemu-system-arm --version && \
    python3 --version

WORKDIR /app

COPY requirements.txt requirements-dev.txt pyproject.toml README.md ./
COPY kernelsmith/ ./kernelsmith/
COPY examples/ ./examples/
COPY reference/ ./reference/
COPY tests/ ./tests/

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir -e . && \
    pip list

RUN python -m pytest tests/ -v || true

RUN kernelsmith --help && \
    kernelsmith list-operators && \
    kernelsmith list-targets

ENTRYPOINT ["kernelsmith"]
CMD ["--help"]
