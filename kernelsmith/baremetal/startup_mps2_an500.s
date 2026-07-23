    .syntax unified
    .cpu cortex-m7
    .fpu fpv5-sp-d16
    .thumb

    .global _isr_vector
    .global Reset_Handler
    .global Default_Handler
    .global _exit_semihost

    /* External symbols from linker script */
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
    /* 64 external IRQs */
    .rept 64
    .word Default_Handler
    .endr

    .section .text.Reset_Handler, "ax", %progbits
    .type Reset_Handler, %function
    .thumb_func
Reset_Handler:
    /* Set stack pointer - HW does it from vector, but ensure */
    ldr r0, =_estack
    mov sp, r0

    /* Enable FPU: CP10 & CP11 full access (CPACR bits 20-23 = 0xF) */
    ldr r0, =0xE000ED88      /* CPACR */
    ldr r1, [r0]
    orr r1, r1, #(0xF << 20)
    str r1, [r0]
    dsb
    isb

    /* Enable DWT + ITM: set TRCENA in DEMCR (0xE000EDFC bit 24) */
    ldr r0, =0xE000EDFC
    ldr r1, [r0]
    orr r1, r1, #(1 << 24)
    str r1, [r0]

    /* Reset and enable CYCCNT in DWT_CTRL (0xE0001000) */
    ldr r0, =0xE0001000
    movs r1, #0
    str r1, [r0, #4]         /* DWT_CYCCNT = 0 */
    ldr r1, [r0]             /* DWT_CTRL */
    orr r1, r1, #1           /* Enable CYCCNT */
    str r1, [r0]

    /* Copy .data from Flash (_sidata) to RAM (_sdata.._edata) */
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

    /* Zero .bss: _sbss .. _ebss */
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

    /* Call SystemInit if defined */
    bl SystemInit

    /* Call C++ static constructors via __libc_init_array if present */
    bl __libc_init_array

    /* Call main() */
    bl main

    /* main returns exit code in r0 -> _exit */
    bl _exit

    /* Should not return; loop forever */
    b .

    .size Reset_Handler, .-Reset_Handler

    /* Weak default isr handlers */
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
