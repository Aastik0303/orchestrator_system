"""Isolated execution of untrusted Python snippets.

Model- or user-supplied code NEVER runs in the API process. Layers:

1. Static policy (AST): import allowlist, banned builtins (eval/exec/open/
   __import__/getattr...), no dunder attribute access, size limits.
2. Separate interpreter process: `python -I -S -B` (isolated mode: no user
   site-packages, no PYTHON* env vars), empty environment (no secrets are
   inherited), a fresh temporary working directory removed afterwards.
3. Runtime audit hook inside the child (PEP 578) that aborts on network
   (socket.*), process creation (subprocess/os.system/exec/spawn/fork), file
   opens, directory listing, ctypes and imports outside the allowlist.
4. Resource limits: wall-clock timeout with process kill; on POSIX,
   RLIMIT_CPU/AS/FSIZE/NOFILE/NPROC; on Windows, a Job Object caps memory and
   kills the process tree when the job handle closes. Output is truncated.

This is defense in depth, not a hardened multi-tenant sandbox. Production
deployments should additionally run the executor in a network-less container
(e.g. gVisor/Firecracker or `docker run --network none --read-only`).
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field

ALLOWED_IMPORTS = frozenset(
    {
        "math",
        "cmath",
        "statistics",
        "json",
        "re",
        "datetime",
        "itertools",
        "functools",
        "collections",
        "random",
        "decimal",
        "fractions",
        "string",
        "textwrap",
        "heapq",
        "bisect",
        "operator",
        "typing",
        "dataclasses",
        "enum",
        "copy",
        "pprint",
        "numbers",
    }
)
BANNED_NAMES = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "open",
        "input",
        "breakpoint",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "memoryview",
        "exit",
        "quit",
        "help",
        "license",
        "credits",
        "copyright",
    }
)
MAX_CODE_CHARS = 20_000
MAX_AST_NODES = 5_000


@dataclass
class SandboxResult:
    status: str  # ok | error | timeout | blocked | memory_exceeded
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    duration_ms: int = 0
    violations: list[str] = field(default_factory=list)
    limits: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def check_code(code: str) -> list[str]:
    """Return static policy violations (empty list means the code may run)."""
    if len(code) > MAX_CODE_CHARS:
        return [f"Code exceeds {MAX_CODE_CHARS} characters."]
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        return [f"Syntax error: {exc.msg} (line {exc.lineno})."]
    violations: list[str] = []
    node_count = 0
    for node in ast.walk(tree):
        node_count += 1
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in ALLOWED_IMPORTS:
                    violations.append(f"Import of '{alias.name}' is not allowed.")
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[0]
            if node.level or module not in ALLOWED_IMPORTS:
                violations.append(f"Import from '{node.module}' is not allowed.")
        elif isinstance(node, ast.Name):
            if node.id in BANNED_NAMES:
                violations.append(f"Use of '{node.id}' is not allowed.")
            elif node.id.startswith("__"):
                violations.append(f"Access to '{node.id}' is not allowed.")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") or node.attr in {"f_globals", "f_locals", "f_builtins", "gi_frame", "cr_frame", "tb_frame", "f_back"}:
                violations.append(f"Access to attribute '{node.attr}' is not allowed.")
        elif isinstance(node, (ast.Global, ast.Nonlocal)) and any(name.startswith("__") for name in node.names):
            violations.append("Dunder globals are not allowed.")
    if node_count > MAX_AST_NODES:
        violations.append(f"Code is too complex ({node_count} AST nodes).")
    return list(dict.fromkeys(violations))


# Runs inside the child interpreter. The user code arrives on stdin as JSON.
_BOOTSTRAP = r"""
import json, sys
payload = json.loads(sys.stdin.read())
allowed = set(payload["allowed"])
limits = payload["limits"]
try:
    import resource
    mem = limits["memory_bytes"]
    resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    cpu = limits["cpu_seconds"]
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16))
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    except (ValueError, OSError):
        pass
except ImportError:
    pass
for name in allowed:
    try:
        __import__(name)
    except Exception:
        pass
if limits.get("random_seed") is not None:
    import random
    random.seed(limits["random_seed"])
code = compile(payload["code"], "<sandbox>", "exec")
BLOCKED_PREFIXES = ("socket.", "subprocess.", "os.system", "os.exec", "os.spawn", "os.posix_spawn",
    "os.fork", "os.kill", "os.forkpty", "ctypes.", "os.listdir", "os.scandir", "os.remove", "os.unlink",
    "os.rename", "os.rmdir", "os.mkdir", "os.chmod", "os.chdir", "shutil.", "winreg.", "_winapi.",
    "sys._getframe", "urllib.", "http.", "ftplib.", "smtplib.", "webbrowser.", "pty.", "fcntl.")
state = {"armed": False}
def hook(event, args):
    if not state["armed"]:
        return
    if event == "open" or event.startswith(BLOCKED_PREFIXES) or event in ("builtins.input", "code.__new__", "sys.settrace", "sys.setprofile", "object.__getattr__"):
        raise RuntimeError("sandbox violation: " + event)
    if event == "import":
        root = str(args[0]).split(".")[0]
        if root not in allowed and root not in sys.modules:
            raise RuntimeError("sandbox violation: import " + root)
