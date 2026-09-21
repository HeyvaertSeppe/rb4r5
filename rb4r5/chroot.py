"""The soft-float XDJ-RX3 chroot: assembly, device stubs, bind mounts.

The player is a 32-bit ARM soft-float glibc-2.13 binary, so it runs inside a
chroot holding the RX3's own userland.  You supply that userland (extracted
from firmware you own - docs/03-payload.md); this module puts it together,
creates the devices rbp expects, and mounts the host's /dev, /proc, /sys and
/tmp into it.  /tmp matters most: it is how the shims inside the player and
the daemons outside it exchange FIFO records.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import struct
from pathlib import Path

from . import config, util

# What the chroot must contain to be usable (relative to its root).
REQUIRED = [
    "lib/ld-linux.so.3",
    "root/pdj/rbp",
    "usr/lib/directfb-1.4-6/systems/libdirectfb_fbdev.so",
    "usr/lib/memshim.so",
    "usr/lib/fbshim.so",
    "usr/lib/audioshim.so",
    "usr/lib/keyshim.so",
    "etc/directfbrc",
]

OPTIONAL = [
    "usr/bin/edb_streamd",
    "root/gui",
    "usr/share/alsa",
]

BIND_MOUNTS = ["dev", "proc", "sys", "tmp"]

# Devices the RX3 firmware expects.  FIFOs for anything a thread polls or
# reads (a regular file would be EOF/always-ready and burn a core); plain files
# for ioctl-only devices.  /dev/paudiog0 must be ABSENT or JUCE takes a dead
# USB-gadget audio path.
FIFO_STUBS = ["subucom_spi1.0", "subucom_spi2.0", "subucom_spi_rdy3.0",
              "subucom_spi_rdy4.0", "hidg0"]
FILE_STUBS = ["printkdrv0", "tsc2007_2-0048", "gpiodrv"]
ABSENT = ["paudiog0"]

DIRECTFBRC = """\
# rb4r5 DirectFB configuration (inside the chroot).
#
# The logical layer must match what fbshim.so reports to rbp (1280x800 RGB565);
# the patched fbdev driver scales and converts that into the real Pi 5
# framebuffer (normally 1920x1080 XRGB8888).  Do NOT set pixelformat=rgb32.
system=fbdev
fbdev={fbdev}
mode={width}x{height}
depth=16
module-dir=/usr/lib/directfb-1.4-6

