"""The rekordbox USB library watcher.

Presents a real rekordbox stick to the player exactly as the XDJ-RX3 presents
its own USB1 slot, so the native DeviceSQL import runs and the UI shows the
full library (songs, playlists, categories) rather than a folder listing:

    stick plugged in
      -> mount /dev/sdX1 at /media/usb1/sda1 with the RX3's vfat options
      -> bind that into the chroot
      -> write "umount <path>" then "mount <path>" to /tmp/udev_usb1
           |
           v  ui::UsbMountManager::run()  (the player's FIFO reader thread)
           v  ui::DbProxy::reqAttach() -> db::DbIF::mount(path, type=3)
           v  DeviceSQL imports PIONEER/rekordbox/export.pdb
           v  detect flag (kind 2) = 2  ->  the UI shows "USB1 <label>"

Two behaviours are not obvious and both are deliberate (they were found the
hard way on the earlier ports):

  * the umount must precede the mount - the player's PathDecider ignores a
    mount that was not preceded by an unmount;
  * a mount event delivered while the player is still starting is silently
    dropped, so we re-send it until the detect flag reads "ready".

Never write to /tmp/udev_usbctn* - that raises the cosmetic
"USB Error. Remove the device." caution.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from . import config, probe, util

FIFO = config.FIFO_UDEV[0]          # /tmp/udev_usb1
PROBE_MOUNT = Path("/run/rb4r5-probe")

# The options the XDJ-RX3 itself uses for a FAT library stick.
VFAT_OPTS = ("flush,rw,noatime,shortname=mixed,dmask=000,fmask=000,"
             "codepage=437,iocharset=iso8859-1,usefree,utf8")


def _system_disks() -> set[str]:
    """Disks carrying /, /boot or /boot/firmware - never touch these."""
    protect = set()
    for line in util.read_text("/proc/mounts").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        device, mountpoint = parts[0], parts[1]
        if mountpoint in ("/", "/boot", "/boot/firmware") and device.startswith("/dev/"):
            name = Path(device).name
            protect.add(name.rstrip("0123456789") if name.startswith("sd") else name)
            protect.add(name)
    return protect


def candidates() -> list[dict]:
    """Removable USB block devices that could hold a library."""
    protected = _system_disks()
    found = []
    for block in sorted(Path("/sys/block").glob("sd*")):
        name = block.name
        if name in protected or name.rstrip("0123456789") in protected:
            continue
        target = os.path.realpath(str(block))
        if "usb" not in target:
            continue                      # not on a USB bus
        size = util.read_int(block / "size", 0)
        if size <= 0:
            continue                      # card reader with no card
        found.append({
            "name": name,
            "dev": f"/dev/{name}",
            "size_mb": size * 512 // (1024 * 1024),
            "removable": util.read_int(block / "removable", 0) == 1,
            "model": util.read_text(block / "device/model").strip(),
        })
    return found


def _blkid(device: str, tag: str) -> str:
    return util.out(["blkid", "-s", tag, "-o", "value", device])


def partition_of(disk: str, wait: float = 2.0) -> str | None:
    """First partition of a disk, or the whole disk if it holds a filesystem."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        for suffix in ("1", "2"):
            candidate = f"{disk}{suffix}"
            if Path(candidate).exists() and _blkid(candidate, "TYPE"):
                return candidate
        if _blkid(disk, "TYPE"):
            return disk
        time.sleep(0.2)
    return None


def _mounted_at(device: str) -> str | None:
    for line in util.read_text("/proc/mounts").splitlines():
        parts = line.split()
        if len(parts) > 1 and parts[0] == device:
            return parts[1]
    return None


def has_rekordbox_db(partition: str) -> bool:
    """Look for PIONEER/rekordbox/export.pdb without disturbing anything."""
    existing = _mounted_at(partition)
    if existing:
        return Path(existing, "PIONEER/rekordbox/export.pdb").exists()
    util.ensure_dir(PROBE_MOUNT)
    proc = util.run(["mount", "-o", "ro", partition, str(PROBE_MOUNT)], check=False)
    if proc.returncode != 0:
        return False
    try:
        return Path(PROBE_MOUNT, "PIONEER/rekordbox/export.pdb").exists()
    finally:
        util.run(["umount", str(PROBE_MOUNT)], check=False)


def pick() -> dict | None:
    """The stick to present: a real rekordbox library wins, else any filesystem."""
    fallback = None
    for disk in candidates():
        partition = partition_of(disk["dev"])
        if not partition:
            continue
        disk["partition"] = partition
        disk["fstype"] = _blkid(partition, "TYPE")
        disk["label"] = _blkid(partition, "LABEL")
        if not disk["fstype"]:
            continue
        if has_rekordbox_db(partition):
            disk["rekordbox"] = True
            return disk
        if fallback is None:
            disk["rekordbox"] = False
            fallback = disk
    return fallback


# --------------------------------------------------------------------------
def notify(message: str, log=util.info) -> bool:
    """Send one line to the player's udev FIFO, never blocking."""
    if not Path(FIFO).is_fifo():
        log(f"usb: {FIFO} missing (the player creates it)")
        return False
    try:
        fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        # ENXIO: no reader.  The player is down or not at that stage yet.
        return False
    try:
        os.write(fd, message.encode())
        log(f"usb: notified '{message}'")
        return True
    except OSError as exc:
        log(f"usb: notify failed ({exc})")
        return False
    finally:
        os.close(fd)


