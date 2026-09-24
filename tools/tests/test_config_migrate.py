#!/usr/bin/env python3
"""A config saved with an old default gets the new default.

The installer used to save EVERY default into /etc/rb4r5/config.json, which
froze that day's defaults: a later fix to one - the jog's ticks per turn,
native touch - never reached the Pi, because the saved old value won.

Run:  python3 tools/tests/test_config_migrate.py
"""
import copy
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from rb4r5 import config  # noqa: E402

failures = []


def check(label, got, want=True):
    if got != want:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label}")


with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "config.json"
    old = copy.deepcopy(config.DEFAULTS)
    old["controller"]["jog_ticks_per_rev"] = 1800
    old["touch"]["native"] = False
    old["touch"]["native_format"] = "fxy"
    old["touch"]["tap_ms"] = 300                 # somebody's own choice
    old["controller"]["jog_scale"] = 1.5         # and another
    path.write_text(json.dumps(old))

    cfg = config.load(path)
    check("an old saved jog_ticks_per_rev (1800) gets the FLX4's 600",
          cfg.get("controller.jog_ticks_per_rev"), 600)
    check("an old saved native=false gets native touch",
          cfg.get("touch.native"), True)
    check("and the verified record", cfg.get("touch.native_format"), "rx3")
    check("the launcher is told what was migrated", len(cfg.migrated), 3)
    check("a setting that is somebody's choice is kept",
          (cfg.get("touch.tap_ms"), cfg.get("controller.jog_scale")),
          (300, 1.5))

    cfg.save()
    saved = json.loads(path.read_text())
    check("saving writes only what differs from the defaults",
          saved, {"touch": {"tap_ms": 300}, "controller": {"jog_scale": 1.5}})

    path.write_text(json.dumps({"touch": {"native": True,
                                          "native_format": "bxy"}}))
    cfg = config.load(path)
    check("a value that was never a default is left alone",
          cfg.get("touch.native_format"), "bxy")

print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("old defaults cannot freeze a config any more")
