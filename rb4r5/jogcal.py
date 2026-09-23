"""Measuring the FLX4's jog wheel, instead of guessing at it.

Two numbers decide whether a jog wheel feels like a jog wheel:

  * how many messages the controller sends for one revolution of its own
    wheel - which turns those messages into a speed in revolutions per
    second, the only thing the engine understands;
  * which direction it counts in.

Neither is documented anywhere for this controller, and getting the first one
wrong is what makes the wheel feel dead (too few revolutions per second) while
the position it derives runs away (the track skipping).  So: turn the wheel
once, and this counts.

    sudo python3 launch.py jogtest
"""
from __future__ import annotations

import os
import select
import time
from pathlib import Path

from . import audio, inputs, util

# the CCs a Pioneer jog sends: the plate (scratch), the rim (bend), and the
# shifted plate (search).  All are relative: 64 is zero, above is forward.
JOG_CCS = {0x21: "rim (bend)", 0x22: "plate (scratch)",
           0x23: "plate (non-vinyl)", 0x29: "SHIFT+plate (search)"}
TOUCH_NOTES = {0x36: "plate touch", 0x67: "SHIFT+plate touch"}


def find_midi(cfg) -> str | None:
    """The FLX4's raw MIDI node."""
    explicit = cfg.get("controller.midi_device")
    if explicit and Path(explicit).exists():
        return str(explicit)
    card = audio.find_controller_card(cfg) if hasattr(
        audio, "find_controller_card") else None
    nodes = inputs.midi_nodes(card if isinstance(card, int) else None)
    if not nodes:
        nodes = inputs.midi_nodes(None)
    return nodes[0] if nodes else None


class Counter:
    """Running totals per CC and per MIDI channel."""

    def __init__(self):
        self.ticks: dict[tuple[int, int], int] = {}
        self.messages: dict[tuple[int, int], int] = {}
        self.notes: dict[tuple[int, int], int] = {}
        self.first = None
        self.last = None

    def cc(self, channel: int, controller: int, value: int) -> int:
        delta = value - 128 if value >= 64 else value
        key = (channel, controller)
        self.ticks[key] = self.ticks.get(key, 0) + delta
        self.messages[key] = self.messages.get(key, 0) + 1
        now = time.monotonic()
        self.first = self.first if self.first is not None else now
        self.last = now
        return delta

    def note(self, channel: int, number: int, on: bool) -> None:
        key = (channel, number)
        self.notes[key] = self.notes.get(key, 0) + (1 if on else 0)


def parse(blob: bytes, counter: Counter, running: list) -> list[str]:
    """Feed raw MIDI bytes in, get human-readable lines out."""
    lines = []
    index = 0
    while index < len(blob):
        byte = blob[index]
        if byte & 0x80:
            if byte >= 0xF8:                      # clock and friends
                index += 1
                continue
            running[:] = [byte]
            index += 1
            continue
        if not running:
            index += 1
            continue
        status = running[0]
        kind, channel = status & 0xF0, status & 0x0F
        if kind in (0xB0, 0x90, 0x80) and index + 1 < len(blob):
            first, second = blob[index], blob[index + 1]
            index += 2
            if kind == 0xB0:
                if first in JOG_CCS:
                    delta = counter.cc(channel, first, second)
                    lines.append(f"  jog  ch{channel + 1} CC 0x{first:02x} "
                                 f"{JOG_CCS[first]:20} {delta:+4d}")
            else:
                on = kind == 0x90 and second > 0
                counter.note(channel, first, on)
                if first in TOUCH_NOTES:
                    lines.append(f"  note ch{channel + 1} 0x{first:02x} "
                                 f"{TOUCH_NOTES[first]:20} "
                                 f"{'ON' if on else 'off'}")
        else:
            index += 1
    return lines


def listen(path: str, seconds: float, counter: Counter,
           show: bool = True) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    running: list = []
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if not ready:
                continue
            try:
                blob = os.read(fd, 4096)
            except (BlockingIOError, OSError):
                continue
            for line in parse(blob, counter, running):
                if show:
                    print(line)
    finally:
        os.close(fd)