def mount_stick(cfg, disk: dict) -> bool:
    media = cfg.media
    util.ensure_dir(media)
    if util.is_mountpoint(media):
        util.info(f"usb: {media} already mounted, refreshing the bind only")
    else:
        fstype = disk.get("fstype") or "vfat"
        if fstype == "vfat":
            cmd = ["mount", "-t", "vfat", "-o", VFAT_OPTS, disk["partition"], str(media)]
        elif fstype == "exfat":
            cmd = ["mount", "-t", "exfat", "-o", "rw,noatime",
                   disk["partition"], str(media)]
        elif fstype == "hfsplus":
            cmd = ["mount", "-t", "hfsplus", "-o", "force,rw,noatime",
                   disk["partition"], str(media)]
        else:
            cmd = ["mount", disk["partition"], str(media)]
        proc = util.run(cmd, check=False)
        if proc.returncode != 0:
            util.warn(f"usb: mounting {disk['partition']} failed: "
                      f"{(proc.stdout or '').strip()}")
            return False
        util.ok(f"usb: mounted {disk['partition']} ({fstype}"
                f"{', label ' + disk['label'] if disk.get('label') else ''}) "
                f"-> {media}")

    bind = cfg.chroot_media
    util.ensure_dir(bind)
    if not util.is_mountpoint(bind):
        proc = util.run(["mount", "--bind", str(media), str(bind)], check=False)
        if proc.returncode != 0:
            util.warn(f"usb: binding into the chroot failed: {proc.stdout}")
            return False
        util.info(f"usb: bound into the chroot at {bind}")
    return True


def unmount_stick(cfg) -> None:
    notify(f"umount {cfg.media}")
    time.sleep(1.0)
    for target in (cfg.chroot_media, cfg.media):
        if util.is_mountpoint(target):
            util.run(["umount", "-l", str(target)], check=False)
            util.info(f"usb: unmounted {target}")


def announce(cfg) -> None:
    """umount-then-mount, which is the sequence the player accepts."""
    notify(f"umount {cfg.media}")
    time.sleep(0.3)
    notify(f"mount {cfg.media}")


def run(cfg, once: bool = False) -> int:
    """Watch for sticks until stopped (this is what the supervisor runs)."""
    poll = float(cfg.get("usb.poll", 1.0))
    tries_max = int(cfg.get("usb.confirm_tries", 18))
    interval = float(cfg.get("usb.confirm_interval", 5.0))

    current = None
    last_set = None
    last_pid = probe.player_pid()
    tries_left = 0
    next_try = 0.0
    util.info("usb: watching for a rekordbox stick")

    while True:
        present = candidates()
        signature = tuple(sorted(d["name"] for d in present))
        if signature != last_set:
            last_set = signature
            util.info(f"usb: storage devices: {', '.join(signature) or 'none'}")
            chosen = pick()
        else:
            chosen = current if current and any(
                d["name"] == current["name"] for d in present) else pick()

        pid = probe.player_pid()

        if chosen:
            if not current or chosen["name"] != current["name"]:
                if current:
                    unmount_stick(cfg)
                util.info(f"usb: attaching {chosen['name']} "
                          f"({chosen['size_mb']} MB"
                          f"{', rekordbox library' if chosen.get('rekordbox') else ''})")
                if mount_stick(cfg, chosen):
                    current = chosen
                    announce(cfg)
                    tries_left, next_try = tries_max, time.monotonic() + interval
                else:
                    current = None
                    tries_left = 0
            elif pid and pid != last_pid:
                util.info(f"usb: the player restarted ({last_pid} -> {pid}), "
                          "re-announcing the stick")
                time.sleep(2.0)
                announce(cfg)
                tries_left, next_try = tries_max, time.monotonic() + interval
            elif tries_left > 0 and pid and time.monotonic() >= next_try:
                if probe.usb1_ready(pid):
                    util.ok("usb: the player reports the USB1 database ready")
                    tries_left = 0
                else:
                    tries_left -= 1
                    next_try = time.monotonic() + interval
                    util.info(f"usb: not imported yet, re-announcing "
                              f"({tries_left} tries left)")
                    announce(cfg)
        elif current:
            util.info("usb: stick removed")
            unmount_stick(cfg)
            current = None
            tries_left = 0

        last_pid = pid
        if once:
            return 0
        time.sleep(poll)


def describe(cfg) -> list[str]:
    lines = []
    found = candidates()
    for disk in found:
        partition = partition_of(disk["dev"], wait=0.2) or "-"
        lines.append(f"{disk['dev']} {disk['size_mb']:>8} MB  "
                     f"{disk['model'][:20]:20} part={partition} "
                     f"fs={_blkid(partition, 'TYPE') if partition != '-' else '?'} "
                     f"label={_blkid(partition, 'LABEL') if partition != '-' else ''}")
    if not found:
        lines.append("no USB storage devices (the library stick is optional; "
                     "the player runs without one)")
    chosen = pick()
    lines.append(f"would use: {chosen['partition']} "
                 f"({'rekordbox library' if chosen.get('rekordbox') else 'folder view'})"
                 if chosen else "would use: nothing")
    lines.append(f"host mount:  {cfg.media} "
                 f"({'mounted' if util.is_mountpoint(cfg.media) else 'not mounted'})")
    lines.append(f"chroot bind: {cfg.chroot_media} "
                 f"({'mounted' if util.is_mountpoint(cfg.chroot_media) else 'not mounted'})")
    return lines
