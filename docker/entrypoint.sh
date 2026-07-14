#!/bin/bash
set -euo pipefail

# kernelsmith Docker entrypoint with toolchain awareness
# Dispatches to kernelsmith CLI, ensures workspace setup, toolchain validation

export KERNELSMITH_WORKSPACE="${KERNELSMITH_WORKSPACE:-/workspace}"
export KERNELSMITH_TARGET_DIR="${KERNELSMITH_TARGET_DIR:-/app/targets}"

mkdir -p "$KERNELSMITH_WORKSPACE/output" "$KERNELSMITH_WORKSPACE/results" "$KERNELSMITH_WORKSPACE/build"

# Toolchain validation function
validate_toolchain() {
    local target="$1"
    case "$target" in
        cortex-m7|cortex_m7|cortex-m4|cortex-m3|cortex-m0*)
            if ! command -v arm-none-eabi-gcc >/dev/null 2>&1; then
                echo "ERROR: arm-none-eabi-gcc not found for target $target" >&2
                exit 1
            fi
            if ! command -v qemu-arm >/dev/null 2>&1 && ! command -v qemu-system-arm >/dev/null 2>&1; then
                echo "ERROR: QEMU ARM emulation not found" >&2
                exit 1
            fi
            ;;
        cortex-a*|armv7-a|armv8-a)
            if ! command -v arm-linux-gnueabihf-gcc >/dev/null 2>&1; then
                echo "WARN: arm-linux-gnueabihf-gcc not found, falling back to arm-none-eabi" >&2
            fi
            ;;
        *)
            echo "WARN: Unknown target $target, using default arm-none-eabi toolchain" >&2
            ;;
    esac
}

# If first arg is a kernelsmith subcommand needing target, validate
if [[ "${1:-}" == "optimize" || "${1:-}" == "generate" || "${1:-}" == "benchmark" || "${1:-}" == "validate" ]]; then
    # Extract --target value if present
    TARGET=""
    prev=""
    for arg in "$@"; do
        if [[ "$prev" == "--target" || "$prev" == "-t" ]]; then
            TARGET="$arg"
            break
        fi
        prev="$arg"
    done
    if [[ -n "$TARGET" ]]; then
        validate_toolchain "$TARGET"
    else
        validate_toolchain "${KERNELSMITH_DEFAULT_TARGET:-cortex-m7}"
    fi
fi

# Special helper commands for docker environment
case "${1:-}" in
    toolchain-info)
        echo "=== Kernelsmith Toolchain Environment ==="
        echo "arm-none-eabi-gcc: $(arm-none-eabi-gcc --version | head -n1)"
        echo "arm-linux-gnueabihf-gcc: $(arm-linux-gnueabihf-gcc --version | head -n1 || echo 'not installed')"
        echo "qemu-arm: $(qemu-arm --version | head -n1)"
        echo "qemu-system-arm: $(qemu-system-arm --version | head -n1)"
        echo "gdb-multiarch: $(gdb-multiarch --version | head -n1)"
        echo "Workspace: $KERNELSMITH_WORKSPACE"
        echo "Target dir: $KERNELSMITH_TARGET_DIR"
        echo "Default target: ${KERNELSMITH_DEFAULT_TARGET}"
        echo "QEMU mode: ${KERNELSMITH_QEMU_MODE}"
        echo "Metrics mode: ${KERNELSMITH_METRICS}"
        exit 0
        ;;
    shell|bash)
        exec /bin/bash "${@:2}"
        ;;
    compile-test)
        # Quick compile test for generated C
        shift
        exec /app/scripts/compile_and_run.sh "$@"
        ;;
esac

# Default: pass through to kernelsmith CLI
exec kernelsmith "$@"
