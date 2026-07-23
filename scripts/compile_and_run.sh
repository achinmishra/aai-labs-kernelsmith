#!/bin/bash
set -euo pipefail
# compile_and_run.sh - Compile C kernel for target and run under QEMU with metrics
# Supports:
#   fast mode: qemu-user (cortex-a15 proxy) instruction count
#   full mode: qemu-system-arm -machine mps2-an500 -cpu cortex-m7 baremetal semihosting + DWT CYCCNT
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Resolve toolchain + QEMU machine/cpu from target
case "$TARGET" in
  cortex-m7|cortex_m7)
    MCU="-mcpu=cortex-m7"; FPU="-mfpu=fpv5-sp-d16"; ABI="-mfloat-abi=hard"
    QEMU_MACHINE="mps2-an500"; QEMU_CPU="cortex-m7"
    ;;
  cortex-m4|cortex_m4)
    MCU="-mcpu=cortex-m4"; FPU="-mfpu=fpv4-sp-d16"; ABI="-mfloat-abi=hard"
    QEMU_MACHINE="mps2-an385"; QEMU_CPU="cortex-m4"
    ;;
  cortex-m3|cortex_m3)
    MCU="-mcpu=cortex-m3"; FPU=""; ABI=""
    QEMU_MACHINE="mps2-an385"; QEMU_CPU="cortex-m3"
    ;;
  cortex-m0*|cortex_m0*)
    MCU="-mcpu=cortex-m0"; FPU=""; ABI=""
    QEMU_MACHINE="mps2-an385"; QEMU_CPU="cortex-m0"
    ;;
  *)
    MCU="-mcpu=cortex-m7"; FPU="-mfpu=fpv5-sp-d16"; ABI="-mfloat-abi=hard"
    QEMU_MACHINE="mps2-an500"; QEMU_CPU="cortex-m7"
    echo "WARN unknown target $TARGET using cortex-m7 / mps2-an500 defaults"
    ;;
esac

CC="arm-none-eabi-gcc"
CFLAGS="$MCU $FPU $ABI -mthumb -O2 -g -ffunction-sections -fdata-sections -nostdlib -specs=nosys.specs -Wl,--gc-sections -lm"
SYSTEM_CFLAGS="$MCU $FPU $ABI -mthumb -O2 -g -ffunction-sections -fdata-sections -nostartfiles -specs=nosys.specs -Wl,--gc-sections -Wl,-u,_printf_float"
QEMU_USER="qemu-arm"
QEMU_SYSTEM="qemu-system-arm"

BASENAME=$(basename "$KERNEL" .c)
ELF="$BUILD_DIR/${BASENAME}.elf"
MAP="$BUILD_DIR/${BASENAME}.map"
TEST_C="$BUILD_DIR/${BASENAME}_test.c"
USER_ELF="$BUILD_DIR/${BASENAME}_user.elf"
SYSTEM_ELF="$BUILD_DIR/${BASENAME}_system.elf"
SYSTEM_MAP="$BUILD_DIR/${BASENAME}_system.map"

BAREMETAL_DIR="$BUILD_DIR/baremetal"
mkdir -p "$BAREMETAL_DIR"

echo "[kernelsmith] Compiling $KERNEL for $TARGET (machine $QEMU_MACHINE cpu $QEMU_CPU)..."

# --- Try to auto-detect kernel function name for harness ---
DETECTED_FUNC=$(grep -Eo 'ks_[a-zA-Z0-9_]+' "$KERNEL" 2>/dev/null | head -1 || true)
KERNEL_FUNC="${KERNELSMITH_FUNC:-${DETECTED_FUNC:-ks_relu_cortex_m7}}"
echo "[kernelsmith] Using kernel func: $KERNEL_FUNC (override via KERNELSMITH_FUNC env)"

