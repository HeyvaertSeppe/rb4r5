"""The parts of the screen the launcher draws itself.

The XDJ-RX3 has a row of buttons above its display - SOURCE, BROWSE, TAG
LIST, INFO, MENU, BACK - which a Pi with a touchscreen has nowhere to put.
So the driver keeps a strip of rows at the top of the panel clear (RB_FB_TOP,
see src/directfb/rb4r5_scale.h) and this module owns it: it draws the buttons,
lights them, and turns a touch there into the same keycode the real button
sends.

It also owns two things that take the whole screen for a moment:

  * the effect list - the FLX4 has one FX knob and no way to show what it is
    set to, so every Beat FX is listed down the left border with the selected
    one lit.  FX SELECT moves down it, SHIFT+FX SELECT moves up, and a tap
    chooses
    one;
  * the boot splash - the player takes a while to come up and a black screen
    for that long looks broken, so a progress screen covers it while the UI
    loads behind, and is taken down when the first real frame appears.

Nothing here composites: the player never writes to the strip, and while a
full-screen overlay is up the player's frames are still going into the
framebuffer underneath, so taking the overlay down means asking the player to
redraw (which it does continuously anyway).
"""
from __future__ import annotations

import json
import math
import os
import select
import struct
import time
from pathlib import Path

from . import canvas, config, fb, font, inputs, keys, util, zones

CMD_FIFO = "/tmp/rb-overlay.fifo"
STATE_FILE = "/tmp/rb-overlay.state"
LEVELS_FILE = "/tmp/rb-levels.dat"      # written by audioshim, 5 x int32
MASTER_FILE = "/tmp/rb-master.dat"      # the controller's MASTER knob, 0..1
MODAL_FILE = "/tmp/rb-overlay.modal"    # while this exists the player holds off
FRAMES_FILE = "/tmp/rb-frames.dat"      # the driver's frame counter

# --- colours, near enough to the RX3's own panel ---------------------------
# The player's scheme: flat grey panels, no shading, and the blue that
# lights a control when it is active.  Nothing here is a brand asset - it is
# the palette the hardware uses, matched by eye.  Flat on purpose: the RX3's
# own buttons are blocks of colour, and a gradient reads as a web page.
BG          = (8, 9, 12)              # the panel between the controls
BUTTON_BG   = (46, 49, 56)            # a button body: flat grey
BUTTON_EDGE = (72, 77, 88)
LABEL       = (214, 219, 230)
LIT_BG      = (0, 122, 204)           # the blue an active control lights
LIT_LABEL   = (255, 255, 255)
LIT_EDGE    = (92, 186, 255)
ACCENT      = (0, 122, 204)
# the meter: a fine scale that runs green to red, drawn as thin lines
METER_LOW   = (46, 200, 116)
METER_MID   = (236, 196, 52)
METER_HOT   = (236, 62, 48)
METER_OFF   = (26, 28, 33)
# the effect list down the left border
FX_ON_BG    = LIT_BG
FX_ON       = LIT_LABEL
FX_OFF      = (150, 157, 172)
FX_ROW      = (30, 32, 38)

# Beat FX, in the order the player steps through them.  Choosing one sends
# that many steps of the player's own FX-select control, so the ORDER is what
# matters, not the spelling: if the player lands on the wrong effect, fix the
# order in /etc/rb4r5/fx-list.json and the two agree again.
DEFAULT_FX = [
    # The XDJ-RX3's fourteen, in the order its BEAT FX selector steps through
    # them.  Fourteen is not a coincidence: onEv_BeatEffectType(SW_BFX_TYPE)
    # is a 14-position switch, and what the launcher sends is the POSITION,
    # so this order has to be the switch's.  The names are only labels - if
    # the player lands on a different effect from the one that was tapped,
    # the order is wrong, not the spelling.  Fix it in fx-list.json.
    #
    # ENIGMA JET, MOBIUS SAW and MOBIUS TRI were in this list and are DJM-900
    # effects, not the RX3's; PITCH, VINYL BRAKE, HELIX and FILTER were
    # missing.
    "DELAY", "ECHO", "PING PONG", "SPIRAL", "REVERB", "TRANS", "FILTER",
    "FLANGER", "PHASER", "PITCH", "SLIP ROLL", "ROLL", "VINYL BRAKE", "HELIX",
]

# The buttons above the RX3's screen.  "key" is a name from rb4r5/keys.py;
# "action" is ours.
DEFAULT_BUTTONS = [
    {"label": "SOURCE", "key": "source"},
    {"label": "BROWSE", "key": "browse"},
    {"label": "TAG",    "key": "taglist"},
    {"label": "INFO",   "key": "info"},
    {"label": "MENU",   "key": "menu"},
    {"label": "BACK",   "key": "back"},
    {"label": "FX",     "action": "fx"},
]


