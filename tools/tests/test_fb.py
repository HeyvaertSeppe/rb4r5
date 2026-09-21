#!/usr/bin/env python3
"""rb4r5/fb.py - the framebuffer as it really is.

The launcher reads and writes /dev/fb0 for screenshots and for `fbtest`, and
both have to agree with the pixel format the kernel reports.  The same mistake
that broke the display (assuming 32bpp on a 16bpp framebuffer) would silently
produce unreadable screenshots, which is worse than none: it makes a working
screen look broken.  So: every format, packed and unpacked, checked against
the known colours the C side uses too (tools/tests/test_fbscale.c).
"""
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from rb4r5 import fb                                        # noqa: E402

FAIL = 0


def check(cond, what):
    global FAIL
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        FAIL += 1


def info_for(fmt, width=8, height=4, pad=0):
    layout = {
        "RGB565":   (16, (11, 5), (5, 6), (0, 5)),
        "BGR565":   (16, (0, 5), (5, 6), (11, 5)),
        "RGB555":   (16, (10, 5), (5, 5), (0, 5)),
        "XRGB8888": (32, (16, 8), (8, 8), (0, 8)),
        "XBGR8888": (32, (0, 8), (8, 8), (16, 8)),
        "RGB888":   (24, (16, 8), (8, 8), (0, 8)),
        "BGR888":   (24, (0, 8), (8, 8), (16, 8)),
    }[fmt]
    bpp, red, green, blue = layout
    info = dict(dev="/dev/fb0", present=True, bpp=bpp, width=width,
                height=height, line_length=width * (bpp // 8) + pad,
                xres_virtual=width, yres_virtual=height, xoffset=0, yoffset=0,
                red=red, green=green, blue=blue, transp=(0, 0),
                id="test", smem_len=1 << 20, error="")
    info["fmt"] = fb.classify(info)
    check(info["fmt"] == fmt, f"classify {fmt}")
    return info


print("== classification")
for name in ("RGB565", "BGR565", "RGB555", "XRGB8888", "XBGR8888",
             "RGB888", "BGR888"):
    info_for(name)
check(fb.classify(dict(bpp=8, red=(0, 3), green=(3, 3), blue=(6, 2))) == "",
      "an 8bpp palette framebuffer is not one of ours")
check(fb.fmt_or_guess(dict(fmt="", bpp=16)) == ("RGB565", True),
      "an unknown 16bpp layout falls back to RGB565 and says it guessed")
check(fb.fmt_or_guess(dict(fmt="", bpp=32)) == ("XRGB8888", True),
      "an unknown 32bpp layout falls back to XRGB8888")

print("\n== pack and read back: primaries survive the round trip")
# 565 keeps 5 or 6 bits per channel, so only values that survive that are
# expected back exactly; these do.
COLOURS = [(0, 0, 0), (255, 255, 255), (255, 0, 0), (0, 255, 0), (0, 0, 255),
           (255, 255, 0), (0, 255, 255), (255, 0, 255)]
for name in ("RGB565", "BGR565", "RGB555", "XRGB8888", "XBGR8888",
             "RGB888", "BGR888"):
    info = info_for(name, width=len(COLOURS), height=1)
    row = b"".join(fb.pack(info, *rgb) for rgb in COLOURS)
    got = fb.to_rgb(row, info)
    want = b"".join(bytes(rgb) for rgb in COLOURS)
    check(got == want, f"{name}: 8 primaries pack and unpack unchanged")

print("\n== the exact bytes, so a channel swap cannot hide")
info = info_for("RGB565", width=1, height=1)
check(fb.pack(info, 255, 0, 0) == b"\x00\xf8", "RGB565 red is 0xf800")
check(fb.pack(info, 0, 0, 255) == b"\x1f\x00", "RGB565 blue is 0x001f")
check(fb.pack(info, 0, 255, 0) == b"\xe0\x07", "RGB565 green is 0x07e0")
info = info_for("XRGB8888", width=1, height=1)
check(fb.pack(info, 255, 0, 0) == b"\x00\x00\xff\xff",
      "XRGB8888 red is b,g,r,x = 00 00 ff ff in memory")
info = info_for("XBGR8888", width=1, height=1)
check(fb.pack(info, 255, 0, 0) == b"\xff\x00\x00\xff",
      "XBGR8888 red is r,g,b,x = ff 00 00 ff in memory")

print("\n== stride padding is skipped, not decoded")
# A framebuffer whose line length is longer than the visible width (very
# common) must not leak the padding into the picture.
info = info_for("RGB565", width=4, height=2, pad=16)
rows = b""
for y in range(2):
    rows += b"".join(fb.pack(info, 255 if y == 0 else 0, 0,
                             0 if y == 0 else 255) for _ in range(4))
    rows += b"\xaa" * 16                        # padding: must be ignored
got = fb.to_rgb(rows, info)
check(got == bytes((255, 0, 0)) * 4 + bytes((0, 0, 255)) * 4,
      "padded lines decode to the visible pixels only")

print("\n== the test pattern")
info = info_for("RGB565", width=320, height=240)
pattern = fb.test_pattern(info, 1280, 800)
check(len(pattern) == info["line_length"] * info["height"],
      "the pattern is exactly one screen")
rgb = fb.to_rgb(pattern, info)


def px(x, y):
    at = (y * info["width"] + x) * 3
    return tuple(rgb[at:at + 3])


check(px(0, 0) == (255, 255, 255), "the frame reaches the top left pixel")
check(px(319, 239) == (255, 255, 255),
      "the frame reaches the bottom right pixel")
# Sample each bar a quarter of the way down, clear of the centre cross.
bar_y = info["height"] // 2 - info["height"] // 5
bar_w = info["width"] // len(fb.BARS)
for index, (name, want) in enumerate(fb.BARS):
    got = px(index * bar_w + bar_w // 2, bar_y)
    if name == "grey 50%":
        # RGB565 has no exact 128: 5 bits give 132, 6 bits give 130
        check(max(got) - min(got) <= 4 and 120 < got[0] < 140,
              f"bar {index + 1} is grey ({got})")
    else:
        check(got == want, f"bar {index + 1} is {name} ({got})")

print("\n== PNG output")
out = "/tmp/rb4r5-test-fb.png"
fb.write_png(out, info["width"], info["height"], rgb)
data = Path(out).read_bytes()
check(data[:8] == b"\x89PNG\r\n\x1a\n", "PNG signature")
check(data[12:16] == b"IHDR" and data[16:24] ==
      info["width"].to_bytes(4, "big") + info["height"].to_bytes(4, "big"),
      "PNG size in the header")
check(data[-8:-4] == b"IEND", "PNG ends with IEND")
# decode it back the hard way and compare with what we wrote
idat = b""
pos = 8
while pos < len(data):
    length = int.from_bytes(data[pos:pos + 4], "big")
    tag = data[pos + 4:pos + 8]
    if tag == b"IDAT":
        idat += data[pos + 8:pos + 8 + length]
    pos += 12 + length
raw = zlib.decompress(idat)
stride = info["width"] * 3 + 1
rebuilt = b"".join(raw[y * stride + 1:(y + 1) * stride]
                   for y in range(info["height"]))
check(rebuilt == rgb, "the PNG holds exactly the pixels we passed in")

print("\n== the stale-driver markers")
# chroot.module_report() decides whether the player is loading the current
# display driver by looking for these strings in the built .so.  If the patch
# stops emitting one of them the check silently passes everything, so tie the
# two together here.
from rb4r5 import chroot                                     # noqa: E402
patch = Path(__file__).resolve().parents[2] / "src/directfb/directfb-pi5.patch"
text = patch.read_text()
for marker in chroot.MODULE_MARKERS:
    check(marker in text,
          f"the driver still emits {marker!r} (module_report looks for it)")

print("\n" + ("all fb tests passed" if not FAIL else f"{FAIL} FAILURES"))
sys.exit(1 if FAIL else 0)
