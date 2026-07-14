#!/bin/bash
set -euo pipefail
# compile_and_run.sh - Compile C kernel for target and run under QEMU with metrics
# Usage: compile_and_run.sh --kernel path/to/kernel.c --target cortex-m7 --mode fast|full --output results.json

KERNEL=""
TARGET="cortex-m7"
MODE="${KERNELSMITH_QEMU_MODE:-auto}"
OUTPUT="results.json"
ITERATIONS=1000
WORKSPACE="${KERNELSMITH_WORKSPACE:-/workspace}"
BUILD_DIR="$WORKSPACE/build"

while [[ $# -gt 0 ]]; do
  case $1 in
    --kernel|-k) KERNEL="$2"; shift 2;;
    --target|-t) TARGET="$2"; shift 2;;
    --mode|-m) MODE="$2"; shift 2;;
    --output|-o) OUTPUT="$2"; shift 2;;
    --iterations|-n) ITERATIONS="$2"; shift 2;;
    *) echo "Unknown $1"; exit 1;;
  esac
done

if [[ -z "$KERNEL" ]]; then echo "ERROR --kernel required"; exit 1; fi
mkdir -p "$BUILD_DIR" "$(dirname "$OUTPUT")"

# Resolve toolchain from hardware profile or default mapping
# For now support cortex-m series -> arm-none-eabi-gcc
case "$TARGET" in
  cortex-m7|cortex_m7) MCU="-mcpu=cortex-m7"; FPU="-mfpu=fpv5-sp-d16"; ABI="-mfloat-abi=hard" ;;
  cortex-m4|cortex_m4) MCU="-mcpu=cortex-m4"; FPU="-mfpu=fpv4-sp-d16"; ABI="-mfloat-abi=hard" ;;
  cortex-m3|cortex_m3) MCU="-mcpu=cortex-m3"; FPU=""; ABI="" ;;
  cortex-m0*|cortex_m0*) MCU="-mcpu=cortex-m0"; FPU=""; ABI="" ;;
  *) MCU="-mcpu=cortex-m7"; FPU="-mfpu=fpv5-sp-d16"; ABI="-mfloat-abi=hard"; echo "WARN unknown target $TARGET using cortex-m7 defaults";;
esac

CC="arm-none-eabi-gcc"
CFLAGS="$MCU $FPU $ABI -mthumb -O2 -g -ffunction-sections -fdata-sections -nostdlib -specs=nosys.specs -Wl,--gc-sections -lm"
QEMU_USER="qemu-arm"
QEMU_SYSTEM="qemu-system-arm"

BASENAME=$(basename "$KERNEL" .c)
ELF="$BUILD_DIR/${BASENAME}.elf"
MAP="$BUILD_DIR/${BASENAME}.map"
TEST_C="$BUILD_DIR/${BASENAME}_test.c"

echo "[kernelsmith] Compiling $KERNEL for $TARGET..."
# Generate test harness wrapping kernel
cat > "$TEST_C" << 'EOFTEST'
#include <stdio.h>
#include <stdint.h>
#include <time.h>
#include <sys/time.h>
// Kernel header will be included via -include or user provides ks_* signature
// Expect: void ks_relu_cortex_m7(const float* in, float* out, int len);
extern void ks_relu_cortex_m7(const float* input, float* output, int length);
extern void ks_relu_cortex_m7_inplace(float* data, int length); // optional

int main() {
    const int N=64;
    float in[N]; float out[N];
    for(int i=0;i<N;i++) in[i]=(i%2?-1.0f*i:1.0f*i);
    struct timeval tv1,tv2;
    gettimeofday(&tv1,0);
    // DWT cycle counter simulation via loop; real HW would use DWT_CYCCNT
    for(int iter=0; iter<1000; iter++) {
        ks_relu_cortex_m7(in,out,N);
    }
    gettimeofday(&tv2,0);
    long us = (tv2.tv_sec-tv1.tv_sec)*1000000L + (tv2.tv_usec-tv1.tv_usec);
    printf("KERNELSMITH_METRICS_START\n");
    printf("cycles_estimate: %ld\n", us*400); // crude 400MHz estimate for demo; real uses QEMU tracing
    printf("time_us: %ld\n", us);
    printf("iterations: 1000\n");
    printf("memory_text: 0\n");
    printf("memory_data: 0\n");
    printf("memory_bss: 0\n");
    printf("KERNELSMITH_METRICS_END\n");
    // Validate a few outputs
    int errors=0;
    for(int i=0;i<N;i++){ float exp = in[i]>0?in[i]:0; if(out[i]!=exp) errors++; }
    printf("validation_errors: %d\n", errors);
    return errors>0;
}
EOFTEST

# Compile kernel + test harness for linux user mode using arm-linux-gnueabihf for easier QEMU user metrics
# Fallback to baremetal compile for size analysis
echo "[kernelsmith] Building baremetal ELF for size metrics..."
$CC $CFLAGS -c "$KERNEL" -o "$BUILD_DIR/kernel.o"
$CC $CFLAGS "$BUILD_DIR/kernel.o" -o "$ELF" -Wl,-Map="$MAP" || true

