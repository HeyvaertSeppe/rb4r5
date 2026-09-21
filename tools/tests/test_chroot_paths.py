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

print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("chroot paths resolve the way the player sees them")
