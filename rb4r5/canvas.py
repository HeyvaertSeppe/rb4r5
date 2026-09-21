"""Drawing into the framebuffer: rectangles, text, and nothing else.

The overlay (top button bar, effect picker, splash) is drawn by the launcher
itself, straight into /dev/fb0 in whatever pixel format the panel reports.
It is deliberately small: no compositing, no alpha, no image decoding - every
surface is built in RAM and pushed as whole rows, which is both fast enough
for a 2880px panel and impossible to tear in a way that matters, since the
regions it owns are ones the player never writes to.

Colours are (r, g, b) triples; the Canvas packs them once per call.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import fb, font


class Canvas:
    """An off-screen region, blitted to the framebuffer in one go."""

    def __init__(self, info: dict, x: int, y: int, width: int, height: int):
        self.info = info
        self.x, self.y = x, y
        self.w, self.h = max(0, width), max(0, height)
        self.bpp = max(2, info.get("bpp", 16) // 8)
        self.stride = self.w * self.bpp
        self.buf = bytearray(self.stride * self.h)

    # -- painting ----------------------------------------------------------
    def pack(self, colour: tuple[int, int, int]) -> bytes:
        return fb.pack(self.info, *colour)

    def fill(self, colour: tuple[int, int, int]) -> None:
        self.rect(0, 0, self.w, self.h, colour)

    def rect(self, x: int, y: int, w: int, h: int,
             colour: tuple[int, int, int]) -> None:
        px = self.pack(colour)
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.w, x + w), min(self.h, y + h)
        if x1 <= x0 or y1 <= y0:
            return
        row = px * (x1 - x0)
        for line in range(y0, y1):
            start = line * self.stride + x0 * self.bpp
            self.buf[start:start + len(row)] = row

    def frame(self, x: int, y: int, w: int, h: int,
              colour: tuple[int, int, int], thickness: int = 1) -> None:
        self.rect(x, y, w, thickness, colour)
        self.rect(x, y + h - thickness, w, thickness, colour)
        self.rect(x, y, thickness, h, colour)
        self.rect(x + w - thickness, y, thickness, h, colour)

    def text(self, x: int, y: int, message: str,
             colour: tuple[int, int, int], scale: int = 2,
             tracking: int = 1) -> int:
        """Draw a string; returns the width it took."""
        px = self.pack(colour)
        step = (font.GLYPH_W + tracking) * scale
        for index, char in enumerate(str(message)):
            bits = font.glyph(char)
            left = x + index * step
            for row, pattern in enumerate(bits):
                if not pattern:
                    continue
                top = y + row * scale
                for bit in range(font.GLYPH_W):
                    if not (pattern >> (font.GLYPH_W - 1 - bit)) & 1:
                        continue
                    # one font pixel is scale x scale screen pixels
                    x0 = left + bit * scale
                    for line in range(top, min(self.h, top + scale)):
                        if line < 0:
                            continue
                        start = line * self.stride + max(0, x0) * self.bpp
                        span = min(scale, self.w - x0)
                        if span > 0:
                            self.buf[start:start + span * self.bpp] = px * span
        return font.text_width(str(message), scale, tracking)

    def text_centred(self, cx: int, y: int, message: str,
                     colour: tuple[int, int, int], scale: int = 2,
                     tracking: int = 1) -> None:
        width = font.text_width(str(message), scale, tracking)
        self.text(cx - width // 2, y, message, colour, scale, tracking)

    def fit_scale(self, message: str, width: int, height: int,
                  cap: int = 6) -> int:
        """The largest integer scale at which a string fits a box."""
        for scale in range(cap, 0, -1):
            if (font.text_width(str(message), scale) <= width and
                    font.text_height(scale) <= height):
                return scale
        return 1

    # -- output ------------------------------------------------------------
    def blit(self, target: str = "/dev/fb0") -> None:
        """Copy the canvas into the framebuffer, one row per seek."""
        stride = self.info["line_length"]
        with open(target, "r+b", buffering=0) as handle:
            for line in range(self.h):
                handle.seek((self.y + line) * stride + self.x * self.bpp)
                handle.write(self.buf[line * self.stride:
                                      (line + 1) * self.stride])

    def to_png(self, path: str) -> str:
        """For tests and for looking at the thing without a panel."""
        rgb = fb.to_rgb(bytes(self.buf),
                        dict(self.info, width=self.w, height=self.h,
                             line_length=self.stride))
        return fb.write_png(path, self.w, self.h, rgb)
