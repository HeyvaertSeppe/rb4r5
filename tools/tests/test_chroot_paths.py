#!/usr/bin/env python3
"""Paths, resolved the way the chroot resolves them.

A chroot's absolute symlinks point at the chroot's own root.  Python's path
handling does not know that, so `root / "usr/lib/fbshim.so"` can follow
`usr/lib -> /lib` clean out of the runtime and land on a HOST file.  When that
happens a shim is installed outside the chroot, passes every check that looks
from the host, and is still invisible to the player - which reports only
"cannot be preloaded" and then runs without it.

That is a silent, total failure, so the resolver gets its own test.

Run:  python3 tools/tests/test_chroot_paths.py
"""
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from rb4r5 import chroot  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(label)
        print(f"FAIL {label}\n     got  {got!r}\n     want {want!r}")
    else:
        print(f"ok   {label}")


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    root = tmp / "chroot"
    (root / "lib").mkdir(parents=True)
    (root / "usr").mkdir()
    (root / "real/pdj").mkdir(parents=True)

    # a host /lib that must never be touched
    host_lib = tmp / "hostlib"
    host_lib.mkdir()

    # the shape that breaks it: usr/lib is an ABSOLUTE symlink
    os.symlink("/lib", root / "usr/lib")
    (root / "lib/fbshim.so").write_bytes(b"real")

    check("an absolute symlink re-roots at the chroot, not the host",
          chroot.inside(root, "usr/lib/fbshim.so"), root / "lib/fbshim.so")
    check("and the naive path is a different file",
          (root / "usr/lib/fbshim.so").resolve() != root / "lib/fbshim.so",
          True)
    check("the trail says which symlink did it",
          chroot.trail(root, "usr/lib/fbshim.so"),
          [f"{root / 'usr/lib'} -> /lib"])

    # relative symlinks resolve against the link's own directory
    os.symlink("../real/pdj", root / "pdj")
    (root / "real/pdj/rbp").write_bytes(b"player")
    check("a relative symlink resolves beside the link",
          chroot.inside(root, "pdj/rbp"), root / "real/pdj/rbp")

    # a symlink to a symlink
    os.symlink("/usr/lib", root / "libs")
    check("symlinks chain", chroot.inside(root, "libs/fbshim.so"),
          root / "lib/fbshim.so")

    # .. can never climb out of the chroot
    check("..  stops at the root", chroot.inside(root, "../../../etc/passwd"),
          root / "etc/passwd")
    os.symlink("../../../../hostlib", root / "escape")
    got = chroot.inside(root, "escape/secret")
    check("a symlink cannot climb out either", str(got).startswith(str(root)),
          True)

    # a path that does not exist still resolves, so callers can create it
    check("a missing leaf still resolves",
          chroot.inside(root, "usr/lib/newshim.so"), root / "lib/newshim.so")

    # a loop must end, not hang
    os.symlink("loop_b", root / "loop_a")
    os.symlink("loop_a", root / "loop_b")
    got = chroot.inside(root, "loop_a")
    check("a symlink loop terminates", isinstance(got, Path), True)

    # no symlinks at all: the plain case must be untouched
    plain = tmp / "plain"
    (plain / "usr/lib").mkdir(parents=True)
    check("a plain tree resolves to itself",
          chroot.inside(plain, "usr/lib/memshim.so"),
          plain / "usr/lib/memshim.so")
    check("and reports no symlinks", chroot.trail(plain, "usr/lib/memshim.so"),
          [])

    # --- the ELF header ld.so checks before it will load anything ---------
    def elf(machine=40, cls=1, data=1, etype=3, flags=0x05000000, osabi=0):
        import struct as _s
        blob = bytearray(52)
        blob[0:4] = b"\x7fELF"
        blob[4], blob[5], blob[6], blob[7], blob[8] = cls, data, 1, osabi, 0
        _s.pack_into("<HH", blob, 16, etype, machine)
        _s.pack_into("<I", blob, 36, flags)
        return bytes(blob)

    shim = tmp / "shim.so"
    shim.write_bytes(elf())
    note = chroot.elf_note(shim)
    check("a good shim reads as 32-bit ARM EABI5 shared object",
          note, "32-bit, little-endian, version 1, OSABI SYSV/0, "
                "ET_DYN (shared object), ARM, EABI5")

    shim.write_bytes(elf(flags=0x05000400))
    check("hard-float is called out", "HARD-FLOAT" in chroot.elf_note(shim), True)

    shim.write_bytes(elf(cls=2))
    check("a 64-bit object is called out",
          "64-bit" in chroot.elf_note(shim), True)

    shim.write_bytes(elf(machine=62))
    check("the wrong machine is called out",
          "machine 62?" in chroot.elf_note(shim), True)

    shim.write_bytes(b"#!/bin/sh\necho not an elf\n" + b"\0" * 40)
    check("a non-ELF file is called out",
          chroot.elf_note(shim), "not an ELF file at all")

    shim.write_bytes(b"\x7fELF")
    check("a truncated file is called out",
          chroot.elf_note(shim), "not an ELF file at all")


    # --- the environment must be set inside the chroot, not around it -----
    #
    # `chroot` is a HOST binary.  LD_PRELOAD in its environment is read by the
    # HOST's loader, before chroot() happens, so it chases in-chroot paths on
    # the host, fails, and logs a preload error for every shim - which reads
    # exactly like the shims having failed, while they load fine a moment
    # later.  That cost a long detour once; it gets a test.
    envroot = tmp / "envroot"
    (envroot / "usr/bin").mkdir(parents=True)
    (envroot / "usr/bin/env").write_bytes(b"\x7fELF")

    cmd, around = chroot.chroot_cmd(
        envroot, ["/lib/ld-linux.so.3", "/root/pdj/rbp"],
        {"LD_PRELOAD": "/usr/lib/memshim.so", "HOME": "/root"})
    check("nothing is left to set around the host process", around, {})
    check("the env runs inside the chroot", cmd[:3],
          ["chroot", str(envroot), "/usr/bin/env"])
    check("every variable is passed as an argument, after the chroot",
          cmd[3:5], ["LD_PRELOAD=/usr/lib/memshim.so", "HOME=/root"])
    check("the program still comes last",
          cmd[-2:], ["/lib/ld-linux.so.3", "/root/pdj/rbp"])
    check("LD_PRELOAD is never left where the host loader reads it",
          any(part.startswith("LD_PRELOAD=") for part in cmd[:2]), False)

    check("no env means no env binary in the way",
          chroot.chroot_cmd(envroot, ["/bin/sh"]),
          (["chroot", str(envroot), "/bin/sh"], {}))

    # a runtime without /usr/bin/env still has to start
    bare = tmp / "bare"
    bare.mkdir()
    fallback, around = chroot.chroot_cmd(bare, ["/bin/sh"], {"HOME": "/root"})
    check("without /usr/bin/env it falls back rather than failing",
          fallback, ["chroot", str(bare), "/bin/sh"])
    check("and hands the environment back so it is not silently dropped",
          around, {"HOME": "/root"})

    # and the supervisor must actually use it
    sup = (REPO / "rb4r5/supervisor.py").read_text()
    check("the supervisor builds the player command with it",
          "chroot.chroot_cmd(" in sup and "rbp_argv" in sup, True)
    check("and gives the host process only what could not go inside",
          "env=rbp_around" in sup, True)
    check("and no longer hands it the player environment directly",
          "env=player_env" in sup, False)


print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("chroot paths resolve the way the player sees them")
