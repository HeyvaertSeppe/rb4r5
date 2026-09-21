"""Small shared helpers: logging, subprocess, files, root checks."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

_COLOR = sys.stderr.isatty() and os.environ.get("NO_COLOR") is None
_LEVELS = {"debug": 10, "info": 20, "ok": 20, "warn": 30, "error": 40}
_VERBOSE = False

_TAGS = {
    "debug": ("··", "\033[2m"),
    "info": ("--", ""),
    "ok": ("ok", "\033[32m"),
    "warn": ("!!", "\033[33m"),
    "error": ("XX", "\033[31m"),
    "step": ("==", "\033[1m"),
}


def set_verbose(on: bool) -> None:
    global _VERBOSE
    _VERBOSE = on


def verbose() -> bool:
    return _VERBOSE


def _emit(level: str, msg: str) -> None:
    tag, color = _TAGS.get(level, ("--", ""))
    line = f"[{tag}] {msg}"
    if _COLOR and color:
        line = f"{color}{line}\033[0m"
    print(line, file=sys.stderr, flush=True)


def step(msg: str) -> None:
    _emit("step", msg)


def info(msg: str) -> None:
    _emit("info", msg)


def ok(msg: str) -> None:
    _emit("ok", msg)


def warn(msg: str) -> None:
    _emit("warn", msg)


def error(msg: str) -> None:
    _emit("error", msg)


def debug(msg: str) -> None:
    if _VERBOSE:
        _emit("debug", msg)


class Fail(Exception):
    """A fatal, already-explained error.  The CLI prints it and exits 1."""


def run(cmd, check: bool = True, capture: bool = True, timeout: int | None = 300,
        env: dict | None = None, input_: str | None = None, cwd=None):
    """Run a command.  `cmd` may be a list or a shell string."""
    shell = isinstance(cmd, str)
    printable = cmd if shell else " ".join(str(c) for c in cmd)
    debug(f"run: {printable}")
    full_env = None
    if env:
        full_env = dict(os.environ)
        full_env.update(env)
    try:
        proc = subprocess.run(
            cmd, shell=shell, check=False, timeout=timeout, env=full_env,
            cwd=str(cwd) if cwd else None, input=input_,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
            text=True,
        )
    except FileNotFoundError as exc:
        if check:
            raise Fail(f"command not found: {printable}") from exc
        return subprocess.CompletedProcess(cmd, 127, "", "")
    except subprocess.TimeoutExpired as exc:
        if check:
            raise Fail(f"timed out after {timeout}s: {printable}") from exc
        return subprocess.CompletedProcess(cmd, 124, "", "")
    if proc.returncode != 0:
        if capture and proc.stdout:
            debug(proc.stdout.strip())
        if check:
            out = (proc.stdout or "").strip()
            raise Fail(f"command failed ({proc.returncode}): {printable}"
                       + (f"\n{out}" if out else ""))
    return proc


def out(cmd, default: str = "") -> str:
    """Run a command and return its stdout, or `default` if it fails."""
    proc = run(cmd, check=False, capture=True)
    if proc.returncode != 0:
        return default
    return (proc.stdout or "").strip()


def have(program: str) -> bool:
    return shutil.which(program) is not None


def is_root() -> bool:
    return os.geteuid() == 0


def require_root(what: str = "this command") -> None:
    if not is_root():
        raise Fail(f"{what} needs root - run it with sudo:\n"
                   f"    sudo python3 {sys.argv[0]} {' '.join(sys.argv[1:])}")


def read_text(path, default: str = "") -> str:
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return default


def read_int(path, default: int = -1) -> int:
    try:
        return int(read_text(path).strip().split()[0])
    except (ValueError, IndexError):
        return default


def write_text(path, content: str, mode: int = 0o644) -> bool:
    """Write a file only when the content changed.  Returns True if written."""
    path = Path(path)
    if path.exists() and path.read_text(errors="replace") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".rb4r5-tmp")
    tmp.write_text(content)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    return True


def ensure_dir(path, mode: int = 0o755) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    return path


def ensure_fifo(path, mode: int = 0o666) -> None:
    path = Path(path)
    if path.is_fifo():
        os.chmod(path, mode)
        return
    if path.exists():
        path.unlink()
    os.mkfifo(path, mode)
    os.chmod(path, mode)


def is_mountpoint(path) -> bool:
    return os.path.ismount(str(path))


def pgrep(pattern: str) -> list[int]:
    """PIDs whose /proc/<pid>/cmdline contains `pattern` (excluding ourselves)."""
    pids = []
    me = os.getpid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                cmdline = handle.read().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if pattern in cmdline:
            pids.append(pid)
    return pids


def pgrep_exe(names) -> list[int]:
    """PIDs whose argv[0] basename is exactly one of `names`.

    Substring matching over the whole cmdline is far too loose for process
    names like "X" or "rb" - use this whenever the name matters.
    """
    if isinstance(names, str):
        names = [names]
    wanted = set(names)
    pids = []
    me = os.getpid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                argv0 = handle.read().split(b"\0")[0].decode(errors="replace")
        except OSError:
            continue
        if argv0 and os.path.basename(argv0) in wanted:
            pids.append(pid)
    return pids


def pgrep_arg(arg: str) -> list[int]:
    """PIDs having `arg` as one whole argv entry.

    Substring matching over the joined cmdline also hits the shell command that
    merely mentions the path (including our own), which is how the Chromebit
    port's `pkill -f "rbp -a"` used to kill its own SSH session.
    """
    pids = []
    me = os.getpid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                argv = handle.read().split(b"\0")
        except OSError:
            continue
        if arg.encode() in argv:
            pids.append(pid)
    return pids


def wait_for(predicate, timeout: float = 10.0, interval: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}PB"
