"""rbtouchd - the touchscreen daemon.

Reads the panel's evdev stream, turns contacts into XDJ-RX3 controls through
the keyshim FIFOs, and publishes the current contact for the (optional) native
tsc2007 path in memshim.

    22" USB touch panel --evdev--> touchd --> /tmp/rb-keys.fifo   (buttons)
                                          --> /tmp/rb-ctrl.fifo   (knob, jog)
                                          --> /tmp/rb-touch.dat   (raw contact)

Multitouch protocol B (ABS_MT_SLOT/TRACKING_ID) and plain single-touch panels
are both handled; only the first contact drives the UI, extra fingers are
tracked so that lifting them cannot strand a gesture.
"""
from __future__ import annotations

import os
import select
import struct
import time
from pathlib import Path

from . import config, fb, inputs, keys, util, zones


class Contact:
    __slots__ = ("nx", "ny", "start_nx", "start_ny", "t0", "zone", "moved",
                 "accum", "active", "last_t", "last_nx")

    def __init__(self):
        self.reset()

    def reset(self):
        self.nx = self.ny = 0.0
        self.start_nx = self.start_ny = 0.0
        self.last_nx = 0.0
        self.t0 = self.last_t = 0.0
        self.zone = None
        self.moved = 0.0
        self.accum = 0.0
        self.active = False


