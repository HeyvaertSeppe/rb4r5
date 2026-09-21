"""Touch zones: what a tap or a drag on the 22" panel does.

The XDJ-RX3 engine has no way to be told "the user touched pixel (x, y)" that
we can trust (see docs/07-touch.md), so the supported touch path maps regions
of the screen onto the controls the engine *does* accept: keys, the browse
knob, and the jog wheels.

A zone rect is [x0, y0, x1, y1] in fractions of the screen, so the same map
works on any panel size.  Types:

    key     press on touch-down, release on touch-up      (key, ch)
    scroll  vertical drag turns the browse knob;
            a tap sends `tap_key`                         (key, tap_key, ch)
    list    a browse list: drag scrolls it, and a tap on a
            row moves the highlight onto that row and
            opens it.  A long press says "the highlight is
            already here" and re-syncs without sending
            anything                                      (key, select_key, rows)
    jog     horizontal drag scrubs a deck                 (ch)
    value   drag sets a 10-bit value (faders, EQ, filter)  (key, ch, op, axis)
    none    ignore touches here

Edit /etc/rb4r5/touch-zones.json to change the layout; `rb4r5 calibrate`
prints which zone each touch lands in.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import util

DEFAULT = {
    "_comment": [
        "rb4r5 touch zones.  rect = [x0, y0, x1, y1] as fractions of the",
        "screen (0,0 = top left).  The first zone that contains the touch",
        "wins, so put small zones before large ones.",
        "Run 'sudo python3 launch.py calibrate' to see which zone you hit.",
    ],
    "zones": [
        # --- top strip: the buttons the RX3 front panel would have ---------
        {"name": "source", "rect": [0.00, 0.00, 0.12, 0.09],
         "type": "key", "key": "source"},
        {"name": "browse", "rect": [0.12, 0.00, 0.24, 0.09],
         "type": "key", "key": "browse"},
        {"name": "menu", "rect": [0.24, 0.00, 0.36, 0.09],
         "type": "key", "key": "menu"},
        {"name": "back", "rect": [0.36, 0.00, 0.48, 0.09],
         "type": "key", "key": "back"},
        {"name": "info", "rect": [0.88, 0.00, 1.00, 0.09],
         "type": "key", "key": "info"},
        {"name": "status-bar", "rect": [0.48, 0.00, 0.88, 0.09],
         "type": "none"},

        # --- the big middle area: browse list / menus ----------------------
        # A vertical drag turns the browse knob (the only way the engine can
        # move a selection); a tap is SELECT.
        # Tapping a row has to MOVE the highlight onto it first: the engine
        # has no "open the thing at this pixel", only "turn the knob" and
        # "press it".  Tapping and pressing select alone opens whatever was
        # already highlighted, which is why tapping a playlist appeared to do
        # nothing (docs/07-touch.md).
        {"name": "list", "rect": [0.00, 0.09, 1.00, 0.70],
         "type": "list", "key": "selector", "select_key": "select",
         "rows": 9, "invert": False},

        # --- waveform row: scrub each deck ---------------------------------
        {"name": "deck1-scrub", "rect": [0.00, 0.70, 0.50, 0.82],
         "type": "jog", "ch": 1},
        {"name": "deck2-scrub", "rect": [0.50, 0.70, 1.00, 0.82],
         "type": "jog", "ch": 2},

        # --- transport row -------------------------------------------------
        {"name": "load1", "rect": [0.000, 0.82, 0.125, 1.00],
         "type": "key", "key": "load", "ch": 1},
        {"name": "play1", "rect": [0.125, 0.82, 0.250, 1.00],
         "type": "key", "key": "play", "ch": 1},
        {"name": "cue1", "rect": [0.250, 0.82, 0.375, 1.00],
         "type": "key", "key": "cue", "ch": 1},
        {"name": "sync1", "rect": [0.375, 0.82, 0.500, 1.00],
         "type": "key", "key": "sync", "ch": 1},
        {"name": "sync2", "rect": [0.500, 0.82, 0.625, 1.00],
         "type": "key", "key": "sync", "ch": 2},
        {"name": "cue2", "rect": [0.625, 0.82, 0.750, 1.00],
         "type": "key", "key": "cue", "ch": 2},
        {"name": "play2", "rect": [0.750, 0.82, 0.875, 1.00],
         "type": "key", "key": "play", "ch": 2},
        {"name": "load2", "rect": [0.875, 0.82, 1.000, 1.00],
         "type": "key", "key": "load", "ch": 2},
    ],
}

VALID_TYPES = {"key", "scroll", "jog", "value", "none"}


def load(path: str | Path | None) -> dict:
    if not path:
        return DEFAULT
    path = Path(path)
    if not path.exists():
        return DEFAULT
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        util.warn(f"{path}: {exc}; using the built-in zone map")
        return DEFAULT
    problems = validate(data)
    if problems:
        for problem in problems:
            util.warn(f"{path}: {problem}")
        util.warn("using the built-in zone map instead")
        return DEFAULT
    return data


def save_default(path: str | Path) -> bool:
    return util.write_text(path, json.dumps(DEFAULT, indent=2) + "\n")


VALID_TYPES = ("key", "scroll", "list", "jog", "value", "none")


def validate(data: dict) -> list[str]:
    problems = []
    zones = data.get("zones")
    if not isinstance(zones, list) or not zones:
        return ["no 'zones' list"]
    for index, zone in enumerate(zones):
        label = zone.get("name", f"#{index}")
        rect = zone.get("rect")
        if (not isinstance(rect, list) or len(rect) != 4 or
                not all(isinstance(v, (int, float)) for v in rect)):
            problems.append(f"zone {label}: rect must be [x0, y0, x1, y1]")
            continue
        x0, y0, x1, y1 = rect
        if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
            problems.append(f"zone {label}: rect {rect} is not inside 0..1 "
                            "with x0<x1 and y0<y1")
        kind = zone.get("type", "key")
        if kind not in VALID_TYPES:
            problems.append(f"zone {label}: unknown type '{kind}' "
                            f"(one of {sorted(VALID_TYPES)})")
        if kind in ("key", "scroll", "list", "value") and not zone.get("key"):
            problems.append(f"zone {label}: type '{kind}' needs a 'key'")
    return problems


def hit(data: dict, nx: float, ny: float) -> dict | None:
    """The first zone containing the normalised point, or None."""
    for zone in data.get("zones", []):
        x0, y0, x1, y1 = zone["rect"]
        if x0 <= nx <= x1 and y0 <= ny <= y1:
            return zone
    return None


def describe(data: dict) -> list[str]:
    lines = []
    for zone in data.get("zones", []):
        x0, y0, x1, y1 = zone["rect"]
        kind = zone.get("type", "key")
        extra = ""
        if kind == "key":
            extra = f"key={zone['key']} ch={zone.get('ch', 1)}"
        elif kind == "scroll":
            extra = f"drag={zone['key']} tap={zone.get('tap_key', '-')}"
        elif kind == "jog":
            extra = f"deck {zone.get('ch', 1)}"
        elif kind == "value":
            extra = (f"key={zone['key']} ch={zone.get('ch', 1)} "
                     f"axis={zone.get('axis', 'y')}")
        lines.append(f"  {zone.get('name', '?'):<14} "
                     f"[{x0:.3f} {y0:.3f} {x1:.3f} {y1:.3f}] {kind:<7} {extra}")
    return lines
