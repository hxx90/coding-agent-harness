"""Fail-closed Linux worker confinement; frozen with each program runtime.

Landlock restricts file contents to the immutable runtime and program. Seccomp
blocks network access, process creation/exec and operations Landlock ABI 1 does
not mediate. No namespaces, root privileges or writable sandbox directory are
required. The fixed liveness thread must be started before installing this.
"""

from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path


def restrict(read_paths: list[str]) -> None:
    if os.uname().machine not in {"x86_64", "aarch64"}:
        raise RuntimeError("sandbox_unavailable: unsupported Linux syscall ABI")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    seccomp = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    seccomp.seccomp_attr_set.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]

    def check(result: int, operation: str) -> int:
        if result < 0:
            raise OSError(ctypes.get_errno() or errno.EPERM, operation)
        return result

    # x86_64 and aarch64 share the Landlock syscall numbers.
    check(libc.syscall(444, 0, 0, 1), "sandbox_unavailable: Landlock ABI query")
    check(libc.prctl(38, 1, 0, 0, 0), "sandbox_unavailable: no_new_privs")

    class Ruleset(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64)]

    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int)]

    # All filesystem rights supported by ABI 1, including execute and writes.
    rules = Ruleset((1 << 13) - 1)
    rules_fd = check(
        libc.syscall(444, ctypes.byref(rules), ctypes.sizeof(rules), 0),
        "sandbox_unavailable: create Landlock ruleset",
    )
    try:
        for path in sorted({str(Path(p).resolve()) for p in read_paths}):
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rights = (1 << 2) | ((1 << 3) if Path(path).is_dir() else 0)
                rule = PathRule(rights, fd)
                check(
                    libc.syscall(445, rules_fd, 1, ctypes.byref(rule), 0),
                    "sandbox_unavailable: add read-only path",
                )
            finally:
                os.close(fd)
        check(libc.syscall(446, rules_fd, 0), "sandbox_unavailable: enforce Landlock")
    finally:
        os.close(rules_fd)

    context = seccomp.seccomp_init(0x00050000 | errno.EPERM)  # Default deny.
    if not context:
        raise RuntimeError("sandbox_unavailable: create seccomp filter")
    try:
        # Synchronize the fixed liveness thread too; failure is not a fallback.
        check(seccomp.seccomp_attr_set(context, 4, 1), "seccomp TSYNC")
        # File opens remain subject to Landlock; only inherited pipes are
        # writable. No networking, new processes, kernel management, signals
        # to other processes, metadata writes or unmediated I/O mechanisms.
        allowed = (
            "read",
            "write",
            "readv",
            "writev",
            "pread64",
            "close",
            "close_range",
            "lseek",
            "fstat",
            "newfstatat",
            "stat",
            "lstat",
            "statx",
            "statfs",
            "fstatfs",
            "access",
            "faccessat",
            "faccessat2",
            "readlink",
            "readlinkat",
            "getdents64",
            "open",
            "openat",
            "openat2",
            "fcntl",
            "dup",
            "dup2",
            "dup3",
            "ioctl",
            "brk",
            "mmap",
            "mprotect",
            "munmap",
            "mremap",
            "madvise",
            "membarrier",
            "arch_prctl",
            "rt_sigaction",
            "rt_sigprocmask",
            "rt_sigreturn",
            "sigaltstack",
            "futex",
            "futex_time64",
            "set_tid_address",
            "set_robust_list",
            "rseq",
            "sched_yield",
            "sched_getaffinity",
            "getcpu",
            "clock_gettime",
            "clock_gettime64",
            "clock_getres",
            "gettimeofday",
            "time",
            "nanosleep",
            "clock_nanosleep",
            "clock_nanosleep_time64",
            "restart_syscall",
            "getpid",
            "getppid",
            "gettid",
            "getuid",
            "geteuid",
            "getgid",
            "getegid",
            "getgroups",
            "getrandom",
            "uname",
            "sysinfo",
            "getrusage",
            "getrlimit",
            "poll",
            "ppoll",
            "ppoll_time64",
            "select",
            "pselect6",
            "pselect6_time64",
            "epoll_create1",
            "epoll_wait",
            "epoll_pwait",
            "epoll_ctl",
            "getcwd",
            "chdir",
            "fchdir",
            "exit",
            "exit_group",
        )
        for name in allowed:
            syscall = seccomp.seccomp_syscall_resolve_name(name.encode())
            if syscall >= 0:
                check(
                    seccomp.seccomp_rule_add(context, 0x7FFF0000, syscall, 0),
                    "seccomp rule: " + name,
                )
        check(seccomp.seccomp_load(context), "sandbox_unavailable: enforce seccomp")
    finally:
        seccomp.seccomp_release(context)