class TouchDaemon:
    def __init__(self, cfg):
        self.cfg = cfg
        self.zone_map = zones.load(cfg.get("touch.zones_file"))
        self.tap_ms = float(cfg.get("touch.tap_ms", 400))
        self.tap_slop = float(cfg.get("touch.tap_slop", 0.02))
        self.scroll_step = float(cfg.get("touch.scroll_step", 0.035))
        self.jog_scale = float(cfg.get("touch.jog_scale", 3.0))
        self.swap = bool(cfg.get("touch.swap_xy", False))
        self.invert_x = bool(cfg.get("touch.invert_x", False))
        self.invert_y = bool(cfg.get("touch.invert_y", False))
        self.verbose = bool(cfg.get("touch.verbose", False))
        self.ui_w = int(cfg.get("display.ui_width", 1280))
        self.ui_h = int(cfg.get("display.ui_height", 800))
        self.frame = self._frame_fractions()
        self.state_path = config.TOUCH_STATE
        self.seq = 0
        self.contact = Contact()
        self.slot = 0
        self.slots: dict[int, bool] = {}
        self.dev = None
        self.reader = None
        self.axis = {}
        self.pending = {}          # axis values within the current SYN frame
        self.btn_touch = None
        self.on_zone = None        # callback for `calibrate`

    def _frame_fractions(self) -> tuple[float, float, float, float]:
        """Where the UI sits on the panel, as fractions of the panel.

        With display.fit = aspect the player's 16:10 frame is centred with
        black bars, so the panel and the UI are not the same rectangle any
        more: a touch two thirds of the way across the glass is NOT two thirds
        of the way across the UI.  Everything below works in UI coordinates,
        so the conversion happens once, here.
        """
        aspect = str(self.cfg.get("display.fit", "aspect")) != "fill"
        info = fb.screeninfo(self.cfg.get("display.fbdev", "/dev/fb0"))
        if info.get("error") or not info.get("width"):
            return 0.0, 0.0, 1.0, 1.0          # no framebuffer: assume it fills
        x, y, w, h = fb.frame_rect(info, self.ui_w, self.ui_h, aspect)
        fw, fh = info["width"], info["height"]
        if not (fw and fh and w and h):
            return 0.0, 0.0, 1.0, 1.0
        return x / fw, y / fh, w / fw, h / fh

    def frame_note(self) -> str:
        fx, fy, fw, fh = self.frame
        if fw >= 0.999 and fh >= 0.999:
            return "the UI fills the panel"
        return (f"the UI covers {fw * 100:.0f}% x {fh * 100:.0f}% of the panel "
                f"at {fx * 100:.0f}%,{fy * 100:.0f}% (black bars) - touches are "
                f"mapped through that")

    # -- device ------------------------------------------------------------
    def open_device(self) -> bool:
        info = inputs.find_touchscreen(self.cfg.get("touch.name"),
                                       self.cfg.get("touch.device"))
        if not info:
            return False
        self.dev = info
        self.reader = inputs.Reader(info["path"])
        mt = info["multitouch"]
        ax = info["abs"]
        self.axis = {
            "x": ax.get("mt_x" if mt else "x") or ax.get("x") or {"min": 0, "max": 4095},
            "y": ax.get("mt_y" if mt else "y") or ax.get("y") or {"min": 0, "max": 4095},
        }
        util.ok(f"touch: {info['path']} ({info['name']}) "
                f"{'multitouch' if mt else 'single-touch'} "
                f"x={self.axis['x']['min']}..{self.axis['x']['max']} "
                f"y={self.axis['y']['min']}..{self.axis['y']['max']}")
        return True

    def close_device(self) -> None:
        if self.reader:
            self.reader.close()
        self.reader = None
        self.dev = None

    # -- coordinates -------------------------------------------------------
    def normalise(self, raw_x: int, raw_y: int) -> tuple[float, float, bool]:
        """Panel coordinates -> UI coordinates, plus "was it on the UI".

        The zone map is in UI space (0..1 of the RX3's 1280x800), so the black
        bars have to come out here or every zone is shifted.
        """
        ax, ay = self.axis["x"], self.axis["y"]
        span_x = max(1, ax["max"] - ax["min"])
        span_y = max(1, ay["max"] - ay["min"])
        nx = (raw_x - ax["min"]) / span_x
        ny = (raw_y - ay["min"]) / span_y
        if self.swap:
            nx, ny = ny, nx
        if self.invert_x:
            nx = 1.0 - nx
        if self.invert_y:
            ny = 1.0 - ny

        fx, fy, fw, fh = self.frame
        nx = (nx - fx) / fw if fw > 0 else nx
        ny = (ny - fy) / fh if fh > 0 else ny
        inside = -0.001 <= nx <= 1.001 and -0.001 <= ny <= 1.001
        return min(max(nx, 0.0), 1.0), min(max(ny, 0.0), 1.0), inside

    def publish_state(self, down: bool, nx: float, ny: float) -> None:
        """Hand the contact to memshim (native tsc2007 path, docs/07-touch.md)."""
        self.seq += 1
        payload = struct.pack("<IIII", self.seq, 1 if down else 0,
                              int(nx * (self.ui_w - 1)), int(ny * (self.ui_h - 1)))
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "wb") as handle:
                handle.write(payload)
            os.replace(tmp, self.state_path)
            os.chmod(self.state_path, 0o666)
        except OSError:
            pass

    # -- gestures ----------------------------------------------------------
    def down(self, nx: float, ny: float) -> None:
        contact = self.contact
        contact.reset()
        contact.active = True
        contact.nx = contact.start_nx = contact.last_nx = nx
        contact.ny = contact.start_ny = ny
        contact.t0 = contact.last_t = time.monotonic()
        contact.zone = zones.hit(self.zone_map, nx, ny)
        zone = contact.zone
        if self.on_zone:
            self.on_zone(nx, ny, zone)
        if not zone or zone.get("type") == "none":
            return
        kind = zone.get("type", "key")
        if kind == "key":
            keys.send_key(zone["key"], zone.get("ch", 1), True)
            self.log(f"{zone['name']}: press {zone['key']} ch{zone.get('ch', 1)}")
        elif kind == "jog":
            keys.tap_ctrl("jogtouch", zone.get("ch", 1))
            self.log(f"{zone['name']}: jog touch deck {zone.get('ch', 1)}")
        elif kind == "value":
            self.emit_value(zone, nx, ny)

    def move(self, nx: float, ny: float) -> None:
        contact = self.contact
        if not contact.active:
            return
        dx, dy = nx - contact.nx, ny - contact.ny
        contact.moved = max(contact.moved,
                            abs(nx - contact.start_nx) + abs(ny - contact.start_ny))
        zone = contact.zone
        contact.nx, contact.ny = nx, ny
        if not zone:
            return
        kind = zone.get("type", "key")
        if kind == "scroll":
            # Vertical drag turns the browse knob.  Dragging down moves the
            # highlight down, which is a clockwise turn (+1) - docs/07.
            contact.accum += -dy if zone.get("invert") else dy
            step = self.scroll_step or 0.035
            while abs(contact.accum) >= step:
                direction = 1 if contact.accum > 0 else -1
                contact.accum -= direction * step
                keys.rotate(zone["key"], zone.get("ch", 1), direction)
            if abs(dy) > 0:
                self.log(f"{zone['name']}: scroll dy={dy:+.3f}")
        elif kind == "jog":
            now = time.monotonic()
            dt = max(now - contact.last_t, 0.001)
            contact.last_t = now
            # Horizontal drag across the whole screen = jog_scale revolutions.
            revs = dx * self.jog_scale
            speed = revs / dt
            speed = max(min(speed, 8.0), -8.0)
            position = int((nx * self.jog_scale * 1800)) & 0xFFFF
            keys.rotate("jog", zone.get("ch", 1), 0, speed, position)
        elif kind == "value":
            self.emit_value(zone, nx, ny)

    def up(self) -> None:
        contact = self.contact
        if not contact.active:
            return
        zone = contact.zone
        held_ms = (time.monotonic() - contact.t0) * 1000.0
        if zone:
            kind = zone.get("type", "key")
            if kind == "key":
                keys.send_key(zone["key"], zone.get("ch", 1), False)
            elif kind == "jog":
                keys.rotate("jog", zone.get("ch", 1), 0, 0.0, 0)
                keys.send_ctrl("jogtouch", zone.get("ch", 1), keys.OP_RELEASE)
            elif kind == "scroll":
                if contact.moved <= self.tap_slop and held_ms <= self.tap_ms:
                    tap = zone.get("tap_key")
                    if tap:
                        keys.tap_key(tap, zone.get("ch", 1))
                        self.log(f"{zone['name']}: tap -> {tap}")
        contact.reset()

    def emit_value(self, zone: dict, nx: float, ny: float) -> None:
        axis = zone.get("axis", "y")
        x0, y0, x1, y1 = zone["rect"]
        if axis == "x":
            frac = (nx - x0) / max(x1 - x0, 1e-6)
        else:
            frac = 1.0 - (ny - y0) / max(y1 - y0, 1e-6)
        frac = min(max(frac, 0.0), 1.0)
        ten_bit = int(frac * 1023 + 0.5)
        op = keys.OP_VALUE if zone.get("op") == "value" else keys.OP_ROTATE
        keys.send_ctrl(zone["key"], zone.get("ch", 1), op, ten_bit, frac)
        self.log(f"{zone['name']}: value {ten_bit} ({frac:.2f})")

    def log(self, msg: str) -> None:
        if self.verbose:
            util.info(f"touch: {msg}")

    # -- event loop --------------------------------------------------------
    def handle_events(self, events) -> None:
        for etype, code, value in events:
            if etype == inputs.EV_ABS:
                if code == inputs.ABS_MT_SLOT:
                    self.slot = value
                elif code == inputs.ABS_MT_TRACKING_ID:
                    if value == -1:
                        self.slots.pop(self.slot, None)
                        if self.slot == 0 or not self.slots:
                            self.pending["up"] = True
                    else:
                        self.slots[self.slot] = True
                        if self.slot == 0:
                            self.pending["down"] = True
                elif code in (inputs.ABS_MT_POSITION_X, inputs.ABS_X):
                    if self.slot == 0:
                        self.pending["x"] = value
                elif code in (inputs.ABS_MT_POSITION_Y, inputs.ABS_Y):
                    if self.slot == 0:
                        self.pending["y"] = value
            elif etype == inputs.EV_KEY and code == inputs.BTN_TOUCH:
                # single-touch panels announce contact with BTN_TOUCH
                self.pending["down" if value else "up"] = True
            elif etype == inputs.EV_SYN and code == inputs.SYN_REPORT:
                self.flush_frame()

    def flush_frame(self) -> None:
        frame, self.pending = self.pending, {}
        raw_x = frame.get("x")
        raw_y = frame.get("y")
        have_position = raw_x is not None or raw_y is not None
        if have_position:
            last_x = raw_x if raw_x is not None else self._last_raw_x
            last_y = raw_y if raw_y is not None else self._last_raw_y
            self._last_raw_x, self._last_raw_y = last_x, last_y
            nx, ny, inside = self.normalise(last_x, last_y)
        else:
            nx, ny, inside = self.contact.nx, self.contact.ny, True

        if frame.get("up"):
            # A release always wins, even in the same frame as a new contact.
            self._down_pending = False
            self.publish_state(False, self.contact.nx, self.contact.ny)
            self.up()
            return

        if frame.get("down") and not self.contact.active:
            # Some panels report the tracking id in one frame and the position
            # in the next.  Acting on the down without a position would place
            # the touch at (0, 0) - i.e. in whatever zone is top left - so wait
            # for coordinates before deciding which zone was hit.
            if not have_position:
                self._down_pending = True
                return
            # A touch on the black bar beside the picture is not a touch on
            # anything; a drag that wanders onto one keeps working, because
            # only the press decides the zone.
            if not inside:
                self.log(f"touch at {nx:.3f},{ny:.3f} is off the picture")
                return
            self.down(nx, ny)
            self.publish_state(True, nx, ny)
        elif self._down_pending and have_position:
            self._down_pending = False
            if not inside:
                return
            self.down(nx, ny)
            self.publish_state(True, nx, ny)
        elif self.contact.active and have_position:
            self.move(nx, ny)
            self.publish_state(True, nx, ny)

    _last_raw_x = 0
    _last_raw_y = 0
    _down_pending = False

    def run(self, once: bool = False) -> int:
        util.info("touch daemon starting "
                  f"(zones: {self.cfg.get('touch.zones_file')})")
        util.info(self.frame_note())
        self.publish_state(False, 0.0, 0.0)
        backoff = 1.0
        while True:
            if not self.reader and not self.open_device():
                if once:
                    util.warn("no touchscreen found")
                    return 1
                util.warn(f"waiting for a touchscreen ({backoff:.0f}s)")
                time.sleep(backoff)
                backoff = min(backoff * 2, 10.0)
                continue
            backoff = 1.0
            try:
                while True:
                    ready, _, _ = select.select([self.reader.fd], [], [], 1.0)
                    if not ready:
                        continue
                    events = self.reader.read()
                    if not events:
                        raise OSError("device gone")
                    self.handle_events(events)
            except OSError as exc:
                util.warn(f"touch device lost ({exc}); rescanning")
                self.close_device()
                self.contact.reset()
                self._down_pending = False
                self.slots.clear()
                time.sleep(1.0)
                if once:
                    return 1


