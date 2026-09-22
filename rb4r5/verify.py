"""`rb4r5 verify` - prove, on the machine itself, that it all really works.

Three things this checks that nothing else can:

  * the player is on screen and **filling** it (not letterboxed in a corner),
    with a real screenshot saved so you can look at it later or send it on;
  * audio is running through the FLX4 with the master and cue pairs where they
    should be;
  * **every** FLX4 control reaches the engine - it walks you through them one
    at a time and watches what the engine actually received.

The control walk is the honest version of "all the controls work": rather than
asserting it, it asks you to move each one and reports what arrived.
"""
from __future__ import annotations

import select
import sys
import time
from pathlib import Path

from . import audio, config, display, inputs, probe, supervisor, util, zones

# Where the engine records what it was told (keyshim) and what the bridge
# translated.  The first is the ground truth: it means the engine got it.
LOG_ENGINE = "/tmp/keyshim.log"
LOG_FLIP = "/tmp/flipdbg.log"
LOG_AUDIO = "/tmp/audioshim.log"


class Result:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []   # (area, name, state)

    def add(self, area: str, name: str, state: str, detail: str = "") -> None:
        self.rows.append((area, name, state, detail))
        mark = {"pass": "ok", "fail": "XX", "skip": "--", "warn": "!!"}[state]
        line = f"  [{mark}] {name}"
        if detail:
            line += f"  {detail}"
        print(line, flush=True)

    def count(self, state: str) -> int:
        return sum(1 for row in self.rows if row[2] == state)

    def report(self) -> str:
        width = max((len(r[1]) for r in self.rows), default=10)
        lines = []
        area = None
        for row_area, name, state, detail in self.rows:
            if row_area != area:
                area = row_area
                lines.append(f"\n{area}")
                lines.append("-" * len(area))
            lines.append(f"  {state.upper():<5} {name:<{width}}  {detail}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# log watching
# --------------------------------------------------------------------------
class LogWatch:
    """Follow one or more log files from wherever they are right now."""

    def __init__(self, paths):
        self.offsets = {}
        for path in paths:
            try:
                self.offsets[path] = Path(path).stat().st_size
            except OSError:
                self.offsets[path] = 0

    def new_text(self) -> str:
        chunks = []
        for path, offset in list(self.offsets.items()):
            try:
                with open(path, "r", errors="replace") as handle:
                    handle.seek(offset)
                    data = handle.read()
                    self.offsets[path] = handle.tell()
            except OSError:
                continue
            if data:
                chunks.append(data)
        return "".join(chunks)


def wait_for(watch: LogWatch, needles, timeout: float,
             allow_skip: bool = True) -> tuple[bool, str]:
    """Wait for any of `needles` to appear in the logs, or for Enter."""
    deadline = time.monotonic() + timeout
    buffer = ""
    while time.monotonic() < deadline:
        buffer += watch.new_text()
        for needle in needles:
            if needle.lower() in buffer.lower():
                return True, needle
        if allow_skip and sys.stdin.isatty():
            ready, _, _ = select.select([sys.stdin], [], [], 0.25)
            if ready:
                line = sys.stdin.readline().strip().lower()
                return False, "quit" if line in ("q", "quit") else "skipped"
        else:
            time.sleep(0.25)
    return False, "timeout"


# --------------------------------------------------------------------------
# display
# --------------------------------------------------------------------------
def check_display(cfg, result: Result, shots: Path) -> None:
    print("\n== display")
    fb = display.fb_info(cfg.get("display.fbdev", "/dev/fb0"))
    if not fb["present"]:
        result.add("display", "framebuffer", "fail",
                   f"{fb['dev']} does not exist - is vc4-kms-v3d enabled?")
        return
    result.add("display", "framebuffer", "pass",
               f"{fb['width']}x{fb['height']} "
               f"{fb.get('fmt') or str(fb['bpp']) + 'bpp'} ({fb['name']})")

    running = bool(util.pgrep_arg("/root/pdj/rbp"))
    result.add("display", "player process", "pass" if running else "fail",
               "running" if running else "rbp is not running")

    # Is anything being drawn at all?
    nonzero = display.fb_nonzero(fb["dev"])
    result.add("display", "frames reaching the screen",
               "pass" if nonzero > 1000 else "fail",
               f"{nonzero} non-zero bytes in the first 400k")

    # Is it *full screen*?  Sample the four corners and the centre: a
    # letterboxed or top-left-corner-only frame leaves corners black.
    filled, detail = _fullscreen_probe(fb)
    result.add("display", "fills the whole screen",
               "pass" if filled else "warn", detail)

    # The driver's own account of what it published.
    flip = util.read_text(LOG_FLIP)
    if flip:
        flips = flip.count("FLIP")
        updates = flip.count("UPDATE")
        result.add("display", "driver publish log",
                   "pass" if updates or flips else "warn",
                   f"{flips} FLIP, {updates} UPDATE entries")
    else:
        result.add("display", "driver publish log", "skip",
                   "empty (set RB_DFB_DEBUG=1 to record it)")

    # A screenshot, so there is something to look at afterwards.
    try:
        shot = shots / f"screen-{time.strftime('%Y%m%d-%H%M%S')}.png"
        message = display.fb_dump(str(shot), fb["dev"])
        result.add("display", "screenshot", "pass", message)
    except util.Fail as exc:
        result.add("display", "screenshot", "warn", str(exc))


def _fullscreen_probe(fb: dict) -> tuple[bool, str]:
    """Read the framebuffer's corners and centre; all-black corners mean the
    frame is not covering the panel."""
    width, height, bpp = fb["width"], fb["height"], fb["bpp"]
    if not width or not height or bpp not in (16, 24, 32):
        return False, "cannot sample this framebuffer"
    step = bpp // 8
    stride = fb["stride"] if fb["stride"] > 0 else width * step
    points = {
        "top-left": (4, 4),
        "top-right": (width - 5, 4),
        "bottom-left": (4, height - 5),
        "bottom-right": (width - 5, height - 5),
        "centre": (width // 2, height // 2),
    }
    lit = []
    try:
        with open(fb["dev"], "rb") as handle:
            for name, (x, y) in points.items():
                handle.seek(y * stride + x * step)
                pixel = handle.read(step)
                if any(pixel[:3]):
                    lit.append(name)
    except OSError as exc:
        return False, f"could not read the framebuffer ({exc})"
    if len(lit) == len(points):
        return True, "all four corners and the centre carry pixels"
    if not lit:
        return False, "the screen is black everywhere"
    return False, ("only " + ", ".join(lit) +
                   " carry pixels - the frame is not covering the panel")


# --------------------------------------------------------------------------
# audio
# --------------------------------------------------------------------------
def check_audio(cfg, result: Result) -> None:
    print("\n== audio")
    chosen = audio.select(cfg)
    if chosen["source"] == "none":
        result.add("audio", "output device", "fail",
                   "no ALSA card - the transport cannot run")
        return
    result.add("audio", "output device",
               "pass" if chosen["source"] == "controller" else "warn",
               f"{chosen['device']} ({chosen['channels']}ch, "
               f"source={chosen['source']})")
    result.add("audio", "master / cue routing",
               "pass" if chosen["channels"] == 4 else "warn",
               "master -> ch 1/2, cue -> ch 3/4" if chosen["channels"] == 4
               else "stereo sink: no separate headphone cue")

    if chosen["card"]:
        live = audio.pcm_status(chosen["card"]["index"])
        state = live["state"]
        result.add("audio", "PCM state",
                   "pass" if state == "RUNNING" else "warn",
                   f"{state}" + (f", {live['hw_params'].get('rate', '?')} Hz, "
                                 f"{live['hw_params'].get('channels', '?')} ch"
                                 if live["hw_params"] else ""))

    log = util.read_text(LOG_AUDIO)
    if not log:
        result.add("audio", "shim negotiation", "warn",
                   f"{LOG_AUDIO} is empty - is the player running?")
        return
    opened = [l for l in log.splitlines() if "opened real" in l]
    result.add("audio", "shim opened the device",
               "pass" if any("res=0" in l for l in opened) else "fail",
               opened[-1].strip() if opened else "no open recorded")

    # Is the engine actually producing samples?
    peaks = [l for l in log.splitlines() if "writei #" in l]
    if peaks:
        last = peaks[-1].strip()
        moving = "peak_m=0 " not in last
        result.add("audio", "samples flowing", "pass" if moving else "warn",
                   last.split("audioshim: ")[-1])
    else:
        result.add("audio", "samples flowing", "warn", "no writes recorded yet")


# --------------------------------------------------------------------------
# the FLX4 control walk
# --------------------------------------------------------------------------
# (name, what to do, the keycodes the engine should see)
CONTROLS = [
    ("PLAY deck 1", "press PLAY/PAUSE on the left deck", ["key=00004101"]),
    ("CUE deck 1", "press CUE on the left deck", ["key=00004102"]),
    ("PLAY deck 2", "press PLAY/PAUSE on the right deck", ["key=00004101"]),
    ("BEAT SYNC", "press BEAT SYNC on either deck", ["key=00004112"]),
    ("SYNC long press", "hold BEAT SYNC until it latches (master)",
     ["key=00004111"]),
    ("SHIFT + PLAY", "hold SHIFT and press PLAY (censor)", ["key=0000410f"]),
    ("browse knob", "turn the browse knob", ["key=0000420c"]),
    ("browse push", "press the browse knob", ["key=0000420c"]),
    ("SHIFT + browse push", "hold SHIFT and press the browse knob (back)",
     ["key=0000420d"]),
    ("LOAD deck 1", "press LOAD on the left deck", ["key=00004311"]),
    ("channel fader", "move a channel fader", ["key=0000501e"]),
    ("TRIM", "turn a TRIM knob", ["key=00005019"]),
    ("EQ HI", "turn an EQ HI knob", ["key=0000501a"]),
    ("EQ MID", "turn an EQ MID knob", ["key=0000501b"]),
    ("EQ LOW", "turn an EQ LOW knob", ["key=0000501c"]),
    ("crossfader", "move the crossfader", ["key=00006017"]),
    ("tempo slider", "move a tempo slider", ["key=00004107"]),
    ("FILTER knob", "turn a FILTER knob", ["key=0000509d"]),
    ("HEADPHONES MIXING", "turn the HEADPHONES MIXING knob", ["key=00004405"]),
    ("jog touch", "rest a hand on a jog platter", ["key=00004306"]),
    ("jog turn", "turn a jog platter", ["key=00004305"]),
    ("LOOP IN", "press LOOP IN / 4 BEAT", ["key=0000410c"]),
    ("LOOP OUT", "press LOOP OUT", ["key=0000410d"]),
    ("RELOOP", "press RELOOP/EXIT", ["key=0000410e"]),
    ("pad mode HOT CUE", "press the HOT CUE pad mode button", ["key=00004113"]),
    ("pad 1", "press performance pad 1", ["key=00004117"]),
    ("pad 8", "press performance pad 8", ["key=0000411e"]),
    ("pad mode BEAT LOOP", "press the BEAT LOOP pad mode button",
     ["key=00004114"]),
    ("pad mode BEAT JUMP", "press the BEAT JUMP pad mode button",
     ["key=00004116"]),
    # BEAT FX SELECT reaches the engine the long way round: the bridge tells
    # the launcher to move down its effect list, and the launcher sends the
    # engine one step of the FX-type key.  Same key to watch for, one more
    # process in between - so this only passes with the launcher running,
    # which is the point of an end-to-end walk.
    ("BEAT FX select", "press BEAT FX SELECT", ["key=0000448b"]),
    ("BEAT FX on/off", "press BEAT FX ON/OFF", ["key=0000448d"]),
    ("BEAT FX depth", "turn the BEAT FX LEVEL/DEPTH knob", ["key=0000448f"]),
    ("BEAT < / >", "press BEAT < or BEAT >", ["key=00004490", "key=00004491"]),
]

QUICK = {"PLAY deck 1", "browse knob", "channel fader", "jog turn", "pad 1",
         "tempo slider", "crossfader"}


def check_controller(cfg, result: Result, timeout: float,
                     quick: bool = False) -> None:
    print("\n== DDJ-FLX4 controls")
    card = audio.find_controller(cfg.get("controller.name", "FLX4"))
    if not card:
        result.add("controller", "controller present", "fail",
                   "no FLX4 in /proc/asound/cards")
        return
    result.add("controller", "controller present", "pass",
               f"card {card['index']} [{card['id']}] {card['name']}")
    nodes = inputs.midi_nodes(card["index"])
    result.add("controller", "MIDI node", "pass" if nodes else "fail",
               ", ".join(nodes) or "no rawmidi device")

    bridge_running = bool(util.pgrep_arg(str(cfg.bindir / "flx4-bridge")))
    result.add("controller", "bridge running",
               "pass" if bridge_running else "fail",
               "flx4-bridge is up" if bridge_running else
               "flx4-bridge is not running")
    if not Path(LOG_ENGINE).exists():
        result.add("controller", "engine log", "fail",
                   f"{LOG_ENGINE} missing - the player/keyshim is not running")
        return

    controls = [c for c in CONTROLS if not quick or c[0] in QUICK]
    print(f"\n  Move each control when asked.  Enter skips one, 'q' stops the "
          f"walk.\n  ({timeout:.0f}s each, {len(controls)} to go)\n")
    for name, instruction, needles in controls:
        watch = LogWatch([LOG_ENGINE])
        print(f"  -> {instruction} ...", end=" ", flush=True)
        found, why = wait_for(watch, needles, timeout)
        if found:
            print("seen")
            result.rows.append(("controller", name, "pass",
                                "the engine received it"))
        elif why == "quit":
            print("stopping")
            result.rows.append(("controller", name, "skip", "walk stopped"))
            break
        elif why == "skipped":
            print("skipped")
            result.rows.append(("controller", name, "skip", ""))
        else:
            print("NOT SEEN")
            result.rows.append(("controller", name, "fail",
                                "nothing reached the engine"))


# --------------------------------------------------------------------------
# touch, library
# --------------------------------------------------------------------------
def check_touch(cfg, result: Result, timeout: float) -> None:
    print("\n== touchscreen")
    try:
        panel = inputs.find_touchscreen(cfg.get("touch.name"),
                                        cfg.get("touch.device"))
    except util.Fail as exc:
        result.add("touch", "panel", "fail", str(exc))
        return
    if not panel:
        result.add("touch", "panel", "fail",
                   "no touchscreen (is the panel's USB cable in?)")
        return
    result.add("touch", "panel", "pass", f"{panel['path']} ({panel['name']})")
    running = bool(util.pgrep("launch.py touchd"))
    result.add("touch", "daemon", "pass" if running else "fail",
               "rbtouchd is up" if running else "rbtouchd is not running")

    zone_map = zones.load(cfg.get("touch.zones_file"))
    targets = [z for z in zone_map["zones"]
               if z.get("type") == "key" and z.get("key") in
               ("play", "cue", "load", "source")][:3]
    for zone in targets:
        watch = LogWatch([LOG_ENGINE])
        print(f"  -> tap the '{zone['name']}' area of the screen ...",
              end=" ", flush=True)
        from . import keys as keymod
        code = f"key={keymod.resolve(zone['key']):08x}"
        found, why = wait_for(watch, [code], timeout)
        print("seen" if found else ("skipped" if why == "skipped" else "NOT SEEN"))
        result.rows.append(("touch", f"zone {zone['name']}",
                            "pass" if found else
                            ("skip" if why in ("skipped", "quit") else "fail"),
                            "" if found else "nothing reached the engine"))
        if why == "quit":
            break


def check_library(cfg, result: Result) -> None:
    print("\n== rekordbox library")
    state = probe.media_state()
    if state["error"]:
        result.add("library", "player memory", "skip", state["error"])
        return
    for row in state["kinds"]:
        if row["kind"] != 2:
            continue
        ready = row["detect"] == 2
        result.add("library", "USB1 database",
                   "pass" if ready else "warn",
                   f"detect={row['detect']} ({row['state']})"
                   + (f", {row.get('songs', 0)} songs, "
                      f"{row.get('playlists', 0)} playlists"
                      if row.get("songs") else ""))


# --------------------------------------------------------------------------
def run(cfg, quick: bool = False, timeout: float = 20.0,
        skip_controller: bool = False, skip_touch: bool = False) -> int:
    util.require_root("verify")
    shots = util.ensure_dir(cfg.logs / "screenshots")
    result = Result()

    print("rb4r5 verification - checking what is actually happening on this Pi")
    print("=" * 70)
    for line in supervisor.status(cfg):
        print(f"  {line}")

    check_display(cfg, result, shots)
    check_audio(cfg, result)
    if not skip_controller:
        check_controller(cfg, result, timeout, quick=quick)
    if not skip_touch:
        check_touch(cfg, result, timeout)
    check_library(cfg, result)

    report = result.report()
    print("\n" + "=" * 70)
    print(report)
    passed, failed = result.count("pass"), result.count("fail")
    skipped, warned = result.count("skip"), result.count("warn")
    print(f"\n{passed} passed, {failed} failed, {warned} warnings, "
          f"{skipped} skipped")

    out = cfg.logs / "verify-report.txt"
    util.write_text(out, f"rb4r5 verification {time.strftime('%F %T')}\n"
                         f"{report}\n\n{passed} passed, {failed} failed, "
                         f"{warned} warnings, {skipped} skipped\n")
    print(f"\nreport:      {out}")
    print(f"screenshots: {shots}")
    if failed:
        print("\nSomething is not working - docs/10-troubleshooting.md is "
              "organised by symptom.")
    return 1 if failed else 0
