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
               firmware, keys, platform5, probe, provision, supervisor, touchd,
               usbwatch, util, zones)

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
                                     enable_service=not args.no_service))
    util.ok("setup done")
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
        for line in firmware.describe(cfg):
            print(line)
        return 0 if firmware.ready(cfg) else 1
    _print_notes(firmware.prepare(cfg, upd=args.upd, key=args.key,
                                  force=args.force, ask=not args.no_ask))
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
    return sup.run(foreground=not args.detach)


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
            util.step("first run: the player's runtime has not been unpacked yet")
            print()
            print("rb4r5 ships no Pioneer firmware, so point it at the .UPD "
                  "update file you downloaded;")
            print("everything after that - decrypting, unpacking, patching, "
                  "building - is automatic.")
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


def cmd_status(args, cfg) -> int:
    for line in supervisor.status(cfg):
        print(line)
    return 0


def cmd_doctor(args, cfg) -> int:
    return doctor.run(cfg, verbose=args.verbose)


def cmd_touchd(args, cfg) -> int:
    return touchd.run(cfg, once=args.once)


def cmd_usbwatch(args, cfg) -> int:
    util.require_root("the USB watcher")
    return usbwatch.run(cfg, once=args.once)


def cmd_calibrate(args, cfg) -> int:
    return touchd.calibrate(cfg, seconds=args.seconds)


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


def cmd_fbdump(args, cfg) -> int:
    print(display.fb_dump(args.path, cfg.get("display.fbdev", "/dev/fb0")))
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
    for line in audio.describe(cfg):
        print(line)
    if args.env:
        print()
        for key, value in audio.env(cfg).items():
            print(f"{key}={value}")
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
    setup.add_argument("-y", "--yes", action="store_true",
                       help="do not ask for confirmation")
    setup.set_defaults(func=cmd_setup)

    fw = sub.add_parser("firmware", help="pick your .UPD and unpack it into a "
                                        "ready payload")
    fw.add_argument("--upd", metavar="FILE",
                    help="the firmware file (skips the prompt)")
    fw.add_argument("--key", metavar="FILE",
                    help="the aes256.key (normally found automatically)")
    fw.add_argument("--force", action="store_true",
                    help="unpack again even if it was done before")
    fw.add_argument("--no-ask", action="store_true",
                    help="never prompt; fail instead")
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
    run.add_argument("--detach", action="store_true")
    run.set_defaults(func=cmd_run)

    stop = sub.add_parser("stop", help="stop the player and release the screen")
    stop.add_argument("--restore-console", action="store_true",
                      help="also give the text console back")
    stop.set_defaults(func=cmd_stop)

    sub.add_parser("status", help="what is running").set_defaults(func=cmd_status)

    doc = sub.add_parser("doctor", help="check every subsystem and say what is "
                                        "wrong")
    doc.set_defaults(func=cmd_doctor)

    touch = sub.add_parser("touchd", help="the touchscreen daemon (run by the "
                                          "supervisor)")
    touch.add_argument("--once", action="store_true")
    touch.set_defaults(func=cmd_touchd)

    usb = sub.add_parser("usbwatch", help="the USB library watcher (run by the "
                                          "supervisor)")
    usb.add_argument("--once", action="store_true")
    usb.set_defaults(func=cmd_usbwatch)

    cal = sub.add_parser("calibrate", help="show which zone each touch hits")
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