def load_json(path, fallback):
    try:
        if path and Path(path).exists():
            data = json.loads(Path(path).read_text())
            if data:
                return data
    except (OSError, ValueError) as exc:
        util.warn(f"{path} is not readable JSON ({exc}); using the default")
    return fallback


class Layout:
    """Where everything is, in framebuffer pixels."""

    def __init__(self, cfg, info: dict | None = None):
        self.cfg = cfg
        self.info = info if info is not None else fb.screeninfo(
            cfg.get("display.fbdev", "/dev/fb0"))
        self.fw = self.info.get("width", 0)
        self.fh = self.info.get("height", 0)
        self.bar_h = self.bar_height()
        self.frame = fb.frame_rect(
            self.info,
            int(cfg.get("display.ui_width", 1280)),
            int(cfg.get("display.ui_height", 800)),
            str(cfg.get("display.fit", "aspect")) != "fill",
            self.bar_h)

    def bar_height(self) -> int:
        if not self.cfg.get("display.top_bar", True) or not self.fh:
            return 0
        wanted = config.top_bar_height(
            self.fh, int(self.cfg.get("display.top_bar_height", 0) or 0))
        return min(wanted, self.fh // 3)

    def bar_rect(self) -> tuple[int, int, int, int]:
        return 0, 0, self.fw, self.bar_h


class Button:
    __slots__ = ("label", "key", "action", "channel", "x", "w", "lit", "until")

    def __init__(self, spec: dict):
        self.label = str(spec.get("label", "?")).upper()
        self.key = spec.get("key")
        self.action = spec.get("action")
        self.channel = int(spec.get("ch", 1))
        self.x = self.w = 0
        self.lit = False
        self.until = 0.0


class Overlay:
    def __init__(self, cfg):
        self.cfg = cfg
        self.layout = Layout(cfg)
        self.buttons = [Button(spec) for spec in
                        load_json(cfg.get("overlay.buttons_file"),
                                  DEFAULT_BUTTONS)]
        self.fx = [str(name).upper() for name in
                   load_json(cfg.get("overlay.fx_file"), DEFAULT_FX)]
        self.fx_index = 0                  # what we believe is selected
        self._logo = None                  # None = not looked yet, () = none
        self.mode = "none"                 # none | splash
        self.splash_progress = 0.0
        self.splash_message = ""
        self.layout_buttons()
        self.write_state()

    # -- geometry ----------------------------------------------------------
    def layout_buttons(self) -> None:
        count = max(1, len(self.buttons))
        gap = max(4, self.layout.fw // 400)
        width = (self.layout.fw - gap * (count + 1)) // count
        for index, button in enumerate(self.buttons):
            button.x = gap + index * (width + gap)
            button.w = width

    def button_at(self, x: int, y: int) -> Button | None:
        if not (0 <= y < self.layout.bar_h):
            return None
        for button in self.buttons:
            if button.x <= x < button.x + button.w:
                return button
        return None

    # -- the bar -----------------------------------------------------------
    def draw_bar(self, target: str | None = None) -> canvas.Canvas:
        bar = canvas.Canvas(self.layout.info, 0, 0,
                            self.layout.fw, self.layout.bar_h)
        bar.fill(BG)
        pad = max(2, self.layout.bar_h // 14)
        height = self.layout.bar_h - 2 * pad
        scale = self.bar_scale(pad)

        for button in self.buttons:
            lit = button.lit or (button.until > time.monotonic())
            bar.rect(button.x, pad, button.w, height,
                     LIT_BG if lit else BUTTON_BG)
            bar.frame(button.x, pad, button.w, height,
                      LIT_EDGE if lit else BUTTON_EDGE, 1)
            bar.text_centred(button.x + button.w // 2,
                             (self.layout.bar_h - font.text_height(scale)) // 2,
                             button.label, LIT_LABEL if lit else LABEL, scale)
        bar.rect(0, self.layout.bar_h - 1, self.layout.fw, 1, ACCENT)
        if target is not False:
            bar.blit(target or self.cfg.get("display.fbdev", "/dev/fb0"))
        return bar

    def bar_scale(self, pad: int) -> int:
        """One text size for the whole row: the largest every label fits in."""
        probe = canvas.Canvas(self.layout.info, 0, 0, 1, 1)
        sizes = [probe.fit_scale(button.label, button.w - pad * 4,
                                 self.layout.bar_h - pad * 5)
                 for button in self.buttons]
        return min(sizes) if sizes else 1

    # -- the splash --------------------------------------------------------
    def boot_logo(self) -> tuple[int, int, bytes] | None:
        """The image on the boot screen, if there is one to show.

        Looked for in order: `display.boot_logo`, the player's own firmware
        payload, /etc/rb4r5/boot-logo.png, and one placed next to this file.
        Without any of them the boot screen is the bar on black.
        """
        if self._logo is not None:
            return self._logo or None
        self._logo = ()                      # remember that we looked
        wanted = self.cfg.get("display.boot_logo")
        places = [wanted] if wanted else []
        places += [str(Path(self.cfg.chroot) / "root/gui/logo.png"),
                   "/etc/rb4r5/boot-logo.png",
                   str(Path(__file__).resolve().parent / "boot-logo.png")]
        for place in places:
            if not place or not Path(place).exists():
                continue
            try:
                self._logo = fb.read_png(place)
                util.info(f"overlay: boot logo from {place} "
                          f"({self._logo[0]}x{self._logo[1]})")
                return self._logo
            except (OSError, ValueError) as exc:
                util.warn(f"overlay: cannot use {place} as the boot logo "
                          f"({exc})")
        return None

    def draw_splash(self, progress: float = 0.0, message: str = "",
                    target: str | None = None) -> canvas.Canvas:
        """The boot screen: a logo and a bar filling, and nothing else.

        The player's own boot screen has no words on it, so neither does
        this.  `message` is still accepted and still logged by the caller -
        it just does not go on the screen.
        """
        self.splash_progress = max(0.0, min(1.0, progress))
        self.splash_message = message or self.splash_message
        screen = canvas.Canvas(self.layout.info, 0, 0,
                               self.layout.fw, self.layout.fh)
        screen.fill(BG)

        cx, cy = self.layout.fw // 2, self.layout.fh // 2

        # the bar sits below the middle, where the player puts it
        bar_w = self.layout.fw // 3
        bar_h = max(4, self.layout.fh // 180)
        bx, by = cx - bar_w // 2, cy + self.layout.fh // 10
        screen.round_rect(bx, by, bar_w, bar_h, (30, 33, 40), bar_h // 2)
        filled = int(bar_w * self.splash_progress)
        if filled > 0:
            screen.round_rect(bx, by, filled, bar_h, (238, 241, 248),
                              bar_h // 2)

        logo = self.boot_logo()
        if logo:
            src_w, src_h, rgba = logo
            # a third of the width, never more than a sixth of the height,
            # and never scaled up past its own size
            width = min(src_w * 4, self.layout.fw // 3)
            height = round(width * src_h / src_w)
            ceiling = self.layout.fh // 6
            if height > ceiling:
                height = ceiling
                width = round(height * src_w / src_h)
            screen.image(cx - width // 2, by - self.layout.fh // 12 - height,
                         width, height, rgba, src_w, src_h, under=BG)

        if target is not False:
            screen.blit(target or self.cfg.get("display.fbdev", "/dev/fb0"))
        return screen

    # -- the master level meter -------------------------------------------
    def meter_rect(self) -> tuple[int, int, int, int]:
        """The right-hand black bar, which is otherwise wasted.

        With the aspect fit there is a strip of black down each side of the
        picture; the master meter goes in the right one, where a DJ looks for
        it on the player itself.  If the frame fills the panel there is no bar
        to use and the meter is off.
        """
        fx, fy, fw, fh = self.layout.frame
        right = fx + fw
        spare = self.layout.fw - right
        if spare < 24:
            return 0, 0, 0, 0
        pad = max(2, spare // 10)
        return right + pad, fy + pad, spare - 2 * pad, fh - 2 * pad

    # -- the effect list ---------------------------------------------------
    def fx_rect(self) -> tuple[int, int, int, int]:
        """The left-hand black bar, which is otherwise wasted.

        The aspect fit leaves a strip of black down each side of the picture.
        The master meter uses the right one; the effect list uses the left,
        where it is always visible - the FLX4 has one FX knob and no way to
        show what it is set to, and a list you can see beats a box that has
        to be opened.
        """
        fx, fy, _fw, fh = self.layout.frame
        if fx < 24:
            return 0, 0, 0, 0
        pad = max(2, fx // 10)
        return pad, fy + pad, fx - 2 * pad, fh - 2 * pad

    def fx_rows(self, w: int, h: int) -> list[tuple[str, int, int]]:
        """(name, y, height) for each effect, top to bottom."""
        count = max(1, len(self.fx))
        row_h = max(10, h // count)
        return [(name, index * row_h, row_h)
                for index, name in enumerate(self.fx)]

    def fx_hit(self, x: int, y: int) -> int | None:
        """Which effect a touch landed on, or None."""
        rx, ry, rw, rh = self.fx_rect()
        if rw <= 0 or not (rx <= x < rx + rw and ry <= y < ry + rh):
            return None
        for index, (_name, top, height) in enumerate(self.fx_rows(rw, rh)):
            if top <= y - ry < top + height:
                return index
        return None

    @staticmethod
    def fx_lines(name: str) -> list[str]:
        """An effect name, split so it can be read in a narrow column.

        "ENIGMA JET" on one line in a 200px bar has to be tiny; on two it can
        be twice the size.  Only names with a space in them split - the rest
        are short enough already.
        """
        if " " not in name:
            return [name]
        words = name.split()
        if len(words) == 2:
            return words
        half = (len(words) + 1) // 2
        return [" ".join(words[:half]), " ".join(words[half:])]

    def fx_scale(self, w: int, h: int) -> int:
        """One text size for the whole list: the largest every name fits in.

        Sized per row, "ROLL" comes out twice the height of "MOBIUS SAW" and
        the list reads as a jumble.  The shortest name has to come down to
        the longest one's size, not the other way round.
        """
        if not self.fx:
            return 1
        probe = canvas.Canvas(self.layout.info, 0, 0, 1, 1)
        best = None
        for name, _top, height in self.fx_rows(w, h):
            lines = self.fx_lines(name)
            room = (height - 8) // len(lines)
            for line in lines:
                size = probe.fit_scale(line, w - 8, room)
                best = size if best is None else min(best, size)
        return max(1, best or 1)

    def draw_fx_strip(self, target: str | None = None) -> canvas.Canvas | None:
        """The effect list, with the selected one lit."""
        x, y, w, h = self.fx_rect()
        if w <= 0 or h <= 0:
            return None
        strip = canvas.Canvas(self.layout.info, x, y, w, h)
        strip.fill(BG)
        scale = self.fx_scale(w, h)
        line_h = font.text_height(scale)

        for index, (name, top, height) in enumerate(self.fx_rows(w, h)):
            live = index == self.fx_index
            body_h = height - 2
            strip.rect(0, top, w, body_h, FX_ON_BG if live else FX_ROW)
            if live:
                strip.frame(0, top, w, body_h, LIT_EDGE, 1)
            lines = self.fx_lines(name)
            block = line_h * len(lines) + 2 * (len(lines) - 1)
            start = top + max(0, (body_h - block) // 2)
            for row, text in enumerate(lines):
                strip.text_centred(w // 2, start + row * (line_h + 2), text,
                                   FX_ON if live else FX_OFF, scale)
        if target is not False:
            strip.blit(target or self.cfg.get("display.fbdev", "/dev/fb0"))
        return strip

    def choose_fx(self, index: int, resync: bool = False) -> str:
        """Put the player's effect selector on the effect that was chosen.

        `onEv_BeatEffectType(SW_BFX_TYPE)` is a **14-position switch**, and
        the engine is told which position it is on:

            send_rx_key(K_BFXTYPE, OP_VALUE, CH_GLOBAL, position)

        - op 5 VALUE, not ROTATE
        - the parameter is the POSITION, 0..13 - not a 10-bit 0..1023 value,
          and not a delta

        That is from the live-verified SC Live 4 port (knobshim2.c
        handle_fx_select), and it is why a rotate with a delta, a rotate with
        an absolute position, and a press and release all left the player on
        DELAY: none of them is what that control takes.

        The player shows whichever effect that position is, so the ORDER of
        the list here has to be the switch's order.  `launch.py fxhunt
        --positions` walks all fourteen so the real order can be written
        down; correct it in /etc/rb4r5/fx-list.json.
        """
        count = len(self.fx)
        index = max(0, min(index, count - 1))
        if resync:
            self.fx_index = index
            return f"marked {self.fx[index]} as the selected effect"

        mode = str(self.cfg.get("overlay.fx_mode", "position")).lower()
        landed = True
        if mode == "tap":
            for _ in range((index - self.fx_index) % count):
                landed &= keys.tap_ctrl("bfxtype", 1)
                time.sleep(0.02)
        elif mode == "delta":
            steps = index - self.fx_index
            for _ in range(abs(steps)):
                landed &= keys.rotate("bfxtype", 1, 1 if steps > 0 else -1)
                time.sleep(0.02)
        elif mode == "tenbit":
            span = max(1, count - 1)
            norm = index / span
            landed = keys.rotate("bfxtype", 1, round(norm * 1023), norm,
                                 round(norm * 16383))
        else:
            # the verified one: the switch position, as a VALUE
            landed = keys.value("bfxtype", 1, index, index / max(1, count - 1))

        self.fx_index = index
        if not landed:
            util.warn(f"overlay: nothing is reading {config.FIFO_CTRL} - the "
                      "effect was not sent to the player")
            return f"{self.fx[index]} (NOT SENT - no reader on the control fifo)"
        return f"selected {self.fx[index]} (switch position {index})"

    def step_fx(self, direction: int) -> str:
        """Move the selection one effect down (+1) or up (-1)."""
        count = len(self.fx)
        return self.choose_fx((self.fx_index + direction) % count)

    def read_levels(self) -> tuple[float, float, int]:
        """(left, right, sequence) as 0..1 of full scale."""
        try:
            blob = Path(LEVELS_FILE).read_bytes()
            if len(blob) < 20:
                return 0.0, 0.0, 0
            seq, left, right, _phones, full = struct.unpack("<5i", blob[:20])
        except (OSError, struct.error):
            return 0.0, 0.0, 0
        full = full or 8388607
        # The master knob is after the audio this measures, so the meter has
        # to be told about it or it reads the same however far it is turned
        # down.  The bridge publishes it when it is mapped; without that the
        # scale is 1 and the meter shows what is leaving the player.
        scale = self.master_level()
        return (min(1.0, max(0.0, left * scale / full)),
                min(1.0, max(0.0, right * scale / full)), seq)

    def master_level(self) -> float:
        """Where the controller's MASTER knob is, 0..1, or 1 if unknown."""
        try:
            return max(0.0, min(1.0, float(
                Path(MASTER_FILE).read_text().strip())))
        except (OSError, ValueError):
            return 1.0

    def draw_meter(self, left: float, right: float,
                   target: str | None = None) -> canvas.Canvas | None:
        """The master level, as two columns of fine lines.

        Thin lines with a gap between them, coloured along the scale rather
        than in three blocks - close to what the player draws, and it reads
        as a meter at a glance instead of as a bar chart.
        """
        x, y, w, h = self.meter_rect()
        if w <= 0 or h <= 0:
            return None
        meter = canvas.Canvas(self.layout.info, x, y, w, h)
        meter.fill(BG)

        line_h = max(1, h // 150)          # thin
        gap = max(1, line_h)
        step = line_h + gap
        segments = max(8, h // step)
        col_w = (w - max(2, w // 10)) // 2
        col_gap = w - col_w * 2

        for column, level in enumerate((left, right)):
            cx = column * (col_w + col_gap)
            db = -60.0 if level <= 0.0005 else 20.0 * math.log10(level)
            filled = int(round((db + 48.0) / 48.0 * segments))
            for index in range(segments):
                top = h - (index + 1) * step + gap
                colour = (METER_OFF if index >= filled
                          else self.meter_colour(index / max(1, segments - 1)))
                meter.rect(cx, top, col_w, line_h, colour)

        # where the player puts its red line
        line_y = h - int(h * 0.88) - 1
        meter.rect(0, line_y, w, 1, (86, 38, 34))
        scale = meter.fit_scale("LR", w, max(6, h // 40))
        meter.text(0, h - font.text_height(scale) - 1, "L", FX_OFF, scale)
        meter.text(col_w + col_gap, h - font.text_height(scale) - 1, "R",
                   FX_OFF, scale)
        if target is not False:
            meter.blit(target or self.cfg.get("display.fbdev", "/dev/fb0"))
        return meter

    @staticmethod
    def meter_colour(share: float) -> tuple[int, int, int]:
        """Green at the bottom through amber to red at the top, blended."""
        share = max(0.0, min(1.0, share))
        if share < 0.72:
            mix = share / 0.72
            low, high = METER_LOW, METER_MID
        else:
            mix = (share - 0.72) / 0.28
            low, high = METER_MID, METER_HOT
        return tuple(int(round(low[i] + (high[i] - low[i]) * mix))
                     for i in range(3))

    def hold_screen(self, mine: bool) -> None:
        """Tell the display driver to stop publishing (or start again).

        Without this the player's next frame paints straight over whatever
        was drawn - something that appears and vanishes inside 30ms, which
        reads as a flicker rather than as a change.
        """
        try:
            if mine:
                Path(MODAL_FILE).write_text("1")
                os.chmod(MODAL_FILE, 0o666)
            elif Path(MODAL_FILE).exists():
                Path(MODAL_FILE).unlink()
        except OSError as exc:
            util.warn(f"overlay: cannot set {MODAL_FILE} ({exc}); the player "
                      "will draw over the overlay")

    def frame_count(self) -> int:
        """How many frames the player's driver has published."""
        try:
            return int(Path(FRAMES_FILE).read_text().strip() or 0)
        except (OSError, ValueError):
            return -1

    def write_state(self) -> None:
        """touchd reads this: while a modal is up the UI must not be touched."""
        state = {
            "mode": self.mode,
            "bar_h": self.layout.bar_h,
            "frame": list(self.layout.frame),
            "modal": self.mode == "splash",
        }
        try:
            tmp = STATE_FILE + ".tmp"
            Path(tmp).write_text(json.dumps(state))
            os.replace(tmp, STATE_FILE)
            os.chmod(STATE_FILE, 0o666)
        except OSError:
            pass


def read_state() -> dict:
    try:
        return json.loads(Path(STATE_FILE).read_text())
    except (OSError, ValueError):
        return {}


def modal_up() -> bool:
    return bool(read_state().get("modal"))


class OverlayDaemon:
    """Owns the top bar, the effect list, the splash and the touches on them.

    It opens the touch panel itself rather than being fed by rbtouchd: evdev
    hands every reader its own copy of the events, and the two daemons split
    the screen cleanly - rbtouchd ignores everything outside the player's
    frame (that is the black bars and this bar), and this ignores everything
    inside it unless a modal is up, in which case it takes the lot.
    """

    HOLD_MS = 600

    def __init__(self, cfg):
        self.cfg = cfg
        self.overlay = Overlay(cfg)
        self.reader = None
        self.axis = {}
        self.pending = {}
        self.down_at = 0.0
        self.down_xy = (0, 0)
        self.contact = False
        self.hit = None
        self.fifo = None
        self.dirty = True
        self.splash_started = 0.0
        self.splash_frames = -1
        self.splash_sample = b""
        self.splash_min = float(cfg.get("overlay.splash_min_seconds", 2.0))
        self.splash_max = float(cfg.get("overlay.splash_max_seconds", 75.0))
        self.meter_on = bool(cfg.get("overlay.meter", True))
        self.meter_at = 0.0
        self.meter_seq = -1
        self.meter_last = (-1.0, -1.0)

    # -- devices -----------------------------------------------------------
    def open_touch(self) -> bool:
        info = inputs.find_touchscreen(self.cfg.get("touch.name"),
                                       self.cfg.get("touch.device"))
        if not info:
            return False
        self.reader = inputs.Reader(info["path"])
        mt = info["multitouch"]
        ax = info["abs"]
        self.axis = {
            "x": ax.get("mt_x" if mt else "x") or ax.get("x") or {"min": 0, "max": 4095},
            "y": ax.get("mt_y" if mt else "y") or ax.get("y") or {"min": 0, "max": 4095},
        }
        util.ok(f"overlay: touch on {info['path']} ({info['name']})")
        return True

    def open_fifo(self) -> None:
        """The command channel: the FLX4 bridge and the launcher write here."""
        try:
            if not Path(CMD_FIFO).is_fifo():
                if Path(CMD_FIFO).exists():
                    Path(CMD_FIFO).unlink()
                os.mkfifo(CMD_FIFO, 0o666)
            os.chmod(CMD_FIFO, 0o666)
            # O_RDWR so the fifo never reports EOF when a writer closes
            self.fifo = os.open(CMD_FIFO, os.O_RDWR | os.O_NONBLOCK)
        except OSError as exc:
            util.warn(f"overlay: cannot open {CMD_FIFO} ({exc}); "
                      "the controller cannot change the effect")
            self.fifo = None

    # -- coordinates -------------------------------------------------------
    def to_panel(self, raw_x: int, raw_y: int) -> tuple[int, int]:
        ax, ay = self.axis["x"], self.axis["y"]
        nx = (raw_x - ax["min"]) / max(1, ax["max"] - ax["min"])
        ny = (raw_y - ay["min"]) / max(1, ay["max"] - ay["min"])
        if self.cfg.get("touch.swap_xy"):
            nx, ny = ny, nx
        if self.cfg.get("touch.invert_x"):
            nx = 1.0 - nx
        if self.cfg.get("touch.invert_y"):
            ny = 1.0 - ny
        return (int(min(max(nx, 0.0), 1.0) * (self.overlay.layout.fw - 1)),
                int(min(max(ny, 0.0), 1.0) * (self.overlay.layout.fh - 1)))

    # -- touch -------------------------------------------------------------
    def press(self, x: int, y: int) -> None:
        over = self.overlay
        if over.mode == "splash":
            self.hit = None
            return
        index = over.fx_hit(x, y)
        if index is not None:
            self.hit = ("fx", index)
            return
        button = over.button_at(x, y)
        if not button:
            self.hit = None
            return
        self.hit = ("bar", button)
        button.lit = True
        self.dirty = True
        if button.key:
            keys.send_key(button.key, button.channel, True)

    def release(self, held_ms: float) -> None:
        over = self.overlay
        what = self.hit
        self.hit = None
        if not what:
            return
        kind, target = what

        if kind == "bar":
            if target.key:
                keys.send_key(target.key, target.channel, False)
            if target.action == "fx":
                util.info(f"overlay: {over.step_fx(1)}")
                over.draw_fx_strip()
            target.lit = False
            target.until = time.monotonic() + 0.12   # a short afterglow
            self.dirty = True
            return

        if kind == "fx":
            if target is None:
                return
            note = over.choose_fx(target, resync=held_ms >= self.HOLD_MS)
            util.info(f"overlay: {note}")
            over.draw_fx_strip()

    # -- modes -------------------------------------------------------------
    def splash(self, progress: float, message: str = "") -> None:
        if self.overlay.mode != "splash":
            self.splash_started = time.monotonic()
            self.splash_frames = self.overlay.frame_count()
        self.overlay.mode = "splash"
        self.overlay.hold_screen(True)
        self.overlay.write_state()
        self.overlay.draw_splash(progress, message)
        # take the fingerprint AFTER drawing: these are the splash's own
        # pixels, and the player redrawing over them is what ends the splash
        self.splash_sample = self.sample_frame()

    def sample_frame(self) -> bytes:
        """A few pixels from inside the player's rectangle.

        The splash covers that rectangle, so "has the player drawn yet" cannot
        be answered by asking whether anything is there - it can only be
        answered by noticing that what is there has changed.
        """
        info = self.overlay.layout.info
        x, y, w, h = self.overlay.layout.frame
        if not (w and h) or info.get("error"):
            return b""
        step = max(2, info.get("bpp", 16) // 8)
        blob = b""
        try:
            with open(info["dev"], "rb") as handle:
                for fraction in (0.30, 0.55, 0.80):
                    handle.seek(int(y + h * fraction) * info["line_length"] +
                                (x + w // 4) * step)
                    blob += handle.read(step * 96)
        except OSError:
            return b""
        return blob

    # How long to wait for touch before going round the loop again.  The
    # meter is the only thing that needs the loop to turn on its own.
    METER_TICK = 0.03
    IDLE_TICK = 0.2

    def idle_wait(self) -> float:
        if self.meter_on and self.overlay.mode != "splash":
            return self.METER_TICK
        return self.IDLE_TICK

    def poll_meter(self) -> None:
        """Redraw the master meter, but only when it would look different.

        It is a handful of rectangles in a strip the player never touches, so
        the cost is the framebuffer write and nothing else - but there is no
        point doing even that 50 times a second when the level has not moved
        a segment.
        """
        if not self.meter_on or self.overlay.mode == "splash":
            return
        now = time.monotonic()
        if now - self.meter_at < 0.05:
            return
        self.meter_at = now
        left, right, seq = self.overlay.read_levels()
        if seq == self.meter_seq and (left, right) == self.meter_last:
            return
        self.meter_seq = seq
        self.meter_last = (left, right)
        self.overlay.draw_meter(left, right)

    def poll_splash(self) -> None:
        if self.overlay.mode != "splash":
            return
        waited = time.monotonic() - self.splash_started
        if waited < self.splash_min:
            return
        if waited > self.splash_max:
            util.warn(f"overlay: the player had not drawn after {waited:.0f}s; "
                      "taking the splash down anyway")
            self.end_splash()
            return
        frames = self.overlay.frame_count()
        if frames >= 0:
            # The driver counts the frames it publishes - and the ones it
            # holds back while this splash is up - so this says "the player
            # is drawing" without the splash having to get out of the way to
            # find out.
            if self.splash_frames < 0:
                self.splash_frames = frames
            elif frames - self.splash_frames >= 8:
                util.info(f"overlay: the player is drawing "
                          f"({frames - self.splash_frames} frames) after "
                          f"{waited:.0f}s")
                self.end_splash()
            return
        now = self.sample_frame()          # no driver counter: watch the pixels
        if now and self.splash_sample and now != self.splash_sample:
            util.info(f"overlay: the player drew its first frame after "
                      f"{waited:.0f}s")
            self.end_splash()

    def redraw_borders(self) -> None:
        """Put the bar, the effect list and the meter back.

        Anything that covers the whole panel - the splash - takes them with
        it, and the player does not know they are there to restore them.
        """
        if self.overlay.mode == "splash":
            return
        self.overlay.draw_bar()
        self.overlay.draw_fx_strip()
        if self.meter_on:
            left, right, _seq = self.overlay.read_levels()
            self.overlay.draw_meter(left, right)

    def end_splash(self) -> None:
        if self.overlay.mode == "splash":
            self.overlay.mode = "none"
            self.overlay.hold_screen(False)
            self.overlay.write_state()
            self.redraw_borders()
            self.dirty = True

    # -- commands ----------------------------------------------------------
    def handle_command(self, line: str) -> None:
        parts = line.strip().split()
        if not parts:
            return
        word = parts[0].lower()
        if word in ("fx", "fx+", "fxdown", "picker"):
            util.info(f"overlay: {self.overlay.step_fx(1)}")
            self.overlay.draw_fx_strip()
        elif word in ("fx-", "fxup"):
            util.info(f"overlay: {self.overlay.step_fx(-1)}")
            self.overlay.draw_fx_strip()
        elif word == "close":
            self.end_splash()
        elif word == "splash":
            try:
                progress = float(parts[1]) if len(parts) > 1 else 0.0
            except ValueError:
                progress = 0.0
            self.splash(progress, " ".join(parts[2:]))
        elif word == "bar":
            self.dirty = True
        elif word == "quit":
            raise KeyboardInterrupt

    def read_commands(self) -> None:
        if self.fifo is None:
            return
        try:
            blob = os.read(self.fifo, 4096)
        except (BlockingIOError, OSError):
            return
        for line in blob.decode(errors="replace").splitlines():
            self.handle_command(line)

    # -- events ------------------------------------------------------------
    def handle_events(self, events) -> None:
        for etype, code, value in events:
            if etype == inputs.EV_ABS:
                if code in (inputs.ABS_MT_POSITION_X, inputs.ABS_X):
                    self.pending["x"] = value
                elif code in (inputs.ABS_MT_POSITION_Y, inputs.ABS_Y):
                    self.pending["y"] = value
                elif code == inputs.ABS_MT_TRACKING_ID:
                    self.pending["up" if value == -1 else "down"] = True
            elif etype == inputs.EV_KEY and code == inputs.BTN_TOUCH:
                self.pending["down" if value else "up"] = True
            elif etype == inputs.EV_SYN and code == inputs.SYN_REPORT:
                self.flush()

    def flush(self) -> None:
        frame, self.pending = self.pending, {}
        if "x" in frame or "y" in frame:
            self.down_xy = self.to_panel(frame.get("x", self._x),
                                         frame.get("y", self._y))
            self._x = frame.get("x", self._x)
            self._y = frame.get("y", self._y)

        if frame.get("up") and self.contact:
            self.contact = False
            self.release((time.monotonic() - self.down_at) * 1000.0)
        elif frame.get("down") and not self.contact:
            if "x" not in frame and "y" not in frame:
                self.pending["down"] = True        # wait for coordinates
                return
            self.contact = True
            self.down_at = time.monotonic()
            self.press(*self.down_xy)

    _x = 0
    _y = 0

    # -- loop --------------------------------------------------------------
    def run(self) -> int:
        over = self.overlay
        if not over.layout.fw:
            raise util.Fail("no framebuffer - nothing to draw on")
        util.info(f"overlay: bar {over.layout.bar_h}px, player frame "
                  f"{over.layout.frame[2]}x{over.layout.frame[3]} at "
                  f"{over.layout.frame[0]},{over.layout.frame[1]}")
        self.open_fifo()
        over.draw_bar()
        if over.fx_rect()[2] > 0:
            over.draw_fx_strip()
            util.info(f"overlay: the effect list is in the left bar "
                      f"({over.fx_rect()[2]}px wide, {len(over.fx)} effects)")
        else:
            util.info("overlay: no left border to put the effect list in "
                      "(the picture fills the panel)")
        if self.meter_on and over.meter_rect()[2] > 0:
            over.draw_meter(0.0, 0.0)
            util.info(f"overlay: master meter in the right bar "
                      f"({over.meter_rect()[2]}px wide)")
        elif self.meter_on:
            util.info("overlay: no room for the master meter (the frame "
                      "fills the panel - display.fit=aspect leaves a strip)")
        backoff = 1.0
        while True:
            if not self.reader and not self.open_touch():
                util.warn(f"overlay: waiting for a touchscreen ({backoff:.0f}s)")
                time.sleep(min(backoff, 0.5))
                backoff = min(backoff * 2, 10.0)
                self.read_commands()
                self.poll_splash()
                continue
            backoff = 1.0
            try:
                while True:
                    watch = [self.reader.fd] + ([self.fifo] if self.fifo else [])
                    # Touch wakes this loop by itself; the meter does not, so
                    # the timeout is what sets its frame rate.  At 0.2s it
                    # could only ever redraw five times a second however
                    # often the level was published, which looks like lag
                    # rather than like a meter.
                    ready, _, _ = select.select(watch, [], [], self.idle_wait())
                    if self.fifo in ready:
                        self.read_commands()
                    if self.reader.fd in ready:
                        events = self.reader.read()
                        if not events:
                            raise OSError("touch device gone")
                        self.handle_events(events)
                    self.poll_splash()
                    self.poll_meter()
                    if self.dirty or any(b.until and b.until < time.monotonic()
                                         for b in over.buttons):
                        for button in over.buttons:
                            if button.until and button.until < time.monotonic():
                                button.until = 0.0
                        if over.mode != "splash":
                            over.draw_bar()
                            over.draw_fx_strip()
                        self.dirty = False
            except OSError as exc:
                util.warn(f"overlay: touch lost ({exc}); rescanning")
                try:
                    self.reader.close()
                except Exception:                        # noqa: BLE001
                    pass
                self.reader = None
                time.sleep(1.0)


def run(cfg) -> int:
    daemon = OverlayDaemon(cfg)
    try:
        return daemon.run()
    finally:
        # a flag file left behind would leave the panel frozen on whatever
        # was last drawn, which is far worse than no overlay at all
        daemon.overlay.hold_screen(False)


def command(text: str) -> bool:
    """Send one command to a running overlay daemon."""
    try:
        with open(CMD_FIFO, "w") as handle:
            handle.write(text.rstrip() + "\n")
        return True
    except OSError:
        return False