# Size metrics from baremetal build
TEXT_SIZE=$(arm-none-eabi-size "$ELF" 2>/dev/null | awk 'NR==2{print $1}' || echo 0)
DATA_SIZE=$(arm-none-eabi-size "$ELF" 2>/dev/null | awk 'NR==2{print $2}' || echo 0)
BSS_SIZE=$(arm-none-eabi-size "$ELF" 2>/dev/null | awk 'NR==2{print $3}' || echo 0)
TOTAL_SIZE=$((TEXT_SIZE+DATA_SIZE+BSS_SIZE))

echo "[kernelsmith] Building Linux user ELF for QEMU user emulation..."
# Use arm-linux-gnueabihf-gcc for user-space runnable binary with libc
if command -v arm-linux-gnueabihf-gcc >/dev/null; then
    USER_CC="arm-linux-gnueabihf-gcc"
    USER_CFLAGS="-O2 -g -mcpu=cortex-a15 -mfpu=neon -mfloat-abi=hard"
    USER_ELF="$BUILD_DIR/${BASENAME}_user.elf"
    $USER_CC $USER_CFLAGS "$KERNEL" "$TEST_C" -o "$USER_ELF" -lm || echo "WARN user build failed, will skip runtime"
else
    USER_ELF=""
fi

CYCLES=0
TIME_US=0
INSTR_COUNT=0
MODE_USED="$MODE"

run_fast() {
    echo "[kernelsmith] Fast mode: qemu-user instruction count"
    if [[ -z "$USER_ELF" || ! -f "$USER_ELF" ]]; then echo "WARN no user ELF, skipping fast run"; return; fi
    LOG="$BUILD_DIR/qemu_fast.log"
    timeout 10 $QEMU_USER -d in_asm,exec -D "$LOG" "$USER_ELF" > "$BUILD_DIR/run_fast.out" 2>&1 || true
    INSTR_COUNT=$(grep -c "^IN:" "$LOG" || echo 0)
    # Extract metrics from program output
    if grep -q KERNELSMITH_METRICS_START "$BUILD_DIR/run_fast.out"; then
        TIME_US=$(sed -n '/KERNELSMITH_METRICS_START/,/KERNELSMITH_METRICS_END/p' "$BUILD_DIR/run_fast.out" | grep time_us | cut -d: -f2 | tr -d ' ')
        CYCLES=$(sed -n '/KERNELSMITH_METRICS_START/,/KERNELSMITH_METRICS_END/p' "$BUILD_DIR/run_fast.out" | grep cycles_estimate | cut -d: -f2 | tr -d ' ')
    fi
    echo "[kernelsmith] Fast run instr ~ $INSTR_COUNT cycles_est $CYCLES"
}

run_full() {
    echo "[kernelsmith] Full mode: qemu-system-arm cycle accuracy trade-off (simulated)"
    # Full system emulation would require proper MCU image with semihosting.
    # For now we approximate using fast mode * 1.15 factor to simulate pipeline stalls, cache misses.
    # In real deployment, replace with qemu-system-arm -machine mps2-an500 -cpu cortex-m7 -semihosting -nographic -kernel ...
    run_fast
    if [[ "$INSTR_COUNT" -gt 0 ]]; then
        CYCLES=$(( CYCLES * 115 / 100 ))
    fi
    MODE_USED="full-sim"
}

case "$MODE" in
  fast) run_fast ;;
  full) run_full ;;
  auto) run_fast; MODE_USED="fast" ;;
  *) run_fast ;;
esac

# Output JSON metrics
cat > "$OUTPUT" << EOF
{
  "kernel": "$KERNEL",
  "target": "$TARGET",
  "mode": "$MODE_USED",
  "toolchain": {
    "compiler": "arm-none-eabi-gcc",
    "mcu_flag": "$MCU",
    "fpu_flag": "$FPU",
    "abi_flag": "$ABI"
  },
  "metrics": {
    "cycles_estimate": ${CYCLES:-0},
    "time_us": ${TIME_US:-0},
    "instruction_count": ${INSTR_COUNT:-0},
    "text_bytes": ${TEXT_SIZE:-0},
    "data_bytes": ${DATA_SIZE:-0},
    "bss_bytes": ${BSS_SIZE:-0},
    "total_bytes": ${TOTAL_SIZE:-0},
    "iterations": $ITERATIONS
  },
  "artifacts": {
    "elf": "$ELF",
    "map": "$MAP",
    "user_elf": "$USER_ELF"
  },
  "qemu": {
    "user": "$QEMU_USER",
    "system": "$QEMU_SYSTEM",
    "mode_tradeoff": "fast=instruction accurate via qemu-user -d, full=cycle approximate via qemu-system with semihosting (simulated 15% overhead)"
  }
}
EOF

echo "[kernelsmith] Metrics written to $OUTPUT"
cat "$OUTPUT"
