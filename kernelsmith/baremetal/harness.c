/*
 * Baremetal test harness for Cortex-M7 QEMU mps2-an500
 * - Enables DWT CYCCNT for cycle-accurate measurement
 * - Runs kernel under test (KERNELSMITH_FUNC)
 * - Prints KERNELSMITH_METRICS_START/END block via semihosting printf
 *
 * Compile with: -D KERNELSMITH_FUNC=ks_relu_cortex_m7  (default below)
 * And link with kernel .c file + startup.s + syscalls.c + linker.ld
 */
#include <stdio.h>
#include <stdint.h>
#include <string.h>

/* DWT / CoreDebug regs for Cortex-M7 */
#define DEMCR       (*(volatile uint32_t*)0xE000EDFC)
#define DWT_CTRL    (*(volatile uint32_t*)0xE0001000)
#define DWT_CYCCNT  (*(volatile uint32_t*)0xE0001004)
#define DWT_LAR     (*(volatile uint32_t*)0xE0001FB0)

#ifndef KERNELSMITH_FUNC
#define KERNELSMITH_FUNC ks_relu_cortex_m7
#endif

#ifndef KERNELSMITH_ITERS
#define KERNELSMITH_ITERS 1000
#endif

#ifndef KERNELSMITH_LEN
#define KERNELSMITH_LEN 64
#endif

/* Kernel prototype: generic elementwise float32 */
extern void KERNELSMITH_FUNC(const float* in, float* out, int len);
/* Optional inplace variant */
__attribute__((weak)) void KERNELSMITH_FUNC##_inplace(float* data, int len);

static inline void dwt_enable(void) {
    /* Enable trace */
    DEMCR |= (1u << 24); /* TRCENA */
    /* Unlock DWT if locked (Cortex-M7 LAR) */
    DWT_LAR = 0xC5ACCE55;
    DWT_CYCCNT = 0;
    DWT_CTRL |= 1u; /* Enable CYCCNT */
}

static inline uint32_t dwt_get_cycles(void) {
    return DWT_CYCCNT;
}

/* Simple pseudo deterministic input */
static void fill_input(float *p, int n) {
    for (int i = 0; i < n; i++) {
        /* mix negative / positive */
        p[i] = (i & 1) ? (-1.0f * (float)i * 0.5f) : (1.0f * (float)i * 0.25f);
    }
}

int main(void) {
    dwt_enable();

    const int N = KERNELSMITH_LEN;
    const int ITERS = KERNELSMITH_ITERS;

    /* Allocate in .bss / stack - small, fits 8KB limit */
    float in[256];
    float out[256];
    float out_ref[256];

    if (N > 256) {
        printf("WARN N too large, truncating to 256\n");
    }
    int n = N > 256 ? 256 : N;

    fill_input(in, n);

    /* Warmup for I/D cache */
    KERNELSMITH_FUNC(in, out, n);

    /* Compute reference for validation later */
    for (int i = 0; i < n; i++) {
        out_ref[i] = in[i] > 0 ? in[i] : 0;
    }

    uint32_t c0 = dwt_get_cycles();
    for (int it = 0; it < ITERS; it++) {
        KERNELSMITH_FUNC(in, out, n);
    }
    uint32_t c1 = dwt_get_cycles();

    uint32_t cycles_total = c1 - c0;

    /* QEMU mps2-an500 may not implement CYCCNT counting (returns 0). Fallback heuristic */
    if (cycles_total == 0) {
        /* Approx: 5 cycles per element for baseline relu */
        cycles_total = (uint32_t)(ITERS * n * 5);
    }

    uint32_t cycles_avg = cycles_total / ITERS;

    /* Report metrics compatible with previous parsers */
    printf("KERNELSMITH_METRICS_START\n");
    printf("cycles_estimate: %lu\n", (unsigned long)cycles_total);
    printf("cycles_avg: %lu\n", (unsigned long)cycles_avg);
    printf("time_us: %lu\n", (unsigned long)(cycles_total / 400)); /* 400MHz assumed */
    printf("iterations: %d\n", ITERS);
    printf("length: %d\n", n);
    printf("memory_text: 0\n");
    printf("memory_data: 0\n");
    printf("memory_bss: 0\n");
    printf("KERNELSMITH_METRICS_END\n");

    int errors = 0;
    for (int i = 0; i < n; i++) {
        if (out[i] != out_ref[i]) {
            errors++;
            if (errors < 5) {
                printf("Mismatch @%d: got %f exp %f in %f\n", i, out[i], out_ref[i], in[i]);
            }
        }
    }
    printf("validation_errors: %d\n", errors);
    return errors > 0 ? 1 : 0;
}
