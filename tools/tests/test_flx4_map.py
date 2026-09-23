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
  * the pad-mode lamps are on the deck channel (0x90/0x91), where their
    buttons are - they were being sent to the pad channel and never lit.

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
    (0x50, "K_EFFECTQUANT", "SHIFT+RELOOP (quantize)"),
    (0x3E, "K_SRREV", "SHIFT+LOOP CALL < (search back)"),
    (0x3D, "K_SRFWD", "SHIFT+LOOP CALL > (search forward)"),
):
    check(f"note 0x{note:02X} is {what}", bound(note, key))

note_fn = SRC[SRC.index("static void handle_note("):]
note_fn = note_fn[:note_fn.index("\n}\n")]
check("LOOP CALL < / > resize a running loop (loop_call), not the Beat FX "
      "beat", "note == 0x51 || note == 0x53" in note_fn
      and "loop_call(d, note == 0x53)" in note_fn
      and not bound(0x51, "K_BEATPREV") and not bound(0x53, "K_BEATNEXT"))
check("headphone CUE toggles the player's own channel cue",
      "RB_CMD_PFL_TOGGLE" in note_fn)

print("\n== the pad-mode buttons")
# NOT in the note table: it sends the key on every press, and a second press
# of an RX3 bank key flips it to that bank's second function.
modes = SRC[SRC.index("} pad_modes[PM_COUNT] = {"):]
modes = modes[:modes.index("};")]
for note, base, key, sub, what in (
    (0x1B, 0x0, "K_HOTCUE", 0, "hot cue"),
    (0x6D, 0x6, "K_ALOOP", 0, "beat loop"),
    (0x20, 0x2, "K_BEATJUMP", 0, "beat jump"),
    (0x1E, 0x1, "K_SLIPLOOP", 1, "PAD FX1 -> release FX (second function)"),
    (0x22, 0x3, "K_SLIPLOOP", 0, "SAMPLER -> slip loop (first function)"),
):
    check(f"note 0x{note:02X} selects {what}",
          bool(re.search(rf"0x{note:02X},\s*0x{base:X},\s*{key},\s*{sub},",
                         modes, re.I)))
    check(f"and note 0x{note:02X} is not in the note table too",
          bound(note, key), False)

enter = SRC[SRC.index("static void pad_mode_enter("):]
enter = enter[:enter.index("\n}\n")]
check("a bank key is only sent when the deck is not in that bank",
      "if (rbp_bank(d) != pm->key)" in enter)
check("and only banks with a second function are ever pressed twice",
      "bank_has_sub(pm->key)" in enter)
check("a pad press enters its bank the same way", "pad_mode_enter(deck, m)" in SRC)

print("\n== the lamps")
refresh = SRC[SRC.index("static void lamps_refresh("):]
refresh = refresh[:refresh.index("\n}\n")]
check("the lamps follow the player's LED table when it is there",
      "rs_has(RBS_LEDSTAT)" in refresh and "k->play_led" in refresh)
check("the pad-mode lamps are on the DECK channel, where their buttons are",
      "led_set(ch, pad_modes[pm].note" in refresh)
check("a loop's lamps go out with the loop",
      "led_set(ch, 0x4D, looping)" in refresh)
pads = SRC[SRC.index("static void pads_refresh("):]
pads = pads[:pads.index("\n}\n")]
check("a pad lamp is sent on the plain channel", "led_set(plain, note, v)" in pads)
check("and on the SHIFT channel, or it goes dark under SHIFT",
      "led_set(shift, note, v)" in pads)
check("lamps are only written when they change",
      "if (*last == on)" in SRC)

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
