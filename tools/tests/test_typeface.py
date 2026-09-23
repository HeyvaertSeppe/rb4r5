#!/usr/bin/env python3
"""The overlay's labels are set in the RX3's own typeface when it has one.

The fonts come from the user's firmware (the chroot's /root/gui), whatever
their file names, and are rendered through the system's FreeType.  Skips
the rendering half when this machine has no FreeType or no TrueType font to
stand in for the RX3's.

Run:  python3 tools/tests/test_typeface.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from rb4r5 import canvas, config, font, overlay, typeface, util  # noqa: E402

failures = []
util.info = lambda *a, **k: None


def check(label, got, want=True):
    if got != want:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label}")


def system_ttf():
    for pattern in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "FreeSans*.ttf",
                    "*.ttf"):
        for base in (Path("/usr/share/fonts"),):
            for path in sorted(base.rglob(pattern)) if base.is_dir() else []:
                return path
    return None


class Cfg:
    def __init__(self, root, font_path=None):
        self.chroot = root
        self.font_path = font_path

    def get(self, key, default=None):
        return self.font_path if key == "overlay.font" else default


ttf = system_ttf()

print("== finding the firmware's fonts")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    fontdata = root / "root/gui/pset/fontdata"
    fontdata.mkdir(parents=True)
    (fontdata / "notafont.bin").write_bytes(b"\x89PNG not a font")
    if ttf:
        # the firmware's names are its own; the magic number is what counts
        shutil.copy(ttf, fontdata / "UI_Bold.bin")
        shutil.copy(ttf, fontdata / "UI_JP_Bold.otf")
        shutil.copy(ttf, fontdata / "UI_Light.ttf")
        found = typeface.candidates(Cfg(root))
        check("a font is found by its contents, not its name",
              fontdata / "UI_Bold.bin" in found)
        check("and something that is not a font is not",
              fontdata / "notafont.bin" in found, False)
        check("the Latin bold face ranks above the Japanese and the light",
              found[0].name, "UI_Bold.bin")
    else:
        print("--   no TrueType font on this machine to stand in; skipped")
    check("no fonts in the firmware: None, and the pixel font stays",
          typeface.load(Cfg(Path(tmp) / "empty")), None)

if ttf and typeface._freetype() is not None:
    print("\n== drawing with it")
    face = typeface.load(Cfg(Path("/nonexistent"), str(ttf)))
    check("the configured font loads", face is not None)
    font.use_typeface(face)
    try:
        size = font.pixel_size(3)
        h = face.glyph("H", size)
        check("capitals come out as tall as the layout asked (3 x 7 px)",
              abs(h.top - font.text_height(3)) <= 1)
        check("text_width follows the face",
              font.text_width("HELIX", 3), face.width("HELIX", size))

        info = dict(dev="t", present=True, fmt="RGB565", bpp=16, width=300,
                    height=60, line_length=600, red=(11, 5), green=(5, 6),
                    blue=(0, 5), transp=(0, 0))
        cv = canvas.Canvas(info, 0, 0, 300, 60)
        cv.fill((20, 20, 20))
        width = cv.text(10, 20, "REVERB", (240, 240, 240), 3)
        check("draw returns the width it measured",
              width, font.text_width("REVERB", 3))
        shades = {cv.pixel(x, y) for x in range(300) for y in range(60)}
        check("the edges are anti-aliased, not stair-stepped", len(shades) > 4)
        check("and the background around it is left alone",
              cv.pixel(5, 5), (16, 20, 16))

        cfg = config.load("/nonexistent-overlay.json")
        over = overlay.Overlay.__new__(overlay.Overlay)
        over.cfg = cfg
        over.layout = overlay.Layout(cfg, dict(info, width=2880, height=1620,
                                                line_length=5760))
        over.buttons = [overlay.Button(s) for s in overlay.DEFAULT_BUTTONS]
        over.fx = list(overlay.DEFAULT_FX)
        over.fx_index = 0
        over.mode = "none"
        over.layout_buttons()
        over.draw_bar(target=False)
        strip = over.draw_fx_strip(target=False)
        check("the top bar and the effect list still draw", strip is not None)
    finally:
        font.use_typeface(None)
else:
    print("--   no FreeType here; drawing skipped")

print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("the labels use the RX3's typeface when it is there")
