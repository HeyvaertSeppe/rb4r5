"""The XDJ-RX3's own typeface, for the overlay's labels.

The overlay drew its text with a home-made 5x7 pixel font, which looks like
nothing on the RX3.  The RX3's UI fonts ship in its firmware - gui.tar.gz is
unpacked into the chroot at /root/gui, fontdata and all - so the overlay uses
those: the same letters as the player beside it, and nothing redistributed,
because the files come from the user's own firmware.

Rendering goes through the system's FreeType (libfreetype.so.6) by ctypes, so
there is no Python package to install.  When there is no FreeType, or no
TrueType/OpenType file in the firmware, the overlay keeps its bitmap font and
says why once.

`overlay.font` in the config names a file to use instead.
"""
from __future__ import annotations

import ctypes
import ctypes.util
from pathlib import Path

from . import util

# --------------------------------------------------------------------------
# FreeType's public structures, as far as the fields that are read.  These
# are FreeType 2's stable ABI (freetype.h); every integer there is FT_Long /
# FT_Pos / FT_Fixed (a C long) or a fixed-width type, so ctypes' c_long lays
# them out correctly on 32- and 64-bit alike.
# --------------------------------------------------------------------------
c_long, c_int, c_uint, c_short, c_ushort, c_ubyte, c_void_p = (
    ctypes.c_long, ctypes.c_int, ctypes.c_uint, ctypes.c_short,
    ctypes.c_ushort, ctypes.c_ubyte, ctypes.c_void_p)


class _Generic(ctypes.Structure):
    _fields_ = [("data", c_void_p), ("finalizer", c_void_p)]


class _Vector(ctypes.Structure):
    _fields_ = [("x", c_long), ("y", c_long)]


class _BBox(ctypes.Structure):
    _fields_ = [("xMin", c_long), ("yMin", c_long),
                ("xMax", c_long), ("yMax", c_long)]


class _GlyphMetrics(ctypes.Structure):
    _fields_ = [(name, c_long) for name in (
        "width", "height", "horiBearingX", "horiBearingY", "horiAdvance",
        "vertBearingX", "vertBearingY", "vertAdvance")]


class _Bitmap(ctypes.Structure):
    _fields_ = [("rows", c_uint), ("width", c_uint), ("pitch", c_int),
                ("buffer", ctypes.POINTER(c_ubyte)), ("num_grays", c_ushort),
                ("pixel_mode", c_ubyte), ("palette_mode", c_ubyte),
                ("palette", c_void_p)]


class _GlyphSlot(ctypes.Structure):
    _fields_ = [("library", c_void_p), ("face", c_void_p), ("next", c_void_p),
                ("glyph_index", c_uint), ("generic", _Generic),
                ("metrics", _GlyphMetrics), ("linearHoriAdvance", c_long),
                ("linearVertAdvance", c_long), ("advance", _Vector),
                ("format", c_uint), ("bitmap", _Bitmap),
                ("bitmap_left", c_int), ("bitmap_top", c_int)]


class _Face(ctypes.Structure):
    _fields_ = [("num_faces", c_long), ("face_index", c_long),
                ("face_flags", c_long), ("style_flags", c_long),
                ("num_glyphs", c_long), ("family_name", ctypes.c_char_p),
                ("style_name", ctypes.c_char_p), ("num_fixed_sizes", c_int),
                ("available_sizes", c_void_p), ("num_charmaps", c_int),
                ("charmaps", c_void_p), ("generic", _Generic),
                ("bbox", _BBox), ("units_per_EM", c_ushort),
                ("ascender", c_short), ("descender", c_short),
                ("height", c_short), ("max_advance_width", c_short),
                ("max_advance_height", c_short),
                ("underline_position", c_short),
                ("underline_thickness", c_short),
                ("glyph", ctypes.POINTER(_GlyphSlot)), ("size", c_void_p),
                ("charmap", c_void_p)]


FT_LOAD_RENDER = 1 << 2
FT_PIXEL_MODE_GRAY = 2

_lib = None
_library = None


def _freetype():
    """The FreeType library handle, or None when there is no FreeType."""
    global _lib, _library
    if _library is not None:
        return _lib
    name = ctypes.util.find_library("freetype") or "libfreetype.so.6"
    try:
        lib = ctypes.CDLL(name)
    except OSError:
        _library = False
        return None
    lib.FT_Init_FreeType.argtypes = [ctypes.POINTER(c_void_p)]
    lib.FT_New_Face.argtypes = [c_void_p, ctypes.c_char_p, c_long,
                                ctypes.POINTER(ctypes.POINTER(_Face))]
    lib.FT_Set_Pixel_Sizes.argtypes = [ctypes.POINTER(_Face), c_uint, c_uint]
    lib.FT_Load_Char.argtypes = [ctypes.POINTER(_Face), ctypes.c_ulong,
                                 ctypes.c_int32]
    lib.FT_Get_Char_Index.argtypes = [ctypes.POINTER(_Face), ctypes.c_ulong]
    lib.FT_Get_Char_Index.restype = c_uint
    handle = c_void_p()
    if lib.FT_Init_FreeType(ctypes.byref(handle)) != 0:
        _library = False
        return None
    _lib, _library = lib, handle
    return lib


class Glyph:
    __slots__ = ("left", "top", "width", "rows", "alpha", "advance")

    def __init__(self, left, top, width, rows, alpha, advance):
        self.left, self.top = left, top
        self.width, self.rows = width, rows
        self.alpha, self.advance = alpha, advance


