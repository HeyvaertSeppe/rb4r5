"""The command line: `python3 launch.py [command]`.

With no command it does the whole job - provision what is missing, build what
is missing, then run the player full screen.  Individual commands exist so each
step can be run, inspected and repeated on its own.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import (__version__, audio, build, chroot, config, display, doctor,
               fb, firmware, jogcal, keys, overlay, platform5, probe,
               provision, subucom, supervisor, touchd, usbwatch, util, verify,
               zones)

REPO = Path(__file__).resolve().parent.parent


def _print_notes(notes) -> None:
    for note in notes:
        util.info(note)


# --------------------------------------------------------------------------
def cmd_setup(args, cfg) -> int:
    util.require_root("setup")
    if args.undo:
        util.step("undoing the system changes rb4r5 made")
        _print_notes(provision.all_steps(cfg, REPO, undo=True))
        util.ok("system changes reverted (reboot to get the desktop back)")
        return 0

    util.step(f"provisioning {platform5.model()} ({platform5.os_pretty()})")
    if not platform5.is_pi5():
        util.warn(f"this does not look like a Raspberry Pi 5 ({platform5.model()})")
        if not args.yes:
            reply = input("continue anyway? [y/N] ").strip().lower()
            if reply not in ("y", "yes"):
                return 1
    _print_notes(provision.all_steps(cfg, REPO,
                                     enable_service=not args.no_service,
                                     take_over=args.console))
    util.ok("setup done")
    print()
    print("If the screen ever goes black and you want the desktop back:")
    print("    press Ctrl+Alt+F2 for a login prompt, or SSH in, then")
    print("    sudo python3 launch.py recover")
    print()
    print("Next steps:")
    print(f"  1. put your extracted XDJ-RX3 firmware in {cfg.payload}")
    print("     (see docs/03-payload.md - nothing vendor-owned ships here)")
    print("  2. sudo python3 launch.py build")
    print("  3. sudo python3 launch.py run     (or reboot: the service starts it)")
    if platform5.compositor_running():
        print()
        util.warn("a desktop is still running; reboot so the player can own "
                  "the screen")
    return 0


def cmd_firmware(args, cfg) -> int:
    util.require_root("unpacking the firmware")
    if args.show:
        for line in firmware.describe(cfg, verbose=args.verbose):
            print(line)
        return 0 if firmware.ready(cfg) else 1
    if args.offline:
        cfg.set("firmware.auto_download", False)
    _print_notes(firmware.prepare(cfg, upd=args.upd, key=args.key,
                                  force=args.force, ask=args.ask))
    util.ok("the firmware payload is ready")
    print()
    print("Next:  sudo python3 launch.py build     (then it runs)")
    return 0


def cmd_payload(args, cfg) -> int:
    util.require_root("assembling the runtime")
    if not firmware.ready(cfg):
        _print_notes(firmware.prepare(cfg))
    util.step(f"assembling {cfg.chroot} from {cfg.payload}")
    _print_notes(chroot.assemble(cfg, force=args.force))
    _print_notes(chroot.make_stubs(cfg))
    state = chroot.status(cfg)
    if state["ready"]:
        util.ok("the runtime is complete")
    else:
        util.warn("still missing: " + ", ".join(state["missing"]))
        util.info("that is expected before `launch.py build` has run")
    return 0


def cmd_build(args, cfg) -> int:
    util.require_root("building")
    util.step("building the runtime")
    _print_notes(build.all_steps(cfg, REPO,
                                 with_directfb=not args.no_directfb,
                                 with_player=not args.no_player,
                                 fast_dfb=args.fast_directfb))
    state = chroot.status(cfg)
    if state["ready"]:
        util.ok("the runtime is complete - run it: sudo python3 launch.py run")
    else:
        util.warn("still missing: " + ", ".join(state["missing"]))
    return 0


def cmd_run(args, cfg) -> int:
    util.require_root("running the player")
    sup = supervisor.Supervisor(cfg, REPO)
    return sup.run(foreground=not args.detach, force=args.force)


def cmd_auto(args, cfg) -> int:
    """The default: set up whatever is missing, then run."""
    util.require_root("rb4r5")
    problems = platform5.check(strict=False)
    needs_setup = (not cfg.path.exists() or
                   any("compositor" in p or "boot target" in p for p in problems))
    if needs_setup:
        util.step("first run: provisioning this Pi")
        _print_notes(provision.all_steps(cfg, REPO, enable_service=True))
        cfg = config.load(cfg.path)

    state = chroot.status(cfg)
    if not state["ready"]:
        if not firmware.ready(cfg):
            util.step("first run: fetching and unpacking the player's firmware")
            _print_notes(firmware.prepare(cfg))
        util.step("building the runtime")
        _print_notes(build.all_steps(cfg, REPO))

    return supervisor.Supervisor(cfg, REPO).run(foreground=not args.detach)


def cmd_stop(args, cfg) -> int:
    util.require_root("stopping the player")
    if util.have("systemctl"):
        util.run(["systemctl", "stop", "rb4r5.service"], check=False)
    sup = supervisor.Supervisor(cfg, REPO)
    sup.stop_stale()

    def terminate(pids, label):
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
                util.info(f"stopped {label} (pid {pid})")
            except OSError:
                pass        # it exited between the scan and the signal

    for name in ("flx4-bridge", "rbkeyd"):
        terminate(util.pgrep_arg(str(cfg.bindir / name)), name)
    for needle in ("touchd", "usbwatch"):
        terminate(util.pgrep(f"launch.py {needle}"), needle)
    _print_notes(chroot.umount_binds(cfg))
    if args.restore_console:
        _print_notes(display.restore_console())
    util.ok("stopped")
    return 0


def cmd_recover(args, cfg) -> int:
    """Give the machine back: desktop, login prompt, console.

    For the moment when the screen is black and you just want a working Pi.
    """
    util.require_root("recover")
    util.step("handing the screen back to the desktop")
    sup = supervisor.Supervisor(cfg, REPO)
    if util.have("systemctl"):
        util.run(["systemctl", "stop", "rb4r5.service"], check=False)
        if args.disable_service:
            util.run(["systemctl", "disable", "rb4r5.service"], check=False)
            util.info("rb4r5.service disabled - it will not start at boot")
    sup.stop_stale()
    _print_notes(chroot.umount_binds(cfg))
    _print_notes(display.restore_console())
    _print_notes(provision.console_mode(undo=True))
    if args.all:
        _print_notes(provision.all_steps(cfg, REPO, undo=True))
        util.info("the boot config changes were reverted too")
    util.ok("done - reboot to land in the desktop")
    print()
    print("  sudo reboot")
    print()
    print("Nothing under /opt/rb4r5 was touched, so the runtime you built is")
    print("still there.  Start the player again with:")
    print("    sudo python3 launch.py run")
    return 0


def cmd_status(args, cfg) -> int:
    for line in supervisor.status(cfg):
        print(line)
    return 0


def cmd_doctor(args, cfg) -> int:
    return doctor.run(cfg, verbose=args.verbose)


def cmd_verify(args, cfg) -> int:
    return verify.run(cfg, quick=args.quick, timeout=args.timeout,
                      skip_controller=args.no_controller,
                      skip_touch=args.no_touch)


def cmd_touchd(args, cfg) -> int:
    return touchd.run(cfg, once=args.once)


def cmd_usbwatch(args, cfg) -> int:
    util.require_root("the USB watcher")
    return usbwatch.run(cfg, once=args.once)


def cmd_calibrate(args, cfg) -> int:
    return touchd.calibrate(cfg, seconds=args.seconds, raw=args.raw)


def cmd_probe(args, cfg) -> int:
    util.require_root("reading the player's state")
    for line in probe.describe(cfg):
        print(line)
    return 0 if probe.usb1_ready() else 1


def cmd_keys(args, cfg) -> int:
    if args.list or not args.key:
        print("XDJ-RX3 keys (name, code, whether it takes a deck channel):")
        print("\n".join(keys.listing()))
        return 0
    if not Path(config.FIFO_KEYS).is_fifo():
        raise util.Fail(f"{config.FIFO_KEYS} does not exist - is the player "
                        "running?  (sudo python3 launch.py run)")
    code = keys.resolve(args.key)
    if args.op == "tap":
        ok = keys.tap_key(code, args.channel)
    elif args.op == "press":
        ok = keys.send_key(code, args.channel, True)
    elif args.op == "release":
        ok = keys.send_key(code, args.channel, False)
    elif args.op == "rotate":
        ok = keys.rotate(code, args.channel, args.value)
    else:
        ok = keys.value(code, args.channel, args.value,
                        args.value / 1023.0 if args.value else 0.0)
    if not ok:
        raise util.Fail("nothing is reading the FIFO (the player is not "
                        "running, or keyshim did not load - see "
                        "/tmp/keyshim.log)")
    util.ok(f"sent {args.op} 0x{code:04x} ch={args.channel}"
            + (f" value={args.value}" if args.op in ("rotate", "value") else ""))
    print("check /tmp/keyshim.log to see the engine receive it")
    return 0


def cmd_zones(args, cfg) -> int:
    path = args.path or cfg.get("touch.zones_file")
    if args.write_default:
        util.require_root("writing the zone file")
        wrote = zones.save_default(path)
        util.ok(f"{'wrote' if wrote else 'kept'} {path}")
        return 0
    zone_map = zones.load(path)
    print(f"zones from {path if Path(path).exists() else '(built-in default)'}:")
    print("\n".join(zones.describe(zone_map)))
    problems = zones.validate(zone_map)
    for problem in problems:
        util.warn(problem)
    return 1 if problems else 0


def cmd_logs(args, cfg) -> int:
    names = [args.name] if args.name else [
        "rbp.log", "flx4-bridge.log", "touchd.log", "usbwatch.log",
        "edb_streamd.log"]
    paths = []
    for name in names:
        candidate = Path(name)
        if not candidate.is_absolute():
            candidate = cfg.logs / name
        if candidate.exists():
            paths.append(str(candidate))
    for extra in [config.LOG_RBP] + config.LOG_SHIMS:
        if args.name in (None, Path(extra).name) and Path(extra).exists():
            paths.append(extra)
    if not paths:
        util.warn("no log files yet")
        return 1
    cmd = ["tail"] + (["-F"] if args.follow else ["-n", str(args.lines)]) + paths
    return subprocess.call(cmd)


def cmd_shimtest(args, cfg) -> int:
    """Ask the chroot's loader whether it will preload each shim, and why not."""
    util.require_root("running the chroot's loader")
    for line in chroot.shim_lines(cfg):
        print("  " + line)
    print()
    for line in chroot.shim_probe(cfg):
        print(line)
    return 0


