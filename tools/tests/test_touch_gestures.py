#!/usr/bin/env python3
"""Offline checks for the touch gesture logic (no touchscreen needed).

Feeds synthetic multitouch (protocol B) and single-touch evdev frames into
the daemon and asserts which engine controls come out.

Run:  python3 tools/tests/test_touch_gestures.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from rb4r5 import config, inputs, keys, touchd  # noqa: E402

EV_ABS, EV_KEY, EV_SYN = inputs.EV_ABS, inputs.EV_KEY, inputs.EV_SYN
SLOT, TRACK = inputs.ABS_MT_SLOT, inputs.ABS_MT_TRACKING_ID
MTX, MTY = inputs.ABS_MT_POSITION_X, inputs.ABS_MT_POSITION_Y
SYN = (EV_SYN, inputs.SYN_REPORT, 0)

sent = []
failures = []


def _record(kind):
    def hook(*args, **kwargs):
        sent.append((kind, args, kwargs))
        return True
    return hook


for name in ("send_key", "tap_key", "send_ctrl", "tap_ctrl", "rotate", "value"):
    setattr(keys, name, _record(name))


def daemon(**overrides):
    cfg = config.load("/nonexistent-rb4r5.json")
    for dotted, value in overrides.items():
        cfg.set(dotted.replace("__", "."), value)
    dae = touchd.TouchDaemon(cfg)
    dae.axis = {"x": {"min": 0, "max": 1000}, "y": {"min": 0, "max": 1000}}
    dae.publish_state = lambda *a, **k: None      # no /tmp writes in the test
    return dae


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label} = {got!r}")


def touch_at(dae, x, y, slot=0):
    dae.handle_events([(EV_ABS, SLOT, slot), (EV_ABS, TRACK, 100 + slot),
                       (EV_ABS, MTX, x), (EV_ABS, MTY, y), SYN])


def move_to(dae, x, y, slot=0):
    dae.handle_events([(EV_ABS, SLOT, slot), (EV_ABS, MTX, x), (EV_ABS, MTY, y), SYN])


def release(dae, slot=0):
    dae.handle_events([(EV_ABS, SLOT, slot), (EV_ABS, TRACK, -1), SYN])


# ---------------------------------------------------------------- buttons
sent.clear()
dae = daemon()
touch_at(dae, 800, 900)          # 0.80, 0.90 -> play2 zone
release(dae)
kinds = [(s[0], s[1][0], s[1][1] if len(s[1]) > 1 else None, s[1][2] if len(s[1]) > 2 else None)
         for s in sent]
check("button press/release", kinds,
      [("send_key", "play", 2, True), ("send_key", "play", 2, False)])

# ---------------------------------------------------------------- tap in list
# A tap on a browse list has to move the highlight onto the row that was
# touched before opening it: pressing select alone opens whatever happened to
# be highlighted, which is why tapping a playlist looked like it did nothing.
sent.clear()
dae = daemon()
dae.highlight["list"] = 0        # the highlight starts at the top
touch_at(dae, 500, 400)          # inside the 'list' zone, part way down
move_to(dae, 502, 401)           # tiny movement, still a tap
release(dae)
steps = [s for s in sent if s[0] == "rotate"]
check("tap in list steps the highlight", len(steps) > 0, True)
check("tap in list turns the selector", steps[0][1][0], "selector")
check("tap in list then opens it", sent[-1][0], "tap_key")
check("tap in list opens with select", sent[-1][1][0], "select")

# tapping the row that is already highlighted just opens it
sent.clear()
dae = daemon()
row = dae.row_of(dae.zone_map["zones"][6], 400 / 1000)
dae.highlight["list"] = row
touch_at(dae, 500, 400)
release(dae)
check("tapping the highlighted row sends no steps",
      [s[0] for s in sent], ["tap_key"])

# a long press says "the highlight is already here" and sends nothing
sent.clear()
dae = daemon()
dae.hold_ms = 10
touch_at(dae, 500, 400)
time.sleep(0.05)
release(dae)
check("long press re-syncs silently", sent, [])
check("long press moved the tracked row",
      dae.highlight["list"], dae.row_of(dae.zone_map["zones"][6], 400 / 1000))

# ---------------------------------------------------------------- scroll
sent.clear()
dae = daemon(touch__scroll_step=0.05)
touch_at(dae, 500, 200)          # y 0.20
for y in range(210, 430, 10):    # drag down to y 0.42 => 0.22 / 0.05 = 4 steps
    move_to(dae, 500, y)
release(dae)
rotates = [s for s in sent if s[0] == "rotate"]
check("scroll emits browse steps", len(rotates), 4)
check("scroll direction (down = +1)", rotates[0][1][2], 1)
check("scroll sends no tap", any(s[0] == "tap_key" for s in sent), False)

sent.clear()
dae = daemon(touch__scroll_step=0.05)
touch_at(dae, 500, 600)          # drag up
for y in range(590, 370, -10):
    move_to(dae, 500, y)
release(dae)
rotates = [s for s in sent if s[0] == "rotate"]
check("scroll up direction (-1)", rotates[0][1][2], -1)

# ---------------------------------------------------------------- jog
sent.clear()
dae = daemon()
touch_at(dae, 100, 750)          # deck1-scrub zone
move_to(dae, 200, 750)
move_to(dae, 300, 750)
release(dae)
check("jog touch then release",
      [s[0] for s in sent].count("tap_ctrl"), 1)
jogs = [s for s in sent if s[0] == "rotate"]
check("jog rotate events", len(jogs) >= 2, True)
check("jog forward speed > 0", jogs[0][1][3] > 0, True)
check("jog stop sent at release", jogs[-1][1][3], 0.0)
check("jogtouch released", any(s[0] == "send_ctrl" and
                               s[1][2] == keys.OP_RELEASE for s in sent), True)

# ------------------------------------------------------- long press on a scroll
# (the browse-knob zones keep the old rule: too slow to be a tap = no key)
sent.clear()
dae = daemon(touch__tap_ms=10)
dae.zone_map["zones"][6] = dict(dae.zone_map["zones"][6], type="scroll",
                                tap_key="select")
touch_at(dae, 500, 400)
time.sleep(0.05)
release(dae)
check("long press is not a tap", sent, [])

# ---------------------------------------------------------------- extra fingers
sent.clear()
dae = daemon()
touch_at(dae, 800, 900)          # finger 1 on play2
touch_at(dae, 100, 900, slot=1)  # second finger elsewhere
release(dae, slot=1)             # lifting it must not release the button
check("second finger ignored", [s[0] for s in sent], ["send_key"])
release(dae, slot=0)
check("first finger release", [s[0] for s in sent], ["send_key", "send_key"])

# ---------------------------------------------------------------- single-touch panel
sent.clear()
dae = daemon()
dae.handle_events([(EV_KEY, inputs.BTN_TOUCH, 1), (EV_ABS, inputs.ABS_X, 800),
                   (EV_ABS, inputs.ABS_Y, 900), SYN])
dae.handle_events([(EV_KEY, inputs.BTN_TOUCH, 0), SYN])
check("single-touch panel works",
      [(s[0], s[1][0], s[1][2]) for s in sent],
      [("send_key", "play", True), ("send_key", "play", False)])

# --------------------------------------- tracking id and position in separate frames
# Panels that report the contact id in one SYN frame and its coordinates in the
# next must not have their touch-down evaluated at (0,0) - that would press
# whatever zone is in the top-left corner.
sent.clear()
dae = daemon()
dae.handle_events([(EV_ABS, SLOT, 0), (EV_ABS, TRACK, 7), SYN])
check("id-only frame sends nothing", sent, [])
dae.handle_events([(EV_ABS, MTX, 800), (EV_ABS, MTY, 900), SYN])
check("down uses the real coordinates",
      [(s[0], s[1][0], s[1][1]) for s in sent], [("send_key", "play", 2)])
release(dae)
check("release still pairs up", len(sent), 2)

# ---------------------------------------------------------------- axis flips
dae = daemon(touch__invert_x=True, touch__swap_xy=True)
dae.axis = {"x": {"min": 0, "max": 1000}, "y": {"min": 0, "max": 1000}}
check("swap+invert", tuple(round(v, 3) for v in dae.normalise(250, 750)),
      (0.25, 0.25, True))

# ------------------------------------------------------- the letterbox bars
# With display.fit = aspect the picture does not fill the panel, so panel
# coordinates are not UI coordinates.  On a 2880x1620 screen the 16:10 frame
# is 2592 wide with 144px bars: the left bar is off the picture, and the
# picture's own left edge is at 5% of the glass.
dae = daemon()
dae.axis = {"x": {"min": 0, "max": 1000}, "y": {"min": 0, "max": 1000}}
dae.frame = (144 / 2880, 0.0, 2592 / 2880, 1.0)
check("bar touch is off the picture", dae.normalise(20, 500)[2], False)
check("picture left edge maps to 0", round(dae.normalise(50, 500)[0], 3), 0.0)
check("picture right edge maps to 1", round(dae.normalise(950, 500)[0], 3), 1.0)
check("picture centre stays centred", round(dae.normalise(500, 500)[0], 3), 0.5)
check("a press on the bar presses nothing", (lambda: (
    sent.clear(),
    dae.handle_events([(EV_ABS, MTX, 10), (EV_ABS, MTY, 500),
                       (EV_ABS, TRACK, 3), SYN]),
    list(sent))[-1])(), [])

print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("all touch gesture checks passed")
