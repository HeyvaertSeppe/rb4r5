"""The framebuffer, as it really is.

Everything that touches /dev/fb0 outside the DirectFB driver goes through here:
screenshots, the fbtest pattern, the doctor's report.  The point of the module
is that no pixel format is assumed anywhere.

The Pi 5 runs vc4-kms-v3d and /dev/fb0 is the DRM fbdev emulation of it.  What
comes out is *not* fixed: depending on the kernel version it is RGB565 at 16bpp
or XRGB8888 at 32bpp, and a "video=HDMI-A-1:1920x1080-32" on the kernel command
line changes it again.  Assuming one of them is how the player ended up drawing
half the UI across the whole panel in olive and lavender: 32-bit pixels written
into a 16-bit framebuffer cover two screen pixels each.

So the format is read from the driver (FBIOGET_VSCREENINFO), never guessed, and
the same classification the C publish path uses (src/directfb/rb4r5_scale.h)
is mirrored here.
"""
from __future__ import annotations

import fcntl
import os
import struct
import zlib
from pathlib import Path

from . import util

FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602

# struct fb_var_screeninfo: 40 x __u32 (four fb_bitfields of three each)
VAR_FMT = "@40I"
VAR_SIZE = struct.calcsize(VAR_FMT)
# struct fb_fix_screeninfo
FIX_FMT = "@16sLI3I3HILIIH2H"
FIX_SIZE = struct.calcsize(FIX_FMT)

FORMATS = {
    #  (bpp, r.off, r.len, g.off, g.len, b.off, b.len): name
    (16, 11, 5, 5, 6, 0, 5): "RGB565",
    (16, 0, 5, 5, 6, 11, 5): "BGR565",
    (16, 10, 5, 5, 5, 0, 5): "RGB555",
    (32, 16, 8, 8, 8, 0, 8): "XRGB8888",
    (32, 0, 8, 8, 8, 16, 8): "XBGR8888",
    (24, 16, 8, 8, 8, 0, 8): "RGB888",
    (24, 0, 8, 8, 8, 16, 8): "BGR888",
}


