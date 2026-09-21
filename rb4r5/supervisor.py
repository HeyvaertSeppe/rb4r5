"""Bring the whole player up, keep it up, and take it down cleanly.

Start order matters:

  1. console hygiene            nothing else may paint on /dev/fb0
  2. device stubs + FIFOs       the player opens them while starting
  3. bind mounts                /dev /proc /sys /tmp into the chroot
  4. edb_streamd                DeviceSQL, or the library never imports
  5. rbp                        the player itself (full screen, no window)
  6. flx4-bridge, rbtouchd,     controls; they wait for their hardware and for
     rbkeyd, usbwatch           the player's FIFOs, so order is free here

Everything is a child process, each with its own log under /var/log/rb4r5.
A child that dies is restarted; the player has a restart budget so a boot loop
cannot hide a real fault.
"""
from __future__ import annotations

import os
import resource
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import audio, chroot, config, display, platform5, util

REPO = Path(__file__).resolve().parent.parent


class Child:
    """One supervised process."""

    def __init__(self, name: str, argv: list[str], log: Path,
                 env: dict | None = None, essential: bool = False,
                 restart: bool = True, delay: float = 3.0,
                 nproc_limit: int | None = None, cwd: str | None = None):
        self.name = name
        self.argv = argv
        self.log = log
        self.env = env
        self.essential = essential
        self.restart = restart
        self.delay = delay
        self.nproc_limit = nproc_limit
        self.cwd = cwd
        self.proc: subprocess.Popen | None = None
        self.starts = 0
        self.first_start = 0.0
        self.last_start = 0.0

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _preexec(self):
        os.setsid()
        if self.nproc_limit:
            # A runaway init fork-bombed the earlier ports.  The player needs
            # ~40 threads, so this is generous but finite.  Best effort only:
            # the kernel does not enforce RLIMIT_NPROC for a process holding
            # CAP_SYS_RESOURCE, which root does.
            try:
                resource.setrlimit(resource.RLIMIT_NPROC,
                                   (self.nproc_limit, self.nproc_limit))
            except (ValueError, OSError):
                pass

    def start(self) -> None:
        util.ensure_dir(self.log.parent)
        env = dict(os.environ)
        if self.env:
            env.update(self.env)
        # The log handle is closed in this process straight away: the child
        # holds its own copy, and leaking one per restart would eventually run
        # the supervisor out of descriptors.
        with open(self.log, "ab", buffering=0) as handle:
            handle.write(f"\n=== {self.name} started {time.strftime('%F %T')}: "
                         f"{' '.join(self.argv)}\n".encode())
            self.proc = subprocess.Popen(
                self.argv, stdout=handle, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=env, preexec_fn=self._preexec,
                cwd=self.cwd, close_fds=True)
        self.starts += 1
        self.last_start = time.monotonic()
        if not self.first_start:
            self.first_start = self.last_start
        util.ok(f"{self.name}: pid {self.proc.pid} (log: {self.log})")

    def stop(self, timeout: float = 8.0) -> None:
        if not self.alive():
            return
        assert self.proc is not None
        util.info(f"{self.name}: stopping (pid {self.proc.pid})")
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except OSError:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            util.warn(f"{self.name}: did not exit, killing")
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except OSError:
                self.proc.kill()


