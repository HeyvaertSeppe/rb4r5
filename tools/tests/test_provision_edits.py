#!/usr/bin/env python3
"""Offline checks that boot-config edits are idempotent and reversible.

Run:  python3 tools/tests/test_provision_edits.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from rb4r5 import config, platform5, provision  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label}")


with tempfile.TemporaryDirectory() as tmp:
    boot = Path(tmp)
    (boot / "config.txt").write_text("# original\narm_64bit=1\ndtparam=audio=on\n")
    (boot / "cmdline.txt").write_text(
        "console=serial0,115200 console=tty1 root=PARTUUID=abc rootfstype=ext4 "
        "fsck.repair=yes loglevel=8 rootwait quiet splash\n")
    platform5.boot_dir = lambda: boot

    cfg = config.load("/nonexistent-rb4r5.json")
    cfg.set("display.force_mode", "1920x1080@60")

    provision.config_txt(cfg)
    first = (boot / "config.txt").read_text()
    check("config.txt keeps the original lines", "arm_64bit=1" in first, True)
    check("config.txt gains the KMS overlay", "dtoverlay=vc4-kms-v3d" in first, True)
    check("config.txt made a backup", (boot / "config.txt.rb4r5.bak").exists(), True)
    # the lines that could black-screen a boot must not be there
    for risky in ("disable_fw_kms_setup", "max_framebuffers", "hdmi_cvt",
                  "hdmi_group", "hdmi_force_hotplug"):
        check(f"config.txt does not touch {risky}", risky in first, False)

    provision.config_txt(cfg)
    check("config.txt is idempotent", (boot / "config.txt").read_text(), first)
    check("only one marked block", first.count(provision.BEGIN), 1)

    # running setup repeatedly must never lose the overlay it added
    for _ in range(3):
        provision.config_txt(cfg)
    repeated = (boot / "config.txt").read_text()
    check("repeated runs keep the KMS overlay",
          "dtoverlay=vc4-kms-v3d" in repeated, True)
    check("and still only one block", repeated.count(provision.BEGIN), 1)

    # an existing KMS overlay is respected instead of duplicated
    (boot / "config.txt").write_text(
        "arm_64bit=1\ndtoverlay=vc4-kms-v3d,cma-512\n")
    provision.config_txt(cfg)
    with_existing = (boot / "config.txt").read_text()
    check("an existing vc4 overlay is left alone",
          with_existing.count("dtoverlay=vc4-kms-v3d"), 1)
    check("and is acknowledged",
          "already configured" in with_existing, True)
    (boot / "config.txt").write_text("# original\narm_64bit=1\n")
    provision.config_txt(cfg)

    provision.cmdline_txt(cfg)
    cmdline = (boot / "cmdline.txt").read_text().split()
    check("the mode is forced the way KMS understands it",
          "video=HDMI-A-1:1920x1080@60D" in cmdline, True)
    check("cmdline gains consoleblank=0", "consoleblank=0" in cmdline, True)
    check("cmdline gains logo.nologo", "logo.nologo" in cmdline, True)
    check("cmdline loglevel replaced", [t for t in cmdline
                                        if t.startswith("loglevel")], ["loglevel=3"])
    check("cmdline keeps root=", any(t.startswith("root=") for t in cmdline), True)
    check("cmdline stays one line",
          len((boot / "cmdline.txt").read_text().strip().splitlines()), 1)

    provision.cmdline_txt(cfg)
    tokens = (boot / "cmdline.txt").read_text().split()
    check("cmdline is idempotent", tokens.count("consoleblank=0"), 1)
    check("and the mode is not repeated",
          len([t for t in tokens if t.startswith("video=")]), 1)

    # changing the mode replaces it rather than stacking another one
    cfg.set("display.force_mode", "1280x800@60")
    provision.cmdline_txt(cfg)
    tokens = (boot / "cmdline.txt").read_text().split()
    check("a changed mode replaces the old one",
          [t for t in tokens if t.startswith("video=")],
          ["video=HDMI-A-1:1280x800@60D"])

    provision.config_txt(cfg, undo=True)
    provision.cmdline_txt(cfg, undo=True)
    undone = (boot / "config.txt").read_text()
    check("undo removes our block", provision.BEGIN in undone, False)
    check("undo keeps the original", "arm_64bit=1" in undone, True)
    check("undo cleans cmdline",
          "consoleblank=0" in (boot / "cmdline.txt").read_text(), False)
    check("undo removes the forced mode",
          "video=HDMI-A-1" in (boot / "cmdline.txt").read_text(), False)
    check("undo keeps root= in cmdline",
          "root=PARTUUID=abc" in (boot / "cmdline.txt").read_text(), True)

print("\n== the shims are checked before the player is started")
import tempfile                                               # noqa: E402
from rb4r5 import build as buildmod                           # noqa: E402
from rb4r5 import chroot as chrootmod                          # noqa: E402

# a minimal 32-bit ARM ELF header: 0x7fELF, class 1, little endian, machine 40
ARM_ELF = bytearray(64)
ARM_ELF[0:4] = b"\x7fELF"
ARM_ELF[4] = 1          # 32-bit
ARM_ELF[5] = 1          # little endian
ARM_ELF[16] = 3         # ET_DYN
ARM_ELF[18] = 40        # EM_ARM

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "usr/lib").mkdir(parents=True)
    (root / "lib").mkdir()
    (root / "lib/libc.so.6").write_bytes(b"x")

    cfg = config.load("/nonexistent-shim.json")
    cfg.set("paths.chroot", str(root))

    needed = {}
    buildmod.needed_libs = lambda so: needed.get(Path(so).name, [])

    for name in chrootmod.SHIM_NAMES:
        (root / "usr/lib" / name).write_bytes(bytes(ARM_ELF))
    needed.update({name: ["libc.so.6"] for name in chrootmod.SHIM_NAMES})

    ok, bad = chrootmod.shims_loadable(cfg)
    check("all four present and resolvable", (ok, bad), (True, []))

    # one needing a library the chroot does not have
    needed["audioshim.so"] = ["libc.so.6", "librt.so.1"]
    ok, bad = chrootmod.shims_loadable(cfg)
    check("a missing dependency is caught", (ok, bad), (False, ["audioshim.so"]))
    check("and it is named in the report",
          any("librt.so.1" in line and "audioshim" in line
              for line in chrootmod.shim_lines(cfg)), True)
    needed["audioshim.so"] = ["libc.so.6"]

    # one that is not an ARM object at all
    (root / "usr/lib/fbshim.so").write_bytes(b"#!/bin/sh\necho no\n")
    ok, bad = chrootmod.shims_loadable(cfg)
    check("a non-ARM file is caught", (ok, bad), (False, ["fbshim.so"]))
    check("and says so",
          any("NOT a 32-bit ARM" in line for line in chrootmod.shim_lines(cfg)),
          True)
    (root / "usr/lib/fbshim.so").write_bytes(bytes(ARM_ELF))

    # one missing entirely
    (root / "usr/lib/keyshim.so").unlink()
    ok, bad = chrootmod.shims_loadable(cfg)
    check("a missing shim is caught", (ok, bad), (False, ["keyshim.so"]))
    check("and says where it should be",
          any("MISSING" in line and "keyshim" in line
              for line in chrootmod.shim_lines(cfg)), True)

print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("all provisioning edit checks passed")


# ------------------------------------------------- the shims must be loadable
# The whole port rests on four .so files being preloaded into the player.  If
# one is not, ld.so says so once into the player's log and runs it WITHOUT the
# shim - the engine then talks to real hardware and dies in a way that looks
# like anything but a missing file.  So it is checked, before the player runs.
