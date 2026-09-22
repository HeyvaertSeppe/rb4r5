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

from . import (audio, chroot, config, display, jogcal, overlay, platform5,
               util)

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

    LOG_MAX_BYTES = 8 * 1024 * 1024

    def start(self) -> None:
        util.ensure_dir(self.log.parent)
        # A crash-looping child writes its log without end.  Unbounded, it
        # fills the disk - and a full disk does not announce itself, it just
        # truncates whatever is written next.
        note = util.cap_file(self.log, self.LOG_MAX_BYTES)
        if note:
            util.info(note)
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
        self.crashes: dict[str, int] = {}
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

        loadable, bad = chroot.shims_loadable(self.cfg)
        if not loadable:
            for line in chroot.shim_lines(self.cfg):
                util.error("shim " + line)
            message = (
                f"the player cannot preload {', '.join(bad)}.\n"
                "    ld.so says so once, into the player's log, and then runs "
                "it WITHOUT\n    the shim - so the engine talks straight to "
                "hardware it was never\n    meant to see, and what comes back "
                "is a crash that looks like\n    anything but a missing file."
                "\n    Rebuild them:  sudo python3 launch.py build")
            if strict:
                raise util.Fail(message)
            util.warn(message)

        # A display driver from an older build is the one fault that looks
        # like broken hardware: wrong colours, a stretched or half-width
        # picture.  Say so here rather than let it reach the panel.
        report = chroot.module_report(self.cfg)
        if report["present"] and (report["missing"] or report["stale"]) and strict:
            for line in chroot.module_lines(self.cfg):
                util.error(line)
            raise util.Fail(
                "the display driver in the chroot is not the current build.\n"
                "    Colours and scaling will be wrong.  Rebuild and install "
                "it:\n"
                "        sudo python3 launch.py build --fast-directfb\n"
                "    (or `launch.py run --force` to run it anyway)")

    def hold_console(self, release: bool = False) -> None:
        """Stop the tty1 login prompt while the player owns the screen.

        Stopped, not disabled: if the player is not running - including after
        a reboot with a half-built runtime - the console comes back by itself.
        """
        if not util.have("systemctl"):
            return
        if release:
            util.run(["systemctl", "start", "getty@tty1.service"], check=False)
            return
        proc = util.run(["systemctl", "is-active", "getty@tty1.service"],
                        check=False)
        if "active" in (proc.stdout or ""):
            util.run(["systemctl", "stop", "getty@tty1.service"], check=False)
            util.info("stopped the tty1 login prompt for the player's sake "
                      "(it returns when the player stops)")

    # Below this, a write can fail or truncate silently - and the things that
    # get truncated are whatever happens to be written next.
    DISK_FLOOR = 256 * 1024 * 1024

    def check_disk(self) -> None:
        """Say something before the disk runs out, not after.

        A full disk does not announce itself.  It truncates: a log, a source
        file being edited, a git object being written.  By the time anything
        complains, the damage is somewhere else entirely.
        """
        for where in (self.cfg.logs, Path("/tmp"), self.cfg.chroot):
            free = util.free_bytes(where)
            if free < 0:
                continue
            if free < self.DISK_FLOOR:
                util.warn(f"only {util.human(free)} free on the filesystem "
                          f"holding {where}.  A full disk truncates whatever "
                          f"is written next, without an error - clear some "
                          f"space before running:\n"
                          f"    sudo python3 launch.py logs --prune\n"
                          f"    sudo rm -f /tmp/audioshim.log")

    def prepare_system(self) -> None:
        util.step("preparing the screen and the runtime")
        self.check_disk()
        self.hold_console()
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
            streamd, _ = chroot.chroot_cmd(
                root, ["/lib/ld-linux.so.3", "/usr/bin/edb_streamd"])
            self.children.append(Child(
                "edb_streamd", streamd,
                logs / "edb_streamd.log", env={}, restart=True, delay=5.0))
        else:
            util.warn("edb_streamd is not in the runtime: a rekordbox database "
                      "cannot be imported (folder browsing still works)")

        args = [str(a) for a in (cfg.get("player.args") or ["-a"])]
        # player_env goes INSIDE the chroot: handing it to the host `chroot`
        # process makes the host loader chase the shims on the host, where
        # they are not, and log a preload failure for every one of them.
        rbp_argv, rbp_around = chroot.chroot_cmd(
            root, ["/lib/ld-linux.so.3", "/root/pdj/rbp"] + args, player_env)
        self.children.append(Child(
            "rbp", rbp_argv,
            logs / "rbp.log", env=rbp_around, essential=True,
            restart=bool(cfg.get("player.restart", True)),
            delay=float(cfg.get("player.restart_delay", 5.0)),
            nproc_limit=int(cfg.get("player.ulimit_procs", 1024) or 0) or None))

        if cfg.get("controller.enabled", True):
            jogcal.seed_tuning(cfg)     # so live tuning starts from the config
            bridge = cfg.bindir / "flx4-bridge"
            if bridge.exists():
                argv = [str(bridge), "-f", config.FIFO_CTRL,
                        "-J", str(cfg.get("controller.jog_ppr", 1800)),
                        "-T", str(cfg.get("controller.jog_ticks_per_rev", 1800)),
                        "-S", str(cfg.get("controller.jog_scale", 1.0)),
                        "-H", str(cfg.get("controller.jog_touch_timeout_ms", 4000)),
                        "-B", str(cfg.get("controller.jog_bend_scale", 0.25)),
                        "-E", str(cfg.get("controller.jog_emit_ms", 10)),
                        "-c", jogcal.JOG_CONF,
                        "-O", overlay.CMD_FIFO]
                if not cfg.get("controller.leds", True):
                    argv.append("-L")
                if cfg.get("controller.jog_reverse"):
                    argv.append("-R")
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

        # Nothing else reads the panel link, and a fifo with no reader stops
        # the writer once its buffer fills - which parks the player's panel
        # thread mid-write and looks like the UI freezing.
        self.children.append(Child(
            "rbpanel", [python, launcher, "subucom",
                        "--daemon"] + (["--capture"]
                                       if cfg.get("panel.capture") else []),
            logs / "panel.log"))

        if cfg.get("overlay.enabled", True):
            self.children.append(Child(
                "rboverlay", [python, launcher, "overlay"],
                logs / "overlay.log"))

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

    # -- the controller being plugged in, at any time ----------------------
    def watch_for_controller(self) -> None:
        """Take the FLX4 into use whenever it appears, however late.

        The engine opens its PCM once, at startup, and there is no way to hand
        a running engine a different card.  So plugging the controller in
        after the player is up leaves the sound on HDMI, the controller
        unrecognised, and its lights dark - and it stays that way however long
        you wait.

        This watches the card list and restarts the player when the
        controller appears, which is the only thing that actually moves the
        audio onto it.  It watches the whole time, not just at startup, so
        unplugging and plugging back in works too.
        """
        if not self.cfg.get("audio.restart_on_controller", True):
            return
        fragment = str(self.cfg.get("controller.name", "FLX4"))

        def watch():
            present = audio.find_controller(fragment) is not None
            settle = float(self.cfg.get("audio.controller_settle", 2.5))
            while not self.stopping:
                time.sleep(2.0)
                if self.stopping:
                    return
                now = audio.find_controller(fragment)
                if (now is not None) == present:
                    continue
                present = now is not None
                if not present:
                    util.warn("the controller was unplugged - the player is "
                              "still running; plug it back in and it will be "
                              "picked up")
                    continue
                util.warn(f"the controller appeared ({now['name']}) after the "
                          "player had already chosen its audio device - "
                          "restarting so master and cue go to it")
                time.sleep(settle)          # let its PCMs and MIDI node settle
                if not self.stopping:
                    self.restart_everything()
                return

        threading.Thread(target=watch, daemon=True).start()

    def restart_everything(self) -> None:
        """Re-exec the launcher, which is the only clean way to re-pick audio."""
        try:
            self.shutdown()
        finally:
            argv = [sys.executable, str(REPO / "launch.py"), "run"]
            util.info("restarting: " + " ".join(argv))
            os.execv(sys.executable, argv)

    # -- the boot splash ---------------------------------------------------
    def show_splash_now(self) -> None:
        """Put the splash up from this process, before any daemon exists.

        Waiting for the overlay daemon to start and then asking it to draw
        means the screen stays black for however long that takes - and if the
        daemon fails to start, for ever.  Drawing it here costs one pass over
        the framebuffer and happens before the chroot is even mounted, so the
        screen is never black while there is something to say.  The daemon
        takes it over when it comes up.
        """
        if not (self.cfg.get("overlay.enabled", True) and
                self.cfg.get("overlay.splash", True)):
            return
        try:
            over = overlay.Overlay(self.cfg)
            if not over.layout.fw:
                return
            over.hold_screen(True)      # the player must not paint over it
            over.mode = "splash"
            over.write_state()
            over.draw_splash(0.03, "starting")
            util.info("splash up; the player loads behind it")
        except Exception as exc:                          # noqa: BLE001
            util.warn(f"could not draw the splash ({exc}); carrying on")


    def splash_until_ready(self) -> None:
        """Cover the screen while the player comes up behind it.

        Nothing is delayed to make this work: the splash is drawn by the
        overlay daemon straight into the framebuffer while rbp starts, loads
        and draws underneath it.  The daemon takes it down when the player's
        own pixels appear - it fingerprints the area it painted and watches
        for it to change - so the hand-off happens when the UI is really
        there, not on a timer.
        """
        if not (self.cfg.get("overlay.enabled", True) and
                self.cfg.get("overlay.splash", True)):
            return
        steps = [(0.10, "starting the engine"),
                 (0.35, "opening the audio device"),
                 (0.60, "loading the library"),
                 (0.85, "drawing the interface")]
        ceiling = float(self.cfg.get("overlay.splash_max_seconds", 75.0))

        def feed():
            started = time.monotonic()
            for index, (progress, message) in enumerate(steps):
                while not overlay.command(f"splash {progress:.2f} {message}"):
                    time.sleep(0.3)          # the daemon is not listening yet
                    if time.monotonic() - started > ceiling:
                        return
                # hold each step long enough to read, then move on
                deadline = started + (index + 1) * 2.5
                while time.monotonic() < deadline:
                    if not overlay.read_state().get("modal"):
                        return               # the player got there first
                    time.sleep(0.25)

        threading.Thread(target=feed, daemon=True).start()

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
        self.hold_console(release=True)
        if restore_console:
            for note in display.restore_console():
                util.info(note)

    def run(self, foreground: bool = True, force: bool = False) -> int:
        self.preflight(strict=not force)
        self.stop_stale()
        self.show_splash_now()          # before anything slow happens
        self.prepare_system()
        self.build_children()
        self.splash_until_ready()
        self.watch_for_controller()

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
                    self.report_exit(child, code)
                    time.sleep(child.delay)
                    if child.name == "rbp":
                        chroot.make_stubs(self.cfg)
                        chroot.make_fifos()
                    child.start()
        finally:
            self.shutdown()
        return 0

    # -- saying why something died -----------------------------------------
    SIGNALS = {
        4: "SIGILL (an illegal instruction)",
        6: "SIGABRT - the program aborted itself: a failed assertion, a "
           "corrupted heap, or a smashed stack.  The reason is printed to "
           "its log just before it goes",
        7: "SIGBUS (a misaligned or bad memory access)",
        8: "SIGFPE (a division by zero)",
        9: "SIGKILL (something killed it, or the OOM killer did)",
        11: "SIGSEGV (it read or wrote memory it does not own)",
        15: "SIGTERM (asked to stop)",
    }

    def report_exit(self, child, code: int) -> None:
        """Say why a child died, with the end of its log.

        "exited (-6)" is a number; the reason is three lines further down in
        the log and nobody thinks to look, least of all while it is looping.
        """
        why = ""
        if code is not None and code < 0:
            why = self.SIGNALS.get(-code, f"signal {-code}")
        elif code:
            why = f"exit status {code}"
        util.warn(f"{child.name} exited ({code}){': ' + why if why else ''}; "
                  f"restarting in {child.delay:.0f}s")

        tail = self.log_tail(child, lines=14)
        if any("cannot be preloaded" in line for line in tail):
            # This used to be reported as the cause of death, and it sent a
            # whole day's debugging after a file that was loading correctly.
            # The message can come from the HOST loader reading the player's
            # LD_PRELOAD off the `chroot` process before chroot() happens, in
            # which case the shims still load fine a moment later.  The
            # environment is set inside the chroot now, so it should not
            # appear at all - but if it does, say what to check rather than
            # asserting a cause.
            util.warn("ld.so mentions a preload it could not open.  Check "
                      "whether the shims actually loaded:\n"
                      "    sudo python3 launch.py shimtest\n"
                      "If shimtest says they are accepted, this line is the "
                      "host loader talking and is not why the player died.")
        if tail:
            util.warn(f"the last of {child.log}:")
            for line in tail:
                print(f"    | {line}")

        self.crashes[child.name] = self.crashes.get(child.name, 0) + 1
        if self.crashes[child.name] in (3, 10, 30):
            util.error(f"{child.name} has now died {self.crashes[child.name]} "
                       f"times.  The lines above are the same every time - "
                       f"that is the fault, not the restarting.")
            if child.name == "rbp" and self.crashes[child.name] >= 3:
                util.error(
                    "If this started when audio began working, run it once "
                    "without audio to find out:\n"
                    "    sudo python3 launch.py config --set audio.disable=true\n"
                    "    sudo python3 launch.py run\n"
                    "If it stops crashing, the audio path is the cause and "
                    "that log says which part.")

    def log_tail(self, child, lines: int = 14) -> list[str]:
        """The interesting end of a log: the last lines, blanks dropped."""
        try:
            blob = Path(child.log).read_bytes()[-16384:]
        except OSError:
            return []
        text = blob.decode(errors="replace").splitlines()
        out = [line.rstrip() for line in text if line.strip()]
        return out[-lines:]

    def capture_screenshot(self) -> None:
        """Save what the player put on screen, a few times as it comes up.

        There is no other way to see the UI over SSH, and it is the first thing
        anyone wants when something looks wrong.  Several captures rather than
        one, because the first seconds are a black screen and a splash: by 90 s
        the library is up.  Best effort throughout - a failure here must never
        touch the player.
        """
        if not self.cfg.get("display.screenshot_on_start", True):
            return
        delays = self.cfg.get("display.screenshot_delays") or [10, 30, 90]
        shots = util.ensure_dir(self.cfg.logs / "screenshots")
        # three per run, a few MB each, kept forever: they filled the disk
        note = util.prune_files(shots, int(self.cfg.get("logs.keep_shots", 30)))
        if note:
            util.info(note)

        def shoot():
            previous = 0.0
            for delay in delays:
                time.sleep(max(0.0, float(delay) - previous))
                previous = float(delay)
                try:
                    path = shots / (f"startup-{time.strftime('%Y%m%d-%H%M%S')}"
                                    f"-{int(delay)}s.png")
                    util.info(display.fb_dump(
                        str(path), self.cfg.get("display.fbdev", "/dev/fb0")))
                except Exception as exc:                  # noqa: BLE001
                    util.debug(f"screenshot at {delay}s failed: {exc}")

        threading.Thread(target=shoot, daemon=True).start()
        util.info(f"screenshots of the player will appear in {shots} "
                  f"(at {', '.join(str(d) + 's' for d in delays)})")

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
