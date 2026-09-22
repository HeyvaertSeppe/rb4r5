#!/usr/bin/env python3
"""The panel link, the jog measurement, and the master meter.

All three are about numbers that were previously guessed at:
  * what the player writes down the panel link (and that somebody drains it);
  * how many messages the FLX4's jog sends for one turn of the wheel;
  * what the master output is actually doing.
"""
import struct
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from rb4r5 import config, jogcal, overlay, subucom          # noqa: E402

FAIL = 0


def check(cond, what):
    global FAIL
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        FAIL += 1


print("== panel link: spotting what changed")
check(subucom.changed_bits(b"\x00\x00", b"\x00\x01") == [(1, 0, 1)],
      "one byte differing is reported once")
check(subucom.changed_bits(b"\x0f", b"\x0f") == [], "identical is silent")
check(subucom.changed_bits(b"", b"\xab") == [(0, -1, 171)],
      "a longer frame reports the new bytes")
check("bit0+" in subucom.describe_bits(0x00, 0x01), "a bit going on")
check("bit3-" in subucom.describe_bits(0x08, 0x00), "a bit going off")
check(subucom.describe_bits(-1, 0x05) == "new byte 00000101", "a new byte")

print("\n== panel link: a capture can be replayed")
frames, _ = subucom.split_frames("subucom_spi1.0", b"\x01\x02\x03")
check(len(frames) == 1 and frames[0].data == b"\x01\x02\x03",
      "a read becomes a frame")
check(frames[0].hex() == "01 02 03", "and prints as hex")
with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as handle:
    for index, payload in enumerate([b"\xaa\xbb", b"\xaa\xbc", b"\x01"]):
        handle.write(struct.pack("<BdI", 0, 100.0 + index, len(payload)) + payload)
    capture = handle.name
rows = list(subucom.read_capture(capture))
check(len(rows) == 3, f"three frames came back ({len(rows)})")
check(rows[0][0] == "subucom_spi1.0" and rows[1][2] == b"\xaa\xbc",
      "node and payload survive the round trip")
check(subucom.changed_bits(rows[0][2], rows[1][2]) == [(1, 0xBB, 0xBC)],
      "and the difference between two captured frames is findable")
Path(capture).unlink()

print("\n== jog: counting one revolution out of raw MIDI")
counter = jogcal.Counter()
running: list = []
# 100 messages of +3 on CC 0x22, MIDI channel 1 (status 0xB0)
blob = b"".join(bytes([0xB0, 0x22, 3]) for _ in range(100))
jogcal.parse(blob, counter, running)
check(counter.ticks[(0, 0x22)] == 300, "100 x +3 is 300 ticks")
check(counter.messages[(0, 0x22)] == 100, "and 100 messages")

counter = jogcal.Counter()
# backwards: 0x7f is -1, 0x7d is -3
jogcal.parse(b"".join(bytes([0xB1, 0x22, 0x7D]) for _ in range(10)),
             counter, [])
check(counter.ticks[(1, 0x22)] == -30,
      f"values above 64 count down ({counter.ticks[(1, 0x22)]})")

counter = jogcal.Counter()
jogcal.parse(bytes([0xB0, 0x22, 2, 0x22, 2, 0x22, 2]), counter, [])
check(counter.ticks[(0, 0x22)] == 6, "running status is followed")

counter = jogcal.Counter()
jogcal.parse(bytes([0x90, 0x36, 0x7F, 0x80, 0x36, 0x00]), counter, [])
check(counter.notes[(0, 0x36)] == 1, "a plate touch is a note on then off")

counter = jogcal.Counter()
jogcal.parse(bytes([0xF8, 0xF8, 0xB0, 0x22, 1, 0xF8]), counter, [])
check(counter.ticks.get((0, 0x22)) == 1, "clock bytes are stepped over")

counter = jogcal.Counter()
jogcal.parse(bytes([0xB0, 0x0A, 64, 0xB0, 0x22, 5]), counter, [])
check(list(counter.ticks) == [(0, 0x22)],
      "CCs that are not the jog are ignored")


print("\n== the jog: what makes a turn a scratch")
#
# The plate being HELD is what makes a turn a scratch, not which CC carried
# it.  Deciding on the CC alone meant touching the top and turning still
# counted as the rim - a quarter-speed nudge - whenever the FLX4 sent the
# rim's CC.  That is "captive touch feels like the side".
bridge_src = (REPO / "src/host/flx4-bridge.c").read_text()
emit = bridge_src[bridge_src.index("static void jog_emit("):]
emit = emit[:emit.index("\n}\n")]
check("s->mode == JOG_BEND && !s->touched" in emit,
      "a held plate is never scaled down to a nudge")
check("s->last_speed = speed" in emit,
      "and the speed is remembered, so it can spin down")

tick = bridge_src[bridge_src.index("static void jog_tick("):]
tick = tick[:tick.index("\n}\n")]
check("jog_spindown_ms" in tick,
      "a let-go wheel runs down instead of stopping dead")
check("!s->touched" in tick,
      "and only while nobody is holding it")

print("\n== SMART FADER holds the pitch")
check("K_TEMPO_SLIDER && g_smart_on" in bridge_src,
      "the tempo fader is held while it is on")
check("hold both" in bridge_src or "BOTH" in bridge_src,
      "one switch holds both decks")
check("g_smart_ch = -1" in bridge_src,
      "and it is not mapped by a guess")

