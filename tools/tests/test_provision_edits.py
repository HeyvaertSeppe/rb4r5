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
    check("config.txt forces the mode", "hdmi_cvt:0=1920 1080 60 3 0 0 0" in first, True)
    check("config.txt made a backup", (boot / "config.txt.rb4r5.bak").exists(), True)

    provision.config_txt(cfg)
    check("config.txt is idempotent", (boot / "config.txt").read_text(), first)
    check("only one marked block", first.count(provision.BEGIN), 1)

    cfg.set("display.force_mode", None)
    provision.config_txt(cfg)
    second = (boot / "config.txt").read_text()
    check("mode force removed when unset", "hdmi_cvt" in second, False)
    check("block replaced, not appended", second.count(provision.BEGIN), 1)

    provision.cmdline_txt(cfg)
    cmdline = (boot / "cmdline.txt").read_text().split()
    check("cmdline gains consoleblank=0", "consoleblank=0" in cmdline, True)
    check("cmdline gains logo.nologo", "logo.nologo" in cmdline, True)
    check("cmdline loglevel replaced", [t for t in cmdline
                                        if t.startswith("loglevel")], ["loglevel=3"])
    check("cmdline keeps root=", any(t.startswith("root=") for t in cmdline), True)
    check("cmdline stays one line",
          len((boot / "cmdline.txt").read_text().strip().splitlines()), 1)

    provision.cmdline_txt(cfg)
    check("cmdline is idempotent",
          (boot / "cmdline.txt").read_text().split().count("consoleblank=0"), 1)

    provision.config_txt(cfg, undo=True)
    provision.cmdline_txt(cfg, undo=True)
    undone = (boot / "config.txt").read_text()
    check("undo removes our block", provision.BEGIN in undone, False)
    check("undo keeps the original", "arm_64bit=1" in undone, True)
    check("undo cleans cmdline",
          "consoleblank=0" in (boot / "cmdline.txt").read_text(), False)
    check("undo keeps root= in cmdline",
          "root=PARTUUID=abc" in (boot / "cmdline.txt").read_text(), True)

print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("all provisioning edit checks passed")