def cmd_subucom(args, cfg) -> int:
    """The panel link: drain it, watch it, or work out what a control lights."""
    util.require_root("reading the panel link")
    if args.replay:
        return subucom.replay(args.replay)
    if args.learn:
        return subucom.learn(cfg, args.learn)
    if args.watch:
        return subucom.watch(cfg, seconds=args.seconds)
    return subucom.run(cfg, capture=args.capture)


def cmd_sniff(args, cfg) -> int:
    """Print what the controller sends, so a button can be identified.

    Press the thing you want to map; the note or CC it sends is printed.  Put
    that in /etc/rb4r5/flx4-map.conf to bind it - for instance to move the
    effect picker onto a different button:

        note ch5 0x63 0xf001 global   FX select opens the picker
    """
    util.require_root("reading the controller's MIDI")
    bridge = cfg.bindir / "flx4-bridge"
    if not bridge.exists():
        raise util.Fail(f"{bridge} is not installed (run: launch.py build)")
    argv = [str(bridge), "-s"]
    if args.device:
        argv += ["-d", args.device]
    print("press the control you want to identify (Ctrl-C to stop)\n")
    return util.run(argv, capture=False, check=False).returncode


def cmd_jogtest(args, cfg) -> int:
    """Measure the FLX4's jog wheel, or retune it while it is in your hand."""
    if any(v is not None for v in (args.tpr, args.scale, args.bend)) or args.reverse:
        return jogcal.tune(cfg, tpr=args.tpr, scale=args.scale, bend=args.bend,
                           reverse=1 if args.reverse else None)
    util.require_root("reading the controller's MIDI")
    return jogcal.run(cfg, seconds=args.seconds, quiet=args.quiet)


