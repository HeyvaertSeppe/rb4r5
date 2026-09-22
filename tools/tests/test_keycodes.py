#!/usr/bin/env python3
"""The engine's keycodes, and the op each control takes.

Every one of these was checked against a live-verified port of the same
engine (the SC Live 4 / knobshim2 work).  Thirty-nine agreed; the ones that
did not are recorded here so they cannot drift back:

  * the tempo fader is 0x4109 HERE.  The reference port has onKey_TempoSlider
    at 0x4107; changing to it broke the fader on the RX3's build, so this one
    is pinned the other way and stays that way
  * BEAT FX SELECT takes op 5 VALUE carrying a SWITCH POSITION 0..13, not a
    rotate with a delta, not a rotate with a 10-bit position, and not a
    button press.  All three of those were tried on hardware and the player
    stayed on the first effect
  * play1/play2 and cue1/cue2 are gone: there is one play key and the deck is
    the channel.  0x4102 is CUE and 0x4104 is VINYL, so those aliases sent
    entirely different controls

Run:  python3 tools/tests/test_keycodes.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from rb4r5 import keys, overlay  # noqa: E402

failures = []


def check(label, got, want=True):
    if got != want:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label}")


# Verified against the same engine in another port.  Name -> code.
VERIFIED = {
    "source": 0x0201, "browse": 0x0202, "taglist": 0x0203, "menu": 0x0206,
    "link": 0x0207, "rekordbox": 0x0208, "usb1": 0x0209, "info": 0x020B,
    "selector": 0x420C, "back": 0x420D, "load": 0x4311,
    "play": 0x4101, "cue": 0x4102, "vinyl": 0x4104,     "keylock": 0x4108, "loopin": 0x410C, "loopout": 0x410D, "reloop": 0x410E,
    "rev": 0x410F, "slip": 0x4110, "master": 0x4111, "sync": 0x4112,
    "hotcue": 0x4113, "aloop": 0x4114, "sliploop": 0x4115, "beatjump": 0x4116,
    "pad1": 0x4117, "searchfwd": 0x411F, "searchrev": 0x4120,
    "trackfwd": 0x4214, "trackrev": 0x4215,
    "jog": 0x4305, "jogtouch": 0x4306,
    "masterlvl": 0x4403, "hpmix": 0x4405, "hplevel": 0x4406,
    "mastercue": 0x4407,
    "bfxtype": 0x448B, "bfxch": 0x448C, "bfx": 0x448D, "bfxtime": 0x448E,
    "depth": 0x448F, "beatprev": 0x4490, "beatnext": 0x4491, "tap": 0x4492,
    "trim": 0x5019, "eqhi": 0x501A, "eqmid": 0x501B, "eqlow": 0x501C,
    "fader": 0x501E, "color": 0x509D, "crush": 0x50A1, "dubecho": 0x50A2,
    "sweep": 0x50A3, "noise": 0x50A4, "space": 0x50A5, "filter": 0x50A6,
    "xfader": 0x6017, "mic": 0x0814, "effectquant": 0x0493,
}

print(f"== {len(VERIFIED)} keycodes against the verified port")
wrong = []
for name, code in sorted(VERIFIED.items(), key=lambda kv: kv[1]):
    if name not in keys.KEYS:
        wrong.append(f"{name} is missing")
    elif keys.KEYS[name][0] != code:
        wrong.append(f"{name} is 0x{keys.KEYS[name][0]:04x}, should be "
                     f"0x{code:04x}")
check("every verified keycode matches", wrong, [])

print("\n== the ones that were wrong, pinned")
# The reference port has onKey_TempoSlider at 0x4107, but swapping to it
# broke the fader on the RX3's own build, which works on 0x4109.  Hardware
# beats inference from another product - pinned so it is not "corrected"
# back again.
check("the tempo fader is 0x4109 on this build", keys.KEYS["tempo"][0], 0x4109)
check("0x4102 is CUE", keys.KEYS["cue"][0], 0x4102)
check("0x4104 is VINYL", keys.KEYS["vinyl"][0], 0x4104)
for gone in ("play1", "play2", "cue1", "cue2"):
    check(f"the {gone!r} alias is gone", gone not in keys.KEYS)

print("\n== the deck is a channel, not a different key")
per_deck = [n for n, (_c, deck) in keys.KEYS.items() if deck]
for name in ("play", "cue", "sync", "jog", "fader", "eqhi"):
    check(f"{name} is addressed per deck", name in per_deck)
for name in ("xfader", "hpmix", "bfxtype", "mastercue"):
    check(f"{name} is global", name not in per_deck)

print("\n== the Beat FX selector is a fourteen-position switch")
check("the effect list has fourteen entries", len(overlay.DEFAULT_FX), 14)
check("it starts at DELAY", overlay.DEFAULT_FX[0], "DELAY")
check("and holds the RX3's effects, not a DJM's",
      {"PITCH", "VINYL BRAKE", "HELIX", "FILTER"} <= set(overlay.DEFAULT_FX))
check("with none of the DJM-900's",
      {"MOBIUS SAW", "MOBIUS TRI", "ENIGMA JET"} & set(overlay.DEFAULT_FX),
      set())

source = (REPO / "rb4r5/overlay.py").read_text()
body = source[source.index("def choose_fx"):source.index("def step_fx")]
check("choosing one sends a VALUE", "keys.value(\"bfxtype\"" in body)
check("carrying the position, not a scaled value",
      "keys.value(\"bfxtype\", 1, index," in body)

print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("the keycodes agree with the verified port")