def run(cfg, once: bool = False) -> int:
    for fifo in (config.FIFO_KEYS, config.FIFO_CTRL):
        if not Path(fifo).is_fifo():
            util.warn(f"{fifo} does not exist yet - the player creates it; "
                      "touches will be dropped until then")
    return TouchDaemon(cfg).run(once=once)


def why_no_touchscreen() -> list[str]:
    """Every event device and why it is not the touchscreen.

    "no touchscreen found" on its own is useless; a panel that the kernel sees
    but we rejected looks exactly like a panel that is not plugged in.
    """
    lines = ["what the kernel reports under /dev/input:"]
    found = inputs.devices()
    if not found:
        return lines + [
            "  nothing at all - no /dev/input/event* devices.",
            "  Check the USB cable of the panel (the touch side needs its own",
            "  USB lead, the video cable does not carry it), and run this as",
            "  root: the nodes are not readable otherwise.",
        ]
    for dev in found:
        why = []
        if dev["is_touchscreen"]:
            why.append("TOUCHSCREEN" + (" (multitouch)" if dev["multitouch"]
                                        else " (single touch)"))
        else:
            if inputs.ABS_MT_POSITION_X not in dev["axes"]:
                why.append("no ABS_MT_POSITION_X")
            if inputs.ABS_X not in dev["axes"]:
                why.append("no ABS_X")
            if inputs.BTN_TOUCH not in dev["keys"]:
                why.append("no BTN_TOUCH")
        lines.append(f"  {dev['path']:20} {dev['name'][:34]:34} "
                     f"{', '.join(why) or 'not a pointer'}")
    lines += [
        "",
        "If the panel is in that list as a TOUCHSCREEN, name it explicitly:",
        "    sudo python3 launch.py config set touch.device /dev/input/eventN",
        "If it is listed but not recognised, its driver reports neither",
        "multitouch nor BTN_TOUCH; send this output and it can be handled.",
    ]
    return lines


