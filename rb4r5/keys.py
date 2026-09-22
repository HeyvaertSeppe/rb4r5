"""XDJ-RX3 keycodes and the two FIFOs that carry them into rbp.

keyshim.so (LD_PRELOADed into the player) reads:

    /tmp/rb-keys.fifo  12-byte records { int32 key, ch, down }
    /tmp/rb-ctrl.fifo  24-byte records { int32 key, ch, op, param; float f;
                                         int32 l }

op: 0 press, 2 release, 4 rotate (relative or 10-bit absolute), 5 value.
Keycodes are the ones verified on hardware by the Prime GO / Chromebit ports
(docs/06-controller.md lists where each came from).
"""
from __future__ import annotations

import os
import struct

from . import config, util

OP_PRESS, OP_RELEASE, OP_ROTATE, OP_VALUE = 0, 2, 4, 5

# name -> (keycode, takes a deck channel?)
KEYS: dict[str, tuple[int, bool]] = {
    # global / browse
    "source": (0x0201, False),
    "browse": (0x0202, False),
    "taglist": (0x0203, False),
    "menu": (0x0206, False),
    "link": (0x0207, False),
    "rekordbox": (0x0208, False),
    "usb1": (0x0209, False),
    "info": (0x020B, False),
    "select": (0x420C, False),
    "back": (0x420D, False),
    "selector": (0x420C, False),      # the browse knob itself (op 4)
    # deck transport
    "load": (0x4311, True),
    "play": (0x4101, True),
    "cue": (0x4102, True),
    "sync": (0x4112, True),
    "master": (0x4111, True),
    # The tempo (pitch) fader is 0x4107, op 5, f in [-1..+1] with 0 at the
    # detent - verified in the SC Live 4 port against onKey_TempoSlider ->
    # DjEngineIF::setTempoSlider.  These two were the other way round here,
    # so the fader was driving whatever 0x4109 is instead.
    "tempo": (0x4107, True),
    "temporange": (0x4109, True),
    "loopin": (0x410C, True),
    "loopout": (0x410D, True),
    "reloop": (0x410E, True),
    "rev": (0x410F, True),
    "jogtouch": (0x4306, True),
    "jog": (0x4305, True),
    # pad banks + pads
    "hotcue": (0x4113, True),
    "aloop": (0x4114, True),
    "sliploop": (0x4115, True),
    "beatjump": (0x4116, True),
    **{f"pad{n}": (0x4117 + n - 1, True) for n in range(1, 9)},
    # mixer
    "fader": (0x501E, True),
    "trim": (0x5019, True),
    "eqhi": (0x501A, True),
    "eqmid": (0x501B, True),
    "eqlow": (0x501C, True),
    "xfader": (0x6017, False),
    "color": (0x509D, True),
    "filter": (0x50A6, True),
    "hpmix": (0x4405, False),
    "hplevel": (0x4406, False),
    # beat FX
    "bfxtype": (0x448B, False),
    "bfxch": (0x448C, False),
    "bfx": (0x448D, False),
    "depth": (0x448F, False),
    "beatprev": (0x4490, False),
    "beatnext": (0x4491, False),
    "tap": (0x4492, False),
    # more of the engine's keys, from the live-verified SC Live 4 port.
    # Every code this project already had agreed with it; these are the ones
    # it did not have at all.
    "vinyl": (0x4104, True),          # VINYL / CDJ mode
    "keylock": (0x4108, True),        # master tempo
    "slip": (0x4110, True),
    "searchfwd": (0x411F, True),
    "searchrev": (0x4120, True),
    "trackfwd": (0x4214, False),
    "trackrev": (0x4215, False),
    "masterlvl": (0x4403, False),
    "mastercue": (0x4407, False),
    "bfxtime": (0x448E, False),
    "crush": (0x50A1, True),          # sound colour FX
    "dubecho": (0x50A2, True),
    "sweep": (0x50A3, True),
    "noise": (0x50A4, True),
    "space": (0x50A5, True),
    "mic": (0x0814, False),
    "effectquant": (0x0493, False),
}

# The old press-key.sh aliases are gone.  play1/play2 and cue1/cue2 read as
# "deck 1's play, deck 2's play", and that is not how the engine works: there
# is ONE play key and the deck is the channel.  0x4102 is CUE, not deck 2's
# play, and 0x4104 is VINYL, not deck 2's cue - so those two sent the wrong
# control entirely.  Use `play 1` / `play 2` and `cue 1` / `cue 2`.


def resolve(name: str) -> int:
    """'play', '0x4101' or '16641' -> keycode."""
    if not name:
        raise util.Fail("empty keycode")
    text = str(name).strip().lower()
    if text in KEYS:
        return KEYS[text][0]
    try:
        return int(text, 0)
    except ValueError as exc:
        raise util.Fail(f"unknown key '{name}' (try: rb4r5 keys --list)") from exc


def takes_channel(name: str) -> bool:
    return KEYS.get(str(name).strip().lower(), (0, True))[1]


def _write(path: str, payload: bytes) -> bool:
    """Write one whole record, never blocking if the player is not reading."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        os.write(fd, payload)       # one write() = one record
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def send_key(key, ch: int = 1, down: bool = True,
             fifo: str = config.FIFO_KEYS) -> bool:
    return _write(fifo, struct.pack("<iii", resolve(key), int(ch), 1 if down else 0))


def tap_key(key, ch: int = 1, fifo: str = config.FIFO_KEYS) -> bool:
    return send_key(key, ch, True, fifo) and send_key(key, ch, False, fifo)


def send_ctrl(key, ch: int = 1, op: int = OP_PRESS, param: int = 0,
              value: float = 0.0, l: int = 0,
              fifo: str = config.FIFO_CTRL) -> bool:
    return _write(fifo, struct.pack("<iiiifi", resolve(key), int(ch), int(op),
                                    int(param), float(value), int(l)))


def tap_ctrl(key, ch: int = 1, fifo: str = config.FIFO_CTRL) -> bool:
    return (send_ctrl(key, ch, OP_PRESS, fifo=fifo) and
            send_ctrl(key, ch, OP_RELEASE, fifo=fifo))


def rotate(key, ch: int = 1, delta: int = 1, speed: float = 0.0, pos: int = 0,
           fifo: str = config.FIFO_CTRL) -> bool:
    return send_ctrl(key, ch, OP_ROTATE, delta, speed, pos, fifo)


def value(key, ch: int = 1, ten_bit: int = 512, norm: float = 0.5,
          fifo: str = config.FIFO_CTRL) -> bool:
    return send_ctrl(key, ch, OP_VALUE, ten_bit, norm, 0, fifo)


def listing() -> list[str]:
    rows = []
    for name, (code, chan) in sorted(KEYS.items(), key=lambda kv: kv[1][0]):
        rows.append(f"  {name:<12} 0x{code:04x}  {'deck channel' if chan else 'global'}")
    return rows