def screeninfo(fbdev: str = "/dev/fb0") -> dict:
    """Authoritative geometry and pixel format, straight from the driver."""
    info = {"dev": fbdev, "present": Path(fbdev).exists(), "fmt": "",
            "bpp": 0, "width": 0, "height": 0, "line_length": 0,
            "xres_virtual": 0, "yres_virtual": 0, "xoffset": 0, "yoffset": 0,
            "red": (0, 0), "green": (0, 0), "blue": (0, 0), "transp": (0, 0),
            "id": "", "smem_len": 0, "error": ""}
    if not info["present"]:
        info["error"] = f"{fbdev} does not exist"
        return info
    try:
        fd = os.open(fbdev, os.O_RDONLY)
    except OSError as exc:
        info["error"] = f"cannot open {fbdev}: {exc}"
        return info
    try:
        # the buffers are deliberately larger than the structs: the kernel
        # writes sizeof(struct), which includes trailing alignment that
        # struct.calcsize() does not count
        buf = bytearray(256)
        fcntl.ioctl(fd, FBIOGET_VSCREENINFO, buf)
        v = struct.unpack_from(VAR_FMT, bytes(buf))
        info.update(
            width=v[0], height=v[1], xres_virtual=v[2], yres_virtual=v[3],
            xoffset=v[4], yoffset=v[5], bpp=v[6],
            red=(v[8], v[9]), green=(v[11], v[12]),
            blue=(v[14], v[15]), transp=(v[17], v[18]),
        )
        buf = bytearray(256)
        fcntl.ioctl(fd, FBIOGET_FSCREENINFO, buf)
        f = struct.unpack_from(FIX_FMT, bytes(buf))
        info["id"] = f[0].split(b"\0", 1)[0].decode(errors="replace")
        info["smem_len"] = f[2]
        info["line_length"] = f[9]
    except OSError as exc:
        info["error"] = f"ioctl on {fbdev} failed: {exc}"
    finally:
        os.close(fd)

    info["fmt"] = classify(info)
    if not info["line_length"]:
        info["line_length"] = info["width"] * max(1, info["bpp"] // 8)
    return info


def classify(info: dict) -> str:
    """Name the pixel layout, exactly as rb4r5_scale.h classifies it."""
    key = (info["bpp"], info["red"][0], info["red"][1],
           info["green"][0], info["green"][1],
           info["blue"][0], info["blue"][1])
    return FORMATS.get(key, "")


def fmt_or_guess(info: dict) -> tuple[str, bool]:
    """The format, or the usual one for that depth plus a "guessed" flag."""
    if info["fmt"]:
        return info["fmt"], False
    return {16: "RGB565", 24: "RGB888", 32: "XRGB8888"}.get(
        info["bpp"], "XRGB8888"), True


def describe(fbdev: str = "/dev/fb0") -> list[str]:
    info = screeninfo(fbdev)
    if info["error"]:
        return [info["error"]]
    fmt, guessed = fmt_or_guess(info)
    lines = [
        f"{info['dev']}: {info['width']}x{info['height']} {fmt}"
        f"{' (assumed - unrecognised bitfields)' if guessed else ''}",
        f"  {info['bpp']} bpp, line length {info['line_length']} bytes"
        f" ({info['line_length'] // max(1, info['bpp'] // 8)} px), "
        f"{info['smem_len'] // 1024} KiB of video memory",
        f"  virtual {info['xres_virtual']}x{info['yres_virtual']} "
        f"at offset {info['xoffset']},{info['yoffset']}, driver "
        f"{info['id'] or '?'}",
        f"  red {info['red']}  green {info['green']}  blue {info['blue']}"
        f"  alpha {info['transp']}   (offset, length)",
    ]
    if info["bpp"] == 16:
        lines.append("  16bpp: the RX3 frame is RGB565 too, so the driver "
                     "scales it with no colour conversion at all")
    return lines


# --------------------------------------------------------------------------
# reading pixels back
# --------------------------------------------------------------------------
_LUT: dict[str, list[bytes]] = {}


def _lut565(fmt: str) -> list[bytes]:
    """65536-entry lookup from a 16-bit pixel to RGB, built once."""
    if fmt in _LUT:
        return _LUT[fmt]
    table = []
    for value in range(65536):
        if fmt == "RGB555":
            r5, g5, b5 = (value >> 10) & 0x1F, (value >> 5) & 0x1F, value & 0x1F
            r, g, b = (r5 << 3) | (r5 >> 2), (g5 << 3) | (g5 >> 2), (b5 << 3) | (b5 >> 2)
        else:
            c1, g6, c2 = (value >> 11) & 0x1F, (value >> 5) & 0x3F, value & 0x1F
            hi, lo = ((c1 << 3) | (c1 >> 2)), ((c2 << 3) | (c2 >> 2))
            g = (g6 << 2) | (g6 >> 4)
            r, b = (hi, lo) if fmt == "RGB565" else (lo, hi)
        table.append(bytes((r, g, b)))
    _LUT[fmt] = table
    return table


def to_rgb(data: bytes, info: dict) -> bytes:
    """One screen's worth of framebuffer bytes -> packed RGB triples."""
    fmt, _ = fmt_or_guess(info)
    width, height = info["width"], info["height"]
    stride = info["line_length"]
    bpp = max(2, info["bpp"] // 8)
    out = bytearray(width * height * 3)

    for y in range(height):
        row = data[y * stride: y * stride + width * bpp]
        if len(row) < width * bpp:
            break
        base = y * width * 3
        if bpp == 2:
            lut = _lut565(fmt)
            pixels = struct.unpack_from(f"<{width}H", row)
            out[base:base + width * 3] = b"".join([lut[p] for p in pixels])
        else:
            # 32bpp is b,g,r,x in memory for XRGB8888 (r,g,b,x for XBGR8888);
            # 24bpp is the same three bytes without the pad.  Slice assignment
            # moves a whole channel at C speed instead of per pixel in Python.
            swapped = fmt in ("XBGR8888", "BGR888")
            r_at, b_at = (0, 2) if swapped else (2, 0)
            out[base + 0: base + width * 3: 3] = row[r_at::bpp]
            out[base + 1: base + width * 3: 3] = row[1::bpp]
            out[base + 2: base + width * 3: 3] = row[b_at::bpp]
    return bytes(out)


def capture(fbdev: str = "/dev/fb0") -> tuple[int, int, bytes]:
    info = screeninfo(fbdev)
    if info["error"]:
        raise util.Fail(info["error"])
    if not info["width"] or not info["height"]:
        raise util.Fail(f"{fbdev} reports no mode (is a screen connected?)")
    want = info["line_length"] * info["height"]
    with open(fbdev, "rb") as handle:
        data = handle.read(want)
    if len(data) < want:
        raise util.Fail(f"short read from {fbdev} "
                        f"({len(data)}/{want} bytes)")
    return info["width"], info["height"], to_rgb(data, info)


def write_png(path: str, width: int, height: int, rgb: bytes) -> str:
    """Minimal PNG writer - no Pillow needed, which keeps screenshots working
    on a stock image where python3-pil was never installed."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)                                   # filter type: none
        raw += rgb[y * width * 3: (y + 1) * width * 3]

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload +
                struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n" +
           chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
           chunk(b"IDAT", zlib.compress(bytes(raw), 6)) +
           chunk(b"IEND", b""))
    Path(path).write_bytes(png)
    return f"wrote {width}x{height} PNG to {path} ({len(png) // 1024} KiB)"


# --------------------------------------------------------------------------
# writing pixels: the test pattern
# --------------------------------------------------------------------------
def pack(info: dict, red: int, green: int, blue: int) -> bytes:
    """One pixel, in the framebuffer's own format."""
    fmt, _ = fmt_or_guess(info)
    if fmt == "RGB565":
        return struct.pack("<H", ((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3))
    if fmt == "BGR565":
        return struct.pack("<H", ((blue & 0xF8) << 8) | ((green & 0xFC) << 3) | (red >> 3))
    if fmt == "RGB555":
        return struct.pack("<H", ((red & 0xF8) << 7) | ((green & 0xF8) << 2) | (blue >> 3))
    if fmt == "RGB888":
        return bytes((blue, green, red))
    if fmt == "BGR888":
        return bytes((red, green, blue))
    if fmt == "XBGR8888":
        return bytes((red, green, blue, 0xFF))
    return bytes((blue, green, red, 0xFF))


# The bars, left to right.  Printed in the terminal as well, so a photo of the
# panel can be checked against it: a swapped pair of channels shows up here
# and nowhere else.
BARS = [
    ("white",   (255, 255, 255)),
    ("red",     (255, 0, 0)),
    ("green",   (0, 255, 0)),
    ("blue",    (0, 0, 255)),
    ("yellow",  (255, 255, 0)),
    ("cyan",    (0, 255, 255)),
    ("magenta", (255, 0, 255)),
    ("grey 50%", (128, 128, 128)),
]


def test_pattern(info: dict, ui_w: int = 1280, ui_h: int = 800,
                 aspect: bool = False) -> bytes:
    """Build a full screen of test pattern in the fb's own format.

    What to look for in a photo of it:
      * the thin white frame touches all four edges     -> no overscan, the
        mode and the panel agree
      * the bars are in the order printed below         -> channels correct
      * eight blocks along the top, one per bar         -> nothing is doubled
        or halved horizontally
      * the dashed rectangle is where the player's UI will be drawn
    """
    width, height = info["width"], info["height"]
    stride = info["line_length"]
    bpp = max(2, info["bpp"] // 8)
    screen = bytearray(stride * height)

    black = pack(info, 0, 0, 0)
    white = pack(info, 255, 255, 255)

    def hline(y: int, x0: int, x1: int, px: bytes) -> None:
        if not 0 <= y < height:
            return
        x0, x1 = max(0, x0), min(width, x1)
        if x1 > x0:
            start = y * stride + x0 * bpp
            screen[start:start + (x1 - x0) * bpp] = px * (x1 - x0)

    def vline(x: int, y0: int, y1: int, px: bytes) -> None:
        if not 0 <= x < width:
            return
        for y in range(max(0, y0), min(height, y1)):
            start = y * stride + x * bpp
            screen[start:start + bpp] = px

    def box(x0: int, y0: int, x1: int, y1: int, px: bytes) -> None:
        for y in range(max(0, y0), min(height, y1)):
            hline(y, x0, x1, px)

    # background, then the colour bars across the middle two thirds
    box(0, 0, width, height, black)
    bar_top, bar_bottom = height // 6, height - height // 6
    bar_w = width // len(BARS)
    for index, (_name, (red, green, blue)) in enumerate(BARS):
        x0 = index * bar_w
        x1 = width if index == len(BARS) - 1 else x0 + bar_w
        box(x0, bar_top, x1, bar_bottom, pack(info, red, green, blue))
        # a white tick per bar, stepping down bar by bar, so a doubled or
        # halved picture is countable in a photograph
        tick = max(2, bar_top // 20)
        box(x0 + 8, 8 + index * tick * 2, x1 - 8,
            8 + index * tick * 2 + tick, white)

    # a one pixel frame right at the edges: any of it missing means the panel
    # is cropping, or the mode is not the one the driver believes in
    hline(0, 0, width, white)
    hline(height - 1, 0, width, white)
    vline(0, 0, height, white)
    vline(width - 1, 0, height, white)

    # corner brackets pointing inwards, sized from the panel so the pattern
    # is legible on a 480x320 screen as well as on a 4K one
    arm = max(16, min(width, height) // 10)
    thick = max(2, min(width, height) // 240)
    for cx, cy in ((0, 0), (width - arm, 0),
                   (0, height - thick), (width - arm, height - thick)):
        box(cx, cy, cx + arm, cy + thick, white)
    for cx, cy in ((0, 0), (width - thick, 0),
                   (0, height - arm), (width - thick, height - arm)):
        box(cx, cy, cx + thick, cy + arm, white)

    # centre cross, thick enough to find in a photograph
    reach = max(12, min(width, height) // 12)
    box(width // 2 - reach, height // 2 - thick, width // 2 + reach,
        height // 2 + thick, white)
    box(width // 2 - thick, height // 2 - reach, width // 2 + thick,
        height // 2 + reach, white)

    # where the 1280x800 UI will land, dashed
    if aspect and ui_w > 0 and ui_h > 0:
        if width * ui_h > height * ui_w:
            fh, fw = height, height * ui_w // ui_h
        else:
            fw, fh = width, width * ui_h // ui_w
    else:
        fw, fh = width, height
    fx, fy = (width - fw) // 2, (height - fh) // 2
    for x in range(fx, fx + fw, 16):
        hline(fy, x, min(x + 8, fx + fw), white)
        hline(fy + fh - 1, x, min(x + 8, fx + fw), white)
    for y in range(fy, fy + fh, 16):
        vline(fx, y, min(y + 8, fy + fh), white)
        vline(fx + fw - 1, y, min(y + 8, fy + fh), white)

    return bytes(screen)


def legend(info: dict) -> list[str]:
    fmt, guessed = fmt_or_guess(info)
    lines = [f"test pattern written in {fmt}"
             f"{' (assumed!)' if guessed else ''} at "
             f"{info['width']}x{info['height']}",
             "the bars, left to right:"]
    lines += [f"   {i + 1}. {name}" for i, (name, _rgb) in enumerate(BARS)]
    lines += [
        "",
        "what to check in a photo of the screen:",
        "  * the thin white frame reaches all four edges of the panel",
        "  * the bars are in that order (if red and blue swap places the",
        "    framebuffer format is not what the driver reports)",
        "  * eight white blocks along the top, one per bar, none doubled",
        "  * the dashed rectangle is where the player's UI will be drawn",
    ]
    return lines
