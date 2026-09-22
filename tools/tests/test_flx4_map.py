#!/usr/bin/env python3
"""The FLX4's own MIDI numbers, against what the bridge does with them.

Taken from the DDJ-FLX4 controller mapping (Mixxx, Robert904, based on the
DDJ-400 one).  Several of these were wrong here and each was a reported
fault:

  * CC 0x23 is the PLATTER with vinyl mode off - still the top of the wheel.
    It was treated as the rim, so touching the top and turning was a
    quarter-speed nudge: "captive touch feels like the side".
  * the headphone CUE buttons are note 0x54 and were not bound at all, so
    they did nothing and never lit.
  * the RX3's release-FX bank belongs on PAD FX1 (note 0x1E), not behind two
    presses of SAMPLER (0x22).
  * a pad's lamp has to be sent on the SHIFT channel too, or it goes dark
    the moment SHIFT is held.

Run:  python3 tools/tests/test_flx4_map.py
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = (REPO / "src/host/flx4-bridge.c").read_text()
failures = []


def check(label, got, want=True):
    if got != want:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label}")


def bound(note, key):
    """Is this note bound to this key, on either deck?"""
    return bool(re.search(rf"MC_DECK[12],\s*0x{note:02X},\s*{key}\b", SRC,
                          re.I))


print("== the jog wheel")
jog = SRC[SRC.index("int mode = (cc =="):]
jog = jog[:jog.index(";") + 1]
check("the platter is 0x22 AND 0x23, both the top of the wheel",
      "cc == 0x22 || cc == 0x23" in jog)
check("and only 0x29 is the search", "cc == 0x29" in jog)
check("the rim is whatever is left, which is 0x21", "JOG_BEND" in jog)

print("\n== the buttons the FLX4 actually sends")
for note, key, what in (
    (0x0B, "K_PLAY", "PLAY/PAUSE"),
    (0x0C, "K_CUE", "CUE"),
    (0x54, "K_MASTERCUE", "headphone CUE"),
    (0x10, "K_LOOPIN", "LOOP IN"),
    (0x11, "K_LOOPOUT", "LOOP OUT"),
    (0x4D, "K_RELOOP", "RELOOP/EXIT"),
    (0x58, "K_SYNC", "BEAT SYNC"),
    (0x1B, "K_HOTCUE", "PAD MODE hot cue"),
    (0x20, "K_BEATJUMP", "PAD MODE beat jump"),
    (0x6D, "K_ALOOP", "PAD MODE beat loop"),
    (0x1E, "K_SLIPLOOP", "PAD MODE pad FX1 -> release FX"),
):
    check(f"note 0x{note:02X} is {what}", bound(note, key))

check("the release FX bank is NOT on SAMPLER any more",
      bound(0x22, "K_SLIPLOOP"), False)

print("\n== the lamps")
pad = SRC[SRC.index("static void handle_pad("):]
pad = pad[:pad.index("\n}\n")]
check("a pad lights", "led_set(ch, note, on)" in pad)
check("and lights on the SHIFT channel too, or it goes dark under SHIFT",
      "MC_PAD1_SH" in pad and "MC_PAD2_SH" in pad)

rules = SRC[SRC.index("static const struct led_rule led_rules[]"):]
rules = rules[:rules.index("};")]
for key, what in (("K_PLAY", "play"), ("K_SYNC", "sync"),
                  ("K_RELOOP", "reloop"), ("K_MASTERCUE", "headphone cue"),
                  ("K_LOOPIN", "loop in"), ("K_LOOPOUT", "loop out")):
    check(f"{what} stays lit while it is on", key in rules)
check("the four pad modes are one group, so only one lights",
      rules.count("LED_RADIO"), 4)

print("\n== the Beat FX channel switch")
fxch = SRC[SRC.index("static int handle_fxch("):]
fxch = fxch[:fxch.index("\n}\n")]
check("CH1 is on channel 5 note 0x10", "MC_FX1 && note == 0x10" in fxch)
check("CH2 is on channel 6 note 0x11", "MC_FX2 && note == 0x11" in fxch)
check("and the value is an index from zero", "v = 0;" in fxch)

print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("the bridge agrees with the FLX4's own mapping")
