#!/usr/bin/env python3
"""The FLX4's lamps follow the PLAYER, not the buttons.

Drives the real flx4-bridge through a pseudo-terminal - a character device,
so the bridge treats it as the controller and lights its lamps down it - with
a stand-in for the state keyshim.so publishes from inside the player
(src/shims/rb_state.h).  Then reads back what the controller was told.

Run:  python3 tools/tests/test_lamps.py
"""
import os
import select
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tty
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
failures = []

RBS_ENGINE, RBS_LEDSTAT, RBS_METERS, RBS_MIXER = 1, 2, 4, 8
OFF, ON, BLINK, UNKNOWN = 0, 1, 2, 0xFF


def check(label, got, want=True):
    if got != want:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label}")


def deck(loaded=0, playing=0, sync=0, looping=0, play_led=UNKNOWN,
         sync_led=UNKNOWN, pads=None, meter=0, pfl=0, pad_mode=0, pad_sub=0,
         rgb=None, end_warn=0):
    pads = pads or [OFF] * 8
    rgb = rgb or [(0x30, 0x30, 0x30)] * 8        # rbp's dim bank colour
    return (bytes([loaded, playing, sync, looping, 0, 0, 0, 0,
                   0, pfl, play_led, sync_led]) + bytes(pads)
            + b"".join(bytes(c) for c in rgb)
            + bytes([meter, pad_mode, pad_sub, end_warn]))


class Player:
    """Publishes a state record the way keyshim does, forty times a second."""

    def __init__(self, path):
        self.path = path
        self.decks = [deck(), deck()]
        self.flags = RBS_ENGINE | RBS_LEDSTAT | RBS_METERS | RBS_MIXER
        self.bfx = OFF
        self.seq = 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def record(self):
        self.seq += 1
        body = (struct.pack("<IIII", 0x54534252, 1, self.seq, self.flags)
                + self.decks[0] + self.decks[1]
                + bytes([self.bfx, 0, 0, 0xFF]) + struct.pack("<I", self.seq))
        assert len(body) == 120, len(body)
        return body

    def run(self):
        while not self.stop.is_set():
            tmp = self.path.with_suffix(".tmp")
            tmp.write_bytes(self.record())
            os.replace(tmp, self.path)
            time.sleep(0.025)


class Controller:
    """The FLX4's side of the pty: what it was told, and what it sends."""

    def __init__(self, fd):
        self.fd = fd
        self.lamps = {}
        self.seen = {}
        self.raw = bytearray()

    def pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([self.fd], [], [], 0.02)
            if ready:
                try:
                    self.raw += os.read(self.fd, 4096)
                except OSError:
                    break
        self.parse()

    def parse(self):
        data, i = bytes(self.raw), 0
        while i < len(data):
            b = data[i]
            if b == 0xF0:
                j = data.find(b"\xf7", i)
                if j < 0:
                    break
                self.seen[("sysex", data[i:j + 1])] = True
                i = j + 1
                continue
            if b & 0x80 and i + 2 < len(data):
                key = (b, data[i + 1])
                self.lamps[key] = data[i + 2]
                self.seen.setdefault(key, set()).add(data[i + 2])
                i += 3
                continue
            i += 1
        self.raw = bytearray(data[i:])

    def send(self, *msg):
        os.write(self.fd, bytes(msg))


