"""Raspberry Pi 5 platform facts and preflight checks.

Nothing here changes the system - it only reports what is true, so `doctor`
and `setup` can explain a problem instead of failing three steps later.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

from . import util

def _arm32_probe_bytes() -> bytes:
    """A 96-byte static ARM (EABI5) ELF whose whole program is exit(0).

    Running it is the only honest answer to "can this kernel execute the
    soft-float player?" - that depends on CONFIG_COMPAT, which the stock Pi 5
    arm64 kernel sets but a custom one may not.  Built here rather than
    committed as a blob so it stays inspectable.
    """
    code = bytes.fromhex(
        "0170a0e3"   # mov r7, #1      (__NR_exit)
        "0000a0e3"   # mov r0, #0      (status 0)
        "000000ef"   # svc #0
    )
    ehsize, phentsize = 52, 32
    base = 0x10000
    total = ehsize + phentsize + len(code)
    entry = base + ehsize + phentsize

    ident = bytes([0x7F]) + b"ELF" + bytes([1, 1, 1, 0]) + bytes(8)
    ehdr = ident + struct.pack(
        "<HHIIIIIHHHHHH",
        2,            # e_type    = ET_EXEC
        40,           # e_machine = EM_ARM
        1,            # e_version
        entry,        # e_entry
        ehsize,       # e_phoff
        0,            # e_shoff
        0x05000000,   # e_flags   = EF_ARM_EABI_VER5
        ehsize, phentsize, 1, 0, 0, 0,
    )
    phdr = struct.pack("<IIIIIIII",
                       1,        # PT_LOAD
                       0,        # p_offset
                       base, base,
                       total, total,
                       5,        # PF_R | PF_X
                       0x1000)
    return ehdr + phdr + code


def model() -> str:
    text = util.read_text("/proc/device-tree/model", "").strip("\x00").strip()
    return text or platform.machine()


def is_pi5() -> bool:
    return "Raspberry Pi 5" in model()


def is_pi() -> bool:
    return "Raspberry Pi" in model()


def kernel() -> str:
    return platform.release()


def arch() -> str:
    return platform.machine()


def os_release() -> dict:
    data = {}
    for line in util.read_text("/etc/os-release").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            data[key] = value.strip().strip('"')
    return data


def os_pretty() -> str:
    return os_release().get("PRETTY_NAME", "unknown")


def is_raspberry_pi_os() -> bool:
    rel = os_release()
    return "raspbian" in rel.get("ID", "").lower() or \
           "Raspberry Pi OS" in rel.get("PRETTY_NAME", "") or \
           Path("/boot/firmware/config.txt").exists()


def boot_dir() -> Path:
    """Where config.txt / cmdline.txt live (Bookworm moved them)."""
    for candidate in ("/boot/firmware", "/boot"):
        if Path(candidate, "config.txt").exists():
            return Path(candidate)
    return Path("/boot/firmware")


def cpu_count() -> int:
    return os.cpu_count() or 4


def mem_total_mb() -> int:
    match = re.search(r"MemTotal:\s+(\d+) kB", util.read_text("/proc/meminfo"))
    return int(match.group(1)) // 1024 if match else 0


def free_space_mb(path="/") -> int:
    try:
        stat = os.statvfs(path)
    except OSError:
        return 0
    return int(stat.f_bavail * stat.f_frsize / (1024 * 1024))


def throttled() -> str:
    """vcgencmd's throttle word - undervoltage here means audio dropouts."""
    if not util.have("vcgencmd"):
        return ""
    return util.out(["vcgencmd", "get_throttled"])