def cmd_overlay(args, cfg) -> int:
    """The daemon that owns the button bar, the picker and the splash."""
    util.require_root("drawing on the framebuffer")
    if args.splash is not None:
        ok = overlay.command(f"splash {args.splash} {args.message or ''}")
        print("sent" if ok else f"no overlay daemon on {overlay.CMD_FIFO}")
        return 0 if ok else 1
    if args.fx:
        ok = overlay.command("fx")
        print("sent" if ok else f"no overlay daemon on {overlay.CMD_FIFO}")
        return 0 if ok else 1
    if args.preview:
        over = overlay.Overlay(cfg)
        base = Path(args.preview)
        util.ensure_dir(base)
        print(over.draw_bar(target=False).to_png(str(base / "top-bar.png")))
        print(over.draw_picker(target=False).to_png(str(base / "fx-picker.png")))
        print(over.draw_splash(0.6, "loading the library",
                               target=False).to_png(str(base / "splash.png")))
        return 0
    return overlay.run(cfg)


def cmd_fbtest(args, cfg) -> int:
    """Put a test pattern on the panel, in the framebuffer's own format.

    This takes rbp, the chroot, the shims and DirectFB out of the picture
    entirely: if the pattern is right, the panel, the mode and the pixel
    format are all right, and anything still wrong is above this layer.  If
    the pattern itself is wrong, a photograph of it says exactly how.
    """
    util.require_root("writing to the framebuffer")
    fbdev = cfg.get("display.fbdev", "/dev/fb0")
    if util.pgrep_arg("/root/pdj/rbp") and not args.force:
        raise util.Fail("the player is running and owns the screen - "
                        "stop it first (launch.py stop), or pass --force")

    info = fb.screeninfo(fbdev)
    if info["error"]:
        raise util.Fail(info["error"])
    for line in fb.describe(fbdev):
        print(line)
    print()

    if args.ui:
        # Push a 1280x800 frame through the driver's own publish path, built
        # from the same header the module is.  If this looks right and the
        # player does not, the player is loading an older module.
        tool = cfg.bindir / "fbpublish"
        if not tool.exists():
            tool = cfg.work / "host/fbpublish"
        if not tool.exists():
            raise util.Fail("fbpublish is not built yet - run: "
                            "sudo python3 launch.py build")
        argv = [str(tool), "-d", fbdev, "-t", str(int(args.seconds))]
        argv += ["-s"] if args.fill else ["-a"]
        if args.nearest:
            argv += ["-n"]
        if args.frame:
            argv += ["-f", args.frame]
        if args.keep:
            argv += ["-k"]
        print("the frame goes through the driver's own scaler "
              "(src/directfb/rb4r5_scale.h):")
        return util.run(argv, capture=False, timeout=args.seconds + 60).returncode

    aspect = args.aspect or (not args.fill and
                             str(cfg.get("display.fit", "aspect")) == "aspect")
    pattern = fb.test_pattern(info,
                              cfg.get("display.ui_width", 1280),
                              cfg.get("display.ui_height", 800),
                              aspect=aspect)
    with open(fbdev, "wb") as handle:
        handle.write(pattern)
    print("\n".join(fb.legend(info)))
    if args.png:
        print(fb.write_png(args.png, info["width"], info["height"],
                           fb.to_rgb(pattern, info)))
    print(f"\nholding it for {args.seconds:.0f}s - photograph the screen now")
    try:
        time.sleep(max(0.0, float(args.seconds)))
    except KeyboardInterrupt:
        pass
    if not args.keep:
        with open(fbdev, "wb") as handle:
            handle.write(b"\x00" * (info["line_length"] * info["height"]))
        print("screen cleared")
    return 0


