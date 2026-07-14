FROM python:3.10-slim

LABEL maintainer="kernelsmith"
LABEL description="kernelsmith - optimized C codegen for ARM Cortex-M with cross toolchain and QEMU emulation"
LABEL version="0.2.0"

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/root/.local/bin:/opt/toolchains/bin:${PATH}"
ENV KERNELSMITH_TARGET_DIR=/app/targets
ENV KERNELSMITH_WORKSPACE=/workspace

# Install base toolchain dependencies and QEMU with multiple ARM targets
RUN apt-get update && apt-get install -y --no-install-recommends \
    # ARM bare-metal cross toolchain (Cortex-M series)
    gcc-arm-none-eabi=15:12.2.rel1-1 \
    binutils-arm-none-eabi \
    libnewlib-arm-none-eabi \
    libstdc++-arm-none-eabi-newlib \
    gdb-multiarch \
    # ARM Linux cross toolchain for qemu-user full metrics (optional)
    gcc-arm-linux-gnueabihf \
    binutils-arm-linux-gnueabihf \
    libc6-dev-armhf-cross \
    qemu-user \
    qemu-user-static \
    qemu-system-arm \
    qemu-system-common \
    qemu-utils \
    # Build and analysis tools
    build-essential \
    make \
    cmake \
    ninja-build \
    git \
    curl \
    wget \
    python3-dev \
    python3-pip \
    strace \
    time \
    binutils \
    file \
    jq \
    bc \
    procps \
    # For metrics and tracing
    linux-perf || true \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Verify toolchains
RUN arm-none-eabi-gcc --version && \
    arm-linux-gnueabihf-gcc --version || true && \
    qemu-arm --version && \
    qemu-arm-static --version || true && \
    qemu-system-arm --version && \
    gdb-multiarch --version && \
    python3 --version

# Create toolchain registry and workspace structure
RUN mkdir -p /opt/toolchains \
    /app/targets \
    /workspace/output \
    /workspace/results \
    /workspace/build \
    /app/scripts \
    /app/templates

WORKDIR /app

# Copy dependency files first for caching
COPY requirements.txt requirements-dev.txt pyproject.toml README.md ./

# Copy source tree
COPY kernelsmith/ ./kernelsmith/
COPY examples/ ./examples/
COPY reference/ ./reference/
COPY tests/ ./tests/
COPY scripts/ ./scripts/
COPY docker/ ./docker/

# Make scripts executable
RUN chmod +x scripts/*.sh docker/*.sh 2>/dev/null || true

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir -e . && \
    pip list | grep -E "click|pyyaml|numpy|jinja|pydantic|openai"

# Create target toolchain mapping
RUN mkdir -p /app/kernelsmith/toolchain /app/kernelsmith/emulator /app/kernelsmith/metrics /app/kernelsmith/harness

# Environment variables for toolchain selection per target
ENV KERNELSMITH_DEFAULT_TARGET=cortex-m7
ENV KERNELSMITH_QEMU_MODE=auto
ENV KERNELSMITH_METRICS=full
# fast = instruction count via qemu-user -d in_asm, full = qemu-system with cycle estimate
ENV QEMU_AUDIO_DRV=none
ENV QEMU_AUDIO_NONE=1

# Health check validates toolchain and QEMU
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD arm-none-eabi-gcc --version && qemu-arm --version && kernelsmith --version || exit 1

# Default workspace volume mount point
VOLUME ["/workspace"]

# Copy entrypoint that dispatches to toolchain-aware runner
COPY docker/entrypoint.sh /usr/local/bin/kernelsmith-entrypoint
RUN chmod +x /usr/local/bin/kernelsmith-entrypoint

# Smoke test
RUN python -m pytest tests/ -v || true && \
    kernelsmith --help && \
    kernelsmith list-operators && \
    kernelsmith list-targets

ENTRYPOINT ["/usr/local/bin/kernelsmith-entrypoint"]
CMD ["--help"]
