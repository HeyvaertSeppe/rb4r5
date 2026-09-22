#!/usr/bin/env python3
"""Every DDJ-FLX4 control, end to end, without a DDJ-FLX4.

This drives the real `flx4-bridge` binary with the MIDI bytes an FLX4 sends for
each control, reads the records it writes to the control FIFO, and renders them
exactly as keyshim.so logs them inside the player.  Then it asks `rb4r5 verify`
whether it would have seen that control arrive.

In other words it exercises the whole chain except the two ends we cannot have
here - the physical controller and the player itself:

    MIDI bytes -> flx4-bridge -> /tmp/rb-ctrl.fifo -> (keyshim format) -> verify

Run:  python3 tools/tests/test_control_chain.py
"""
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from rb4r5 import verify  # noqa: E402

# The MIDI an FLX4 sends for each control in the verification walk.
# 14-bit controls send MSB then LSB; relative encoders send a signed delta.
MIDI = {
    "PLAY deck 1":          [0x90, 0x0B, 0x7F],
    "CUE deck 1":           [0x90, 0x0C, 0x7F],
    "PLAY deck 2":          [0x91, 0x0B, 0x7F],
    "BEAT SYNC":            [0x90, 0x58, 0x7F],
    "SYNC long press":      [0x90, 0x5C, 0x7F],
    "SHIFT + PLAY":         [0x90, 0x0E, 0x7F],
    "browse knob":          [0xB6, 0x40, 0x02],
    "browse push":          [0x96, 0x41, 0x7F],
    "SHIFT + browse push":  [0x96, 0x42, 0x7F],
    "LOAD deck 1":          [0x96, 0x46, 0x7F],
    "channel fader":        [0xB0, 0x13, 0x40, 0xB0, 0x33, 0x00],
    "TRIM":                 [0xB0, 0x04, 0x40, 0xB0, 0x24, 0x00],
    "EQ HI":                [0xB0, 0x07, 0x40, 0xB0, 0x27, 0x00],
    "EQ MID":               [0xB0, 0x0B, 0x40, 0xB0, 0x2B, 0x00],
    "EQ LOW":               [0xB0, 0x0F, 0x40, 0xB0, 0x2F, 0x00],
    "crossfader":           [0xB6, 0x1F, 0x40, 0xB6, 0x3F, 0x00],
    "tempo slider":         [0xB0, 0x00, 0x60, 0xB0, 0x20, 0x00],
    "FILTER knob":          [0xB6, 0x17, 0x40, 0xB6, 0x37, 0x00],
    "HEADPHONES MIXING":    [0xB6, 0x0C, 0x40, 0xB6, 0x2C, 0x00],
    "jog touch":            [0x90, 0x36, 0x7F],
    "jog turn":             [0xB0, 0x22, 0x05],
    "LOOP IN":              [0x90, 0x10, 0x7F],
    "LOOP OUT":             [0x90, 0x11, 0x7F],
    "RELOOP":               [0x90, 0x4D, 0x7F],
    "pad mode HOT CUE":     [0x90, 0x1B, 0x7F],
    "pad 1":                [0x97, 0x00, 0x7F],
    "pad 8":                [0x97, 0x07, 0x7F],
    "pad mode BEAT LOOP":   [0x90, 0x6D, 0x7F],
    "pad mode BEAT JUMP":   [0x90, 0x20, 0x7F],
    "BEAT FX on/off":       [0x94, 0x47, 0x7F],
    "BEAT FX depth":        [0xB4, 0x02, 0x40],
    "BEAT < / >":           [0x94, 0x4B, 0x7F],
}

OPS = {0: "press", 2: "release", 4: "rotate", 5: "value"}

# Controls that reach the engine THROUGH the launcher rather than straight
# from the bridge.  There is no launcher here - this test drives the bridge
# on its own - so they are checked separately, below, by watching what the
# bridge asks the launcher to do.
VIA_LAUNCHER = {"BEAT FX select"}
failures = []


def check(label, got, want=True):
    if got != want:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label}")


def build_bridge(into: Path) -> Path:
    subprocess.run(["make", "-s", "-C", str(REPO / "src/host"), f"OUT={into}"],
                   check=True, stdout=subprocess.DEVNULL)
    return into / "flx4-bridge"