class Supervisor:
    def __init__(self, cfg, repo: Path = REPO):
        self.cfg = cfg
        self.repo = repo
        self.children: list[Child] = []
        self.stopping = False
        self.audio_choice: dict | None = None

    # -- setup -------------------------------------------------------------
    def preflight(self, strict: bool = True) -> None:
        problems = [p for p in platform5.check(strict=False)
                    if not p.startswith("note:")]
        if problems and strict:
            for problem in problems:
                util.error(problem)
            raise util.Fail("the platform is not ready - fix the above, or run "
                            "`sudo python3 launch.py setup`")
        for problem in problems:
            util.warn(problem)
        chroot.require_ready(self.cfg)

    def prepare_system(self) -> None:
        util.step("preparing the screen and the runtime")
        for note in display.quiet_console(int(self.cfg.get("display.quiet_console", 2))):
            util.info(note)
        for note in chroot.make_stubs(self.cfg):
            util.debug(f"stub: {note}")
        util.info(f"fifos: {', '.join(chroot.make_fifos())}")
        for note in chroot.mount_binds(self.cfg):
            util.info(note)

        self.audio_choice = audio.select(self.cfg)
        for note in self.audio_choice["notes"]:
            util.warn(f"audio: {note}")
        util.ok(f"audio: {self.audio_choice['device']} "
                f"({self.audio_choice['channels']}ch @"
                f"{self.audio_choice['rate']} Hz, "
                f"source={self.audio_choice['source']})")
        for note in audio.unmute(self.audio_choice.get("card")):
            util.info(f"audio: {note}")
        util.info(f"display: {display.scale_note(self.cfg)}")

    # -- children ----------------------------------------------------------
    def build_children(self) -> None:
        cfg = self.cfg
        logs = util.ensure_dir(cfg.logs)
        root = str(cfg.chroot)
        player_env = chroot.player_env(cfg, audio.env(cfg, self.audio_choice))
        python = sys.executable or "python3"
        launcher = str(self.repo / "launch.py")

        self.children = []

        # DeviceSQL first: the player looks for its IPC sockets at startup.
        for stale in ("/tmp/guard_LocalDBServer", "/tmp/req_LocalDBServer"):
            Path(stale).unlink(missing_ok=True)
        if (cfg.chroot / "usr/bin/edb_streamd").exists():
            self.children.append(Child(
                "edb_streamd",
                ["chroot", root, "/lib/ld-linux.so.3", "/usr/bin/edb_streamd"],
                logs / "edb_streamd.log", env={}, restart=True, delay=5.0))
        else:
            util.warn("edb_streamd is not in the runtime: a rekordbox database "
                      "cannot be imported (folder browsing still works)")

        args = [str(a) for a in (cfg.get("player.args") or ["-a"])]
        self.children.append(Child(
            "rbp",
            ["chroot", root, "/lib/ld-linux.so.3", "/root/pdj/rbp"] + args,
            logs / "rbp.log", env=player_env, essential=True,
            restart=bool(cfg.get("player.restart", True)),
            delay=float(cfg.get("player.restart_delay", 5.0)),
            nproc_limit=int(cfg.get("player.ulimit_procs", 1024) or 0) or None))

        if cfg.get("controller.enabled", True):
            bridge = cfg.bindir / "flx4-bridge"
            if bridge.exists():
                argv = [str(bridge), "-f", config.FIFO_CTRL,
                        "-J", str(cfg.get("controller.jog_ppr", 1800))]
                if cfg.get("controller.filter_init", True):
                    argv.append("-F")
                if cfg.get("controller.verbose"):
                    argv.append("-v")
                map_file = cfg.get("controller.map_file")
                if map_file and Path(map_file).exists():
                    argv += ["-m", str(map_file)]
                self.children.append(Child("flx4-bridge", argv,
                                           logs / "flx4-bridge.log"))
            else:
                util.warn(f"{bridge} is not installed (run: launch.py build)")

        if cfg.get("touch.enabled", True):
            self.children.append(Child(
                "rbtouchd", [python, launcher, "touchd"], logs / "touchd.log"))

        if cfg.get("keyboard.enabled", True):
            rbkeyd = cfg.bindir / "rbkeyd"
            if rbkeyd.exists():
                argv = [str(rbkeyd)]
                if cfg.get("keyboard.device"):
                    argv += ["-d", str(cfg.get("keyboard.device"))]
                elif cfg.get("keyboard.name"):
                    argv += ["-n", str(cfg.get("keyboard.name"))]
                self.children.append(Child("rbkeyd", argv, logs / "rbkeyd.log"))

        if cfg.get("usb.enabled", True):
            self.children.append(Child(
                "usbwatch", [python, launcher, "usbwatch"], logs / "usbwatch.log"))

    # -- lifecycle ---------------------------------------------------------
    def stop_stale(self) -> None:
        """Never leave a second player behind (two fight over the screen)."""
        for arg in ("/root/pdj/rbp", "/usr/bin/edb_streamd"):
            for pid in util.pgrep_arg(arg):
                util.warn(f"killing a leftover process {pid} ({arg})")
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
        if util.pgrep_arg("/root/pdj/rbp"):
            time.sleep(2.0)
            for pid in util.pgrep_arg("/root/pdj/rbp"):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

    def start_all(self) -> None:
        for child in self.children:
            child.start()
            if child.name == "edb_streamd":
                time.sleep(1.0)     # let it create its sockets
            elif child.name == "rbp":
                time.sleep(1.0)

    def stop_all(self) -> None:
        for child in reversed(self.children):
            child.stop()

    def shutdown(self, restore_console: bool = False) -> None:
        if self.stopping:
            return
        self.stopping = True
        util.step("shutting down")
        self.stop_all()
        for note in chroot.umount_binds(self.cfg):
            util.info(note)
        if restore_console:
            for note in display.restore_console():
                util.info(note)

    def run(self, foreground: bool = True) -> int:
        self.preflight()
        self.stop_stale()
        self.prepare_system()
        self.build_children()

        signals = {}

        def handle(signum, _frame):
            util.info(f"got signal {signum}")
            signals["stop"] = True

        signal.signal(signal.SIGTERM, handle)
        signal.signal(signal.SIGINT, handle)

        util.step("starting the player")
        self.start_all()
        util.ok("rb4r5 is running - the rekordbox UI should be on the screen")
        self.capture_screenshot()
        util.info("logs: " + str(self.cfg.logs) + "  (rbp.log, flx4-bridge.log, "
                  "touchd.log, usbwatch.log)")

        if not foreground:
            return 0

        budget = int(self.cfg.get("player.max_restarts", 10))
        try:
            while not signals.get("stop"):
                time.sleep(2.0)
                for child in self.children:
                    if child.alive() or not child.restart:
                        continue
                    code = child.proc.returncode if child.proc else "?"
                    window = time.monotonic() - child.first_start
                    if child.essential and child.starts > budget and window < 3600:
                        util.error(f"{child.name} died {child.starts} times in "
                                   f"{window / 60:.0f} min (last exit {code}); "
                                   f"giving up - see {child.log}")
                        return 1
                    util.warn(f"{child.name} exited ({code}); restarting in "
                              f"{child.delay:.0f}s")
                    time.sleep(child.delay)
                    if child.name == "rbp":
                        chroot.make_stubs(self.cfg)
                        chroot.make_fifos()
                    child.start()
        finally:
            self.shutdown()
        return 0

    def capture_screenshot(self, delay: float = 25.0) -> None:
        """Save what the player put on screen, a few seconds in.

        There is no other way to see the UI over SSH, and it is the first thing
        anyone asks for when something looks wrong.  Best effort: a failure
        here must never affect the player.
        """
        if not self.cfg.get("display.screenshot_on_start", True):
            return

        def shoot():
            time.sleep(delay)
            try:
                shots = util.ensure_dir(self.cfg.logs / "screenshots")
                path = shots / f"startup-{time.strftime('%Y%m%d-%H%M%S')}.png"
                util.info(display.fb_dump(str(path),
                                          self.cfg.get("display.fbdev",
                                                       "/dev/fb0")))
            except Exception as exc:                      # noqa: BLE001
                util.debug(f"screenshot failed: {exc}")

        thread = threading.Thread(target=shoot, daemon=True)
        thread.start()

    # -- status ------------------------------------------------------------
    def status_lines(self) -> list[str]:
        lines = []
        for child in self.children:
            state = f"pid {child.proc.pid}" if child.alive() else "not running"
            lines.append(f"  {child.name:<14} {state} (starts: {child.starts})")
        return lines