def records(path):
    data = path.read_bytes() if path.exists() else b""
    out = []
    for i in range(0, len(data) - len(data) % 24, 24):
        out.append(struct.unpack("<iiiifi", data[i:i + 24]))
    return out


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    subprocess.run(["make", "-s", "-C", str(REPO / "src/host"), f"OUT={tmp}"],
                   check=True, stdout=subprocess.DEVNULL)
    bridge = tmp / "flx4-bridge"

    master, slave = os.openpty()
    tty.setraw(slave)
    tty.setraw(master)
    slave_path = os.ttyname(slave)

    ctrl = tmp / "ctrl.bin"            # a plain file: every record kept
    ctrl.write_bytes(b"")
    state = tmp / "rb-state.dat"
    player = Player(state)
    DIM = 3
    player.decks[0] = deck(loaded=1, playing=1, play_led=ON, meter=11,
                           # every empty slot lit in the bank's (bright)
                           # colour, one stored cue in its own
                           pads=[ON] * 8,
                           rgb=[(0xC0, 0xC0, 0xC0)] * 2 + [(0xFF, 0x20, 0x20)]
                               + [(0xC0, 0xC0, 0xC0)] * 5)
    player.decks[1] = deck(loaded=1, playing=0, play_led=BLINK, meter=0,
                           pfl=1)
    player.bfx = BLINK             # what rbp reports with the effect OFF
    player.thread.start()

    proc = subprocess.Popen(
        [str(bridge), "-d", slave_path, "-f", str(ctrl), "-P", str(state),
         "-M", str(tmp / "master.dat"),
         "-O", str(tmp / "overlay.fifo"), "-c", str(tmp / "jog.conf")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    flx = Controller(master)
    try:
        flx.pump(1.6)                  # lamp test, then state

        print("== the lamps show what the player says")
        check("deck 1 PLAY is lit while it plays", flx.lamps.get((0x90, 0x0B)), 0x7F)
        check("deck 2 PLAY blinks while paused",
              flx.seen.get((0x91, 0x0B), set()) >= {0x00, 0x7F})
        check("a hot cue the player holds is lit", flx.lamps.get((0x97, 0x02)), 0x7F)
        check("... on the SHIFT channel too", flx.lamps.get((0x98, 0x02)), 0x7F)
        check("an empty hot cue is dark, though rbp lights it in the bank "
              "colour", flx.lamps.get((0x97, 0x03)), 0x00)
        check("and so are all the other empty ones",
              [flx.lamps.get((0x97, p)) for p in (0, 1, 4, 5, 6, 7)],
              [0] * 6)
        check("the HOT CUE mode lamp is on the deck channel",
              flx.lamps.get((0x90, 0x1B)), 0x7F)
        check("and the other modes are dark", flx.lamps.get((0x90, 0x20)), 0x00)
        check("deck 2's headphone CUE is lit", flx.lamps.get((0x91, 0x54)), 0x7F)
        flx.seen.pop((0x94, 0x47), None)    # past the startup lamp test
        flx.pump(0.9)
        check("BEAT FX ON/OFF is lit, steady, while the effect is off",
              0x00 not in flx.seen.get((0x94, 0x47), set())
              and flx.lamps.get((0x94, 0x47)) == 0x7F)
        flx.send(0x94, 0x47, 0x7F)          # effect ON
        flx.send(0x94, 0x47, 0x00)
        flx.seen.pop((0x94, 0x47), None)
        flx.pump(1.0)
        check("and blinks once it is on",
              flx.seen.get((0x94, 0x47), set()) >= {0x00, 0x7F})
        flx.send(0x94, 0x47, 0x7F)          # and OFF again
        flx.send(0x94, 0x47, 0x00)
        flx.pump(0.2)

        print("\n== hot cue pads answer the finger at once")
        flx.send(0x97, 0x02, 0x7F)          # a stored cue: dark while held
        flx.pump(0.05)
        check("a stored hot cue goes dark while it is pressed",
              flx.lamps.get((0x97, 0x02)), 0x00)
        flx.send(0x97, 0x02, 0x00)
        flx.pump(0.05)
        check("and lights again on release", flx.lamps.get((0x97, 0x02)), 0x7F)
        flx.send(0x97, 0x05, 0x7F)          # an empty one: lights, and stays
        flx.pump(0.05)
        check("an empty pad lights when pressed", flx.lamps.get((0x97, 0x05)),
              0x7F)
        flx.send(0x97, 0x05, 0x00)
        flx.pump(0.05)
        check("and stays lit: it holds a cue now", flx.lamps.get((0x97, 0x05)),
              0x7F)

        print("\n== the track is about to end: the pads flash")
        saved = player.decks[0]
        player.decks[0] = deck(loaded=1, playing=1, play_led=ON, end_warn=1)
        flx.seen.pop((0x97, 0x00), None)
        flx.pump(1.2)
        check("pad 1 flashes", flx.seen.get((0x97, 0x00), set()) >= {0, 0x7F})
        player.decks[0] = saved
        flx.pump(0.4)

        print("\n== the MASTER LEVEL knob reaches the on-screen meter")
        flx.send(0xB6, 0x08, 0x20)
        flx.pump(0.2)
        knob = tmp / "master.dat"
        check("an unknown mixer knob is taken as MASTER LEVEL and published",
              knob.exists() and abs(float(knob.read_text()) - 0x20 / 127)
              < 0.01)

        print("\n== each meter is its own deck")
        check("deck 1 playing loud: full", flx.lamps.get((0xB0, 0x02)), 127)
        check("deck 2 stopped: dark", flx.lamps.get((0xB1, 0x02)), 0)

        print("\n== the faders are asked where they are")
        check("the position query went out",
              any(k[0] == "sysex" and k[1].startswith(b"\xf0\x00\x40\x05")
                  for k in flx.seen))

        print("\n== a loop that ends puts its lamps out")
        player.decks[0] = deck(loaded=1, playing=1, play_led=ON, looping=1)
        flx.pump(0.9)
        check("looping: RELOOP/EXIT lit", flx.lamps.get((0x90, 0x4D)), 0x7F)
        check("looping: LOOP OUT flashes",
              flx.seen.get((0x90, 0x11), set()) >= {0x00, 0x7F})
        player.decks[0] = deck(loaded=1, playing=1, play_led=ON, looping=0)
        flx.pump(0.5)
        check("loop gone: RELOOP/EXIT dark", flx.lamps.get((0x90, 0x4D)), 0x00)
        check("loop gone: LOOP IN dark", flx.lamps.get((0x90, 0x10)), 0x00)
        check("loop gone: LOOP OUT dark", flx.lamps.get((0x90, 0x11)), 0x00)

        print("\n== headphone CUE goes to the player's mixer, per channel")
        flx.send(0x90, 0x54, 0x7F)
        flx.send(0x90, 0x54, 0x00)
        flx.pump(0.3)
        check("deck 1 CUE asks keyshim to toggle channel 1",
              (0x7E54, 1, 0) in [(r[0], r[1], r[2]) for r in records(ctrl)])
        check("and not MASTER CUE",
              [r for r in records(ctrl) if r[0] == 0x4407], [])

        print("\n== the pad banks")
        before = len(records(ctrl))
        flx.send(0x90, 0x1B, 0x7F)          # HOT CUE: already there
        flx.pump(0.2)
        check("HOT CUE when already in HOT CUE sends nothing",
              [r for r in records(ctrl)[before:] if r[0] == 0x4113], [])

        flx.send(0x90, 0x1E, 0x7F)          # PAD FX1 -> RELEASE FX
        flx.pump(0.1)
        player.decks[0] = deck(loaded=1, playing=1, play_led=ON,
                               pad_mode=2, pad_sub=0)
        flx.pump(0.6)
        taps = [r for r in records(ctrl)[before:] if r[0] == 0x4115 and r[2] == 0]
        check("PAD FX1 enters the bank, sees it opened on SLIP LOOP, and "
              "flips it once to RELEASE FX", len(taps), 2)
        check("PAD FX1's lamp is lit", flx.lamps.get((0x90, 0x1E)), 0x7F)
        check("HOT CUE's is not", flx.lamps.get((0x90, 0x1B)), 0x00)

        player.decks[0] = deck(loaded=1, playing=1, play_led=ON,
                               pad_mode=2, pad_sub=1)
        flx.pump(0.4)
        before = len(records(ctrl))
        flx.send(0x90, 0x1E, 0x7F)          # PAD FX1 again: stay put
        flx.pump(0.2)
        check("PAD FX1 again does NOT flip release FX over to slip loop",
              [r for r in records(ctrl)[before:] if r[0] == 0x4115], [])
        flx.send(0x90, 0x22, 0x7F)          # SAMPLER -> SLIP LOOP
        flx.pump(0.2)
        taps = [r for r in records(ctrl)[before:] if r[0] == 0x4115 and r[2] == 0]
        check("SAMPLER flips the same bank to SLIP LOOP, once", len(taps), 1)

        print("\n== LOOP CALL resizes a running beat loop")
        player.decks[0] = deck(loaded=1, playing=1, play_led=ON, looping=1,
                               pad_mode=1, pad_sub=0,
                               pads=[OFF, OFF, OFF, ON, OFF, OFF, OFF, OFF])
        flx.send(0x90, 0x6D, 0x7F)          # BEAT LOOP mode
        flx.pump(0.6)
        before = len(records(ctrl))
        flx.send(0x90, 0x53, 0x7F)          # > : longer
        flx.send(0x90, 0x53, 0x00)
        flx.pump(0.2)
        pads = [r for r in records(ctrl)[before:]
                if 0x4117 <= r[0] <= 0x411E and r[2] == 0]
        check("> presses the pad one longer (pad 3, left of lit pad 4)",
              [r[0] - 0x4117 + 1 for r in pads], [3])
        check("and not the Beat FX's BEAT key",
              [r for r in records(ctrl)[before:] if r[0] in (0x4490, 0x4491)], [])
        print("\n== SHIFT + RELOOP/EXIT is key lock")
        before = len(records(ctrl))
        flx.send(0x90, 0x50, 0x7F)
        flx.send(0x90, 0x50, 0x00)
        flx.pump(0.2)
        check("it sends MASTER TEMPO for that deck",
              [(r[0], r[1]) for r in records(ctrl)[before:] if r[2] == 0],
              [(0x4108, 1)])

        print("\n== a backspin runs down instead of stopping dead")
        before = len(records(ctrl))
        flx.send(0x90, 0x36, 0x7F)          # hand on the plate
        for _ in range(12):                 # fling it backwards, hard
            flx.send(0xB0, 0x22, 0x80 - 40)
            time.sleep(0.01)
        flx.send(0x90, 0x36, 0x00)          # and let go
        flx.pump(4.5)
        recs = records(ctrl)[before:]
        letgo = [i for i, r in enumerate(recs) if r[0] == 0x4306 and r[2] == 2]
        spin = [r[4] for i, r in enumerate(recs)
                if r[0] == 0x4305 and letgo and i < letgo[0] and r[4] < -0.05]
        check("the plate is let go of only once the spin has run down",
              bool(letgo) and len(spin) > 20)
        check("the spin keeps more speed than the throw (momentum)",
              bool(spin) and min(spin) < -6.0)
        half = spin[:len(spin) // 2]
        check("and holds its speed before running out - not a tape stop "
              "(steady loss per second, so half-way it is still fast)",
              bool(half) and half[-1] < min(spin) / 2.5)
    finally:
        proc.terminate()
        out = proc.communicate(timeout=5)[0].decode(errors="replace")
        player.stop.set()
        os.close(master)
        os.close(slave)
    if failures:
        print("\n--- bridge log ---\n" + out)

print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("the lamps follow the player")
