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
        "quiet_console": 2,              # 0 keep console, 1 quiet printk, 2 also detach fbcon
        "screenshot_on_start": True,     # save what the UI looks like as it starts
        "screenshot_delays": [10, 30, 90],
        "blank_timeout": 0,              # console blanking, 0 = never
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
        "cue_mirror": False,             # mirror master into the phones until PFL
        "cue_on_stereo": False,          # on a stereo sink, follow the cue mix
        "startup_mute_ms": 1500,         # kill the engine's power-on transient
        "startup_fade_ms": 300,
        "fallback_hdmi": True,           # use HDMI audio when no controller
    },
    # ---- controller --------------------------------------------------------
    "controller": {
        "enabled": True,
        "name": "FLX4",                  # ALSA card name fragment to look for
        "jog_ppr": 1800,                 # jog pulses per revolution
        "filter_init": True,             # select FILTER as the colour FX type
        "map_file": "/etc/rb4r5/flx4-map.conf",
        "verbose": False,
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
        # Native RX3 touch injection through memshim.  OFF by default: the
        # 6-byte tsc2007 record layout is unverified (docs/07-touch.md).
        "native": False,
        "native_format": "fxy",
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
        path = path or self.path
        util.ensure_dir(path.parent)
        util.write_text(path, json.dumps(self.data, indent=2) + "\n")


def load(path: Path | None = None) -> Config:
    path = Path(path) if path else CONFIG_PATH
    data = DEFAULTS
    if path.exists():
        try:
            data = _merge(DEFAULTS, json.loads(path.read_text()))
        except (OSError, ValueError) as exc:
            util.warn(f"{path} is not readable JSON ({exc}); using defaults")
    return Config(data, path)


# Paths that live in /tmp because the chroot bind-mounts the host /tmp, which
# is how the shims inside rbp and the daemons outside it talk to each other.
FIFO_KEYS = "/tmp/rb-keys.fifo"
FIFO_CTRL = "/tmp/rb-ctrl.fifo"
FIFO_UDEV = ["/tmp/udev_usb1", "/tmp/udev_usb2",
             "/tmp/udev_usbctn1", "/tmp/udev_usbctn2"]
TOUCH_STATE = "/tmp/rb-touch.dat"
LOG_RBP = "/tmp/rbp.log"
LOG_SHIMS = ["/tmp/audioshim.log", "/tmp/keyshim.log", "/tmp/flipdbg.log"]