def status(cfg) -> list[str]:
    """Report on a player started by any means (service, launcher, by hand)."""
    lines = []
    pids = util.pgrep_arg("/root/pdj/rbp")
    lines.append(f"player:      {'running, pid ' + str(pids[0]) if pids else 'not running'}")
    for name, arg in (("edb_streamd", "/usr/bin/edb_streamd"),
                      ("flx4-bridge", str(cfg.bindir / 'flx4-bridge')),
                      ("rbkeyd", str(cfg.bindir / 'rbkeyd'))):
        found = util.pgrep_arg(arg)
        lines.append(f"{name + ':':<12} "
                     f"{'running, pid ' + str(found[0]) if found else 'not running'}")
    for name, needle in (("rbtouchd", "touchd"), ("usbwatch", "usbwatch")):
        found = [p for p in util.pgrep("launch.py " + needle)]
        lines.append(f"{name + ':':<12} "
                     f"{'running, pid ' + str(found[0]) if found else 'not running'}")
    if util.have("systemctl"):
        unit = util.out(["systemctl", "is-active", "rb4r5.service"], "unknown")
        enabled = util.out(["systemctl", "is-enabled", "rb4r5.service"], "unknown")
        lines.append(f"service:     {unit} ({enabled} at boot)")
    fb = display.fb_info(cfg.get("display.fbdev", "/dev/fb0"))
    if fb["present"]:
        nonzero = display.fb_nonzero(fb["dev"])
        lines.append(f"framebuffer: {fb['width']}x{fb['height']} @{fb['bpp']}bpp "
                     f"({fb['name']}), {nonzero} non-zero bytes in the first "
                     f"400k {'(the player is drawing)' if nonzero > 1000 else '(blank!)'}")
    return lines