print("\n== the Beat FX channel is an index from zero")
#
# EnBeatEffectSelectChannel: 0 = PLAYER_0, 1 = PLAYER_1, 2 = MIC_0, 5 = MASTER.
# Sending 1 for channel 1 selected channel 2, and 2 for channel 2 selected
# the microphone.
fxch = bridge_src[bridge_src.index("static int handle_fxch("):]
fxch = fxch[:fxch.index("\n}\n")]
check("note == 0x10)      v = 0;" in fxch,
      "channel 1 is PLAYER_0")
check("note == 0x11) v = 1;" in fxch,
      "channel 2 is PLAYER_1")
check("v = 5;" in fxch,
      "and master is 5")

print("\n== the master meter")
info = dict(dev="/dev/fb0", present=True, fmt="RGB565", bpp=16,
            width=2880, height=1620, line_length=5760, xres_virtual=2880,
            yres_virtual=1620, xoffset=0, yoffset=0, red=(11, 5),
            green=(5, 6), blue=(0, 5), transp=(0, 0), id="t", smem_len=0,
            error="")


def build(**overrides):
    cfg = config.load("/nonexistent-meter.json")
    for dotted, value in overrides.items():
        cfg.set(dotted.replace("__", "."), value)
    over = overlay.Overlay.__new__(overlay.Overlay)
    over.cfg = cfg
    over.layout = overlay.Layout(cfg, info)
    over.buttons = [overlay.Button(s) for s in overlay.DEFAULT_BUTTONS]
    over.fx, over.fx_index, over.mode = list(overlay.DEFAULT_FX), 0, "none"
    over.splash_progress, over.splash_message = 0.0, ""
    over.layout_buttons()
    return over


over = build()
x, y, w, h = over.meter_rect()
fx, fy, fw, fh = over.layout.frame
check(x >= fx + fw, "the meter is to the right of the picture")
check(x + w <= over.layout.fw, "and inside the panel")
check(w > 0 and h > 0, f"with room to draw ({w}x{h})")
filled = build(display__fit="fill")
check(filled.meter_rect()[2] == 0,
      "a stretched picture leaves no bar, so no meter")

levels = Path(overlay.LEVELS_FILE)
levels.write_bytes(struct.pack("<5i", 42, 4194303, 8388607, 0, 8388607))
left, right, seq = over.read_levels()
check(abs(left - 0.5) < 0.01 and right == 1.0 and seq == 42,
      f"levels decode ({left:.2f}, {right:.2f}, seq {seq})")
levels.write_bytes(b"\x00\x00")
check(over.read_levels() == (0.0, 0.0, 0), "a short file reads as silence")
levels.unlink()
check(over.read_levels() == (0.0, 0.0, 0), "and so does a missing one")

# the master knob, when the controller sends one: the meter measures the
# audio BEFORE it, so without this it reads the same however far it is down
import os as _os                                             # noqa: E402
_master = overlay.MASTER_FILE
_had = _os.path.exists(_master)
try:
    Path(_master).write_text("1.0\n")
    over.fx_index = 0
    loud_open = over.read_levels()
    Path(_master).write_text("0.25\n")
    turned_down = over.read_levels()
    check(over.master_level() == 0.25, "the master knob is read back")
    Path(_master).write_text("nonsense\n")
    check(over.master_level() == 1.0, "and junk in it does not break the meter")
finally:
    if not _had:
        Path(_master).unlink(missing_ok=True)
check(over.master_level() == 1.0, "no knob mapped means no scaling")

quiet = over.draw_meter(0.0, 0.0, target=False)
loud = over.draw_meter(1.0, 1.0, target=False)
check(bytes(quiet.buf) != bytes(loud.buf), "silence and full scale differ")

from rb4r5 import fb                                        # noqa: E402
rgb = fb.to_rgb(bytes(loud.buf), dict(loud.info, width=loud.w, height=loud.h,
                                      line_length=loud.stride))


def at(x, y, pixels=None, shot=None):
    shot = shot or loud
    pixels = pixels if pixels is not None else rgb
    off = (y * shot.w + x) * 3
    return tuple(pixels[off:off + 3])


def lit_near(y, pixels=None, shot=None, span=12):
    """The brightest pixel within a few rows of y.

    The meter is thin lines with gaps between them now, so a single sample
    can land in a gap and read as the background.
    """
    shot = shot or loud
    pixels = pixels if pixels is not None else rgb
    rows = [at(shot.w // 4, row, pixels, shot)
            for row in range(max(0, y - span), min(shot.h, y + span))]
    return max(rows, key=sum)


bottom = lit_near(loud.h - loud.h // 12)
top = lit_near(loud.h // 40)
check(bottom[1] > bottom[0] and bottom[1] > bottom[2],
      f"the bottom of a full meter is green {bottom}")
check(top[0] > top[1] and top[0] > top[2],
      f"the top of a full meter is red {top}")

half = over.draw_meter(0.02, 0.02, target=False)
hrgb = fb.to_rgb(bytes(half.buf), dict(half.info, width=half.w, height=half.h,
                                       line_length=half.stride))
htop = lit_near(half.h // 40, hrgb, half)
check(sum(htop) < 120, f"a quiet signal leaves the top unlit {tuple(htop)}")

print("\n" + ("all panel/jog/meter tests passed" if not FAIL
              else f"{FAIL} FAILURES"))
sys.exit(1 if FAIL else 0)