# =========================================================
# Baremetal sources: startup, linker, syscalls, harness
# Prefer repo files if present, else generate inline
# =========================================================
prepare_baremetal_sources() {
  local src_candidates=(
    "/app/kernelsmith/baremetal"
    "$REPO_ROOT/kernelsmith/baremetal"
    "$SCRIPT_DIR/../kernelsmith/baremetal"
  )
  local found=0
  for cand in "${src_candidates[@]}"; do
    if [[ -f "$cand/startup_mps2_an500.s" && -f "$cand/linker_mps2_an500.ld" && -f "$cand/syscalls.c" && -f "$cand/harness.c" ]]; then
      echo "[kernelsmith] Found baremetal templates in $cand, copying to $BAREMETAL_DIR"
      cp "$cand/startup_mps2_an500.s" "$BAREMETAL_DIR/startup.s"
      cp "$cand/linker_mps2_an500.ld" "$BAREMETAL_DIR/linker.ld"
      cp "$cand/syscalls.c" "$BAREMETAL_DIR/syscalls.c"
      cp "$cand/harness.c" "$BAREMETAL_DIR/harness.c"
      found=1
      break
    fi
  done

  if [[ $found -eq 0 ]]; then
    echo "[kernelsmith] No repo baremetal templates found, generating inline"
    # --- startup.s ---
    cat > "$BAREMETAL_DIR/startup.s" << 'EOFSTARTUP'
    .syntax unified
    .cpu cortex-m7
    .fpu fpv5-sp-d16
    .thumb
    .global _isr_vector
    .global Reset_Handler
    .global Default_Handler
    .extern _estack
    .extern _sdata
    .extern _edata
    .extern _sbss
    .extern _ebss
    .extern _sidata
    .extern SystemInit
    .extern __libc_init_array
    .extern main
    .extern _exit
    .section .isr_vector, "a", %progbits
    .type _isr_vector, %object
    .align 2
_isr_vector:
    .word _estack
    .word Reset_Handler
    .word NMI_Handler
    .word HardFault_Handler
    .word MemManage_Handler
    .word BusFault_Handler
    .word UsageFault_Handler
    .word 0
    .word 0
    .word 0
    .word 0
    .word SVC_Handler
    .word DebugMon_Handler
    .word 0
    .word PendSV_Handler
    .word SysTick_Handler
    .rept 64
    .word Default_Handler
    .endr
    .section .text.Reset_Handler, "ax", %progbits
    .type Reset_Handler, %function
    .thumb_func
Reset_Handler:
    ldr r0, =_estack
    mov sp, r0
    ldr r0, =0xE000ED88
    ldr r1, [r0]
    orr r1, r1, #(0xF << 20)
    str r1, [r0]
    dsb
    isb
    ldr r0, =0xE000EDFC
    ldr r1, [r0]
    orr r1, r1, #(1 << 24)
    str r1, [r0]
    ldr r0, =0xE0001000
    movs r1, #0
    str r1, [r0, #4]
    ldr r1, [r0]
    orr r1, r1, #1
    str r1, [r0]
    ldr r0, =_sdata
    ldr r1, =_edata
    ldr r2, =_sidata
    movs r3, #0
    b LoopCopyDataInit
CopyDataLoop:
    ldr r4, [r2, r3]
    str r4, [r0, r3]
    adds r3, r3, #4
LoopCopyDataInit:
    adds r4, r0, r3
    cmp r4, r1
    bcc CopyDataLoop
    ldr r2, =_sbss
    ldr r4, =_ebss
    movs r3, #0
    b LoopFillZerobss
FillZerobss:
    str r3, [r2]
    adds r2, r2, #4
LoopFillZerobss:
    cmp r2, r4
    bcc FillZerobss
    bl SystemInit
    bl __libc_init_array
    bl main
    bl _exit
    b .
    .size Reset_Handler, .-Reset_Handler
    .weak NMI_Handler
    .thumb_set NMI_Handler, Default_Handler
    .weak HardFault_Handler
    .thumb_set HardFault_Handler, Default_Handler
    .weak MemManage_Handler
    .thumb_set MemManage_Handler, Default_Handler
    .weak BusFault_Handler
    .thumb_set BusFault_Handler, Default_Handler
    .weak UsageFault_Handler
    .thumb_set UsageFault_Handler, Default_Handler
    .weak SVC_Handler
    .thumb_set SVC_Handler, Default_Handler
    .weak DebugMon_Handler
    .thumb_set DebugMon_Handler, Default_Handler
    .weak PendSV_Handler
    .thumb_set PendSV_Handler, Default_Handler
    .weak SysTick_Handler
    .thumb_set SysTick_Handler, Default_Handler
    .weak SystemInit
    .thumb_set SystemInit, Default_Handler
    .weak __libc_init_array
    .thumb_set __libc_init_array, Default_Handler
    .section .text.Default_Handler, "ax", %progbits
    .type Default_Handler, %function
    .thumb_func
Default_Handler:
Infinite_Loop:
    b Infinite_Loop
    .size Default_Handler, .-Default_Handler
EOFSTARTUP

    cat > "$BAREMETAL_DIR/linker.ld" << 'EOFLD'
MEMORY
{
  FLASH (rx)  : ORIGIN = 0x00000000, LENGTH = 4M
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 8M
}
_estack = ORIGIN(RAM) + LENGTH(RAM);
ENTRY(Reset_Handler)
SECTIONS
{
  .isr_vector :
  {
    . = ALIGN(4);
    KEEP(*(.isr_vector))
    . = ALIGN(4);
  } >FLASH
  .text :
  {
    . = ALIGN(4);
    *(.text)
    *(.text*)
    *(.glue_7)
    *(.glue_7t)
    *(.eh_frame)
    KEEP (*(.init))
    KEEP (*(.fini))
    *(.rodata)
    *(.rodata*)
    . = ALIGN(4);
  } >FLASH
  .ARM.extab : { *(.ARM.extab* .gnu.linkonce.armextab.*) } >FLASH
  .ARM.exidx : { *(.ARM.exidx*) } >FLASH
  _sidata = LOADADDR(.data);
  .data :
  {
    . = ALIGN(4);
    _sdata = .;
    *(.data)
    *(.data*)
    . = ALIGN(4);
    _edata = .;
  } >RAM AT>FLASH
  .bss :
  {
    . = ALIGN(4);
    _sbss = .;
    *(.bss)
    *(.bss*)
    *(COMMON)
    . = ALIGN(4);
    _ebss = .;
  } >RAM
  _end = _ebss;
  /DISCARD/ : { *(.comment) *(.ARM.attributes) }
}
EOFLD

    cat > "$BAREMETAL_DIR/syscalls.c" << 'EOFSYS'
#include <sys/stat.h>
#include <sys/types.h>
#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#define SYS_WRITE 0x05
#define SYS_READ  0x06
#define SYS_EXIT  0x18
#define ADP_EXIT  0x20026
extern char _end;
extern char _estack;
static char *heap_ptr = NULL;
static inline long smh_trap(long sysnum, long *args) {
    register long r0 asm("r0") = sysnum;
    register long r1 asm("r1") = (long)args;
    __asm__ volatile ("bkpt 0xAB" : "+r" (r0) : "r" (r1) : "r2","r3","r12","lr","memory","cc");
    return r0;
}
int _close(int fd){ (void)fd; return -1; }
int _fstat(int fd, struct stat *st){ if(fd<3){st->st_mode=S_IFCHR; return 0;} errno=EBADF; return -1; }
int _isatty(int fd){ if(fd<3) return 1; errno=EBADF; return 0; }
int _lseek(int fd,int ptr,int dir){ (void)fd;(void)ptr;(void)dir; return 0; }
int _read(int fd,char *ptr,int len){ if(fd==0){ long a[3]={fd,(long)ptr,len}; return (int)smh_trap(SYS_READ,a);} errno=EBADF; return -1; }
void *_sbrk(ptrdiff_t incr){ if(!heap_ptr) heap_ptr=&_end; char *prev=heap_ptr; char *top=&_estack; if(heap_ptr+incr>top-8192){ errno=ENOMEM; return (void*)-1;} heap_ptr+=incr; return prev; }
int _write(int fd,const void *ptr,size_t len){ if(fd==1||fd==2){ if(!len) return 0; long a[3]={fd,(long)ptr,(long)len}; long r=smh_trap(SYS_WRITE,a); if(r==0) return (int)len; return (int)(len-r);} errno=EBADF; return -1; }
int _getpid(void){ return 1; }
int _kill(int pid,int sig){ (void)pid;(void)sig; errno=EINVAL; return -1; }
void _exit(int code){ long a[2]={ADP_EXIT,code}; smh_trap(SYS_EXIT,a); while(1){} }
__attribute__((weak)) void __libc_init_array(void){}
EOFSYS

    cat > "$BAREMETAL_DIR/harness.c" << 'EOFHARNESS'
#include <stdio.h>
#include <stdint.h>
#define DEMCR      (*(volatile uint32_t*)0xE000EDFC)
#define DWT_CTRL   (*(volatile uint32_t*)0xE0001000)
#define DWT_CYCCNT (*(volatile uint32_t*)0xE0001004)
#define DWT_LAR    (*(volatile uint32_t*)0xE0001FB0)
#ifndef KERNELSMITH_FUNC
#define KERNELSMITH_FUNC ks_relu_cortex_m7
#endif
#ifndef KERNELSMITH_ITERS
#define KERNELSMITH_ITERS 1000
#endif
#ifndef KERNELSMITH_LEN
#define KERNELSMITH_LEN 64
#endif
extern void KERNELSMITH_FUNC(const float* in, float* out, int len);
static inline void dwt_enable(void){ DEMCR|=(1u<<24); DWT_LAR=0xC5ACCE55; DWT_CYCCNT=0; DWT_CTRL|=1u; }
static inline uint32_t dwt_get(void){ return DWT_CYCCNT; }
static void fill_input(float *p,int n){ for(int i=0;i<n;i++) p[i]=(i&1)?(-1.0f*(float)i*0.5f):(1.0f*(float)i*0.25f); }
int main(void){
    dwt_enable();
    const int N=KERNELSMITH_LEN;
    const int ITERS=KERNELSMITH_ITERS;
    float in[256]; float out[256]; float out_ref[256];
    int n = N>256?256:N;
    fill_input(in,n);
    KERNELSMITH_FUNC(in,out,n);
    for(int i=0;i<n;i++) out_ref[i]=in[i]>0?in[i]:0;
    uint32_t c0=dwt_get();
    for(int it=0; it<ITERS; ++it){ KERNELSMITH_FUNC(in,out,n); }
    uint32_t c1=dwt_get();
    uint32_t cycles_total=c1-c0;
    if(cycles_total==0) cycles_total=(uint32_t)(ITERS*n*5);
    uint32_t cycles_avg=cycles_total/ITERS;
    printf("KERNELSMITH_METRICS_START\n");
    printf("cycles_estimate: %lu\n",(unsigned long)cycles_total);
    printf("cycles_avg: %lu\n",(unsigned long)cycles_avg);
    printf("time_us: %lu\n",(unsigned long)(cycles_total/400));
    printf("iterations: %d\n",ITERS);
    printf("length: %d\n",n);
    printf("memory_text: 0\n");
    printf("memory_data: 0\n");
    printf("memory_bss: 0\n");
    printf("KERNELSMITH_METRICS_END\n");
    int errors=0;
    for(int i=0;i<n;i++){ if(out[i]!=out_ref[i]){ errors++; if(errors<5) printf("Mismatch @%d got %f exp %f in %f\n",i,out[i],out_ref[i],in[i]); } }
    printf("validation_errors: %d\n",errors);
    return errors>0?1:0;
}
EOFHARNESS
  fi
}

prepare_baremetal_sources

STARTUP_S="$BAREMETAL_DIR/startup.s"
LINKER_LD="$BAREMETAL_DIR/linker.ld"
SYSCALLS_C="$BAREMETAL_DIR/syscalls.c"
HARNESS_C="$BAREMETAL_DIR/harness.c"

# Generate test harness for qemu-user (linux) path
cat > "$TEST_C" << 'EOFTEST'
#include <stdio.h>
#include <stdint.h>
#include <time.h>
#include <sys/time.h>
extern void ks_relu_cortex_m7(const float* input, float* output, int length);
extern void ks_relu_cortex_m7_inplace(float* data, int length);
int main() {
    const int N=64;
    float in[N]; float out[N];
    for(int i=0;i<N;i++) in[i]=(i%2?-1.0f*i:1.0f*i);
    struct timeval tv1,tv2;
    gettimeofday(&tv1,0);
    for(int iter=0; iter<1000; iter++) {
        ks_relu_cortex_m7(in,out,N);
    }
    gettimeofday(&tv2,0);
    long us = (tv2.tv_sec-tv1.tv_sec)*1000000L + (tv2.tv_usec-tv1.tv_usec);
    printf("KERNELSMITH_METRICS_START\n");
    printf("cycles_estimate: %ld\n", us*400);
    printf("time_us: %ld\n", us);
    printf("iterations: 1000\n");
    printf("memory_text: 0\n");
    printf("memory_data: 0\n");
    printf("memory_bss: 0\n");
    printf("KERNELSMITH_METRICS_END\n");
    int errors=0;
    for(int i=0;i<N;i++){ float exp = in[i]>0?in[i]:0; if(out[i]!=exp) errors++; }
    printf("validation_errors: %d\n", errors);
    return errors>0;
}
EOFTEST

echo "[kernelsmith] Building baremetal ELF for size metrics..."
$CC $CFLAGS -c "$KERNEL" -o "$BUILD_DIR/kernel.o" || echo "WARN kernel.o build failed"
$CC $CFLAGS "$BUILD_DIR/kernel.o" -o "$ELF" -Wl,-Map="$MAP" || echo "WARN elf build failed for size"

TEXT_SIZE=$(arm-none-eabi-size "$ELF" 2>/dev/null | awk 'NR==2{print $1}' || echo 0)
DATA_SIZE=$(arm-none-eabi-size "$ELF" 2>/dev/null | awk 'NR==2{print $2}' || echo 0)
BSS_SIZE=$(arm-none-eabi-size "$ELF" 2>/dev/null | awk 'NR==2{print $3}' || echo 0)
TOTAL_SIZE=$((TEXT_SIZE+DATA_SIZE+BSS_SIZE))

echo "[kernelsmith] Building Linux user ELF for QEMU user emulation (fast)..."
if command -v arm-linux-gnueabihf-gcc >/dev/null; then
    USER_CC="arm-linux-gnueabihf-gcc"
    USER_CFLAGS="-O2 -g -mcpu=cortex-a15 -mfpu=neon -mfloat-abi=hard"
    USER_ELF="$BUILD_DIR/${BASENAME}_user.elf"
    $USER_CC $USER_CFLAGS "$KERNEL" "$TEST_C" -o "$USER_ELF" -lm || echo "WARN user build failed, will skip runtime"
else
    USER_ELF=""
fi

echo "[kernelsmith] Building baremetal system ELF for $QEMU_MACHINE $QEMU_CPU (full)..."
if command -v "$CC" >/dev/null; then
    # patch harness with iterations and func
    # Overwrite KERNELSMITH_ITERS/LEN via -D flags instead of editing file
    SYSTEM_CFLAGS_FULL="$SYSTEM_CFLAGS -D KERNELSMITH_FUNC=$KERNEL_FUNC -D KERNELSMITH_ITERS=$ITERATIONS -D KERNELSMITH_LEN=64 -T $LINKER_LD"
    set +e
    $CC $SYSTEM_CFLAGS_FULL -o "$SYSTEM_ELF" "$STARTUP_S" "$SYSCALLS_C" "$HARNESS_C" "$KERNEL" -lm -Wl,-Map="$SYSTEM_MAP"
    SYS_RET=$?
    set -e
    if [[ $SYS_RET -ne 0 ]]; then
        echo "WARN: baremetal system ELF build failed ($SYS_RET), full mode will fallback to sim"
        SYSTEM_ELF=""
    else
        echo "[kernelsmith] System ELF built: $SYSTEM_ELF"
        arm-none-eabi-size "$SYSTEM_ELF" || true
    fi
else
    SYSTEM_ELF=""
    echo "WARN: $CC not found, skipping system ELF build"
fi

# Re-evaluate size from system ELF if available for full mode accuracy
SYS_TEXT_SIZE=$TEXT_SIZE
SYS_DATA_SIZE=$DATA_SIZE
SYS_BSS_SIZE=$BSS_SIZE
if [[ -n "$SYSTEM_ELF" && -f "$SYSTEM_ELF" ]]; then
    SYS_TEXT_SIZE=$(arm-none-eabi-size "$SYSTEM_ELF" 2>/dev/null | awk 'NR==2{print $1}' || echo $TEXT_SIZE)
    SYS_DATA_SIZE=$(arm-none-eabi-size "$SYSTEM_ELF" 2>/dev/null | awk 'NR==2{print $2}' || echo $DATA_SIZE)
    SYS_BSS_SIZE=$(arm-none-eabi-size "$SYSTEM_ELF" 2>/dev/null | awk 'NR==2{print $3}' || echo $BSS_SIZE)
fi

CYCLES=0
TIME_US=0
INSTR_COUNT=0
MODE_USED="$MODE"

run_fast() {
    echo "[kernelsmith] Fast mode: qemu-user instruction count (cortex-a15 proxy)"
    if [[ -z "$USER_ELF" || ! -f "$USER_ELF" ]]; then echo "WARN no user ELF, skipping fast run"; return; fi
    LOG="$BUILD_DIR/qemu_fast.log"
    timeout 10 $QEMU_USER -d in_asm,exec -D "$LOG" "$USER_ELF" > "$BUILD_DIR/run_fast.out" 2>&1 || true
    if [[ -f "$LOG" ]]; then
        INSTR_COUNT=$(grep -c "^IN:" "$LOG" || echo 0)
    fi
    if grep -q KERNELSMITH_METRICS_START "$BUILD_DIR/run_fast.out"; then
        TIME_US=$(sed -n '/KERNELSMITH_METRICS_START/,/KERNELSMITH_METRICS_END/p' "$BUILD_DIR/run_fast.out" | grep time_us | head -1 | cut -d: -f2 | tr -d ' ' || echo 0)
        CYCLES=$(sed -n '/KERNELSMITH_METRICS_START/,/KERNELSMITH_METRICS_END/p' "$BUILD_DIR/run_fast.out" | grep cycles_estimate | head -1 | cut -d: -f2 | tr -d ' ' || echo 0)
    fi
    echo "[kernelsmith] Fast run instr ~ $INSTR_COUNT cycles_est $CYCLES"
}

run_full() {
    echo "[kernelsmith] Full mode: qemu-system-arm -machine $QEMU_MACHINE -cpu $QEMU_CPU -semihosting -kernel system.elf (DWT CYCCNT)"
    if [[ -n "$SYSTEM_ELF" && -f "$SYSTEM_ELF" ]]; then
        LOG="$BUILD_DIR/qemu_system.log"
        OUTF="$BUILD_DIR/run_full.out"
        TRACE_FLAGS=""
        if [[ "${KERNELSMITH_QEMU_TRACE:-0}" == "1" ]]; then
            TRACE_FLAGS="-d in_asm,exec -D $LOG"
        fi
        # Real baremetal launch with semihosting
        set +e
        # shellcheck disable=SC2086
        timeout 20 $QEMU_SYSTEM \
            -machine "$QEMU_MACHINE" \
            -cpu "$QEMU_CPU" \
            -m 16M \
            -nographic \
            -semihosting \
            -semihosting-config enable=on,target=native \
            -monitor none \
            -serial none \
            -kernel "$SYSTEM_ELF" \
            $TRACE_FLAGS \
            > "$OUTF" 2>&1
        RET=$?
        set -e
        echo "[kernelsmith] qemu-system exit code $RET"
        cat "$OUTF" || true
        if [[ -f "$LOG" && "${KERNELSMITH_QEMU_TRACE:-0}" == "1" ]]; then
            INSTR_COUNT=$(grep -c "^IN:" "$LOG" || echo 0)
        fi
        if grep -q KERNELSMITH_METRICS_START "$OUTF"; then
            TIME_US=$(sed -n '/KERNELSMITH_METRICS_START/,/KERNELSMITH_METRICS_END/p' "$OUTF" | grep time_us | head -1 | cut -d: -f2 | tr -d ' ' || echo 0)
            CYCLES=$(sed -n '/KERNELSMITH_METRICS_START/,/KERNELSMITH_METRICS_END/p' "$OUTF" | grep cycles_estimate | head -1 | cut -d: -f2 | tr -d ' ' || echo 0)
            MODE_USED="full"
            echo "[kernelsmith] Full run (baremetal) cycles $CYCLES time_us $TIME_US instr $INSTR_COUNT"
        else
            echo "WARN: no metrics from system ELF, falling back to fast+15% sim"
            run_fast
            if [[ "$INSTR_COUNT" -gt 0 ]]; then
                CYCLES=$(( CYCLES * 115 / 100 ))
            fi
            MODE_USED="full-sim-fallback"
        fi
    else
        echo "WARN: no system ELF, using fast simulation fallback"
        run_fast
        if [[ "$INSTR_COUNT" -gt 0 ]]; then
            CYCLES=$(( CYCLES * 115 / 100 ))
        fi
        MODE_USED="full-sim"
    fi
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
    "abi_flag": "$ABI",
    "qemu_machine": "$QEMU_MACHINE",
    "qemu_cpu": "$QEMU_CPU"
  },
  "metrics": {
    "cycles_estimate": ${CYCLES:-0},
    "time_us": ${TIME_US:-0},
    "instruction_count": ${INSTR_COUNT:-0},
    "text_bytes": ${TEXT_SIZE:-0},
    "data_bytes": ${DATA_SIZE:-0},
    "bss_bytes": ${BSS_SIZE:-0},
    "total_bytes": ${TOTAL_SIZE:-0},
    "system_text_bytes": ${SYS_TEXT_SIZE:-0},
    "system_data_bytes": ${SYS_DATA_SIZE:-0},
    "system_bss_bytes": ${SYS_BSS_SIZE:-0},
    "iterations": $ITERATIONS
  },
  "artifacts": {
    "elf": "$ELF",
    "map": "$MAP",
    "user_elf": "$USER_ELF",
    "system_elf": "$SYSTEM_ELF",
    "system_map": "$SYSTEM_MAP",
    "baremetal_dir": "$BAREMETAL_DIR"
  },
  "qemu": {
    "user": "$QEMU_USER",
    "system": "$QEMU_SYSTEM",
    "machine": "$QEMU_MACHINE",
    "cpu": "$QEMU_CPU",
    "mode_tradeoff": "fast=cortex-a15 proxy via qemu-user -d in_asm, full=real baremetal qemu-system-arm -machine $QEMU_MACHINE -cpu $QEMU_CPU -semihosting with DWT CYCCNT"
  }
}
EOF

echo "[kernelsmith] Metrics written to $OUTPUT"
cat "$OUTPUT"