# Headless: never touch a virtual terminal.
#
# DirectFB 1.4.16's systems/fbdev/vt.c:vt_set_fb() declares `struct stat sbf`
# and calls fstat().  The module is built against modern cross headers but
# links the RX3's glibc 2.13, whose struct stat is smaller, so fstat() writes
# past sbf and smashes the stack canary (SIGABRT in __stack_chk_fail).
# system_initialize() only runs that code when dfb_config->vt is set.
no-vt
no-vt-switch
no-vt-switching
"""


# --------------------------------------------------------------------------
# paths, as the chroot sees them
# --------------------------------------------------------------------------
def inside(root, path: str) -> Path:
    """Resolve an in-chroot absolute path the way the kernel does inside it.

    An absolute symlink inside a chroot points at the CHROOT's root, not the
    host's.  Python does not know that, so `root / "usr/lib/fbshim.so"` can
    follow `usr/lib -> /lib` straight out of the runtime and land on a host
    file.  A shim installed that way is written outside the chroot, found by
    every check that looks from the host, and still invisible to the player -
    which then says only `cannot be preloaded` and runs without it.

    Use this for anything the player will open by an absolute path.
    """
    return _walk(Path(root), path)[0]


def trail(root, path: str) -> list[str]:
    """The symlinks `inside()` followed, for when the answer needs explaining."""
    return _walk(Path(root), path)[1]


def _walk(root: Path, path: str) -> tuple[Path, list[str]]:
    here = root
    rest = [part for part in path.split("/") if part and part != "."]
    hops = []
    while rest:
        part = rest.pop(0)
        if part == "..":
            here = root if here == root else here.parent
            continue
        here = here / part
        if not here.is_symlink():
            continue
        if len(hops) > 40:
            hops.append(f"{here}: too many symlinks")
            break
        target = os.readlink(here)
        hops.append(f"{here} -> {target}")
        pieces = [part for part in target.split("/") if part and part != "."]
        here = root if target.startswith("/") else here.parent
        rest = pieces + rest
    return here, hops


def chroot_cmd(root, argv: list[str],
               env: dict | None = None) -> tuple[list[str], dict]:
    """Run argv inside the chroot, with env applied INSIDE it, not around it.

    `chroot` is a host binary.  Anything put in its environment is read by the
    HOST's dynamic loader first, before chroot() has happened - so a
    LD_PRELOAD naming /usr/lib/memshim.so makes the host loader look for that
    path on the host, where it is a 32-bit ARM object that is not there at
    all.  It then prints

        ERROR: ld.so: object '/usr/lib/memshim.so' from LD_PRELOAD cannot be
        preloaded (cannot open shared object file): ignored.

    and carries on.  chroot() then happens, the RX3's own loader runs, and it
    preloads the shim perfectly well - but that error is already in the log,
    where it reads exactly like the shims having failed, for every shim, every
    single start.

    So the variables go after the chroot, as arguments to the runtime's own
    `env`, and the host process never sees them.

    Returns the command and whatever environment still has to be set AROUND
    it - empty when the variables went inside, which is the normal case.
    """
    cmd = ["chroot", str(root)]
    if not env:
        return cmd + argv, {}
    if not inside(root, "usr/bin/env").exists():
        util.warn("the runtime has no /usr/bin/env, so the player's "
                  "environment has to be set around chroot instead of inside "
                  "it.  ld.so will report a failed preload for each shim; "
                  "those come from the host loader and are not real - "
                  "`launch.py shimtest` will show the shims loading.")
        return cmd + argv, dict(env)
    cmd.append("/usr/bin/env")
    cmd += [f"{key}={value}" for key, value in env.items()]
    return cmd + argv, {}


def status(cfg) -> dict:
    """What is present, what is missing, what is mounted."""
    root = cfg.chroot
    missing = [rel for rel in REQUIRED if not inside(root, rel).exists()]
    absent_optional = [rel for rel in OPTIONAL
                       if not inside(root, rel).exists()]
    mounts = {name: util.is_mountpoint(root / name) for name in BIND_MOUNTS}
    return {
        "root": root,
        "exists": root.exists(),
        "missing": missing,
        "missing_optional": absent_optional,
        "mounts": mounts,
        "ready": root.exists() and not missing,
    }


def require_ready(cfg) -> None:
    state = status(cfg)
    if not state["exists"]:
        raise util.Fail(
            f"no runtime at {state['root']}.\n"
            f"Put your extracted XDJ-RX3 firmware in {cfg.payload} and run:\n"
            f"    sudo python3 launch.py build\n"
            f"See docs/03-payload.md.")
    if state["missing"]:
        raise util.Fail(
            "the runtime is incomplete - missing:\n  " +
            "\n  ".join(state["missing"]) +
            "\nRun: sudo python3 launch.py build   (docs/03-payload.md)")


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------
def assemble(cfg, payload: Path | None = None, force: bool = False) -> list[str]:
    """Build the chroot from an extracted firmware payload.

    The payload directory is what docs/03-payload.md tells you to produce:

        <payload>/XDJRX3-rootfs/     the soft-float rootfs (lib, usr, bin, etc)
        <payload>/XDJRX3/            the ISO tree (lib, usr, gui, pdj/rbp)

    Nothing vendor-supplied is shipped with rb4r5; this only copies what you
    extracted yourself.
    """
    payload = Path(payload or cfg.payload)
    root = cfg.chroot
    notes = []

    rootfs = payload / "XDJRX3-rootfs"
    isotree = payload / "XDJRX3"
    if not rootfs.is_dir():
        raise util.Fail(
            f"{rootfs} not found.\nExtract the XDJ-RX3 firmware first "
            f"(docs/03-payload.md) so that {payload} contains XDJRX3-rootfs/ "
            f"and XDJRX3/.")

    util.ensure_dir(root)
    for sub in ("lib", "usr", "bin", "sbin", "etc"):
        src = rootfs / sub
        if not src.is_dir():
            continue
        dst = root / sub
        if dst.exists() and not force:
            notes.append(f"kept existing {dst}")
            continue
        util.step(f"copying {src} -> {dst}")
        util.run(["cp", "-a", f"{src}/.", str(util.ensure_dir(dst))], timeout=1800)
        notes.append(f"copied {sub}/ from the RX3 rootfs")

    if isotree.is_dir():
        for sub in ("lib", "usr", "gui"):
            src = isotree / sub
            if not src.is_dir():
                continue
            dst = root / ("root/gui" if sub == "gui" else sub)
            util.ensure_dir(dst)
            util.run(["cp", "-a", f"{src}/.", str(dst)], timeout=1800)
            notes.append(f"overlaid {sub}/ from the ISO tree")
        pdj = isotree / "pdj"
        if pdj.is_dir():
            # the whole directory, not just the binary: whatever the firmware's
            # pdj.tar.gz holds sits beside the player on the real device
            util.ensure_dir(root / "root/pdj")
            util.run(["cp", "-a", f"{pdj}/.", str(root / "root/pdj")],
                     timeout=600)
            notes.append("copied pdj/ (the stock player and what sits beside "
                         "it; `launch.py build` patches the binary)")

    # busybox shell, so `chroot ... /bin/sh` works for diagnostics
    if (root / "bin/busybox").exists() and not (root / "bin/sh").exists():
        os.symlink("busybox", root / "bin/sh")
        notes.append("bin/sh -> busybox")

    for sub in ("dev", "proc", "sys", "tmp", "run", "media/usb1/sda1",
                "root/pdj", "usr/lib/directfb-1.4-6/systems"):
        util.ensure_dir(root / sub)

    # POSIX getmntent / the engine's vfs_getfsys need a real mtab
    mtab = root / "etc/mtab"
    if not mtab.is_symlink():
        util.ensure_dir(mtab.parent)
        if mtab.exists():
            mtab.unlink()
        os.symlink("/proc/mounts", mtab)
        notes.append("etc/mtab -> /proc/mounts")

    notes.append(write_directfbrc(cfg))
    return notes


def write_directfbrc(cfg) -> str:
    path = cfg.chroot / "etc/directfbrc"
    content = DIRECTFBRC.format(
        fbdev=cfg.get("display.fbdev", "/dev/fb0"),
        width=cfg.get("display.ui_width", 1280),
        height=cfg.get("display.ui_height", 800),
    )
    changed = util.write_text(path, content)
    return f"{'wrote' if changed else 'kept'} {path}"


def install_runtime_bits(cfg, repo: Path) -> list[str]:
    """Copy the built shims / DirectFB / player into the chroot."""
    root = cfg.chroot
    work = cfg.work
    notes = []

    shim_src = work / "shims"
    for name in ("memshim.so", "fbshim.so", "audioshim.so", "keyshim.so"):
        src = shim_src / name
        # where the PLAYER will open it from, not where the host thinks it is
        dst = inside(root, f"usr/lib/{name}")
        if src.exists():
            util.ensure_dir(dst.parent)
            shutil.copy2(src, dst)
            notes.append(f"installed usr/lib/{name}")
        elif not dst.exists():
            notes.append(f"MISSING {name} (run: launch.py build)")

    dfb = work / "dfb/lib"
    if dfb.is_dir():
        for src in dfb.rglob("*"):
            if src.is_dir():
                continue
            dst = inside(root, f"usr/lib/{src.relative_to(dfb)}")
            util.ensure_dir(dst.parent)
            shutil.copy2(src, dst)
        notes.append("installed the rebuilt DirectFB 1.4.16 stack")

    player = work / "rbp-pi5"
    if player.exists():
        target = inside(root, "root/pdj/rbp")
        util.ensure_dir(target.parent)
        shutil.copy2(player, target)
        os.chmod(target, 0o755)
        notes.append("installed root/pdj/rbp (patched)")
    return notes


# --------------------------------------------------------------------------
# runtime: stubs and mounts
# --------------------------------------------------------------------------
def make_stubs(cfg) -> list[str]:
    """Create the RX3 device stubs inside the chroot (idempotent)."""
    root = cfg.chroot
    dev = util.ensure_dir(root / "dev")
    notes = []

    for name in FIFO_STUBS:
        path = dev / name
        if path.is_fifo():
            continue
        if path.exists():
            path.unlink()
        os.mkfifo(path, 0o666)
        os.chmod(path, 0o666)
        notes.append(f"fifo dev/{name}")
    for name in FILE_STUBS:
        path = dev / name
        if path.is_fifo():
            path.unlink()
        if not path.exists():
            path.write_bytes(b"")
        os.chmod(path, 0o666)
        notes.append(f"file dev/{name}")
    for name in ABSENT:
        path = dev / name
        if path.exists():
            path.unlink()
            notes.append(f"removed dev/{name} (must not exist)")

    # /dev/mem: memshim denies the open, but make the permission bits say no too
    for mem in (Path("/dev/mem"), dev / "mem"):
        if mem.exists():
            try:
                os.chmod(mem, 0o000)
            except OSError:
                pass

    util.ensure_dir(cfg.chroot_media)
    return notes


def make_fifos() -> list[str]:
    """The /tmp FIFOs the shims and daemons talk over."""
    notes = []
    for path in [config.FIFO_KEYS, config.FIFO_CTRL] + config.FIFO_UDEV:
        util.ensure_fifo(path)
        notes.append(Path(path).name)
    # the touch state file, so memshim never reads a missing file
    if not Path(config.TOUCH_STATE).exists():
        Path(config.TOUCH_STATE).write_bytes(bytes(16))
        os.chmod(config.TOUCH_STATE, 0o666)
    return notes


def mount_binds(cfg) -> list[str]:
    root = cfg.chroot
    notes = []
    for name in BIND_MOUNTS:
        target = util.ensure_dir(root / name)
        if util.is_mountpoint(target):
            notes.append(f"{name} already mounted")
            continue
        util.run(["mount", "--bind", f"/{name}", str(target)])
        notes.append(f"bind-mounted /{name}")
    return notes


def umount_binds(cfg) -> list[str]:
    root = cfg.chroot
    notes = []
    # the library bind lives under media/, unmount it before the rest
    for target in [cfg.chroot_media] + [root / name for name in reversed(BIND_MOUNTS)]:
        if util.is_mountpoint(target):
            util.run(["umount", "-l", str(target)], check=False)
            notes.append(f"unmounted {target}")
    return notes


def top_bar_rows(cfg) -> int:
    """How many rows the button bar takes, or 0 when it is off."""
    if not cfg.get("display.top_bar", True) or not cfg.get("overlay.enabled", True):
        return 0
    from . import fb                                    # local: keeps import order simple
    wanted = int(cfg.get("display.top_bar_height", 0) or 0)
    if wanted > 0:
        return wanted
    info = fb.screeninfo(cfg.get("display.fbdev", "/dev/fb0"))
    height = info.get("height", 0)
    return max(48, round(height * 0.08)) if height else 0


def player_env(cfg, audio_env: dict) -> dict:
    """The environment rbp runs with (shims, display, audio, touch)."""
    preload = ":".join(f"/usr/lib/{name}" for name in
                       ("memshim.so", "fbshim.so", "audioshim.so", "keyshim.so"))
    env = {
        "LD_PRELOAD": preload,
        "DFB_ROTATE": str(cfg.get("display.rotate", "off")),
        # read by the patched fbdev driver: how to place and resample the
        # 1280x800 frame on the panel (docs/04-display.md)
        "RB_FB_FIT": str(cfg.get("display.fit", "aspect")),
        "RB_FB_SCALE": str(cfg.get("display.scale", "bilinear")),
        # rows at the top of the panel the player must not draw into: the
        # launcher's button bar lives there (rb4r5/overlay.py)
        "RB_FB_TOP": str(top_bar_rows(cfg)),
        "HOME": "/root",
        "TERM": "linux",
    }
    env.update(audio_env)
    if cfg.get("touch.native"):
        env.update({
            "RB_TOUCH_NATIVE": "1",
            "RB_TOUCH_STATE": config.TOUCH_STATE,
            "RB_TOUCH_FMT": str(cfg.get("touch.native_format", "fxy")),
            "RB_TOUCH_MAXX": str(cfg.get("display.ui_width", 1280)),
            "RB_TOUCH_MAXY": str(cfg.get("display.ui_height", 800)),
        })
    env.update({str(k): str(v) for k, v in (cfg.get("player.env") or {}).items()})
    return env


# --------------------------------------------------------------------------
# is the display driver in the chroot the one we built?
# --------------------------------------------------------------------------
FBDEV_MODULE = "usr/lib/directfb-1.4-6/systems/libdirectfb_fbdev.so"

# Strings the current driver carries in its read-only data.  Cheap, exact
# evidence of which build is installed - md5 alone cannot say *what* is
# missing, and "I rebuilt it" and "the player is loading the rebuild" are not
# the same claim.  The display faults this catches (a 32bpp write into a 16bpp
# framebuffer, a nearest-neighbour stretch) look like hardware problems.
MODULE_MARKERS = {
    "FBDev/rb4r5:": "reads the framebuffer's pixel format instead of assuming it",
    "bilinear":     "can interpolate when scaling (not just nearest neighbour)",
}


def module_report(cfg) -> dict:
    """What display driver is installed, and is it the one we last built?"""
    installed = inside(cfg.chroot, FBDEV_MODULE)
    staged = cfg.work / "dfb/lib/directfb-1.4-6/systems/libdirectfb_fbdev.so"
    out = {"installed": str(installed), "staged": str(staged),
           "present": installed.exists(), "staged_present": staged.exists(),
           "md5": "", "staged_md5": "", "missing": [], "current": False,
           "stale": False}
    if not out["present"]:
        return out
    data = installed.read_bytes()
    out["md5"] = hashlib.md5(data).hexdigest()
    out["missing"] = [name for name in MODULE_MARKERS
                      if name.encode() not in data]
    out["current"] = not out["missing"]
    if out["staged_present"]:
        out["staged_md5"] = hashlib.md5(staged.read_bytes()).hexdigest()
        out["stale"] = out["staged_md5"] != out["md5"]
    return out


def module_lines(cfg) -> list[str]:
    report = module_report(cfg)
    if not report["present"]:
        return [f"MISSING {report['installed']} - the player has no display "
                f"driver (run: launch.py build)"]
    lines = [f"{FBDEV_MODULE}: md5 {report['md5']}"]
    for name, what in MODULE_MARKERS.items():
        lines.append(f"  {'yes' if name not in report['missing'] else 'NO '}"
                     f"  {what}")
    if report["stale"]:
        lines.append(f"  the build at {report['staged']} is a DIFFERENT binary "
                     f"(md5 {report['staged_md5']}) - the install step did not "
                     f"run: launch.py build --fast-directfb")
    return lines


# --------------------------------------------------------------------------
# can the player actually load its shims?
# --------------------------------------------------------------------------
SHIM_NAMES = ["memshim.so", "fbshim.so", "audioshim.so", "keyshim.so"]


def shim_report(cfg) -> list[dict]:
    """Whether each shim is there, is ARM, and can resolve what it needs.

    The whole port rests on these four being preloaded into the player.  When
    one is not, ld.so says so once - into the player's log, where nobody is
    looking - and then runs the player WITHOUT it.  The engine then talks
    straight to hardware it was never meant to see, and what comes back is a
    crash with no obvious connection to a missing file.
    """
    from . import build                                  # local: cycle-free

    root = cfg.chroot
    search = [inside(root, "lib"), inside(root, "usr/lib")]
    out = []
    for name in SHIM_NAMES:
        # the path the player opens, resolved as the chroot resolves it
        path = inside(root, f"usr/lib/{name}")
        entry = {"name": name, "path": str(path), "present": path.exists(),
                 "size": 0, "arm": False, "needed": [], "missing": [],
                 "ok": False}
        if not entry["present"]:
            out.append(entry)
            continue
        entry["size"] = path.stat().st_size
        blob = path.read_bytes()[:20]
        # ELF, 32-bit, little endian, e_machine 40 = ARM
        entry["arm"] = (blob[:4] == b"\x7fELF" and len(blob) >= 20 and
                        blob[4] == 1 and blob[18] == 40)
        entry["needed"] = build.needed_libs(path)
        entry["missing"] = [lib for lib in entry["needed"]
                            if not any((place / lib).exists()
                                       for place in search)]
        entry["ok"] = (entry["present"] and entry["arm"] and entry["size"] > 0
                       and not entry["missing"])
        out.append(entry)
    return out


def shim_lines(cfg) -> list[str]:
    lines = []
    root = cfg.chroot
    for entry in shim_report(cfg):
        hops = trail(root, f"usr/lib/{entry['name']}")
        if not entry["present"]:
            lines.append(f"{entry['name']:14} MISSING from "
                         f"{entry['path']} (run: launch.py build)")
            for hop in hops:
                lines.append(f"{'':14}   /usr/lib is {hop}")
            continue
        state = "ok " if entry["ok"] else "NO "
        detail = f"{entry['size']} bytes"
        if not entry["arm"]:
            detail += ", NOT a 32-bit ARM shared object"
        if entry["missing"]:
            detail += f", needs {', '.join(entry['missing'])} which the "
            detail += "chroot does not have"
        elif entry["needed"]:
            detail += f", needs {', '.join(entry['needed'])}"
        lines.append(f"{state}{entry['name']:14} {detail}")
    return lines


def shims_loadable(cfg) -> tuple[bool, list[str]]:
    report = shim_report(cfg)
    bad = [entry for entry in report if not entry["ok"]]
    return (not bad), [entry["name"] for entry in bad]


ELF_CLASS = {1: "32-bit", 2: "64-bit"}
ELF_DATA = {1: "little-endian", 2: "big-endian"}
ELF_OSABI = {0: "SYSV", 3: "Linux", 9: "FreeBSD"}
ELF_TYPE = {1: "ET_REL", 2: "ET_EXEC", 3: "ET_DYN (shared object)", 4: "ET_CORE"}


def elf_note(path: Path) -> str:
    """Decode the header fields ld.so checks before it will load a file."""
    try:
        blob = path.read_bytes()[:52]
    except OSError as exc:
        return f"unreadable ({exc.strerror})"
    if len(blob) < 52 or blob[:4] != b"\x7fELF":
        return "not an ELF file at all"
    cls, data, version, osabi, abiver = blob[4], blob[5], blob[6], blob[7], blob[8]
    etype, machine = struct.unpack_from("<HH", blob, 16)
    flags = struct.unpack_from("<I", blob, 36)[0]
    eabi = (flags >> 24) & 0xff
    bits = [
        ELF_CLASS.get(cls, f"class {cls}?"),
        ELF_DATA.get(data, f"data {data}?"),
        f"version {version}",
        f"OSABI {ELF_OSABI.get(osabi, osabi)}/{abiver}",
        ELF_TYPE.get(etype, f"type {etype}?"),
        "ARM" if machine == 40 else f"machine {machine}?",
        f"EABI{eabi}",
    ]
    if flags & 0x400:
        bits.append("HARD-FLOAT (will not load)")
    return ", ".join(bits)


def loader_version(path: Path) -> str:
    """Which glibc the chroot's loader is, from the strings inside it."""
    try:
        blob = path.read_bytes()
    except OSError:
        return "unknown"
    for needle in (b"GNU C Library", b"glibc "):
        at = blob.find(needle)
        if at >= 0:
            end = blob.find(b"\x00", at)
            return blob[at:end if end > 0 else at + 120].decode("latin-1")[:120]
    return "unknown"


