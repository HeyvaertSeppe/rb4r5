"""`rb4r5 doctor` - one screen that says whether this Pi can run the player.

Every check prints what it found, and anything broken prints the fix.  This is
the first thing to run when something does not work, and the last thing to run
after `setup` / `build`.
"""
from __future__ import annotations

from pathlib import Path

from . import (audio, chroot, config, display, inputs, platform5, probe,
               supervisor, usbwatch, util, zones)

REPO = Path(__file__).resolve().parent.parent


def _section(title: str) -> None:
    print(f"\n=== {title}")


def _row(label: str, value: str) -> None:
    print(f"  {label:<20} {value}")


def run(cfg, verbose: bool = False) -> int:
    problems: list[str] = []
    warnings: list[str] = []

    _section("platform")
    for label, value in platform5.summary():
        _row(label, value)
    for problem in platform5.check(strict=False):
        (warnings if problem.startswith("note:") else problems).append(problem)

    _section("runtime (the soft-float XDJ-RX3 chroot)")
    state = chroot.status(cfg)
    _row("chroot", f"{state['root']} "
                   f"({'ready' if state['ready'] else 'INCOMPLETE'})")
    if state["missing"]:
        for missing in state["missing"]:
            _row("missing", missing)
        problems.append("the runtime is incomplete - see docs/03-payload.md, "
                        "then: sudo python3 launch.py build")
    for missing in state["missing_optional"]:
        _row("optional missing", missing)
        if missing.endswith("edb_streamd"):
            warnings.append("edb_streamd is absent: no rekordbox database "
                            "import (folder browsing still works)")
    for name, mounted in state["mounts"].items():
        _row(f"bind /{name}", "mounted" if mounted else "not mounted")
    arm_ok, arm_why = platform5.can_run_arm32(
        str(cfg.chroot / "lib/ld-linux.so.3"))
    _row("32-bit loader", ("ok - " if arm_ok else "FAILED - ") + arm_why)
    if not arm_ok:
        problems.append(f"the player cannot be executed: {arm_why}")

    _section("display")
    fb = display.fb_info(cfg.get("display.fbdev", "/dev/fb0"))
    _row("framebuffer", f"{fb['dev']} "
                        f"{'present' if fb['present'] else 'MISSING'}")
    if fb["present"]:
        _row("geometry", f"{fb['width']}x{fb['height']} @{fb['bpp']}bpp "
                         f"({fb['name']}), stride {fb['stride']}, pan {fb['pan']}")
        _row("scaling", display.scale_note(cfg))
        nonzero = display.fb_nonzero(fb["dev"])
        _row("content", f"{nonzero} non-zero bytes in the first 400k"
                        + (" (something is drawn)" if nonzero > 1000 else " (blank)"))
        if fb["bpp"] not in (16, 32):
            warnings.append(f"the framebuffer is {fb['bpp']}bpp; the driver's "
                            "convert path expects 16 or 32")
    else:
        problems.append("no framebuffer: add dtoverlay=vc4-kms-v3d to "
                        f"{platform5.boot_dir()}/config.txt and reboot")
    for conn in display.connectors():
        _row(conn["name"], f"{conn['status']} {conn['mode']}")
    if not any(c["status"] == "connected" for c in display.connectors()):
        warnings.append("no connected display output found")
    comp = platform5.compositor_running()
    _row("compositor", comp or "none (good)")

    _section("audio")
    for line in audio.describe(cfg):
        print(f"  {line}")
    chosen = audio.select(cfg)
    if chosen["source"] == "none":
        problems.append("no audio device: without a running PCM the player's "
                        "transport never advances (no playback, frozen waveform)")
    elif chosen["source"] == "hdmi":
        warnings.append("running on HDMI audio: plug in the DDJ-FLX4 for master "
                        "out plus headphone cue")
    elif chosen["channels"] != 4:
        warnings.append("the controller is not offering 4 playback channels, so "
                        "there is no separate headphone cue")
    if chosen["card"]:
        live = audio.pcm_status(chosen["card"]["index"])
        _row("pcm state", live["state"])
        if live["hw_params"]:
            _row("pcm params", ", ".join(f"{k}={v}" for k, v in
                                         list(live["hw_params"].items())[:5]))

    _section("controller (DDJ-FLX4)")
    card = audio.find_controller(cfg.get("controller.name", "FLX4"))
    if card:
        _row("card", f"{card['index']} [{card['id']}] {card['name']}")
        nodes = inputs.midi_nodes(card["index"])
        _row("midi", ", ".join(nodes) or "NO rawmidi node")
        if not nodes:
            problems.append("the controller has no /dev/snd/midiC*D* node: "
                            "is snd_usb_audio loaded?")
    else:
        _row("card", "not connected")
        warnings.append("no DJ controller found (the player still runs; touch "
                        "and keyboard control work)")
    bridge = cfg.bindir / "flx4-bridge"
    _row("bridge", f"{bridge} {'installed' if bridge.exists() else 'NOT BUILT'}")
    if not bridge.exists():
        problems.append(f"{bridge} is missing: sudo python3 launch.py build")
    map_file = cfg.get("controller.map_file")
    _row("map file", f"{map_file} "
                     f"{'present' if map_file and Path(map_file).exists() else 'default map'}")

    _section("touchscreen")
    for line in inputs.describe():
        print(f"  {line}")
    touch = None
    try:
        touch = inputs.find_touchscreen(cfg.get("touch.name"),
                                        cfg.get("touch.device"))
    except util.Fail as exc:
        problems.append(str(exc))
    if touch:
        _row("using", f"{touch['path']} ({touch['name']})")
        axis = touch["abs"].get("mt_x") or touch["abs"].get("x")
        if axis:
            _row("x range", f"{axis['min']}..{axis['max']}")
        if not touch["multitouch"] and not touch["single_touch"]:
            warnings.append(f"{touch['path']} has no usable touch axes")
    else:
        warnings.append("no touchscreen found: check the panel's USB cable "
                        "(it is a separate cable from HDMI)")
    zone_map = zones.load(cfg.get("touch.zones_file"))
    _row("zones", f"{len(zone_map['zones'])} zones from "
                  f"{cfg.get('touch.zones_file')}")
    _row("native touch", "on (experimental)" if cfg.get("touch.native")
         else "off (zone mapping, the supported path)")
    if verbose:
        for line in zones.describe(zone_map):
            print(line)

    _section("rekordbox library (USB)")
    for line in usbwatch.describe(cfg):
        print(f"  {line}")

    _section("player state")
    for line in supervisor.status(cfg):
        print(f"  {line}")
    if probe.player_pid():
        for line in probe.describe(cfg):
            print(f"  {line}")

    _section("logs")
    for name in ("rbp.log", "flx4-bridge.log", "touchd.log", "usbwatch.log",
                 "edb_streamd.log"):
        path = cfg.logs / name
        _row(name, f"{path.stat().st_size} bytes" if path.exists() else "-")
    for path in [config.LOG_RBP] + config.LOG_SHIMS:
        if Path(path).exists():
            _row(Path(path).name, f"{Path(path).stat().st_size} bytes (in /tmp)")

    print()
    if problems:
        print(f"{len(problems)} problem(s):")
        for problem in problems:
            print(f"  ! {problem}")
    if warnings:
        print(f"{len(warnings)} warning(s):")
        for warning in warnings:
            print(f"  - {warning.removeprefix('note: ')}")
    if not problems:
        print("no blocking problems found.")
        if not warnings:
            print("everything checks out.")
    return 1 if problems else 0
