#!/usr/bin/env python3
"""If the player dies of a memory fault with keyshim's engine reader in it,
the next start goes without the meter hook, and the one after that without
the reader - and the controller keeps working either way.

Run:  python3 tools/tests/test_engine_fallback.py
"""
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from rb4r5 import chroot, supervisor, util  # noqa: E402

failures = []
util.warn = lambda *a, **k: None


def check(label, got, want=True):
    if got != want:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label}")


class Child:
    name, argv, env = "rbp", ["old"], {}


chroot.chroot_cmd = lambda root, cmd, env: (["chroot", root] + cmd +
                                            [f"{k}={v}" for k, v in
                                             sorted(env.items())], {})

with tempfile.TemporaryDirectory() as tmp:
    log = Path(tmp) / "keyshim.log"
    sup = supervisor.Supervisor.__new__(supervisor.Supervisor)
    sup.KEYSHIM_LOG = log
    sup.player_env = {"LD_PRELOAD": "x"}
    sup.player_cmd = ("/opt/rb4r5/chroot", ["/root/pdj/rbp"])
    child = Child()

    log.write_text("keyshim: loaded into rbp, pid 1\n"
                   "keyshim: meter hook installed (RB_METER_HOOK=0 ...)\n"
                   "keyshim: engine state publisher up\n")
    sup.engine_fallback(child, -6)
    check("an abort is not blamed on the reader", "RB_METER_HOOK=0" in child.argv,
          False)

    sup.engine_fallback(child, -11)
    check("a crash with the hook in: restart without the hook",
          "RB_METER_HOOK=0" in child.argv)
    check("but the reader stays", "RB_ENGINE_STATE=0" in child.argv, False)

    log.write_text(log.read_text() + "keyshim: loaded into rbp, pid 2\n"
                   "keyshim: engine state publisher up\n")
    sup.engine_fallback(child, -11)
    check("crashing again without the hook: restart without the reader",
          "RB_ENGINE_STATE=0" in child.argv)

    before = list(child.argv)
    log.write_text(log.read_text() + "keyshim: loaded into rbp, pid 3\n")
    sup.engine_fallback(child, -11)
    check("with both off, a crash is somebody else's: nothing more changes",
          child.argv, before)

    sup.player_env = {"LD_PRELOAD": "x"}
    child.argv = ["old"]
    log.write_text("keyshim: meter hook installed\n"
                   "keyshim: loaded into rbp, pid 9\n")
    sup.engine_fallback(child, -11)
    check("only the log of the player that just died counts",
          child.argv, ["old"])

print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("a crash in the engine reader costs lamps, not the player")
