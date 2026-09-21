#!/usr/bin/env python3
"""Offline checks for `rb4r5 verify` - the parts that do not need hardware.

The full-screen probe and the log watcher are the two pieces of real logic in
there, and both can be driven from files: a synthetic framebuffer that is
either filled or letterboxed, and a log that grows while we watch it.

Run:  python3 tools/tests/test_verify.py
"""
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from rb4r5 import verify  # noqa: E402

failures = []


def check(label, got, want=True):
    if got != want:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label}")


WIDTH, HEIGHT, BPP = 320, 200, 32
STRIDE = WIDTH * 4


def framebuffer(path: Path, mode: str) -> dict:
    """Write a fake 32bpp framebuffer: 'full', 'corner', 'letterbox' or 'black'."""
    data = bytearray(STRIDE * HEIGHT)
    def paint(x0, y0, x1, y1):
        for y in range(y0, y1):
            for x in range(x0, x1):
                offset = y * STRIDE + x * 4
                data[offset:offset + 4] = b"\x40\x80\xc0\xff"
    if mode == "full":
        paint(0, 0, WIDTH, HEIGHT)
    elif mode == "corner":                     # the classic "no scaling" bug
        paint(0, 0, WIDTH // 2, HEIGHT // 2)
    elif mode == "letterbox":                  # aspect kept, bars top and bottom
        paint(0, HEIGHT // 6, WIDTH, HEIGHT - HEIGHT // 6)
    path.write_bytes(bytes(data))
    return {"dev": str(path), "width": WIDTH, "height": HEIGHT,
            "bpp": BPP, "stride": STRIDE}


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)

    filled, detail = verify._fullscreen_probe(framebuffer(tmp / "full.fb", "full"))
    check("a full frame is recognised", filled)
    check("and says so", "corners" in detail)

    filled, detail = verify._fullscreen_probe(
        framebuffer(tmp / "corner.fb", "corner"))
    check("a top-left-only frame is caught", filled, False)
    check("and names what is lit", "top-left" in detail)

    filled, detail = verify._fullscreen_probe(
        framebuffer(tmp / "letterbox.fb", "letterbox"))
    check("a letterboxed frame is caught", filled, False)

    filled, detail = verify._fullscreen_probe(
        framebuffer(tmp / "black.fb", "black"))
    check("a black screen is caught", filled, False)
    check("and is described as black", "black everywhere" in detail)

    missing = verify._fullscreen_probe({"dev": str(tmp / "nope.fb"),
                                        "width": WIDTH, "height": HEIGHT,
                                        "bpp": 32, "stride": STRIDE})
    check("a missing framebuffer is handled", missing[0], False)

    # ------------------------------------------------------------ log watching
    log = tmp / "keyshim.log"
    log.write_text("keyshim: thread started\nsendkey key=00000201\n")
    watch = verify.LogWatch([str(log)])
    check("old content is not re-read", watch.new_text(), "")

    with open(log, "a") as handle:
        handle.write("keyshim: got key ch=1\nsendkey key=00004101\n")
    check("new content is seen", "key=00004101" in watch.new_text())

    # wait_for: the line arrives while we are waiting
    watch = verify.LogWatch([str(log)])

    def append_later():
        time.sleep(0.4)
        with open(log, "a") as handle:
            handle.write("keyshim: ctrl key=0000501e op=4 ch=1 param=512\n")

    threading.Thread(target=append_later, daemon=True).start()
    found, why = verify.wait_for(watch, ["key=0000501e"], timeout=3.0,
                                 allow_skip=False)
    check("wait_for sees a control arrive", found)
    check("and reports which needle", why, "key=0000501e")

    watch = verify.LogWatch([str(log)])
    found, why = verify.wait_for(watch, ["key=deadbeef"], timeout=0.6,
                                 allow_skip=False)
    check("wait_for times out cleanly", (found, why), (False, "timeout"))

    watch = verify.LogWatch([str(tmp / "does-not-exist.log")])
    check("a missing log is not fatal", watch.new_text(), "")

    # ------------------------------------------------------------ the walk itself
    names = [c[0] for c in verify.CONTROLS]
    check("every deck transport control is covered",
          all(n in names for n in ("PLAY deck 1", "CUE deck 1", "BEAT SYNC",
                                   "LOOP IN", "LOOP OUT", "RELOOP")))
    check("every mixer control is covered",
          all(n in names for n in ("channel fader", "TRIM", "EQ HI", "EQ MID",
                                   "EQ LOW", "crossfader", "FILTER knob",
                                   "HEADPHONES MIXING")))
    check("jog, pads and Beat FX are covered",
          all(n in names for n in ("jog touch", "jog turn", "pad 1", "pad 8",
                                   "BEAT FX select", "BEAT FX on/off",
                                   "BEAT FX depth")))
    check("the quick set is a subset of the full walk",
          verify.QUICK.issubset(set(names)))
    codes = [n for _, _, needles in verify.CONTROLS for n in needles]
    check("every control names a keycode to look for",
          all(c.startswith("key=") and len(c) == 12 for c in codes))

    # the keycodes must be the ones the bridge actually sends
    from rb4r5 import keys as keymod
    walked = {c[0]: c[2][0] for c in verify.CONTROLS}
    for label, key_name in (("PLAY deck 1", "play"), ("CUE deck 1", "cue"),
                            ("channel fader", "fader"), ("crossfader", "xfader"),
                            ("tempo slider", "tempo"), ("jog turn", "jog"),
                            ("pad 1", "pad1"), ("pad 8", "pad8"),
                            ("EQ MID", "eqmid"), ("FILTER knob", "color")):
        want = f"key={keymod.resolve(key_name):08x}"
        check(f"{label} watches for the right keycode", walked[label], want)

print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("all verify checks passed")