sys.addaudithook(hook)
namespace = {"__name__": "__sandbox__"}
state["armed"] = True
exec(code, namespace)
"""


def _windows_job_limit(process: subprocess.Popen, memory_bytes: int):
    """Assign the child to a Job Object with a memory cap. Returns the job
    handle (keep it alive) or None if unavailable."""
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:  # pragma: no cover
        return None

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectExtendedLimitInformation = 9
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.OpenProcess.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = (
        JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    )
    info.BasicLimitInformation.ActiveProcessLimit = 1
    info.ProcessMemoryLimit = memory_bytes
    if not kernel32.SetInformationJobObject(
        job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
    ):
        kernel32.CloseHandle(job)
        return None
    PROCESS_ALL_ACCESS = 0x1F0FFF
    handle = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, process.pid)
    if not handle or not kernel32.AssignProcessToJobObject(job, handle):
        if handle:
            kernel32.CloseHandle(handle)
        kernel32.CloseHandle(job)
        return None
    kernel32.CloseHandle(handle)

    class _Job:
        def close(self) -> None:
            kernel32.CloseHandle(job)

    return _Job()


def run_python(
    code: str,
    *,
    timeout_seconds: float = 5.0,
    memory_mb: int = 256,
    max_output_bytes: int = 20_000,
    random_seed: int | None = None,
) -> SandboxResult:
    """Run `code` in the sandbox. With `random_seed`, the `random` module is
    seeded so the same snippet produces the same output."""
    limits = {
        "timeout_seconds": timeout_seconds,
        "memory_mb": memory_mb,
        "max_output_bytes": max_output_bytes,
        "network": "blocked (audit hook)",
        "filesystem": "temporary directory, opens blocked",
        "memory_enforced": sys.platform != "win32",
    }
    violations = check_code(code)
    if violations:
        return SandboxResult(status="blocked", violations=violations, limits=limits)

    payload = json.dumps(
        {
            "code": code,
            "allowed": sorted(ALLOWED_IMPORTS),
            "limits": {
                "memory_bytes": memory_mb * 1024 * 1024,
                "cpu_seconds": max(1, int(timeout_seconds) + 1),
                "random_seed": random_seed,
            },
        }
    )
    # Minimal environment: nothing from the parent (no API keys, DB URLs).
    env = {"PYTHONIOENCODING": "utf-8", "PYTHONHASHSEED": "0"}
    if sys.platform == "win32":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="sandbox-") as workdir:
        # Passed as a file: multi-line `-c` arguments are mangled by Windows
        # command-line quoting.
        bootstrap_path = os.path.join(workdir, "_bootstrap.py")
        with open(bootstrap_path, "w", encoding="utf-8") as handle:
            handle.write(_BOOTSTRAP)
        creationflags = 0
        popen_kwargs: dict[str, object] = {}
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_SUSPENDED", 0x4)
        else:
            popen_kwargs["start_new_session"] = True
        process = subprocess.Popen(
            [_interpreter(), "-I", "-S", "-B", bootstrap_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workdir,
            env=env,
            creationflags=creationflags,
            **popen_kwargs,  # type: ignore[arg-type]
        )
        job = None
        if sys.platform == "win32":
            job = _windows_job_limit(process, memory_mb * 1024 * 1024)
            limits["memory_enforced"] = job is not None
            _resume_windows_process(process)
        status = "ok"
        try:
            stdout, stderr = process.communicate(payload.encode("utf-8"), timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _kill(process)
            stdout, stderr = process.communicate()
            status = "timeout"
        finally:
            if job is not None:
                job.close()
    duration_ms = int((time.perf_counter() - started) * 1000)
    # Normalize newlines: Windows text-mode stdout would otherwise yield
    # platform-dependent output (CRLF) for the same program.
    out = stdout[:max_output_bytes].decode("utf-8", errors="replace").replace("\r\n", "\n")
    err = stderr[:max_output_bytes].decode("utf-8", errors="replace").replace("\r\n", "\n")
    if len(stdout) > max_output_bytes:
        out += "\n[output truncated]"
    if status == "ok" and process.returncode != 0:
        status = "error"
        if "sandbox violation" in err:
            violation = err.strip().splitlines()[-1][:300]
            return SandboxResult(
                status="blocked",
                stdout=out,
                stderr=_short_traceback(err),
                exit_code=process.returncode,
                duration_ms=duration_ms,
                violations=[violation],
                limits=limits,
            )
        if "MemoryError" in err or process.returncode in {-9, 137} or (
            sys.platform == "win32" and process.returncode in {1816, 3221225495, 0xC0000017}
        ):
            status = "memory_exceeded"
    return SandboxResult(
        status=status,
        stdout=out,
        stderr=_short_traceback(err),
        exit_code=process.returncode,
        duration_ms=duration_ms,
        limits=limits,
    )


def _interpreter() -> str:
    # On Windows a venv's python.exe is a launcher that spawns the base
    # interpreter as a child process, which the one-process job limit forbids.
    return getattr(sys, "_base_executable", None) or sys.executable


def _short_traceback(stderr: str) -> str:
    lines = [line for line in stderr.strip().splitlines() if line.strip()]
    return "\n".join(lines[-6:])[:2000]


def _kill(process: subprocess.Popen) -> None:
    if sys.platform != "win32":
        import signal

        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass
    process.kill()


def _resume_windows_process(process: subprocess.Popen) -> None:
    """Resume a process created with CREATE_SUSPENDED after it joined its job."""
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPTHREAD = 0x00000004
    THREAD_SUSPEND_RESUME = 0x0002

    class THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.OpenThread.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    entry = THREADENTRY32()
    entry.dwSize = ctypes.sizeof(THREADENTRY32)
    try:
        if not kernel32.Thread32First(snapshot, ctypes.byref(entry)):
            return
        while True:
            if entry.th32OwnerProcessID == process.pid:
                thread = kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if thread:
                    kernel32.ResumeThread(thread)
                    kernel32.CloseHandle(thread)
            if not kernel32.Thread32Next(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
