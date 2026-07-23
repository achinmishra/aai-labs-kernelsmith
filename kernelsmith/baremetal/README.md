# Baremetal QEMU System Support for Cortex-M7

This directory implements true baremetal system emulation for `qemu-system-arm -machine mps2-an500 -cpu cortex-m7` with semihosting and DWT cycle counter.

## Why previous code used cortex-a15?

The old `compile_and_run.sh` at L100-L109 used:

```bash
-mcpu=cortex-a15 -mfpu=neon
qemu-arm
```

Because `qemu-arm` (qemu-user / linux user-mode) **does not support Cortex-M** CPUs. M-profile has no MMU, Thumb-only, cannot run Linux user ELF. The toolchain `arm-linux-gnueabihf-gcc` also cannot target M-profile. So the script used a cortex-a15 proxy for fast instruction count.

## New real baremetal path

Now `full` mode does:

```bash
qemu-system-arm -machine mps2-an500 -cpu cortex-m7 -m 16M -nographic \
  -semihosting -semihosting-config enable=on,target=native \
  -monitor none -serial none \
  -kernel <system_elf>
```

Where `system_elf` is built from:

- `startup_mps2_an500.s` – vector table, Reset_Handler: enables FPU (CPACR), enables DWT TRCENA+CYCCNT, copies .data, zeros .bss, calls main, then _exit semihosting.
- `linker_mps2_an500.ld` – FLASH at 0x00000000 (vector at 0), RAM at 0x20000000, standard sections.
- `syscalls.c` – minimal newlib semihosting stubs: _write via SYS_WRITE (0x05), _exit via SYS_EXIT (0x18) with ADP_Stopped_ApplicationExit (0x20026), _sbrk heap between _end and _estack-8K.
- `harness.c` – test harness: enables DWT, runs KERNELSMITH_FUNC (default ks_relu_cortex_m7) for KERNELSMITH_ITERS, measures cycles via DWT_CYCCNT, prints KERNELSMITH_METRICS_START/END compatible with previous parser, validates output.

### Compile

Python API:

```python
from kernelsmith.toolchain import resolve_toolchain, compile_baremetal_system_elf
tc = resolve_toolchain("cortex-m7")
compile_baremetal_system_elf(
    kernel_source=Path("ks_relu_cortex_m7.c"),
    output_elf=Path("build/ks_system.elf"),
    tc=tc,
    build_dir=Path("build"),
    func_name="ks_relu_cortex_m7",
    iters=1000,
    length=64,
)
```

Shell:

```bash
./scripts/compile_and_run.sh --kernel my_kernel.c --target cortex-m7 --mode full --output results.json
# set KERNELSMITH_QEMU_TRACE=1 to get instruction trace log
```

### Emulator

`kernelsmith/emulator/__init__.py:run_qemu_system()` now really runs qemu-system-arm with semihosting, captures stdout for metrics, parses cycles_estimate, time_us. If ELF is not baremetal (legacy), falls back to fast+15% simulated overhead to preserve backward compat.

### DWT cycle accuracy

Harness uses real DWT_CYCCNT if QEMU implements it (QEMU's Cortex-M7 does implement CYCCNT counting in recent versions). If CYCCNT returns 0 (unimplemented), fallback heuristic `iters * len * 5` is used.

### QEMU limitation for -cpu cortex-m7 flag

You **cannot** do `qemu-arm -cpu cortex-m7` – qemu-user only supports A-profile. Must use `qemu-system-arm`. That's why fast mode remains qemu-user with cortex-a15 proxy for quick iteration, full mode is now true M7.

### Machine choices

- cortex-m7 → mps2-an500, cortex-m7
- cortex-m4/m3/m0 → mps2-an385 with respective cpu (registry in toolchain)

For other boards, override via hardware profile yaml:

```yaml
emulation:
  qemu_machine: mps2-an500
  qemu_cpu: cortex-m7
```