def run(cfg, seconds: float = 12.0, quiet: bool = False) -> int:
    path = find_midi(cfg)
    if not path:
        raise util.Fail(
            "no MIDI device found - plug the FLX4 in and check "
            "`launch.py doctor` sees it (the controller section)")
    print(f"reading {path}\n")

    print("STEP 1  Touch the top of the LEFT jog plate and hold it for a "
          "moment,\n        then let go.  (5s)")
    touch = Counter()
    listen(path, 5.0, touch)
    plate = [key for key in touch.notes if key[1] in TOUCH_NOTES]
    if plate:
        for (channel, number), count in sorted(touch.notes.items()):
            if number in TOUCH_NOTES:
                print(f"        saw {TOUCH_NOTES[number]} on MIDI channel "
                      f"{channel + 1} ({count} press(es))")
    else:
        print("        nothing - the plate touch is not reaching us.  Without "
              "it the\n        engine cannot tell scratching from bending: "
              "every nudge seeks.")

    print(f"\nSTEP 2  Turn the LEFT jog wheel exactly ONE full turn "
          f"CLOCKWISE,\n        steadily, then stop.  ({seconds:.0f}s)")
    turn = Counter()
    listen(path, seconds, turn, show=not quiet)

    if not turn.ticks:
        print("\n        no jog messages at all.  Either the wheel is not "
              "moving,\n        or this is not the FLX4's MIDI node.")
        return 1

    print("\n        what one turn produced:")
    best = None
    for (channel, controller), total in sorted(turn.ticks.items()):
        count = turn.messages[(channel, controller)]
        print(f"          ch{channel + 1} CC 0x{controller:02x} "
              f"{JOG_CCS.get(controller, '?'):20} "
              f"net {total:+6d} ticks in {count} messages")
        if best is None or abs(total) > abs(best[1]):
            best = ((channel, controller), total)

    (channel, controller), total = best
    ticks = abs(total)
    elapsed = max(0.001, (turn.last or 0) - (turn.first or 0))
    print(f"\n  ONE REVOLUTION = {ticks} ticks on CC 0x{controller:02x} "
          f"(over {elapsed:.1f}s)")
    if total < 0:
        print("  It counts DOWN when turned clockwise, so the direction needs "
              "flipping.")

    print("\n  Set it with:")
    print(f"      sudo python3 launch.py config --set "
          f"controller.jog_ticks_per_rev={ticks}")
    if total < 0:
        print("      sudo python3 launch.py config --set "
              "controller.jog_reverse=true")
    print("      sudo python3 launch.py stop && sudo python3 launch.py run")
    print("\n  Then: if the platter still moves too far or not far enough for "
          "the\n  turn you give it, controller.jog_scale multiplies it "
          "(0.5 = half).")
    if ticks < 60:
        print("\n  NOTE: that is a very low count for one revolution.  Turn it "
              "again\n  and check - a partial turn reads as a coarse wheel.")
    return 0


# --------------------------------------------------------------------------
# live tuning
# --------------------------------------------------------------------------
JOG_CONF = "/tmp/rb-jog.conf"


def tune(cfg, **values) -> int:
    """Retune the jog on a running bridge, without restarting anything.

    How far a turn moves the deck depends on a number nobody documents - how
    many messages the FLX4 sends for one revolution - and the only way to get
    it right is to turn the wheel and see.  Going through a rebuild and a
    restart for each guess takes minutes; this takes none, so it can be dialled
    in while the wheel is in your hand.
    """
    current = read_tuning()
    for key, value in values.items():
        if value is not None:
            current[key] = value
    lines = [f"{key}={value}" for key, value in sorted(current.items())]
    Path(JOG_CONF).write_text("\n".join(lines) + "\n")
    try:
        os.chmod(JOG_CONF, 0o666)
    except OSError:
        pass
    print(f"wrote {JOG_CONF}:")
    for line in lines:
        print(f"    {line}")
    print("\nThe bridge picks this up within half a second - turn the wheel "
          "and see.\nWhen it feels right, make it the default:")
    for key, value in sorted(current.items()):
        name = {"tpr": "jog_ticks_per_rev", "scale": "jog_scale",
                "bend": "jog_bend_scale", "reverse": "jog_reverse",
                "emit_ms": "jog_emit_ms"}.get(key)
        if name:
            print(f"    sudo python3 launch.py config --set "
                  f"controller.{name}={value}")
    return 0


