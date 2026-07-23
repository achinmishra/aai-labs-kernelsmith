/*
 * Minimal newlib syscalls with ARM semihosting for QEMU mps2-an500
 * Supports printf via SYS_WRITE and exit via SYS_EXIT
 */
#include <sys/stat.h>
#include <sys/types.h>
#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#include <unistd.h>

/* Semihosting operations */
#define SYS_OPEN        0x01
#define SYS_CLOSE       0x02
#define SYS_WRITEC      0x03
#define SYS_WRITE0      0x04
#define SYS_WRITE       0x05
#define SYS_READ        0x06
#define SYS_READC       0x07
#define SYS_ISTTY       0x09
#define SYS_SEEK        0x0A
#define SYS_FLEN        0x0C
#define SYS_EXIT        0x18
#define SYS_CLOCK       0x10
#define SYS_TIME        0x11

/* ADP_Stopped_ApplicationExit */
#define ADP_EXIT        0x20026

extern char _end;     /* defined by linker */
extern char _estack;  /* top of stack */

static char *heap_ptr = NULL;

static inline long smh_trap(long sysnum, long *args) {
    register long r0 asm("r0") = sysnum;
    register long r1 asm("r1") = (long)args;
    __asm__ volatile (
        "bkpt 0xAB\n"
        : "+r" (r0)
        : "r" (r1)
        : "r2", "r3", "r12", "lr", "memory", "cc"
    );
    return r0;
}

/* Optional: writec for single char */
static void smh_writec(char c) {
    long args = (long)&c;
    /* SYS_WRITEC takes pointer to char in r1? Actually spec: r1 = char addr. We use WRITE0 path */
    (void)args;
    /* Fallback to WRITE for simplicity */
}

int _close(int fd) {
    (void)fd;
    return -1;
}

int _fstat(int fd, struct stat *st) {
    if (fd < 3) {
        st->st_mode = S_IFCHR;
        return 0;
    }
    errno = EBADF;
    return -1;
}

int _isatty(int fd) {
    if (fd < 3) return 1;
    errno = EBADF;
    return 0;
}

int _lseek(int fd, int ptr, int dir) {
    (void)fd; (void)ptr; (void)dir;
    return 0;
}

int _read(int fd, char *ptr, int len) {
    if (fd == 0) {
        long args[3];
        args[0] = fd;
        args[1] = (long)ptr;
        args[2] = len;
        long ret = smh_trap(SYS_READ, args);
        return (int)ret;
    }
    errno = EBADF;
    return -1;
}

void *_sbrk(ptrdiff_t incr) {
    if (heap_ptr == NULL) {
        heap_ptr = &_end;
    }
    char *prev = heap_ptr;
    char *stack_top = &_estack;
    /* Keep 8KB guard for stack */
    if (heap_ptr + incr > stack_top - 8192) {
        errno = ENOMEM;
        return (void *)-1;
    }
    heap_ptr += incr;
    return prev;
}

int _write(int fd, const void *ptr, size_t len) {
    if (fd == 1 || fd == 2) {
        if (len == 0) return 0;
        long args[3];
        args[0] = fd; /* 1=stdout, 2=stderr mapped to host */
        args[1] = (long)ptr;
        args[2] = (long)len;
        long ret = smh_trap(SYS_WRITE, args);
        /* SYS_WRITE returns 0 on success, or number of bytes NOT written */
        if (ret == 0) return (int)len;
        return (int)(len - ret);
    }
    errno = EBADF;
    return -1;
}

int _getpid(void) { return 1; }
int _kill(int pid, int sig) { (void)pid; (void)sig; errno = EINVAL; return -1; }

void _exit(int code) {
    long args[2];
    args[0] = ADP_EXIT; /* reason */
    args[1] = code;     /* exit code for QEMU */
    smh_trap(SYS_EXIT, args);
    while (1) { /* should not reach */ }
}

/* For linking with --specs=nosys.specs, provide dummy _exit wrapper for semihost */
void _exit_semihost(int code) {
    _exit(code);
}

/* Weak empty c++ ctors handler if libc doesn't pull its own */
__attribute__((weak)) void __libc_init_array(void) {}
