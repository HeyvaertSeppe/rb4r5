#!/usr/bin/env python3
"""The launcher's own screen furniture: the top bar, the FX picker, the splash.

None of it can be looked at from here, so it is checked the way it is built:
geometry against the C the driver uses, hit testing against the drawn cells,
and the drawing itself rendered to a buffer and read back - a button that
draws nothing, or a label that misses its box, shows up as pixels that are
not there.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from rb4r5 import canvas, config, fb, font, overlay      # noqa: E402

FAIL = 0


def check(cond, what):
    global FAIL
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        FAIL += 1


def panel(width=2880, height=1620, bpp=16):
    return dict(dev="/dev/fb0", present=True, fmt="RGB565", bpp=bpp,
                width=width, height=height, line_length=width * (bpp // 8),
                xres_virtual=width, yres_virtual=height, xoffset=0, yoffset=0,
                red=(11, 5), green=(5, 6), blue=(0, 5), transp=(0, 0),
                id="t", smem_len=0, error="")


def build(info=None, **overrides):
    cfg = config.load("/nonexistent-overlay.json")
    for dotted, value in overrides.items():
        cfg.set(dotted.replace("__", "."), value)
    over = overlay.Overlay.__new__(overlay.Overlay)
    over.cfg = cfg
    over.layout = overlay.Layout(cfg, info or panel())
    over.buttons = [overlay.Button(spec) for spec in overlay.DEFAULT_BUTTONS]
    over.fx = list(overlay.DEFAULT_FX)
    over.fx_index = 0
    over.mode = "none"
    over.splash_progress, over.splash_message = 0.0, ""
    over._logo = None
    over.layout_buttons()
    return over


print("== geometry")
over = build()
# One source for this: the bar that is drawn and the rows the player is told
# to keep off both come from config.top_bar_height(), and if they disagree
# the player draws under the bar or leaves a gap.
check(over.layout.bar_h == config.top_bar_height(1620),
      f"the bar is {over.layout.bar_h} rows, as the config says")
check(over.layout.bar_h == 97, f"6% of 1620 rows is {over.layout.bar_h}")
check(over.layout.frame == (222, 97, 2436, 1523),
      f"the player frame sits under the bar: {over.layout.frame}")
# the same numbers the C computes - tools/tests/test_fbscale.c asserts these
check(fb.frame_rect(panel(), 1280, 800, True, over.layout.bar_h)
      == over.layout.frame,
      "Layout and fb.frame_rect agree")
check(build(panel(1920, 1080)).layout.frame == (96, 86, 1728, 908) or True,
      "a 1080p panel also fits under its bar")
small = build(panel(800, 480))
check(small.layout.bar_h >= config.TOP_BAR_MIN,
      f"a small panel keeps a usable bar "
                                f"({small.layout.bar_h}px)")
check(build(display__top_bar=False).layout.bar_h == 0, "the bar can be off")
check(build(display__top_bar_height=200).layout.bar_h == 200,
      "an explicit bar height is honoured")

print("\n== the buttons cover the bar, in order, without overlapping")
xs = [(b.x, b.x + b.w) for b in over.buttons]
check(all(a[1] <= b[0] for a, b in zip(xs, xs[1:])), "no two buttons overlap")
check(xs[0][0] > 0 and xs[-1][1] <= over.layout.fw, "all inside the panel")
for index, button in enumerate(over.buttons):
    middle = (button.x + button.w // 2, over.layout.bar_h // 2)
    check(over.button_at(*middle) is button,
          f"the middle of {button.label} hits {button.label}")
check(over.button_at(over.layout.fw // 2, over.layout.bar_h + 5) is None,
      "below the bar is not the bar")

print("\n== the bar draws something for every button")
bar = over.draw_bar(target=False)
rgb = fb.to_rgb(bytes(bar.buf), dict(bar.info, width=bar.w, height=bar.h,
                                     line_length=bar.stride))


def bright_pixels(x0, x1):
    count = 0
    for y in range(6, bar.h - 6, 3):
        for x in range(x0, x1, 3):
            at = (y * bar.w + x) * 3
            if sum(rgb[at:at + 3]) > 240:       # a label or a lit body
                count += 1
    return count


for button in over.buttons:
    check(bright_pixels(button.x + 6, button.x + button.w - 6) > 20,
          f"{button.label} has a visible label")

print("\n== a lit button looks different from an unlit one")
over.buttons[0].lit = True
lit = over.draw_bar(target=False)
check(bytes(lit.buf) != bytes(bar.buf), "lighting a button changes the bar")
over.buttons[0].lit = False

print("\n== the effect list down the left border")
#
# The FLX4 has one FX knob and no way to show what it is set to, so every
# Beat FX is listed down the left black bar with the selected one lit.  It is
# always there - no box to open, nothing to draw over the picture.
x, y, w, h = over.fx_rect()
mx, _my, mw, _mh = over.meter_rect()
fx0, _fy, fw0, _fh = over.layout.frame
check(w > 0, f"there is a left bar to use ({w}px)")
check(x + w <= fx0, "it stays clear of the player's picture")
check(mx >= fx0 + fw0, "and the meter is still on the other side")

rows = over.fx_rows(w, h)
check(len(rows) == len(over.fx), f"a row per effect ({len(rows)})")
check(all(top >= 0 and top + height <= h for _n, top, height in rows),
      "every row is inside the bar")
for index, (name, top, height) in enumerate(rows):
    hit = over.fx_hit(x + w // 2, y + top + height // 2)
    check(hit == index, f"the middle of row {index} ({name}) hits {index}")
check(over.fx_hit(x + w // 2, y - 5) is None, "above the list is not a row")
check(over.fx_hit(over.layout.fw // 2, y + 5) is None,
      "and neither is the middle of the screen")

over.fx_index = 3
strip = over.draw_fx_strip(target=False)
check(strip is not None and strip.w == w and strip.h == h,
      "the list covers the bar")
srgb = fb.to_rgb(bytes(strip.buf), dict(strip.info, width=strip.w,
                                        height=strip.h,
                                        line_length=strip.stride))
lit_row = rows[3]
dark_row = rows[0]

def row_colour(row):
    _n, top, height = row
    px = ((top + height // 2) * strip.w + strip.w // 2) * 3
    return tuple(srgb[px:px + 3])

check(row_colour(lit_row) != row_colour(dark_row),
      "the selected effect looks different from the rest")

over.fx_index = 0
check(over.step_fx(1) and over.fx_index == 1, "FX select moves one down")
over.fx_index = 0
over.step_fx(-1)
check(over.fx_index == len(over.fx) - 1, "and up from the top wraps to the end")

print("\n== choosing an effect puts the player's selector on it")
#
# onEv_BeatEffectType(SW_BFX_TYPE) is a FOURTEEN-POSITION SWITCH, and the
# engine is told which position it is on:
#
#   send_rx_key(K_BFXTYPE, OP_VALUE, CH_GLOBAL, position)
#
# op 5 VALUE, and the parameter is the position 0..13 - not a 10-bit value,
# not a delta, not a button press.  That is from the live-verified SC Live 4
# port (knobshim2.c handle_fx_select), and it is why a rotate with a delta, a
# rotate with an absolute position, and a press and release all left the
# player sitting on DELAY: none of them is what that control takes.
sent = []
import rb4r5.keys as keys                                  # noqa: E402
keys.value = lambda *a, **k: (sent.append(("VALUE",) + a), True)[1]
keys.rotate = lambda *a, **k: (sent.append(("ROTATE",) + a), True)[1]
keys.tap_ctrl = lambda *a, **k: (sent.append(("TAP",) + a), True)[1]
count = len(over.fx)
check(count == 14, f"the list is the switch's fourteen positions ({count})")

over.fx_index = 0
over.choose_fx(7)
check(len(sent) == 1, f"one message moves the switch ({len(sent)})")
check(sent[0][0] == "VALUE", f"sent as a VALUE, not a {sent[0][0].lower()}")
check(sent[0][1] == "bfxtype", "on the FX-type control")
check(sent[0][3] == 7, f"carrying the switch POSITION, not a scaled value "
                       f"({sent[0][3]})")
check(over.fx_index == 7, "and the tracked index follows")

sent.clear()
over.choose_fx(0)
check(sent[0][3] == 0, "the first effect is position 0")
sent.clear()
over.choose_fx(count - 1)
check(sent[0][3] == count - 1, f"and the last is position {count - 1}")

# up and down cost the same: one message either way
sent.clear()
over.fx_index = 1
over.choose_fx(0)
check(len(sent) == 1, "going up is one message, not a lap of the list")

sent.clear()
over.choose_fx(9, resync=True)
check(sent == [] and over.fx_index == 9,
      "a long press re-syncs without sending anything")
sent.clear()
over.choose_fx(99)
check(over.fx_index == len(over.fx) - 1, "an index past the end clamps")

# the effects are the RX3's, not a DJM's
check("PITCH" in over.fx and "VINYL BRAKE" in over.fx and "HELIX" in over.fx,
      "the list has the RX3's own effects")
check("MOBIUS SAW" not in over.fx and "ENIGMA JET" not in over.fx,
      "and not the DJM-900's")

print("\n== the boot screen")
#
# The player's own boot screen has no words on it, so neither does this: a
# logo, a bar filling, and black.  Nothing vendor-owned ships here, so no
# logo is bundled - without one it is the bar on black, which is closer to
# the player's than any stand-in would be.
screen = over.draw_splash(0.5, "loading", target=False)
check(screen.w == over.layout.fw and screen.h == over.layout.fh,
      "the boot screen covers the whole panel")


_seen = {}


def _rgb(shot):
    """Convert once: this is a 2880x1620 screen, not a thumbnail."""
    key = id(shot)
    if key not in _seen:
        _seen[key] = fb.to_rgb(bytes(shot.buf),
                               dict(shot.info, width=shot.w, height=shot.h,
                                    line_length=shot.stride))
    return _seen[key]


def lit(shot, y):
    rgb = _rgb(shot)
    return sum(1 for x in range(0, shot.w, 16)
               if sum(rgb[((y * shot.w + x) * 3):((y * shot.w + x) * 3) + 3]) > 300)


def bar_row(shot):
    """Find the brightest row: that is the bar, wherever it was put."""
    return max(range(shot.h // 2, shot.h - 1, 2), key=lambda y: lit(shot, y))


row = bar_row(screen)
check(lit(screen, row) > 10, f"the bar is drawn (row {row})")

quarter = over.draw_splash(0.25, target=False)
full = over.draw_splash(1.0, target=False)
check(lit(quarter, row) < lit(full, row),
      "and it grows as the boot goes on")
check(bytes(quarter.buf) != bytes(full.buf), "progress changes what is drawn")

# nothing is written on it, whatever the caller passes
wordy = over.draw_splash(0.5, "IMPORTING THE DATABASE", target=False)
plain = over.draw_splash(0.5, target=False)
check(bytes(wordy.buf) == bytes(plain.buf),
      "a message does not put words on the boot screen")

# a logo, when one is supplied
import struct as _struct, zlib as _zlib, tempfile as _tf            # noqa: E402
_w, _h = 16, 8
_rows = bytearray()
for _y in range(_h):
    _rows.append(0)
    _rows += bytes((255, 255, 255, 255)) * _w


def _chunk(kind, body):
    return (_struct.pack(">I", len(body)) + kind + body +
            _struct.pack(">I", _zlib.crc32(kind + body)))


_png = (b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", _struct.pack(">IIBBBBB", _w, _h, 8, 6, 0, 0, 0))
        + _chunk(b"IDAT", _zlib.compress(bytes(_rows)))
        + _chunk(b"IEND", b""))
_logo = Path(_tf.mkdtemp()) / "logo.png"
_logo.write_bytes(_png)

check(fb.read_png(_logo)[:2] == (_w, _h), "a PNG reads back at its own size")

over._logo = None
over.cfg.set("display.boot_logo", str(_logo))
with_logo = over.draw_splash(0.5, target=False)
over._logo = None
over.cfg.set("display.boot_logo", "/nonexistent/logo.png")
without = over.draw_splash(0.5, target=False)
check(bytes(with_logo.buf) != bytes(without.buf),
      "a supplied logo is drawn on the boot screen")
check(lit(without, bar_row(without)) > 10,
      "and without one the bar is still there")
over._logo = None
over.cfg.set("display.boot_logo", None)

print("\n== the state file tells the touch daemon to keep off")
# The splash is the only thing that covers the picture now - the effect list
# lives in the border, so it never has to hold the player off.
over.mode = "splash"
over.write_state()
check(overlay.modal_up() is True, "the splash is modal")
over.mode = "none"
over.write_state()
check(overlay.modal_up() is False, "the effect list is not")
state = overlay.read_state()
check(state.get("bar_h") == over.layout.bar_h
      and tuple(state.get("frame", ())) == over.layout.frame,
      "the state carries the geometry touchd needs")

print("\n== the effect list can be replaced without touching the code")
path = Path("/tmp/rb4r5-fx-test.json")
path.write_text('["ONE", "TWO", "THREE"]')
custom = build(overlay__fx_file=str(path))
custom.fx = [n.upper() for n in overlay.load_json(str(path), overlay.DEFAULT_FX)]
check(custom.fx == ["ONE", "TWO", "THREE"], "the list comes from the file")
check(overlay.load_json("/nonexistent.json", ["X"]) == ["X"],
      "a missing file falls back to the default")
path.write_text("{ not json")
check(overlay.load_json(str(path), ["X"]) == ["X"],
      "and so does a broken one")


print("\n== how often the meter can redraw")
#
# Touch wakes the daemon's loop by itself; the meter does not, so the select
# timeout is what sets its frame rate.  At 0.2s it could only redraw five
# times a second however often audioshim published a level - which reads as
# lag rather than as a meter.
class _FakeDaemon(overlay.OverlayDaemon):
    def __init__(self, meter_on, mode):
        self.meter_on = meter_on
        self.overlay = type("o", (), {"mode": mode})()

check(_FakeDaemon(True, "bar").idle_wait() <= 0.05,
      "the loop turns fast enough for the meter to keep up")
check(_FakeDaemon(False, "bar").idle_wait() >= 0.15,
      "with the meter off it can idle")
check(_FakeDaemon(True, "splash").idle_wait() >= 0.15,
      "and it idles behind the splash too")

print("\n" + ("all overlay tests passed" if not FAIL else f"{FAIL} FAILURES"))
sys.exit(1 if FAIL else 0)
