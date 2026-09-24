"""Configuration: defaults, /etc/rb4r5/config.json, and the path layout.

Everything the launcher does is driven from here, so a site can change paths,
the audio device, the screen mode or the touch behaviour without editing code.
`rb4r5 setup` writes the file; every command reads it.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from . import util

CONFIG_PATH = Path(os.environ.get("RB4R5_CONFIG", "/etc/rb4r5/config.json"))

# The repository root (this file is <repo>/rb4r5/config.py)
REPO = Path(__file__).resolve().parent.parent

DEFAULTS: dict = {
    # ---- filesystem layout -------------------------------------------------
    "paths": {
        "root": "/opt/rb4r5",            # everything we install lives here
        "chroot": "/opt/rb4r5/chroot",   # the soft-float XDJ-RX3 userland
        "payload": "/opt/rb4r5/payload", # your extracted firmware (see docs/03)
        "work": "/opt/rb4r5/work",       # build scratch
        "bin": "/usr/local/bin",         # host daemons (flx4-bridge, rbkeyd)
        "logs": "/var/log/rb4r5",
        "media": "/media/usb1/sda1",     # where a rekordbox stick is mounted
    },
    # ---- display -----------------------------------------------------------
    "display": {
        # A PNG for the boot screen.  Nothing vendor-owned ships here, so
        # none is bundled: point this at one you own, or drop it at
        # /etc/rb4r5/boot-logo.png.  The player's own firmware carries one.
        "boot_logo": None,
        "fbdev": "/dev/fb0",
        # The UI the RX3 renders; the patched DirectFB driver scales this to
        # whatever the real framebuffer mode is.  Do not change.
        "ui_width": 1280,
        "ui_height": 800,
        # Force an HDMI mode at boot ("1920x1080@60", or null to leave the
        # monitor's preferred mode alone).  A 22" 1080p panel needs nothing.
        "force_mode": None,
        "hdmi_port": 0,                  # Pi 5 has two: 0 = the one next to USB-C
        "rotate": "off",                 # DFB_ROTATE: off|left|right|180
        # "aspect" (default) keeps the RX3's 16:10 shape and leaves black bars
        # at the sides of a 16:9 panel - the UI is never distorted.  "fill"
        # stretches it over the whole panel instead.
        "fit": "aspect",
        # "bilinear" (default) interpolates when scaling 1280x800 up to the
        # panel; "nearest" duplicates pixels, which is faster but looks coarse.
        "scale": "bilinear",
        # The RX3 has a row of buttons above its screen; on a touch panel they
        # live in a strip at the top that the player is kept out of.
        "top_bar": True,
        "top_bar_height": 0,             # pixels, 0 = 8% of the panel
        "quiet_console": 2,              # 0 keep console, 1 quiet printk, 2 also detach fbcon
        "screenshot_on_start": True,     # save what the UI looks like as it starts
        "screenshot_delays": [10, 30, 90],
        "blank_timeout": 0,              # console blanking, 0 = never
    },
    # ---- overlay (what the launcher draws itself) ---------------------------
    "overlay": {
        # How the player's BEAT FX selector is driven.  "position" sends an
        # absolute 10-bit knob position, which is what a selector knob on the
        # RX3 takes; "delta" and "tap" are here so the other two readings of
        # that control can be tried without a rebuild.
        "fx_mode": "position",
        "enabled": True,
        "buttons_file": "/etc/rb4r5/top-bar.json",
        "fx_file": "/etc/rb4r5/fx-list.json",
        "splash": True,                  # boot screen while the player loads
        "splash_min_seconds": 2.0,       # never flash past too fast to read
        "splash_max_seconds": 75.0,      # give up waiting and show the player
        "nudge_after_picker": False,
        # master level meter in the black bar beside the picture, fed by
        # audioshim (which is the only thing that sees the audio)
        "meter": True,
        # The labels are set in the RX3's own typeface, found in its firmware
        # (the chroot's /root/gui).  A path here picks a font file instead.
        "font": None,
    },
    # ---- audio -------------------------------------------------------------
    "audio": {
        # null = auto-detect: prefer the DDJ-FLX4, fall back to HDMI.
        "card": None,                    # ALSA card name fragment, e.g. "FLX4"
        "device": None,                  # full override, e.g. "plughw:CARD=FLX4,DEV=0"
        "channels": None,                # 4 on the FLX4 (1/2 master, 3/4 phones)
        "rate": 44100,
        "format": 6,                     # SND_PCM_FORMAT_S24_LE
        "plug": True,                    # use plughw: (lets alsa-lib convert)
        # The engine asks for 2 x 64 frames, a 2.9 ms buffer: right for the
        # RX3's local I2S output, far too small for anything over USB, which
        # then underruns on every period until the shim gives up on it.
        "period_frames": 512,
        "periods": 4,
        "cue_mirror": False,             # mirror master into the phones until PFL
        "cue_on_stereo": False,          # on a stereo sink, follow the cue mix
        # The engine picks its output once, at startup.  If the controller is
        # plugged in later, restart so master + cue move onto it.
        # Open no device at all: the engine runs silently, paced in software.
        # For finding out whether a crash belongs to the audio path.
        "disable": False,
        "restart_on_controller": True,
        "controller_settle": 2.5,        # wait this long after it appears
        "startup_mute_ms": 1500,         # kill the engine's power-on transient
        "startup_fade_ms": 300,
        "fallback_hdmi": True,           # use HDMI audio when no controller
    },
    # ---- controller --------------------------------------------------------
    "controller": {
        "enabled": True,
        "name": "FLX4",                  # ALSA card name fragment to look for
        "jog_ppr": 1800,                 # jog pulses per revolution
        # Turn these two if the platter moves the wrong way or by the wrong
        # amount: reverse flips the direction, scale multiplies how far a
        # turn pushes the deck (0.5 = half as far, 2 = twice).
        "jog_reverse": False,
        "jog_scale": 1.0,
        # How many MIDI messages the FLX4 sends for one turn of its own jog
        # wheel.  Measure it: `launch.py jogtest`.  Getting this wrong makes
        # the wheel feel dead and the position run away.
        # An estimate: Pioneer controller wheels send far fewer messages per
        # revolution than the RX3's own 1800, and using the RX3's number makes
        # the deck crawl.  Measure yours: `launch.py jogtest`, and tune it
        # live with `jogtest --tpr N` while the wheel is in your hand.
        "jog_ticks_per_rev": 600,
        # The rim bends, the plate scratches.  bend_scale is how much gentler
        # the rim is than the plate (0.25 = a quarter as far).
        "jog_bend_scale": 0.25,
        "jog_emit_ms": 10,               # one speed per this many ms
        "leds": True,                    # light the controller's buttons
        # Light them from the PLAYER's own state (PLAY, CUE, SYNC, loops, hot
        # cues, BEAT FX, per-channel meters, headphone CUE), read inside the
        # player by keyshim.so.  Off: they follow the buttons instead.
        "engine_state": True,
        "jog_touch_timeout_ms": 4000,    # let a stuck plate-touch go
        "midi_device": None,             # /dev/snd/midiC*D*, null = auto
        "filter_init": True,             # select FILTER as the colour FX type
        "map_file": "/etc/rb4r5/flx4-map.conf",
        "verbose": False,
    },
    # ---- the panel link (subucom) ------------------------------------------
    "panel": {
        # Always drained (see rb4r5/subucom.py); capture writes the stream to
        # /var/log/rb4r5/subucom.bin for decoding the LED protocol.
        "capture": False,
    },
    # ---- touchscreen -------------------------------------------------------
    "touch": {
        "enabled": True,
        "device": None,                  # /dev/input/eventN, null = auto-detect
        "name": None,                    # match by device name instead
        "swap_xy": False,
        "invert_x": False,
        "invert_y": False,
        "zones_file": "/etc/rb4r5/touch-zones.json",
        "tap_ms": 400,                   # longer than this is not a tap
        "tap_slop": 0.02,                # movement (0..1) still counted as a tap
        "scroll_step": 0.035,            # drag distance per browse-knob step
        "jog_scale": 3.0,                # jog revolutions per screen width
        # Native RX3 touch: the player gets the finger itself, through the
        # RX3's own touch device (memshim), and does with it what an RX3
        # does - tap a row, drop the needle on the waveform, the on-screen
        # buttons, everything.  The record ("rx3") is the one the Prime GO
        # and SC Live 4 ports verified on this same player.  False = the
        # older zone map (touch-zones.json), which only presses keys.
        "native": True,
        "native_format": "rx3",
        "native_invert_x": True,         # the RX3 panel is wired mirrored
        "native_min_tap_ms": 90,         # a tap is held at least this long
        "verbose": False,
    },
    # ---- keyboard (optional fallback control) ------------------------------
    "keyboard": {
        "enabled": True,
        "device": None,
        "name": None,
    },
    # ---- USB library -------------------------------------------------------
    "usb": {
        "enabled": True,
        "poll": 1.0,
        "confirm_tries": 18,             # re-notify rbp until DeviceSQL is ready
        "confirm_interval": 5.0,
        # Only consider removable USB disks; never touch the Pi's own storage.
        "require_usb": True,
    },
    # ---- player ------------------------------------------------------------
    "player": {
        "args": ["-a"],
        "restart": True,                 # restart rbp if it dies
        "restart_delay": 5.0,
        "max_restarts": 10,              # per hour, then give up and report
        "ulimit_procs": 1024,
        "env": {},                       # extra environment for rbp
    },
    # ---- firmware ----------------------------------------------------------
    "firmware": {
        # The launcher fetches the firmware this port is built around straight
        # from AlphaTheta, so there is nothing to pick.  A .UPD (or their zip)
        # already sitting in the payload directory is used instead, so an
        # offline Pi works too.
        "auto_download": True,
        # the direct .UPD: nothing to unzip, no consent page
        "url": "https://balvansintlievens.qzz.io/XDJRX3.UPD",
        # tried in turn after `url`; "gdrive:<file id>" is understood
        "mirrors": [
            ("https://downloads.support.alphatheta.com/firmwares/"
             "all-in-one-dj-systems/XDJ-RX3/XDJ-RX3_v120.zip"),
            "gdrive:1FvztdfmpOvzqSXHDSo0eWhxe4RaEP5Ul",
        ],
        "zip_name": "XDJ-RX3_v120.zip",
        "version": "1.20",
        "expect_upd_size": 69171216,
        "verify_rbp_md5": "4f2efcfc0c9e3f539289f863acfddcc6",
    },
    # ---- build -------------------------------------------------------------
    "build": {
        "primebox": "/opt/rb4r5/PrimeBox",
        "neon": True,
        "jobs": 0,                       # 0 = nproc
        # Rebuild the bridge and the shims before a run when their sources
        # have changed since they were installed.  Without it a `git pull`
        # changes nothing until someone remembers `launch.py build`.
        "auto_rebuild": True,
    },
}


def _merge(base: dict, over: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


class Config:
    def __init__(self, data: dict, path: Path = CONFIG_PATH):
        self.data = data
        self.path = path

    # -- access ------------------------------------------------------------
    def __getitem__(self, section: str):
        return self.data[section]

    def get(self, dotted: str, default=None):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value) -> None:
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    # -- derived paths ------------------------------------------------------
    @property
    def chroot(self) -> Path:
        return Path(self.get("paths.chroot"))

    @property
    def payload(self) -> Path:
        return Path(self.get("paths.payload"))

    @property
    def work(self) -> Path:
        return Path(self.get("paths.work"))

    @property
    def logs(self) -> Path:
        return Path(self.get("paths.logs"))

    @property
    def bindir(self) -> Path:
        return Path(self.get("paths.bin"))

    @property
    def media(self) -> Path:
        return Path(self.get("paths.media"))

    @property
    def chroot_media(self) -> Path:
        return self.chroot / str(self.media).lstrip("/")

    # -- persistence --------------------------------------------------------
    def save(self, path: Path | None = None) -> None:
        """Write only what differs from the defaults.

        It used to write EVERY value, which froze the defaults of the day the
        Pi was set up into its config: change a default later - the jog's
        ticks per turn, native touch - and the saved copy of the old one
        quietly won, for ever."""
        path = path or self.path
        util.ensure_dir(path.parent)
        util.write_text(path, json.dumps(_changes(DEFAULTS, self.data),
                                         indent=2) + "\n")


def _changes(base: dict, data: dict) -> dict:
    """The part of `data` that is not simply `base`."""
    out = {}
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            inner = _changes(base[key], value)
            if inner:
                out[key] = inner
        elif key not in base or base[key] != value:
            out[key] = value
    return out


# Defaults that have been replaced, with the old values that configs saved
# before could still carry (see Config.save).  A config holding exactly an
# old default gets the new one - it was never anybody's choice.
SUPERSEDED = {
    "controller.jog_ticks_per_rev": [1800],   # the RX3's wheel, not the FLX4's
    "touch.native": [False],                  # the touch record is verified now
    "touch.native_format": ["fxy"],
}


def _migrate(data: dict, user: dict) -> list[str]:
    notes = []
    for dotted, old_values in SUPERSEDED.items():
        section, _, key = dotted.partition(".")
        mine = user.get(section, {})
        if not isinstance(mine, dict) or key not in mine:
            continue
        if mine[key] in old_values:
            new = DEFAULTS[section][key]
            data[section][key] = new
            notes.append(f"{dotted} was {mine[key]!r}, an old default - "
                         f"using the new one, {new!r}")
    return notes


def load(path: Path | None = None) -> Config:
    path = Path(path) if path else CONFIG_PATH
    # a copy, never DEFAULTS itself: Config.set() writes into this dict, and
    # with no config file on disk that would edit the defaults for the rest of
    # the process - every later load() would inherit the change
    data = copy.deepcopy(DEFAULTS)
    if path.exists():
        try:
            user = json.loads(path.read_text())
            data = _merge(DEFAULTS, user)
            migrated = _migrate(data, user if isinstance(user, dict) else {})
        except (OSError, ValueError) as exc:
            util.warn(f"{path} is not readable JSON ({exc}); using defaults")
            migrated = []
    else:
        migrated = []
    cfg = Config(data, path)
    cfg.migrated = migrated               # the launcher says these once
    return cfg


# Paths that live in /tmp because the chroot bind-mounts the host /tmp, which
# is how the shims inside rbp and the daemons outside it talk to each other.
# How much of the panel the button bar takes when no height is configured.
# It is used in two places - the bar that is drawn (rb4r5/overlay.py) and the
# rows the player is told to keep off (rb4r5/chroot.py) - and if those two
# disagree the player draws under the bar or leaves a gap.
TOP_BAR_SHARE = 0.06
TOP_BAR_MIN = 40


def top_bar_height(panel_height: int, configured: int = 0) -> int:
    """The bar's height in pixels, from the panel's."""
    if configured > 0:
        return configured
    if not panel_height:
        return 0
    return max(TOP_BAR_MIN, round(panel_height * TOP_BAR_SHARE))


FIFO_KEYS = "/tmp/rb-keys.fifo"
FIFO_CTRL = "/tmp/rb-ctrl.fifo"
FIFO_UDEV = ["/tmp/udev_usb1", "/tmp/udev_usb2",
             "/tmp/udev_usbctn1", "/tmp/udev_usbctn2"]
TOUCH_STATE = "/tmp/rb-touch.dat"
LOG_RBP = "/tmp/rbp.log"
LOG_SHIMS = ["/tmp/audioshim.log", "/tmp/keyshim.log", "/tmp/flipdbg.log"]
