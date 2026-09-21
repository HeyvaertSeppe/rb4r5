"""Display: framebuffer facts, HDMI mode, and keeping the console out of the way.

The player draws through DirectFB's fbdev backend into /dev/fb0, which on the
Pi 5 is the DRM fbdev emulation of vc4-kms-v3d.  Two things matter:

  * nothing else may own the screen (no compositor, no getty painting over it,
    no kernel messages) - see quiet_console();
  * the real mode can be anything (a 22" panel is normally 1920x1080); the
    patched DirectFB driver scales the RX3's 1280x800 RGB565 UI into it, so we
    only have to report the geometry and leave the mode alone.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import fb, util

FB_SYS = "/sys/class/graphics/fb0"


def fb_info(fbdev: str = "/dev/fb0") -> dict:
    """Geometry of the framebuffer, read from sysfs (no ioctl needed)."""
    node = Path(FB_SYS if fbdev.endswith("fb0") else
                f"/sys/class/graphics/{Path(fbdev).name}")
    info = {
        "dev": fbdev,
        "present": Path(fbdev).exists(),
        "name": util.read_text(node / "name").strip(),
        "bpp": util.read_int(node / "bits_per_pixel"),
        "stride": util.read_int(node / "stride"),
        "pan": util.read_text(node / "pan").strip(),
        "width": 0,
        "height": 0,
        "vheight": 0,
    }
    virtual = util.read_text(node / "virtual_size").strip()
    match = re.match(r"(\d+),(\d+)", virtual)
    if match:
        info["width"] = int(match.group(1))
        info["vheight"] = int(match.group(2))
    mode = util.read_text(node / "modes").strip().splitlines()
    if mode:
        got = re.search(r"(\d+)x(\d+)", mode[0])
        if got:
            info["height"] = int(got.group(2))
    if not info["height"]:
        # fbdev emulation usually has virtual == visible
        info["height"] = info["vheight"]

    # sysfs is convenient but says nothing about the pixel layout, and its
    # geometry can lag the current mode; the ioctls are the truth.
    real = fb.screeninfo(fbdev)
    if not real["error"] and real["width"]:
        info.update(width=real["width"], height=real["height"],
                    bpp=real["bpp"], stride=real["line_length"],
                    fmt=real["fmt"], vheight=real["yres_virtual"] or info["vheight"])
    info.setdefault("fmt", "")
    return info


def connectors() -> list[dict]:
    """Connected DRM outputs and their modes."""
    result = []
    for path in sorted(Path("/sys/class/drm").glob("card*-*")):
        status = util.read_text(path / "status").strip()
        if not status:
            continue
        modes = util.read_text(path / "modes").strip().splitlines()
        result.append({
            "name": path.name,
            "status": status,
            "mode": modes[0] if modes else "",
            "modes": modes[:12],
            "enabled": util.read_text(path / "enabled").strip(),
        })
    return result


def primary_connector() -> dict | None:
    for conn in connectors():
        if conn["status"] == "connected":
            return conn
    return None


def scale_note(cfg) -> str:
    """Human description of the UI -> panel scaling that will happen."""
    info = fb_info(cfg.get("display.fbdev", "/dev/fb0"))
    ui_w = cfg.get("display.ui_width", 1280)
    ui_h = cfg.get("display.ui_height", 800)
    if not info["present"] or not info["width"]:
        return "framebuffer not available"
    fmt = info.get("fmt") or f"{info['bpp']}bpp"
    same = fmt == "RGB565"
    return (f"{ui_w}x{ui_h} RGB565 (RX3 UI) -> {info['width']}x{info['height']} "
            f"{fmt} ({info['name'] or 'fb0'}), scaled in the DirectFB fbdev "
            f"driver" + (" (no colour conversion needed)" if same else ""))


# --------------------------------------------------------------------------
# console hygiene
# --------------------------------------------------------------------------
def _write_sys(path: str, value: str) -> bool:
    try:
        Path(path).write_text(value)
        return True
    except OSError:
        return False


def quiet_console(level: int = 2) -> list[str]:
    """Stop anything else painting on the framebuffer.

    level 0: leave the console alone (bring-up / debugging)
    level 1: silence kernel messages and the cursor
    level 2: also detach fbcon, so nothing can overdraw DirectFB at all
    Returns a list of what was done.
    """
    done = []
    if level <= 0:
        return ["console left alone (quiet_console=0)"]

    if util.have("dmesg"):
        util.run(["dmesg", "-n", "1"], check=False)
        done.append("kernel messages silenced (dmesg -n 1)")
    if _write_sys("/sys/class/graphics/fbcon/cursor_blink", "0"):
        done.append("console cursor stopped")
    # console blanking would black the player out after 10 minutes
    for tty in ("/dev/tty1", "/dev/tty0"):
        if Path(tty).exists():
            util.run(f"printf '\\033[9;0]\\033[?25l' > {tty}", check=False)
    if level >= 2:
        for vtcon in sorted(Path("/sys/class/vtconsole").glob("vtcon*")):
            name = util.read_text(vtcon / "name").strip()
            if "frame buffer" in name:
                if _write_sys(str(vtcon / "bind"), "0"):
                    done.append(f"{vtcon.name} (fbcon) detached - rebind with "
                                f"echo 1 > {vtcon}/bind")
    return done


def restore_console() -> list[str]:
    done = []
    for vtcon in sorted(Path("/sys/class/vtconsole").glob("vtcon*")):
        if "frame buffer" in util.read_text(vtcon / "name"):
            if _write_sys(str(vtcon / "bind"), "1"):
                done.append(f"{vtcon.name} rebound")
    if util.have("dmesg"):
        util.run(["dmesg", "-n", "7"], check=False)
    for tty in ("/dev/tty1",):
        if Path(tty).exists():
            util.run(f"printf '\\033[?25h' > {tty}", check=False)
    return done


def fb_dump(path: str, fbdev: str = "/dev/fb0") -> str:
    """Save what is on screen as a PNG.

    The pixels are read and converted according to the format the driver
    reports (see rb4r5/fb.py), not a guess: a 16bpp framebuffer decoded as
    32bpp - or the other way round - produces a picture that looks broken when
    the screen is fine, which is worse than no screenshot at all.  No Pillow
    needed either, so this works on a stock image.
    """
    width, height, rgb = fb.capture(fbdev)
    if not path.lower().endswith(".png"):
        path += ".png"
    return fb.write_png(path, width, height, rgb)


def fb_nonzero(fbdev: str = "/dev/fb0", sample: int = 400_000) -> int:
    """How many non-zero bytes the start of the framebuffer holds.

    Zero means the player is not publishing frames (see docs/04-display.md).
    """
    try:
        with open(fbdev, "rb") as handle:
            data = handle.read(sample)
    except OSError:
        return -1
    return sum(1 for byte in data if byte)