class Typeface:
    """One font file, rendered at whatever pixel size is asked for."""

    def __init__(self, path: Path):
        lib = _freetype()
        if lib is None:
            raise OSError("FreeType (libfreetype.so.6) is not installed")
        face = ctypes.POINTER(_Face)()
        if lib.FT_New_Face(_library, str(path).encode(), 0,
                           ctypes.byref(face)) != 0:
            raise OSError(f"FreeType cannot open {path}")
        self.path = Path(path)
        self._lib, self._face = lib, face
        self._size = 0
        self._glyphs: dict[tuple[int, str], Glyph] = {}
        self._cap_ratio: float | None = None
        family = face.contents.family_name or b""
        style = face.contents.style_name or b""
        self.name = f"{family.decode(errors='replace')} " \
                    f"{style.decode(errors='replace')}".strip()

    def covers(self, text: str) -> bool:
        return all(self._lib.FT_Get_Char_Index(self._face, ord(ch))
                   for ch in text if not ch.isspace())

    def glyph(self, char: str, px: int) -> Glyph:
        key = (px, char)
        cached = self._glyphs.get(key)
        if cached is not None:
            return cached
        if self._size != px:
            self._lib.FT_Set_Pixel_Sizes(self._face, 0, px)
            self._size = px
        if self._lib.FT_Load_Char(self._face, ord(char), FT_LOAD_RENDER) != 0:
            glyph = Glyph(0, 0, 0, 0, b"", px // 3)
        else:
            slot = self._face.contents.glyph.contents
            bm = slot.bitmap
            width, rows, pitch = bm.width, bm.rows, bm.pitch
            alpha = b""
            if width and rows and bm.pixel_mode == FT_PIXEL_MODE_GRAY:
                raw = ctypes.string_at(bm.buffer, abs(pitch) * rows)
                alpha = b"".join(raw[r * abs(pitch): r * abs(pitch) + width]
                                 for r in range(rows))
            glyph = Glyph(slot.bitmap_left, slot.bitmap_top, width, rows,
                          alpha, (slot.advance.x + 32) >> 6)
        self._glyphs[key] = glyph
        return glyph

    def cap_ratio(self) -> float:
        """Capital height as a share of the pixel size ('H' at 100px)."""
        if self._cap_ratio is None:
            top = self.glyph("H", 100).top
            self._cap_ratio = (top / 100.0) if top > 0 else 0.7
        return self._cap_ratio

    def px_for_cap(self, cap_height: int) -> int:
        """The pixel size whose capitals are `cap_height` tall."""
        return max(6, int(round(cap_height / self.cap_ratio())))

    def width(self, text: str, px: int, tracking: int = 0) -> int:
        if not text:
            return 0
        return (sum(self.glyph(ch, px).advance for ch in text)
                + tracking * (len(text) - 1))


# --------------------------------------------------------------------------
# finding the RX3's font
# --------------------------------------------------------------------------
_MAGIC = (b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf")
# scripts the button labels are not written in
_OTHER_SCRIPTS = ("jp", "ja", "kr", "ko", "cn", "zh", "sc", "tc", "chs", "cht",
                  "cjk", "hans", "hant", "thai", "arab", "hebr", "gothic",
                  "mincho", "hei", "ming", "song")


def is_font_file(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            return False
        with path.open("rb") as handle:
            return handle.read(4) in _MAGIC
    except OSError:
        return False


def _rank(path: Path) -> int:
    """Higher is more like the Latin UI face the RX3 labels are set in."""
    name = path.stem.lower().replace("-", "_").replace(" ", "_")
    parts = set(name.split("_"))
    score = 0
    if any(tag in parts or name.endswith(tag) for tag in _OTHER_SCRIPTS):
        score -= 100
    if any(w in name for w in ("bold", "medium", "semibold", "demi")):
        score += 20          # the RX3's labels are set heavy
    if any(w in name for w in ("regular", "book", "normal")):
        score += 10
    if any(w in name for w in ("italic", "oblique", "light", "thin", "mono",
                                "symbol", "icon", "digit", "num")):
        score -= 30
    return score


def search_dirs(cfg) -> list[Path]:
    """Where the firmware puts its fonts inside the chroot."""
    try:
        root = Path(cfg.chroot)
    except (AttributeError, TypeError):
        return []
    dirs = [root / "root/gui", root / "usr/share/fonts", root / "root/pdj",
            root / "usr/lib/fonts"]
    return [d for d in dirs if d.is_dir()]


def candidates(cfg) -> list[Path]:
    found = []
    for directory in search_dirs(cfg):
        for path in directory.rglob("*"):
            if is_font_file(path):
                found.append(path)
    return sorted(found, key=lambda p: (-_rank(p), str(p)))


def load(cfg) -> Typeface | None:
    """The RX3's typeface, or None (and a line saying why)."""
    chosen = cfg.get("overlay.font") if hasattr(cfg, "get") else None
    paths = [Path(chosen)] if chosen else candidates(cfg)
    if not paths:
        util.info("overlay: no TrueType/OpenType font in the RX3 firmware's "
                  "gui/ - the labels keep the built-in pixel font "
                  "(set overlay.font to a file to choose one)")
        return None
    for path in paths:
        try:
            face = Typeface(path)
        except OSError as exc:
            util.info(f"overlay: {exc} - the labels keep the built-in font")
            return None
        if face.covers("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"):
            util.info(f"overlay: labels set in {face.name or path.name} "
                      f"({path})")
            return face
    util.info("overlay: none of the firmware's fonts has the Latin capitals; "
              "the labels keep the built-in pixel font")
    return None
