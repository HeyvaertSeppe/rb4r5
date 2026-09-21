"""evdev discovery: which /dev/input/event* is the touchscreen, the keyboard.

Pure ioctl + /proc parsing, no python-evdev dependency (Raspberry Pi OS does
not ship it by default and the launcher must work on a stock image).
"""
from __future__ import annotations

import fcntl
import os
import struct
from pathlib import Path

from . import util

# ---- evdev constants ----------------------------------------------------
EV_SYN, EV_KEY, EV_REL, EV_ABS = 0x00, 0x01, 0x02, 0x03
SYN_REPORT = 0
ABS_X, ABS_Y, ABS_PRESSURE = 0x00, 0x01, 0x18
ABS_MT_SLOT = 0x2F
ABS_MT_POSITION_X, ABS_MT_POSITION_Y = 0x35, 0x36
ABS_MT_TRACKING_ID = 0x39
BTN_TOUCH = 0x14A
KEY_A, KEY_Z, KEY_ENTER = 30, 44, 28

# struct input_event: timeval + type + code + value (size depends on the arch)
EVENT_FMT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FMT)


def _ioc_read(type_char: str, nr: int, size: int) -> int:
    return 0x80000000 | (size << 16) | (ord(type_char) << 8) | nr


def _eviocgname(size: int) -> int:
    return _ioc_read("E", 0x06, size)


def _eviocgbit(ev: int, size: int) -> int:
    return _ioc_read("E", 0x20 + ev, size)


def _eviocgabs(axis: int) -> int:
    return _ioc_read("E", 0x40 + axis, 24)


def _eviocgrab() -> int:
    return 0x40044590  # EVIOCGRAB, _IOW('E', 0x90, int)


def device_name(fd: int) -> str:
    buf = bytearray(256)
    try:
        fcntl.ioctl(fd, _eviocgname(len(buf)), buf)
    except OSError:
        return ""
    return buf.split(b"\0", 1)[0].decode(errors="replace")


def _bits(fd: int, ev: int, nbits: int = 768) -> set[int]:
    nbytes = (nbits + 7) // 8
    buf = bytearray(nbytes)
    try:
        fcntl.ioctl(fd, _eviocgbit(ev, nbytes), buf)
    except OSError:
        return set()
    out = set()
    for index, byte in enumerate(buf):
        if not byte:
            continue
        for bit in range(8):
            if byte & (1 << bit):
                out.add(index * 8 + bit)
    return out


def absinfo(fd: int, axis: int) -> dict | None:
    buf = bytearray(24)
    try:
        fcntl.ioctl(fd, _eviocgabs(axis), buf)
    except OSError:
        return None
    value, minimum, maximum, fuzz, flat, resolution = struct.unpack("iiiiii", buf)
    if minimum == maximum == 0:
        return None
    return {"value": value, "min": minimum, "max": maximum,
            "fuzz": fuzz, "flat": flat, "res": resolution}


def inspect(path: str) -> dict | None:
    """Everything we need to know about one event device."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        name = device_name(fd)
        keys = _bits(fd, EV_KEY)
        axes = _bits(fd, EV_ABS, 64)
        info = {
            "path": path,
            "name": name,
            "keys": keys,
            "axes": axes,
            "multitouch": ABS_MT_POSITION_X in axes and ABS_MT_POSITION_Y in axes,
            "single_touch": ABS_X in axes and ABS_Y in axes and BTN_TOUCH in keys,
            "is_keyboard": (KEY_A in keys and KEY_Z in keys and KEY_ENTER in keys),
            "abs": {},
        }
        for axis, label in ((ABS_MT_POSITION_X, "mt_x"), (ABS_MT_POSITION_Y, "mt_y"),
                            (ABS_X, "x"), (ABS_Y, "y")):
            got = absinfo(fd, axis)
            if got:
                info["abs"][label] = got
        info["is_touchscreen"] = info["multitouch"] or info["single_touch"]
        return info
    finally:
        os.close(fd)


def devices() -> list[dict]:
    out = []
    for path in sorted(Path("/dev/input").glob("event*"),
                       key=lambda p: int(str(p).rsplit("event", 1)[1])):
        info = inspect(str(path))
        if info:
            out.append(info)
    return out


def find_touchscreen(prefer_name: str | None = None,
                     explicit: str | None = None) -> dict | None:
    if explicit:
        info = inspect(explicit)
        if info is None:
            raise util.Fail(f"cannot open touch device {explicit}")
        if not info["is_touchscreen"]:
            util.warn(f"{explicit} ({info['name']}) does not look like a "
                      "touchscreen, using it anyway")
        return info
    candidates = [d for d in devices() if d["is_touchscreen"]]
    if prefer_name:
        for dev in candidates:
            if prefer_name.lower() in dev["name"].lower():
                return dev
    # a real panel reports multitouch; prefer it over a single-touch digitiser
    candidates.sort(key=lambda d: (not d["multitouch"], d["path"]))
    return candidates[0] if candidates else None


def find_keyboard(prefer_name: str | None = None,
                  explicit: str | None = None) -> dict | None:
    if explicit:
        return inspect(explicit)
    candidates = [d for d in devices() if d["is_keyboard"]]
    if prefer_name:
        for dev in candidates:
            if prefer_name.lower() in dev["name"].lower():
                return dev
    return candidates[0] if candidates else None


def midi_nodes(card_index: int | None = None) -> list[str]:
    """Raw MIDI character devices, optionally filtered to one card."""
    out = []
    for path in sorted(Path("/dev/snd").glob("midiC*D*")):
        if card_index is None:
            out.append(str(path))
            continue
        try:
            card = int(str(path.name).split("midiC")[1].split("D")[0])
        except (IndexError, ValueError):
            continue
        if card == card_index:
            out.append(str(path))
    return out


def describe() -> list[str]:
    lines = []
    for dev in devices():
        tags = []
        if dev["is_touchscreen"]:
            tags.append("touch" + ("(mt)" if dev["multitouch"] else "(st)"))
        if dev["is_keyboard"]:
            tags.append("keyboard")
        ranges = ""
        for label in ("mt_x", "x"):
            if label in dev["abs"]:
                axis_x = dev["abs"][label]
                axis_y = dev["abs"].get("mt_y" if label == "mt_x" else "y", {})
                ranges = (f" x={axis_x['min']}..{axis_x['max']}"
                          f" y={axis_y.get('min', 0)}..{axis_y.get('max', 0)}")
                break
        lines.append(f"{dev['path']:22} {dev['name'][:38]:38} "
                     f"{','.join(tags) or '-'}{ranges}")
    if not lines:
        lines.append("no /dev/input/event* devices (no touchscreen, no keyboard)")
    return lines


class Reader:
    """Blocking-ish reader for one event device, yielding (type, code, value)."""

    def __init__(self, path: str):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass

    def read(self) -> list[tuple[int, int, int]]:
        """Read whatever is pending.  Raises OSError when the device is gone."""
        data = os.read(self.fd, EVENT_SIZE * 256)
        events = []
        for offset in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
            _sec, _usec, etype, code, value = struct.unpack_from(
                EVENT_FMT, data, offset)
            events.append((etype, code, value))
        return events