def keyshim_line(record: bytes) -> str:
    """Render a control record the way keyshim.so logs it inside the player."""
    key, ch, op, param, value, pos = struct.unpack("<iiiifi", record)
    return (f"keyshim: ctrl key={key & 0xffffffff:08x}\n"
            f"         op={op}\n         ch={ch}\n         param={param}\n")


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    bridge = build_bridge(tmp)
    check("the bridge builds", bridge.exists())

    midi_fifo = tmp / "midi.fifo"
    ctrl_fifo = tmp / "ctrl.fifo"
    os.mkfifo(midi_fifo)
    os.mkfifo(ctrl_fifo)
    engine_log = tmp / "keyshim.log"
    engine_log.write_text("keyshim: thread started\n")

    # stand in for keyshim.so: read the FIFO, write the player's log format
    stop = threading.Event()

    def fake_keyshim():
        fd = os.open(ctrl_fifo, os.O_RDWR | os.O_NONBLOCK)
        buffer = b""
        with open(engine_log, "a", buffering=1) as log:
            while not stop.is_set():
                try:
                    chunk = os.read(fd, 24 * 64)
                except BlockingIOError:
                    time.sleep(0.01)
                    continue
                if not chunk:
                    time.sleep(0.01)
                    continue
                buffer += chunk
                while len(buffer) >= 24:
                    log.write(keyshim_line(buffer[:24]))
                    buffer = buffer[24:]
        os.close(fd)

    threading.Thread(target=fake_keyshim, daemon=True).start()

    overlay_fifo = tmp / "overlay.fifo"
    os.mkfifo(overlay_fifo)
    proc = subprocess.Popen([str(bridge), "-d", str(midi_fifo),
                             "-f", str(ctrl_fifo),
                             "-O", str(overlay_fifo)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    midi = os.open(midi_fifo, os.O_WRONLY)

    print(f"\nwalking all {len(verify.CONTROLS)} controls of the verification "
          f"list through the real bridge:\n")
    missing_midi = []
    seen = 0
    for name, instruction, needles in verify.CONTROLS:
        if name in VIA_LAUNCHER:
            print(f"--   {name:<22} -> the launcher (checked below)")
            seen += 1
            continue
        if name not in MIDI:
            missing_midi.append(name)
            continue
        watch = verify.LogWatch([str(engine_log)])
        os.write(midi, bytes(MIDI[name]))
        found, why = verify.wait_for(watch, needles, timeout=3.0,
                                     allow_skip=False)
        if found:
            seen += 1
            print(f"ok   {name:<22} -> {needles[0]}")
        else:
            failures.append(name)
            print(f"FAIL {name:<22} -> nothing matching {needles} ({why})")

    # FX SELECT is the one button that does NOT go to the engine.  It moves
    # the launcher's own effect list - down on its own, up with SHIFT - and
    # the launcher sends the engine the steps that takes.
    picker = []
    def read_picker():
        try:
            fd = os.open(overlay_fifo, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return
        for _ in range(40):
            try:
                blob = os.read(fd, 64)
                if blob:
                    picker.append(blob)
            except (BlockingIOError, OSError):
                pass
            time.sleep(0.05)
        os.close(fd)
    watcher = threading.Thread(target=read_picker, daemon=True)
    watcher.start()
    time.sleep(0.2)
    os.write(midi, bytes([0x94, 0x63, 0x7F]))     # FX SELECT press
    time.sleep(0.3)
    os.write(midi, bytes([0x94, 0x63, 0x00]))     # and release
    os.write(midi, bytes([0x94, 0x64, 0x7F]))     # SHIFT + FX SELECT
    time.sleep(0.3)
    os.write(midi, bytes([0x94, 0x64, 0x00]))
    watcher.join(timeout=3)
    told = b"".join(picker)
    check("FX SELECT moves the launcher's effect list down", b"fx+" in told)
    check("and SHIFT + FX SELECT moves it up", b"fx-" in told)
    # and it lights its own lamp: the branch that opens the picker used to
    # return before anything touched an LED, so the button never lit
    bridge_src_fx = (REPO / "src/host/flx4-bridge.c").read_text()
    fx_branch = bridge_src_fx[bridge_src_fx.index(
        "if (notemap[i].key == K_OVERLAY_FX ||"):]
    fx_branch = fx_branch[:fx_branch.index("return;")]
    check("and lights the FX button as it does it",
          "fx_lamp_flash(" in fx_branch)
    check("and neither sends the engine a blind FX-type step",
          engine_log.read_text().count("key=0000448b"), 0)

    os.close(midi)
    stop.set()
    proc.terminate()

    print()
    check("every control in the walk has MIDI to test with",
          [n for n in missing_midi if n not in VIA_LAUNCHER], [])
    check("every control reached the engine", seen, len(verify.CONTROLS))

    # what the engine received, for the record
    text = engine_log.read_text()
    check("the pad bank is switched before a pad",
          text.index("key=00004113") < text.index("key=00004117"))
    check("a 14-bit fader arrives as a 10-bit value",
          "key=0000501e\n         op=4\n         ch=1\n         param=512" in text)
    check("the tempo slider arrives as a value op",
          "key=00004107\n         op=5" in text)
    check("the jog carries a rotate op", "key=00004305\n         op=4" in text)

    # Pads take their own path through the bridge (the note is computed from
    # the mode and the pad number, not looked up), which is how they came to
    # be the one thing that never lit.
    bridge_src = (REPO / "src/host/flx4-bridge.c").read_text()
    pad_body = bridge_src[bridge_src.index("static void handle_pad("):
                          bridge_src.index("/* ---------------- note mapping")]
    check("pads light up on their own path", "led_set(" in pad_body)
    check("pads switch the bank before the pad press",
          pad_body.index("pad_select_bank") < pad_body.index("K_PAD1 + idx"))


print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("all FLX4 controls reached the engine")