def can_run_arm32(loader: str | None = None) -> tuple[bool, str]:
    """Can this kernel execute 32-bit ARM binaries?  (rbp is armel/EABI5.)

    On aarch64 this needs CONFIG_COMPAT=y, which Raspberry Pi's bcm2712 kernel
    ships.  On an armv7/armhf Pi OS it is native.  We answer by executing a
    tiny static ARM ELF rather than by guessing from the config.
    """
    if arch().startswith("arm") and not arch().startswith("aarch64"):
        return True, "native 32-bit userland"

    if loader and Path(loader).exists():
        # The only question is whether the kernel will execute this 32-bit ELF,
        # so ANY output from it is a yes - including a complaint.  glibc 2.13's
        # ld.so predates --version and reports it as a missing library
        # ("--version: cannot open shared object file"), which read as a
        # failure but is the loader running and talking.
        try:
            proc = subprocess.run([loader], capture_output=True, text=True,
                                  timeout=10, check=False)
        except OSError as exc:
            return False, (f"the kernel refused to execute {loader} ({exc}). "
                           "Boot a kernel with CONFIG_COMPAT=y (the stock "
                           "Raspberry Pi OS kernel has it).")
        except subprocess.TimeoutExpired:
            return False, f"{loader} hung"
        blob = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if proc.returncode == 0 or blob:
            return True, f"{loader} runs (the chroot's own soft-float loader)"
        return False, (f"{loader} exited {proc.returncode} without a word - "
                       "the kernel may lack CONFIG_COMPAT")

    with tempfile.NamedTemporaryFile(prefix="rb4r5-arm32-", delete=False) as handle:
        handle.write(_arm32_probe_bytes())
        probe = handle.name
    try:
        os.chmod(probe, 0o755)
        try:
            proc = subprocess.run([probe], timeout=5,
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
            if proc.returncode == 0:
                return True, "kernel executes 32-bit ARM binaries (CONFIG_COMPAT=y)"
            return False, (f"the 32-bit ARM probe exited {proc.returncode}; "
                           "the kernel may lack CONFIG_COMPAT")
        except OSError as exc:
            return False, (f"the kernel refused to execute a 32-bit ARM binary "
                           f"({exc}).  Boot a kernel with CONFIG_COMPAT=y "
                           f"(the stock Raspberry Pi OS kernel has it).")
        except subprocess.TimeoutExpired:
            return False, "the 32-bit ARM probe hung"
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass


def compositor_running() -> str:
    """Name of a desktop compositor holding the display, or ''."""
    names = ("labwc", "wayfire", "wayfire-pi", "Xorg", "X", "weston",
             "kwin_wayland", "gnome-shell", "mutter", "cage", "sway")
    for name in names:
        if util.pgrep_exe(name):
            return name
    return ""


def default_systemd_target() -> str:
    return util.out(["systemctl", "get-default"], "unknown")


def has_module(name: str) -> bool:
    name = name.replace("-", "_")
    for line in util.read_text("/proc/modules").splitlines():
        if line.split(" ")[0].replace("-", "_") == name:
            return True
    # built-in?
    return Path(f"/sys/module/{name}").exists()


def summary() -> list[tuple[str, str]]:
    arm_ok, arm_why = can_run_arm32()
    rows = [
        ("model", model()),
        ("os", os_pretty()),
        ("kernel", f"{kernel()} ({arch()})"),
        ("cpus / ram", f"{cpu_count()} cores / {mem_total_mb()} MB"),
        ("free space (/)", f"{free_space_mb('/')} MB"),
        ("32-bit ARM", ("yes - " if arm_ok else "NO - ") + arm_why),
        ("boot config", str(boot_dir() / "config.txt")),
        ("systemd default", default_systemd_target()),
        ("compositor", compositor_running() or "none (good: the framebuffer is free)"),
    ]
    thr = throttled()
    if thr:
        rows.append(("throttling", thr))
    return rows


def check(strict: bool = False) -> list[str]:
    """Return a list of problems.  `strict` also reports soft warnings."""
    problems = []
    if not is_pi5():
        msg = (f"this is not a Raspberry Pi 5 ({model()}); "
               "the launcher targets the Pi 5 (BCM2712)")
        problems.append(msg if strict else f"note: {msg}")
    arm_ok, arm_why = can_run_arm32()
    if not arm_ok:
        problems.append(f"cannot run the player: {arm_why}")
    if not Path("/dev/fb0").exists():
        problems.append(
            "/dev/fb0 is missing.  The player renders through DirectFB's fbdev "
            "backend; enable the KMS driver (dtoverlay=vc4-kms-v3d in "
            f"{boot_dir()}/config.txt) and reboot.")
    comp = compositor_running()
    if comp:
        problems.append(
            f"the desktop compositor '{comp}' owns the display; the player "
            "cannot draw to /dev/fb0 while it runs.  `rb4r5 setup` switches the "
            "Pi to console autologin (systemctl set-default multi-user.target).")
    if free_space_mb("/") < 500 and strict:
        problems.append(f"only {free_space_mb('/')} MB free on / - the build needs ~1 GB")
    if not shutil.which("systemctl"):
        problems.append("systemd not found; the boot service cannot be installed")
    return problems