def read_tuning() -> dict:
    out: dict = {}
    try:
        for line in Path(JOG_CONF).read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                key, value = line.split("=", 1)
                out[key.strip()] = value.strip()
    except OSError:
        pass
    return out


def seed_tuning(cfg) -> None:
    """Start the live file from the configured values, so tuning is relative.

    Rewritten on every start.  The bridge re-reads this file twice a second
    and it wins over the command line, so a copy left in /tmp from an earlier
    run - an old tpr=1800, say - kept the wheel three times too slow however
    the config was changed.  Tuning that should last goes in the config
    (`jogtest` prints the command). """
    try:
        Path(JOG_CONF).write_text(
            f"tpr={cfg.get('controller.jog_ticks_per_rev', 600)}\n"
            f"scale={cfg.get('controller.jog_scale', 1.0)}\n"
            f"bend={cfg.get('controller.jog_bend_scale', 0.25)}\n"
            f"reverse={1 if cfg.get('controller.jog_reverse') else 0}\n")
        os.chmod(JOG_CONF, 0o666)
    except OSError:
        pass


# --------------------------------------------------------------------------
# what lights which lamp
# --------------------------------------------------------------------------
def led_sweep(cfg, channels=None, first: int = 0x00, last: int = 0x7f,
              hold: float = 0.35, note: bool = True) -> int:
    """Light one lamp at a time and say what was sent, so it can be mapped.

    The FLX4's lamps are lit by the host, and Pioneer does not publish which
    message lights which one.  The way to find out is to send them one at a
    time and watch the controller - so this does that, printing each message
    before it sends it.  Note what lights up and put it in the map.

    Stop with Ctrl-C.  Every lamp touched is turned back off on the way out.
    """
    path = find_midi(cfg)
    if not path:
        raise util.Fail("no /dev/snd/midiC*D* node - is the FLX4 plugged in?")
    if util.pgrep_arg("flx4-bridge"):
        raise util.Fail("the bridge has the controller open - stop it first:\n"
                        "    sudo python3 launch.py stop")

    channels = channels or list(range(16))
    kind = "note" if note else "CC"
    status_base = 0x90 if note else 0xb0
    print(f"sweeping {kind} {first:#04x}..{last:#04x} on MIDI channels "
          f"{channels[0] + 1}..{channels[-1] + 1} of {path}")
    print("watch the controller; note what lights, then Ctrl-C\n")

    touched = []
    try:
        with open(path, "r+b", buffering=0) as port:
            for channel in channels:
                for number in range(first, last + 1):
                    print(f"  ch{channel + 1:<3} {kind} {number:#04x} on ",
                          end="", flush=True)
                    port.write(bytes([status_base | channel, number, 0x7f]))
                    touched.append((channel, number))
                    time.sleep(hold)
                    port.write(bytes([status_base | channel, number, 0x00]))
                    print("off")
    except KeyboardInterrupt:
        print("\nstopped")
    except OSError as exc:
        raise util.Fail(f"could not write to {path}: {exc}") from exc
    finally:
        try:
            with open(path, "r+b", buffering=0) as port:
                for channel, number in touched:
                    port.write(bytes([status_base | channel, number, 0x00]))
        except OSError:
            pass
    print(f"\n{len(touched)} lamp message(s) sent.  Whatever lit up, the line "
          f"above it says\nwhich message did it.")
    return 0
