#!/bin/bash
set -euo pipefail
# docker/build.sh - build kernelsmith docker image with toolchain and QEMU

IMAGE_NAME="${1:-kernelsmith:latest}"
CONTEXT="${2:-.}"

echo "[kernelsmith] Building Docker image $IMAGE_NAME from $CONTEXT"
docker build -t "$IMAGE_NAME" -f Dockerfile "$CONTEXT"

echo "[kernelsmith] Verifying toolchain inside image..."
docker run --rm "$IMAGE_NAME" toolchain-info

echo "[kernelsmith] Testing CLI..."
docker run --rm "$IMAGE_NAME" --help
docker run --rm "$IMAGE_NAME" list-targets
docker run --rm "$IMAGE_NAME" list-operators

echo "[kernelsmith] Build complete. Run examples:"
echo "  docker run --rm -v \$PWD:/workspace -w /workspace $IMAGE_NAME pipeline relu --target cortex-m7 --llm-provider mock --mode fast"
echo "  docker run --rm -v \$PWD:/workspace -w /workspace $IMAGE_NAME benchmark --kernel reference/naive/relu.c --target-name cortex-m7 --mode fast -o results.json"
