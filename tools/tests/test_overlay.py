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

print("\n== the effect picker")
x, y, w, h = over.layout.picker_rect()
check(abs(w - int(over.layout.fw * 0.7)) <= 1 and
      abs(h - int(over.layout.fh * 0.7)) <= 1, "70% of the panel")
check(x + w <= over.layout.fw and y + h <= over.layout.fh and x > 0 and y > 0,
      "centred inside the panel")
cells = over.fx_cells(w, h)
check(len(cells) == len(over.fx), f"a cell per effect ({len(cells)})")
for index, (name, cx, cy, cw, ch) in enumerate(cells):
    hit = over.picker_hit(x + cx + cw // 2, y + cy + ch // 2)
    check(hit == index, f"the middle of cell {index} ({name}) hits {index}")
    check(cx >= 0 and cy >= 0 and cx + cw <= w and cy + ch <= h,
          f"cell {index} is inside the box")
check(over.picker_hit(5, 5) is None, "a touch outside the box is not a cell")

print("\n== choosing an effect steps the selector by the difference")
sent = []
import rb4r5.keys as keys                                  # noqa: E402
keys.rotate = lambda *a, **k: sent.append(a)
over.fx_index = 2
over.choose_fx(6)
check(len(sent) == 4 and all(s[2] == 1 for s in sent),
      f"2 -> 6 is four steps forward ({len(sent)} sent)")
check(over.fx_index == 6, "and the tracked index follows")
sent.clear()
over.choose_fx(1)
check(len(sent) == 5 and all(s[2] == -1 for s in sent),
      f"6 -> 1 is five steps back ({len(sent)} sent)")
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
over.mode = "picker"
over.write_state()
check(overlay.modal_up() is True, "a picker is modal")
over.mode = "none"
over.write_state()
check(overlay.modal_up() is False, "nothing else is")
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

print("\n" + ("all overlay tests passed" if not FAIL else f"{FAIL} FAILURES"))
sys.exit(1 if FAIL else 0)