def calibrate(cfg, seconds: float = 60.0, raw: bool = False) -> int:
    """Print every touch with its normalised position and the zone it hits."""
    daemon = TouchDaemon(cfg)
    if not daemon.open_device():
        print("\n".join(why_no_touchscreen()))
        raise util.Fail("no touchscreen found")
    print(f"\n{daemon.frame_note()}")
    print(f"\nTouch the screen; each contact prints its position and zone.")
    print(f"Zones from {cfg.get('touch.zones_file')}:")
    print("\n".join(zones.describe(daemon.zone_map)))
    print(f"\nListening for {seconds:.0f}s (Ctrl-C to stop)...\n")

    hits = []
    events = [0]

    def report(nx, ny, zone):
        name = zone.get("name", "?") if zone else "(no zone)"
        kind = zone.get("type", "-") if zone else "-"
        px = int(nx * cfg.get("display.ui_width", 1280))
        py = int(ny * cfg.get("display.ui_height", 800))
        print(f"  touch  x={nx:.3f} y={ny:.3f}  (UI {px:4d},{py:4d})  "
              f"-> {name} [{kind}]")
        hits.append(name)

    daemon.on_zone = report
    # do not drive the engine while calibrating
    for name in ("send_key", "tap_key", "send_ctrl", "tap_ctrl", "rotate", "value"):
        setattr(keys, name, lambda *a, **k: True)

    names = {inputs.ABS_MT_SLOT: "ABS_MT_SLOT",
             inputs.ABS_MT_TRACKING_ID: "ABS_MT_TRACKING_ID",
             inputs.ABS_MT_POSITION_X: "ABS_MT_POSITION_X",
             inputs.ABS_MT_POSITION_Y: "ABS_MT_POSITION_Y",
             inputs.ABS_X: "ABS_X", inputs.ABS_Y: "ABS_Y"}

    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([daemon.reader.fd], [], [], 0.5)
            if not ready:
                continue
            batch = daemon.reader.read()
            events[0] += len(batch)
            if raw:
                for etype, code, value in batch:
                    if etype == inputs.EV_SYN:
                        print("  --- SYN_REPORT")
                    else:
                        label = names.get(code, f"code {code}")
                        print(f"  raw    type {etype} {label:20} = {value}")
            daemon.handle_events(batch)
    except KeyboardInterrupt:
        pass
    finally:
        daemon.close_device()

    print(f"\n{events[0]} evdev events, {len(hits)} touches seen.")
    if not events[0]:
        print("The device opened but sent nothing at all.  Either that is not "
              "the touch panel,\nor its contacts are not reaching the "
              "kernel.  Run with --raw and try again;\nif it stays silent:")
        print("\n".join(why_no_touchscreen()))
    elif not hits:
        print("Events arrived but no contact was recognised - the panel may "
              "report only\nABS_MT_* without BTN_TOUCH.  Run with --raw and "
              "send the output.")
    elif all(h == "(no zone)" for h in hits):
        print("Every touch fell outside every zone - check touch.swap_xy / "
              "invert_x / invert_y in the config.")
    return 0