def cmd_fbdump(args, cfg) -> int:
    print(display.fb_dump(args.path, cfg.get("display.fbdev", "/dev/fb0")))
    return 0


def cmd_screenshot(args, cfg) -> int:
    """Grab what is on the screen right now, into the screenshots directory."""
    util.require_root("reading the framebuffer")
    if args.path:
        path = Path(args.path)
    else:
        shots = util.ensure_dir(cfg.logs / "screenshots")
        path = shots / f"shot-{time.strftime('%Y%m%d-%H%M%S')}.png"
    base = path
    for attempt in range(max(1, args.count)):
        if attempt:
            time.sleep(args.interval)
            path = base.with_name(f"{base.stem}-{attempt}{base.suffix}")
        print(display.fb_dump(str(path), cfg.get("display.fbdev", "/dev/fb0")))
    if not util.pgrep_arg("/root/pdj/rbp"):
        util.warn("the player is not running, so this is whatever else is on "
                  "the screen")
    return 0


def cmd_service(args, cfg) -> int:
    util.require_root("managing the service")
    if args.action == "install":
        _print_notes(provision.service_unit(cfg, REPO, enable=True))
        return 0
    if args.action == "remove":
        _print_notes(provision.service_unit(cfg, REPO, undo=True))
        return 0
    return subprocess.call(["systemctl", args.action, "rb4r5.service"])


