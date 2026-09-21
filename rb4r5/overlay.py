"""The parts of the screen the launcher draws itself.

The XDJ-RX3 has a row of buttons above its display - SOURCE, BROWSE, TAG
LIST, INFO, MENU, BACK - which a Pi with a touchscreen has nowhere to put.
So the driver keeps a strip of rows at the top of the panel clear (RB_FB_TOP,
see src/directfb/rb4r5_scale.h) and this module owns it: it draws the buttons,
lights them, and turns a touch there into the same keycode the real button
sends.

It also owns two things that take the whole screen for a moment:

  * the effect picker - the FLX4 has one FX knob and no way to see the list,
    so pressing its FX SELECT opens a grid of every Beat FX and a tap chooses
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
import os
import select
import time
from pathlib import Path

from . import canvas, config, fb, font, inputs, keys, util, zones

CMD_FIFO = "/tmp/rb-overlay.fifo"
STATE_FILE = "/tmp/rb-overlay.state"

# --- colours, near enough to the RX3's own panel ---------------------------
BG          = (10, 11, 14)
BUTTON_BG   = (26, 28, 34)
BUTTON_EDGE = (52, 56, 66)
LABEL       = (196, 202, 214)
LIT_BG      = (196, 150, 40)          # the amber the RX3 lights its buttons
LIT_LABEL   = (16, 14, 10)
ACCENT      = (70, 160, 255)
PICK_BG     = (16, 18, 23)
PICK_EDGE   = (70, 160, 255)

# Beat FX, in the order the player steps through them.  The picker moves the
# selection by sending that many steps on the FX-type encoder, so the ORDER is
# what matters, not the spelling: if the player lands on the wrong one, fix
# the order in /etc/rb4r5/fx-list.json and it will agree again.
DEFAULT_FX = [
    "DELAY", "ECHO", "PING PONG", "SPIRAL", "REVERB", "TRANS", "ENIGMA JET",
    "FLANGER", "PHASER", "FILTER", "SLIP ROLL", "ROLL", "MOBIUS SAW",
    "MOBIUS TRI",
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
        wanted = int(self.cfg.get("display.top_bar_height", 0) or 0)
        if wanted <= 0:
            # 8% of the panel, which on 1620 rows is 130 - about the
            # proportion the RX3's own button row takes
            wanted = max(48, round(self.fh * 0.08))
        return min(wanted, self.fh // 3)

    def bar_rect(self) -> tuple[int, int, int, int]:
        return 0, 0, self.fw, self.bar_h

    def picker_rect(self) -> tuple[int, int, int, int]:
        """70% of the panel, centred - what the request asked for."""
        w = int(self.fw * 0.7)
        h = int(self.fh * 0.7)
        return (self.fw - w) // 2, (self.fh - h) // 2, w, h


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
        self.mode = "none"                 # none | picker | splash
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
        pad = max(3, self.layout.bar_h // 12)
        for button in self.buttons:
            lit = button.lit or (button.until > time.monotonic())
            body = LIT_BG if lit else BUTTON_BG
            ink = LIT_LABEL if lit else LABEL
            bar.rect(button.x, pad, button.w, self.layout.bar_h - 2 * pad, body)
            bar.frame(button.x, pad, button.w, self.layout.bar_h - 2 * pad,
                      ACCENT if lit else BUTTON_EDGE, max(1, pad // 3))
            scale = bar.fit_scale(button.label, button.w - pad * 4,
                                  self.layout.bar_h - pad * 5)
            bar.text_centred(button.x + button.w // 2,
                             (self.layout.bar_h - font.text_height(scale)) // 2,
                             button.label, ink, scale)
        if target is not False:
            bar.blit(target or self.cfg.get("display.fbdev", "/dev/fb0"))
        return bar

    # -- the effect picker -------------------------------------------------
    def draw_picker(self, target: str | None = None) -> canvas.Canvas:
        x, y, w, h = self.layout.picker_rect()
        box = canvas.Canvas(self.layout.info, x, y, w, h)
        box.fill(PICK_BG)
        box.frame(0, 0, w, h, PICK_EDGE, max(2, h // 220))

        title = "BEAT FX"
        tscale = box.fit_scale(title, w // 3, h // 12)
        box.text_centred(w // 2, h // 22, title, ACCENT, tscale)

        cells = self.fx_cells(w, h)
        for index, (name, cx, cy, cw, ch) in enumerate(cells):
            chosen = index == self.fx_index
            box.rect(cx, cy, cw, ch, LIT_BG if chosen else BUTTON_BG)
            box.frame(cx, cy, cw, ch,
                      ACCENT if chosen else BUTTON_EDGE, max(1, ch // 24))
            scale = box.fit_scale(name, cw - cw // 8, ch - ch // 3)
            box.text_centred(cx + cw // 2, cy + (ch - font.text_height(scale)) // 2,
                             name, LIT_LABEL if chosen else LABEL, scale)

        hint = "TAP TO SELECT - HOLD TO SAY IT IS ALREADY ON THAT ONE"
        hscale = box.fit_scale(hint, w - w // 10, h // 22)
        box.text_centred(w // 2, h - h // 14, hint, (120, 128, 142), hscale)
        if target is not False:
            box.blit(target or self.cfg.get("display.fbdev", "/dev/fb0"))
        return box

    def fx_cells(self, w: int, h: int) -> list[tuple]:
        """Grid geometry for the effect names, inside the picker box."""
        count = max(1, len(self.fx))
        columns = 3 if count <= 12 else 4
        rows = (count + columns - 1) // columns
        margin = w // 20
        top = h // 8
        bottom = h - h // 9
        gap = max(4, w // 90)
        cw = (w - 2 * margin - gap * (columns - 1)) // columns
        ch = (bottom - top - gap * (rows - 1)) // max(1, rows)
        cells = []
        for index, name in enumerate(self.fx):
            col, row = index % columns, index // columns
            cells.append((name,
                          margin + col * (cw + gap),
                          top + row * (ch + gap), cw, ch))
        return cells

    def picker_hit(self, x: int, y: int) -> int | None:
        px, py, w, h = self.layout.picker_rect()
        if not (px <= x < px + w and py <= y < py + h):
            return None
        for index, (_name, cx, cy, cw, ch) in enumerate(self.fx_cells(w, h)):
            if cx <= x - px < cx + cw and cy <= y - py < cy + ch:
                return index
        return None

    def choose_fx(self, index: int, resync: bool = False) -> str:
        """Step the player's FX selector to the effect that was tapped.

        There is no way to ask the player which effect is selected, so the
        index is tracked here and moved by the difference.  A long press says
        "it is already on this one" and re-syncs without sending anything,
        which is the repair when the two drift apart.
        """
        index = max(0, min(index, len(self.fx) - 1))
        if resync:
            self.fx_index = index
            return f"marked {self.fx[index]} as the selected effect"
        delta = index - self.fx_index
        step = 1 if delta > 0 else -1
        for _ in range(abs(delta)):
            keys.rotate("bfxtype", 1, step)
            time.sleep(0.01)
        self.fx_index = index
        return f"selected {self.fx[index]} ({abs(delta)} step(s))"

    # -- the splash --------------------------------------------------------
    def draw_splash(self, progress: float = 0.0, message: str = "",
                    target: str | None = None) -> canvas.Canvas:
        self.splash_progress = max(0.0, min(1.0, progress))
        self.splash_message = message or self.splash_message
        screen = canvas.Canvas(self.layout.info, 0, 0,
                               self.layout.fw, self.layout.fh)
        screen.fill(BG)

        cx, cy = self.layout.fw // 2, self.layout.fh // 2
        title = "XDJ-RX3"
        tscale = screen.fit_scale(title, self.layout.fw // 2,
                                  self.layout.fh // 6, cap=24)
        screen.text_centred(cx, cy - font.text_height(tscale) - self.layout.fh // 12,
                            title, LABEL, tscale)

        sub = "ON RASPBERRY PI 5"
        sscale = max(1, tscale // 4)
        screen.text_centred(cx, cy - self.layout.fh // 20, sub, (110, 118, 132),
                            sscale)

        # the progress bar: a thin white line filling left to right
        bar_w = self.layout.fw // 3
        bar_h = max(4, self.layout.fh // 180)
        bx, by = cx - bar_w // 2, cy + self.layout.fh // 14
        screen.rect(bx, by, bar_w, bar_h, (38, 41, 48))
        screen.rect(bx, by, int(bar_w * self.splash_progress), bar_h,
                    (235, 238, 245))

        if self.splash_message:
            mscale = max(1, tscale // 6)
            screen.text_centred(cx, by + bar_h * 4, self.splash_message.upper(),
                                (120, 128, 142), mscale)
        if target is not False:
            screen.blit(target or self.cfg.get("display.fbdev", "/dev/fb0"))
        return screen

    # -- state shared with the touch daemon --------------------------------
    def write_state(self) -> None:
        """touchd reads this: while a modal is up the UI must not be touched."""
        state = {
            "mode": self.mode,
            "bar_h": self.layout.bar_h,
            "frame": list(self.layout.frame),
            "modal": self.mode in ("picker", "splash"),
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
    """Owns the top bar, the picker and the splash, and the touches on them.

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
        self.splash_sample = b""
        self.splash_min = float(cfg.get("overlay.splash_min_seconds", 2.0))
        self.splash_max = float(cfg.get("overlay.splash_max_seconds", 75.0))

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
                      "the FX picker cannot be opened from the controller")
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
        if over.mode == "picker":
            index = over.picker_hit(x, y)
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
                self.toggle_picker()
            target.lit = False
            target.until = time.monotonic() + 0.12   # a short afterglow
            self.dirty = True
            return

        if kind == "fx":
            if target is None:                      # tap outside the grid
                self.close_picker()
                return
            note = over.choose_fx(target, resync=held_ms >= self.HOLD_MS)
            util.info(f"overlay: {note}")
            over.draw_picker()
            time.sleep(0.18)                        # let the choice be seen
            self.close_picker()

    # -- modes -------------------------------------------------------------
    def toggle_picker(self) -> None:
        if self.overlay.mode == "picker":
            self.close_picker()
        else:
            self.open_picker()

    def open_picker(self) -> None:
        self.overlay.mode = "picker"
        self.overlay.write_state()
        self.overlay.draw_picker()
        for button in self.overlay.buttons:
            if button.action == "fx":
                button.lit = True
        self.dirty = True

    def close_picker(self) -> None:
        self.overlay.mode = "none"
        self.overlay.write_state()
        for button in self.overlay.buttons:
            if button.action == "fx":
                button.lit = False
        self.dirty = True
        # the player redraws continuously, so the box disappears on its own;
        # nudging it makes that immediate rather than on the next UI change
        keys.tap_key("info") if self.cfg.get("overlay.nudge_after_picker") else None

    def splash(self, progress: float, message: str = "") -> None:
        if self.overlay.mode != "splash":
            self.splash_started = time.monotonic()
        self.overlay.mode = "splash"
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
        now = self.sample_frame()
        if now and self.splash_sample and now != self.splash_sample:
            util.info(f"overlay: the player drew its first frame after "
                      f"{waited:.0f}s")
            self.end_splash()

    def end_splash(self) -> None:
        if self.overlay.mode == "splash":
            self.overlay.mode = "none"
            self.overlay.write_state()
            self.dirty = True

    # -- commands ----------------------------------------------------------
    def handle_command(self, line: str) -> None:
        parts = line.strip().split()
        if not parts:
            return
        word = parts[0].lower()
        if word in ("fx", "picker"):
            self.toggle_picker()
        elif word == "close":
            self.close_picker()
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
                    ready, _, _ = select.select(watch, [], [], 0.2)
                    if self.fifo in ready:
                        self.read_commands()
                    if self.reader.fd in ready:
                        events = self.reader.read()
                        if not events:
                            raise OSError("touch device gone")
                        self.handle_events(events)
                    self.poll_splash()
                    if self.dirty or any(b.until and b.until < time.monotonic()
                                         for b in over.buttons):
                        for button in over.buttons:
                            if button.until and button.until < time.monotonic():
                                button.until = 0.0
                        if over.mode != "splash":
                            over.draw_bar()
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
    return OverlayDaemon(cfg).run()


def command(text: str) -> bool:
    """Send one command to a running overlay daemon."""
    try:
        with open(CMD_FIFO, "w") as handle:
            handle.write(text.rstrip() + "\n")
        return True
    except OSError:
        return False
