#!/usr/bin/env python3
"""Seeing a bind mount that os.path.ismount() cannot.

os.path.ismount() decides by comparing st_dev with the parent's.  A bind
mount whose source is on the SAME filesystem has the parent's device, so it
says no - and /tmp bound onto <chroot>/tmp is exactly that.  The launcher
believed nothing was mounted, mounted it again on every run, never unmounted
it, and eventually the kernel refused:

    mount: /opt/rb4r5/chroot/tmp: mount(2) system call failed:
           No space left on device.

which for mount(2) is the mount table, not the disk.

Run:  python3 tools/tests/test_mounts.py
"""
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from rb4r5 import util  # noqa: E402

failures = []


def check(label, got, want=True):
    if got != want:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label}")


def mountinfo(*points) -> str:
    """A /proc/self/mountinfo with these mount points, in kernel format."""
    lines = []
    for index, point in enumerate(points):
        escaped = point.replace("\\", "\\134").replace(" ", "\\040")
        lines.append(f"{index + 20} 1 0:{index + 30} / {escaped} rw,relatime "
                     f"shared:1 - ext4 /dev/root rw")
    return "\n".join(lines) + "\n"


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)

    info = tmp / "mountinfo"
    info.write_text(mountinfo("/", "/proc", "/opt/rb4r5/chroot/tmp"))
    check("the mount points are read out of the kernel's table",
          util.mount_points(str(info)),
          ["/", "/proc", "/opt/rb4r5/chroot/tmp"])
    check("a bind mount is counted once",
          util.mount_count("/opt/rb4r5/chroot/tmp", str(info)), 1)
    check("something not mounted counts zero",
          util.mount_count("/opt/rb4r5/chroot/dev", str(info)), 0)

    # the failure this test exists for: the same target, over and over
    info.write_text(mountinfo("/", *["/opt/rb4r5/chroot/tmp"] * 240))
    check("stacked mounts are all counted",
          util.mount_count("/opt/rb4r5/chroot/tmp", str(info)), 240)

    # a mount point with a space in it is escaped in the table
    info.write_text(mountinfo("/media/My Stick"))
    check("an escaped mount point is decoded",
          util.mount_points(str(info)), ["/media/My Stick"])
    check("and can still be counted",
          util.mount_count("/media/My Stick", str(info)), 1)

    # a trailing slash and a relative path must still match
    info.write_text(mountinfo("/opt/rb4r5/chroot/tmp"))
    check("a trailing slash does not hide a mount",
          util.mount_count("/opt/rb4r5/chroot/tmp/", str(info)), 1)

    check("a missing table is empty rather than an error",
          util.mount_points(str(tmp / "nope")), [])

    # and the real thing: / is always mounted, whatever ismount() thinks
    check("the real table sees the root filesystem",
          util.is_mountpoint("/"), True)
    check("and does not invent one", util.is_mountpoint(tmp / "not-a-mount"),
          False)

    # --- keeping the disk from filling ---------------------------------
    #
    # A full disk does not report itself.  It truncates whatever is written
    # next, and what got truncated here was a source file and a git object.
    big = tmp / "big.log"
    big.write_bytes(b"line of log\n" * 200_000)          # ~2.3 MB
    before = big.stat().st_size
    note = util.cap_file(big, 100_000)
    check("an oversized log is trimmed", bool(note), True)
    check("and is smaller afterwards", big.stat().st_size < before, True)
    check("but not empty", big.stat().st_size > 1000, True)
    check("it says what was dropped",
          big.read_bytes().startswith(b"--- earlier lines dropped"), True)
    check("and it still ends on whole lines",
          big.read_bytes().endswith(b"line of log\n"), True)

    small = tmp / "small.log"
    small.write_bytes(b"short\n")
    check("a small log is left alone", util.cap_file(small, 100_000), "")
    check("and is untouched", small.read_bytes(), b"short\n")

    check("a missing file is not an error", util.cap_file(tmp / "nope", 10), "")

    shots = tmp / "shots"
    shots.mkdir()
    for index in range(10):
        shot = shots / f"shot{index}.png"
        shot.write_bytes(b"x" * 100)
        os.utime(shot, (index, index))
    note = util.prune_files(shots, 4)
    check("old screenshots are pruned", len(list(shots.iterdir())), 4)
    check("the newest are the ones kept",
          sorted(f.name for f in shots.iterdir()),
          ["shot6.png", "shot7.png", "shot8.png", "shot9.png"])
    check("and it says how much it freed", "freeing" in note, True)
    check("pruning again does nothing", util.prune_files(shots, 4), "")

    check("free space is reported", util.free_bytes(tmp) > 0, True)
    check("and for a path that is not there it is -1",
          util.free_bytes("/definitely/not/here"), -1)


print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("bind mounts are visible, and stacked ones are countable")