def cmd_config(args, cfg) -> int:
    if args.set:
        util.require_root("changing the config")
        for assignment in args.set:
            if "=" not in assignment:
                raise util.Fail(f"--set wants key=value, got '{assignment}'")
            key, _, raw = assignment.partition("=")
            try:
                value = json.loads(raw)
            except ValueError:
                value = raw
            cfg.set(key.strip(), value)
            util.info(f"{key.strip()} = {value!r}")
        cfg.save()
        util.ok(f"wrote {cfg.path}")
        return 0
    print(f"# effective configuration ({cfg.path}"
          f"{'' if cfg.path.exists() else ' - not written yet, showing defaults'})")
    print(json.dumps(cfg.data, indent=2))
    return 0


def cmd_audio(args, cfg) -> int:
    if args.levels:
        return audio.watch_levels(seconds=args.seconds)
    if args.test:
        util.require_root("opening the audio device")
        if util.pgrep_arg("/root/pdj/rbp"):
            raise util.Fail("the player has the device open - stop it first "
                            "(launch.py stop), then try again")
        return audio.test_tone(cfg, seconds=args.seconds)
    for line in audio.describe(cfg):
        print(line)
    if args.env:
        print()
        for key, value in audio.env(cfg).items():
            print(f"{key}={value}")
    print()
    levels = audio.live_levels()
    if levels["present"]:
        print(f"the player last reported L {levels['left']:.3f} "
              f"R {levels['right']:.3f} (period {levels['seq']})")
    elif levels["note"]:
        print(levels["note"])
    return 0


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="launch.py",
        description="Run the XDJ-RX3 rekordbox player on a Raspberry Pi 5 "
                    "with a DDJ-FLX4 and a touchscreen.",
        epilog="With no command: provision what is missing, build what is "
               "missing, then run the player full screen.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show the commands being run")
    parser.add_argument("-c", "--config", metavar="FILE",
                        help=f"configuration file (default {config.CONFIG_PATH})")
    parser.add_argument("--version", action="version",
                        version=f"rb4r5 {__version__}")
    sub = parser.add_subparsers(dest="command")

    auto = sub.add_parser("auto", help="setup + build if needed, then run "
                                       "(the default)")
    auto.add_argument("--detach", action="store_true",
                      help="start everything and return instead of supervising")
    auto.set_defaults(func=cmd_auto)

    setup = sub.add_parser("setup", help="provision this Pi (packages, boot "
                                         "config, console, service)")
    setup.add_argument("--no-service", action="store_true",
                       help="do not enable the boot service")
    setup.add_argument("--undo", action="store_true",
                       help="revert every system change rb4r5 made")
    setup.add_argument("--console", action="store_true",
                       help="also make the boot target console-only now "
                            "(by default the desktop is left alone until the "
                            "player actually runs)")
    setup.add_argument("-y", "--yes", action="store_true",
                       help="do not ask for confirmation")
    setup.set_defaults(func=cmd_setup)

    fw = sub.add_parser("firmware", help="get the XDJ-RX3 firmware and unpack "
                                        "it into a ready payload")
    fw.add_argument("--upd", metavar="FILE",
                    help="use this .UPD (or AlphaTheta's zip) instead of "
                         "downloading")
    fw.add_argument("--key", metavar="FILE",
                    help="the aes256.key (normally found automatically)")
    fw.add_argument("--force", action="store_true",
                    help="unpack again even if it was done before")
    fw.add_argument("--ask", action="store_true",
                    help="show the file picker instead of choosing by itself")
    fw.add_argument("--offline", action="store_true",
                    help="never download; use only what is already here")
    fw.add_argument("--show", action="store_true",
                    help="just report what is unpacked already")
    fw.set_defaults(func=cmd_firmware)

    payload = sub.add_parser("payload", help="assemble the chroot from the "
                                             "unpacked firmware")
    payload.add_argument("--force", action="store_true",
                         help="overwrite an existing runtime")
    payload.set_defaults(func=cmd_payload)

    build_cmd = sub.add_parser("build", help="build shims, daemons, DirectFB "
                                             "and the player")
    build_cmd.add_argument("--no-directfb", action="store_true")
    build_cmd.add_argument("--no-player", action="store_true")
    build_cmd.add_argument("--fast-directfb", action="store_true",
                           help="rebuild only the fbdev driver")
    build_cmd.set_defaults(func=cmd_build)

    run = sub.add_parser("run", help="run the player full screen and supervise it")
    run.add_argument("--force", action="store_true",
                     help="run even if a check fails (a stale display "
                          "driver, an unprepared platform)")
    run.add_argument("--detach", action="store_true")
    run.set_defaults(func=cmd_run)

    stop = sub.add_parser("stop", help="stop the player and release the screen")
    stop.add_argument("--restore-console", action="store_true",
                      help="also give the text console back")
    stop.set_defaults(func=cmd_stop)

    sub.add_parser("status", help="what is running").set_defaults(func=cmd_status)

    rec = sub.add_parser("recover", help="black screen? give the desktop and "
                                         "the login prompt back")
    rec.add_argument("--disable-service", action="store_true",
                     help="also stop rb4r5 starting at boot")
    rec.add_argument("--all", action="store_true",
                     help="also revert the boot-config changes")
    rec.set_defaults(func=cmd_recover)

    doc = sub.add_parser("doctor", help="check every subsystem and say what is "
                                        "wrong")
    doc.set_defaults(func=cmd_doctor)

    ver = sub.add_parser("verify", help="prove it works: screenshot, audio, "
                                        "and every FLX4 control one by one")
    ver.add_argument("--quick", action="store_true",
                     help="a handful of controls instead of all 33")
    ver.add_argument("--timeout", type=float, default=20.0,
                     help="seconds to wait for each control (default 20)")
    ver.add_argument("--no-controller", action="store_true")
    ver.add_argument("--no-touch", action="store_true")
    ver.set_defaults(func=cmd_verify)

    touch = sub.add_parser("touchd", help="the touchscreen daemon (run by the "
                                          "supervisor)")
    touch.add_argument("--once", action="store_true")
    touch.set_defaults(func=cmd_touchd)

    usb = sub.add_parser("usbwatch", help="the USB library watcher (run by the "
                                          "supervisor)")
    usb.add_argument("--once", action="store_true")
    usb.set_defaults(func=cmd_usbwatch)

    shimt = sub.add_parser("shimtest", help="ask the chroot's loader whether "
                                           "it will preload each shim")
    shimt.set_defaults(func=cmd_shimtest)

    panel = sub.add_parser("subucom", help="the panel link: drain it, and "
                                          "decode what it lights")
    panel.add_argument("--daemon", action="store_true",
                       help="drain it forever (what the supervisor runs)")
    panel.add_argument("--capture", action="store_true",
                       help="with --daemon: also record the stream")
    panel.add_argument("--watch", action="store_true",
                       help="print frames live, marking what changed")
    panel.add_argument("--learn", metavar="WHAT",
                       help="diff the panel across one action, e.g. 'cue 1'")
    panel.add_argument("--replay", metavar="FILE",
                       help="the same view over a capture file")
    panel.add_argument("--seconds", type=float, default=30.0)
    panel.set_defaults(func=cmd_subucom)

    sniff = sub.add_parser("sniff", help="print what the controller sends, to "
                                        "identify a button")
    sniff.add_argument("-d", "--device", help="/dev/snd/midiC*D*")
    sniff.set_defaults(func=cmd_sniff)

    jog = sub.add_parser("jogtest", help="measure the jog wheel: ticks per "
                                        "revolution and which way it counts")
    jog.add_argument("--seconds", type=float, default=12.0,
                     help="how long to watch the turn (default 12)")
    jog.add_argument("--quiet", action="store_true",
                     help="totals only, no per-message lines")
    jog.add_argument("--tpr", type=float, metavar="N",
                     help="retune a running bridge: messages per revolution "
                          "of the wheel (lower = the deck moves further)")
    jog.add_argument("--scale", type=float, metavar="X",
                     help="retune: multiply how far a turn pushes the deck")
    jog.add_argument("--bend", type=float, metavar="X",
                     help="retune: the rim, relative to the plate (0.25)")
    jog.add_argument("--reverse", action="store_true",
                     help="retune: count the other way")
    jog.set_defaults(func=cmd_jogtest)

    ov = sub.add_parser("overlay", help="the top button bar, the effect "
                                       "picker and the boot splash")
    ov.add_argument("--fx", action="store_true",
                    help="open or close the effect picker on a running daemon")
    ov.add_argument("--splash", type=float, metavar="PROGRESS",
                    help="show the splash at this progress (0..1)")
    ov.add_argument("--message", help="with --splash: the line under the bar")
    ov.add_argument("--preview", metavar="DIR",
                    help="render the three surfaces to PNGs instead of the "
                         "panel, to see them without a screen")
    ov.set_defaults(func=cmd_overlay)

    fbt = sub.add_parser("fbtest", help="test pattern on the panel: proves the "
                                       "mode, the pixel format and the colours")
    fbt.add_argument("--seconds", type=float, default=30,
                     help="how long to hold the pattern (default 30)")
    fbt.add_argument("--aspect", action="store_true",
                     help="mark where the UI goes with display.fit=aspect "
                          "(the default)")
    fbt.add_argument("--fill", action="store_true",
                     help="mark it for display.fit=fill instead")
    fbt.add_argument("--png", metavar="PATH",
                     help="also save the pattern as a PNG to compare against")
    fbt.add_argument("--keep", action="store_true",
                     help="leave the pattern on screen afterwards")
    fbt.add_argument("--force", action="store_true",
                     help="write to the framebuffer even while the player runs")
    fbt.add_argument("--ui", action="store_true",
                     help="instead of the colour bars, put a 1280x800 frame "
                          "through the driver's own scaler - the picture the "
                          "player should be getting")
    fbt.add_argument("--nearest", action="store_true",
                     help="with --ui: nearest neighbour, to see the difference")
    fbt.add_argument("--frame", metavar="RAW",
                     help="with --ui: a raw 1280x800 RGB565 frame to publish")
    fbt.set_defaults(func=cmd_fbtest)

    cal = sub.add_parser("calibrate", help="show which zone each touch hits")
    cal.add_argument("--raw", action="store_true",
                     help="also print every evdev event, for a panel that "
                          "looks dead")
    cal.add_argument("--seconds", type=float, default=60.0)
    cal.set_defaults(func=cmd_calibrate)

    sub.add_parser("probe", help="read the player's USB/database state"
                   ).set_defaults(func=cmd_probe)

    keys_cmd = sub.add_parser("keys", help="inject a key into the running player")
    keys_cmd.add_argument("key", nargs="?", help="name or code, e.g. play, 0x4101")
    keys_cmd.add_argument("channel", nargs="?", type=int, default=1,
                          help="deck channel (1 or 2)")
    keys_cmd.add_argument("--op", choices=["tap", "press", "release", "rotate",
                                           "value"], default="tap")
    keys_cmd.add_argument("--value", type=int, default=1,
                          help="delta for rotate, 0..1023 for value")
    keys_cmd.add_argument("-l", "--list", action="store_true")
    keys_cmd.set_defaults(func=cmd_keys)

    zone_cmd = sub.add_parser("zones", help="show or write the touch zone map")
    zone_cmd.add_argument("--path")
    zone_cmd.add_argument("--write-default", action="store_true")
    zone_cmd.set_defaults(func=cmd_zones)

    logs = sub.add_parser("logs", help="tail the logs")
    logs.add_argument("name", nargs="?")
    logs.add_argument("-f", "--follow", action="store_true")
    logs.add_argument("-n", "--lines", type=int, default=40)
    logs.set_defaults(func=cmd_logs)

    fbdump = sub.add_parser("fbdump", help="save what is on screen to a file")
    fbdump.add_argument("path", nargs="?", default="/tmp/rb4r5-screen.png")
    fbdump.set_defaults(func=cmd_fbdump)

    shot = sub.add_parser("screenshot", help="screenshot the player now "
                                             "(into /var/log/rb4r5/screenshots)")
    shot.add_argument("path", nargs="?", help="write here instead")
    shot.add_argument("-n", "--count", type=int, default=1,
                      help="take several, e.g. while operating a control")
    shot.add_argument("-i", "--interval", type=float, default=3.0)
    shot.set_defaults(func=cmd_screenshot)

    service = sub.add_parser("service", help="manage the systemd service")
    service.add_argument("action", choices=["install", "remove", "start", "stop",
                                            "restart", "status", "enable",
                                            "disable"])
    service.set_defaults(func=cmd_service)

    cfg_cmd = sub.add_parser("config", help="show or change the configuration")
    cfg_cmd.add_argument("--set", action="append", metavar="KEY=VALUE",
                         help="e.g. --set audio.channels=2")
    cfg_cmd.set_defaults(func=cmd_config)

    aud = sub.add_parser("audio", help="show the audio devices and the choice")
    aud.add_argument("--levels", action="store_true",
                     help="watch what the player is actually producing")
    aud.add_argument("--test", action="store_true",
                     help="play a tone on the chosen device, player stopped")
    aud.add_argument("--seconds", type=float, default=6.0,
                     help="how long to watch or play (default 6)")
    aud.add_argument("--env", action="store_true",
                     help="also print the environment the shim gets")
    aud.set_defaults(func=cmd_audio)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    util.set_verbose(args.verbose)
    cfg = config.load(args.config)
    func = getattr(args, "func", None)
    if func is None:
        # no subcommand: the "just make it work" path
        args.detach = False
        func = cmd_auto
    try:
        return func(args, cfg)
    except util.Fail as exc:
        util.error(str(exc))
        return 1
    except KeyboardInterrupt:
        print()
        util.info("interrupted")
        return 130
