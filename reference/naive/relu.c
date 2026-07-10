/*
 * Naive reference implementation of ReLU (Rectified Linear Unit)
 * for ARM Cortex-M7 benchmarking baseline.
 *
 * This is intentionally unoptimized - simple scalar loop.
 * Optimized versions in generated/ will use FPU, DSP, loop unrolling.
 */

#include <stdint.h>
#include <stddef.h>

void relu_f32(const float *input, float *output, size_t n) {
    for (size_t i = 0; i < n; ++i) {
        float x = input[i];
        output[i] = x > 0.0f ? x : 0.0f;
    }
}

void relu_f32_inplace(float *data, size_t n) {
    for (size_t i = 0; i < n; ++i) {
        if (data[i] < 0.0f) {
            data[i] = 0.0f;
        }
    }
}

void relu_i8(const int8_t *input, int8_t *output, size_t n) {
    for (size_t i = 0; i < n; ++i) {
        int8_t x = input[i];
        output[i] = x > 0 ? x : 0;
    }
}

void relu_i16(const int16_t *input, int16_t *output, size_t n) {
    for (size_t i = 0; i < n; ++i) {
        int16_t x = input[i];
        output[i] = x > 0 ? x : 0;
    }
}

void relu_f16_reference(const float *input, float *output, size_t n) {
    for (size_t i = 0; i < n; ++i) {
        float x = input[i];
        output[i] = x > 0.0f ? x : 0.0f;
    }
}

#ifdef TEST_MAIN
#include <stdio.h>

int main(void) {
    float in[] = {-2.0f, -1.0f, 0.0f, 0.5f, 1.0f, 2.0f};
    float out[6];
    size_t n = sizeof(in) / sizeof(in[0]);
    relu_f32(in, out, n);
    for (size_t i = 0; i < n; ++i) {
        printf("relu(%f) = %f\n", in[i], out[i]);
    }
    return 0;
}
#endif
