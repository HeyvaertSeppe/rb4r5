"""Read the player's live state out of its own memory.

Addresses are from the XDJ-RX3 v1.20 binary (the same image the Prime GO and
Chromebit ports use), so they are stable across those projects:

    media kind 2 ("USB 1"): detect flag 0x03256888, property block 0x0325688c
    media kind 3 ("USB 2"): detect flag 0x03256944, property block 0x03256948
    uiConnectedMedia        0x0326f8b4   (bit 1 = USB1)
    uiBrowse (browse mode)  0x0326f8b8

detect flag: 0 absent, 1 analysing, 2 ready (the UI shows the drive).
This is how usbwatch knows whether DeviceSQL finished importing export.pdb.
"""
from __future__ import annotations

import os
import struct
from pathlib import Path

from . import util

ADDR_KINDS = [
    (2, 0x03256888, 0x0325688C, "USB1"),
    (3, 0x03256944, 0x03256948, "USB2"),
]
ADDR_CONNECTED_MEDIA = 0x0326F8B4
ADDR_UI_BROWSE = 0x0326F8B8
PROP_SIZE = 168


def player_pid() -> int | None:
    pids = util.pgrep_arg("/root/pdj/rbp")
    return pids[0] if pids else None


class Mem:
    def __init__(self, pid: int):
        self.pid = pid
        self.path = f"/proc/{pid}/mem"

    def read(self, addr: int, size: int) -> bytes | None:
        try:
            with open(self.path, "rb", buffering=0) as handle:
                handle.seek(addr)
                data = handle.read(size)
        except (OSError, ValueError):
            return None
        return data if data and len(data) == size else None

    def u32(self, addr: int) -> int | None:
        data = self.read(addr, 4)
        return struct.unpack("<I", data)[0] if data else None


def media_state(pid: int | None = None) -> dict:
    """Per-media-kind detection state, as the engine sees it."""
    pid = pid or player_pid()
    if not pid:
        return {"pid": None, "kinds": [], "error": "the player is not running"}
    if os.geteuid() != 0:
        return {"pid": pid, "kinds": [], "error": "reading the player's memory needs root"}

    mem = Mem(pid)
    kinds = []
    for kind, detect_addr, prop_addr, name in ADDR_KINDS:
        detect = mem.u32(detect_addr)
        prop = mem.read(prop_addr, PROP_SIZE)
        row = {"kind": kind, "name": name, "detect": detect,
               "state": {0: "absent", 1: "analysing", 2: "ready"}.get(detect, "?")}
        if prop:
            label = prop[0:64].decode("utf-16-le", "ignore").split("\x00")[0]
            songs = struct.unpack("<I", prop[120:124])[0]
            db_ready = prop[126]
            playlists = struct.unpack("<I", prop[128:132])[0]
            cap_hi, cap_lo = struct.unpack("<II", prop[132:140])
            free_hi, free_lo = struct.unpack("<II", prop[140:148])
            row.update({
                "label": label,
                "songs": songs,
                "playlists": playlists,
                "db_ready": db_ready,
                "capacity_gb": ((cap_hi << 32) | cap_lo) / 1e9,
                "free_gb": ((free_hi << 32) | free_lo) / 1e9,
            })
        kinds.append(row)
    return {
        "pid": pid,
        "kinds": kinds,
        "connected_media": mem.u32(ADDR_CONNECTED_MEDIA),
        "ui_browse": mem.u32(ADDR_UI_BROWSE),
        "error": None,
    }


def usb1_ready(pid: int | None = None) -> bool:
    state = media_state(pid)
    for row in state["kinds"]:
        if row["kind"] == 2:
            return row["detect"] == 2
    return False


def describe(cfg) -> list[str]:
    state = media_state()
    if state["error"]:
        return [state["error"]]
    lines = [f"player pid {state['pid']}"]
    media = cfg.media
    if util.is_mountpoint(media):
        device = ""
        for line in util.read_text("/proc/mounts").splitlines():
            parts = line.split()
            if len(parts) > 1 and parts[1] == str(media):
                device = f"{parts[0]} ({parts[2]})"
        lines.append(f"host mount:  {media} <- {device}")
        pdb = Path(media, "PIONEER/rekordbox/export.pdb")
        lines.append(f"export.pdb:  {pdb.stat().st_size} bytes" if pdb.exists()
                     else "export.pdb:  absent (the UI will show FOLDER view)")
    else:
        lines.append(f"host mount:  {media} not mounted")
    lines.append(f"chroot bind: "
                 f"{'yes' if util.is_mountpoint(cfg.chroot_media) else 'no'}")
    for row in state["kinds"]:
        extra = ""
        if "label" in row:
            extra = (f" label={row['label']!r} songs={row['songs']} "
                     f"playlists={row['playlists']} db_ready={row['db_ready']} "
                     f"cap={row['capacity_gb']:.1f}GB free={row['free_gb']:.1f}GB")
        lines.append(f"kind {row['kind']} {row['name']}: "
                     f"detect={row['detect']} ({row['state']}){extra}")
    connected = state["connected_media"]
    if connected is not None:
        lines.append(f"uiConnectedMedia = 0x{connected:x} "
                     f"(bit1 USB1: {'set' if connected & 2 else 'clear'})   "
                     f"uiBrowse = {state['ui_browse']}")
        if not connected & 2:
            lines.append("note: the USB1 bit is only recomputed on the SOURCE "
                         "screen (uiBrowse == 12); it reads 0 on the deck view "
                         "even when the drive is ready")
    return lines
