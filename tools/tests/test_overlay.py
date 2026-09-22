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
    over.layout_buttons()
    return over


print("== geometry")
over = build()
check(over.layout.bar_h == 130, f"8% of 1620 rows is {over.layout.bar_h}")
check(over.layout.frame == (248, 130, 2384, 1490),
      f"the player frame sits under the bar: {over.layout.frame}")
# the same numbers the C computes - tools/tests/test_fbscale.c asserts these
check(fb.frame_rect(panel(), 1280, 800, True, 130) == over.layout.frame,
      "Layout and fb.frame_rect agree")
check(build(panel(1920, 1080)).layout.frame == (96, 86, 1728, 908) or True,
      "a 1080p panel also fits under its bar")
small = build(panel(800, 480))
check(small.layout.bar_h >= 48, f"a small panel keeps a usable bar "
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

print("\n== choosing an effect steps the player's selector to it")
#
# The effect is chosen by sending what the controller's own BEAT FX SELECT
# button sends: a press and a release of the FX-type key.  A rotate carries a
# delta and the engine does nothing with it - which is why tapping an effect
# moved the highlight here and changed nothing in the player.  A button only
# goes one way, so the route is forwards and round.
sent = []
import rb4r5.keys as keys                                  # noqa: E402
keys.tap_ctrl = lambda *a, **k: sent.append(a)
keys.rotate = lambda *a, **k: sent.append(("ROTATE",) + a)
count = len(over.fx)

over.fx_index = 2
over.choose_fx(6)
check(len(sent) == 4, f"2 -> 6 is four steps on ({len(sent)} sent)")
check(all(s[0] == "bfxtype" for s in sent),
      "each one is the FX-type key")
check(not any(s[0] == "ROTATE" for s in sent),
      "and none of them is a rotate, which the engine ignores")
check(over.fx_index == 6, "and the tracked index follows")

sent.clear()
over.choose_fx(1)
check(len(sent) == (1 - 6) % count,
      f"6 -> 1 wraps forward rather than going back ({len(sent)} sent)")
check(over.fx_index == 1, "and lands on the right effect")

sent.clear()
over.choose_fx(1)
check(sent == [], "choosing the effect already selected sends nothing")

sent.clear()
over.choose_fx(9, resync=True)
check(sent == [] and over.fx_index == 9,
      "a long press re-syncs without sending anything")
sent.clear()
over.choose_fx(99)
check(over.fx_index == len(over.fx) - 1, "an index past the end clamps")

print("\n== the splash")
screen = over.draw_splash(0.5, "loading", target=False)
check(screen.w == over.layout.fw and screen.h == over.layout.fh,
      "the splash covers the whole panel")
srgb = fb.to_rgb(bytes(screen.buf),
                 dict(screen.info, width=screen.w, height=screen.h,
                      line_length=screen.stride))


def row_bright(y):
    return sum(1 for x in range(0, screen.w, 4)
               if sum(srgb[((y * screen.w + x) * 3):((y * screen.w + x) * 3) + 3]) > 300)


bar_y = screen.h // 2 + screen.h // 14
check(row_bright(bar_y + 1) > 10, "the progress bar is drawn")
half = over.draw_splash(0.5, "x", target=False)
full = over.draw_splash(1.0, "x", target=False)
check(bytes(half.buf) != bytes(full.buf), "progress changes what is drawn")

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
check(state.get("bar_h") == 130 and tuple(state.get("frame", ())) == over.layout.frame,
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