def shim_probe(cfg) -> list[str]:
    """Ask the chroot's own loader to preload things, and report what it says.

    Static checks say a file is present, is ARM, and needs only libraries that
    exist.  They cannot say whether ld.so will take it - and when it will not,
    it says so once into the player's log and runs the player anyway.

    So this runs the RX3's own loader, inside the chroot, the same way the
    player is started, and asks it to preload four things: each shim, a
    library the runtime already loads, a shim moved to /lib, and a shim named
    without a path.  Which of those fail tells you whether the fault is in our
    files, in one directory, or in preloading at all.
    """
    root = cfg.chroot
    loader = "/lib/ld-linux.so.3"
    lines = [f"chroot {root}", f"loader {loader}",
             f"       {loader_version(inside(root, loader))}", ""]

    lines.append("where each shim really is, resolved as the chroot resolves it:")
    for name in SHIM_NAMES:
        rel = f"usr/lib/{name}"
        path = inside(root, rel)
        for hop in trail(root, rel):
            lines.append(f"    via {hop}")
        try:
            stat = path.lstat()
            lines.append(f"  {name:14} {stat.st_size:8d} bytes  "
                         f"mode {stat.st_mode & 0o7777:04o}  uid {stat.st_uid}"
                         f"  at {path}")
        except OSError as exc:
            lines.append(f"  {name:14} NOT THERE at {path} ({exc.strerror})")
            continue
        lines.append(f"  {'':14} {elf_note(path)}")
        if path != root / rel:
            lines.append(f"  {'':14} the host path {root / rel} is a DIFFERENT "
                         "file - the player cannot see it")

    if not inside(root, loader).exists():
        lines.append(f"\nthere is no {loader} in the chroot, so nothing can "
                     "be loaded at all")
        return lines

    # Something harmless to load them into.  The player is started the same
    # way (the loader invoked explicitly), so this is the real path.
    target = None
    for candidate, args in (("/usr/bin/env", []), ("/bin/true", []),
                            ("/bin/sh", ["-c", ":"]), ("/lib/libc.so.6", [])):
        if inside(root, candidate).exists():
            target = (candidate, args)
            break
    if target is None:
        lines.append("\nthe chroot has no harmless program to load them into, "
                     "so only the paths above can be checked here")
        return lines

    def ask(preload):
        # LD_PRELOAD must be set INSIDE the chroot.  Setting it around the
        # host `chroot` binary makes the HOST loader try to preload an
        # in-chroot path first, and fail, and say so - which is the very
        # message this is here to explain.
        argv, around = chroot_cmd(root, [loader, target[0]] + target[1],
                                  {"LD_PRELOAD": preload})
        proc = util.run(argv, check=False, timeout=15, env=around or None)
        blob = (proc.stdout or "")
        refused = [ln for ln in blob.splitlines() if "cannot be preloaded" in ln]
        if refused:
            reason = refused[0].split("cannot be preloaded", 1)[1]
            return "REFUSED" + reason.replace(": ignored.", "").strip()
        if proc.returncode == 124:
            return "accepted (and kept running)"
        if proc.returncode != 0:
            first = blob.strip().splitlines()
            return f"loader exited {proc.returncode}: " + (
                first[0] if first else "(silent)")
        return "accepted"

    lines.append(f"\nasking the loader to preload things into {target[0]} "
                 "(this is how the player is started):")
    for name in SHIM_NAMES:
        lines.append(f"  {name:24} {ask(f'/usr/lib/{name}')}")

    # The discriminating cases.  If a library the runtime already loads is
    # also refused, nothing is wrong with our shims: preloading itself is not
    # working here, and that is a different repair.
    lines.append("\nand the same question asked three other ways, to say "
                 "where the fault is:")
    for lib in ("libdl.so.2", "libpthread.so.0", "libc.so.6"):
        if inside(root, f"lib/{lib}").exists():
            lines.append(f"  {'/lib/' + lib:24} {ask('/lib/' + lib)}"
                         "   <- already part of the runtime")
            break

    source = inside(root, "usr/lib/memshim.so")
    copy = inside(root, "lib/memshim.so")
    if source.exists() and not copy.exists():
        try:
            shutil.copy2(source, copy)
        except OSError as exc:
            lines.append(f"  could not copy a shim into /lib: {exc}")
        else:
            lines.append(f"  {'/lib/memshim.so':24} {ask('/lib/memshim.so')}"
                         "   <- the same shim, from /lib")
            copy.unlink()
    lines.append(f"  {'memshim.so':24} {ask('memshim.so')}"
                 "   <- by name, no path")

    # ld.so's own account of what it looked for.
    argv, around = chroot_cmd(root, [loader, target[0]] + target[1],
                              {"LD_PRELOAD": "/usr/lib/memshim.so",
                               "LD_DEBUG": "libs,files"})
    proc = util.run(argv, check=False, timeout=15, env=around or None)
    blob = (proc.stdout or "")
    wanted = [ln.strip() for ln in blob.splitlines()
              if "memshim" in ln or "preload" in ln]
    lines.append("\nwhat ld.so says it did (LD_DEBUG):")
    lines.extend(f"  {ln}" for ln in (wanted[:20] or ["(LD_DEBUG said nothing "
                                                     "- this loader was built "
                                                     "without it)"]))
    return lines
