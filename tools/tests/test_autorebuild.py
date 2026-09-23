#!/usr/bin/env python3
"""A run rebuilds what was installed from older sources than the tree's.

The launcher runs from the tree, so a `git pull` changes it at once, but the
bridge and the shims are compiled and stayed whatever was last built - which
made fixes look like they "changed nothing".

Run:  python3 tools/tests/test_autorebuild.py
"""
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from rb4r5 import build, util  # noqa: E402

failures = []


def check(label, got, want=True):
    if got != want:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label}")


class Cfg:
    def __init__(self, root: Path):
        self.bindir = root / "bin"
        self.work = root / "work"
        self.chroot = root / "chroot"

    def get(self, key, default=None):
        return default


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = root / "repo"
    for sub in ("src/host", "src/shims"):
        (repo / sub).mkdir(parents=True)
    (repo / "src/host/flx4-bridge.c").write_text("int main(void){return 0;}\n")
    (repo / "src/shims/keyshim.c").write_text("/* v1 */\n")
    cfg = Cfg(root)
    cfg.bindir.mkdir()
    for name in build.HOST_TOOLS:
        (cfg.bindir / name).write_text("old")
    for name in build.SHIMS:
        dst = cfg.chroot / "usr/lib" / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("old")

    calls = []
    build.build_host_tools = lambda c, r: calls.append("host") or ["host built"]
    build.build_shims = lambda c, r: calls.append("shims") or ["shims built"]
    build.install_shims = lambda c: calls.append("install") or []
    util.have = lambda name: True
    util.step = util.info = util.warn = lambda *a, **k: None

    build.refresh_stale(cfg, repo)
    check("never stamped: both are rebuilt", calls, ["host", "shims", "install"])

    calls.clear()
    build.refresh_stale(cfg, repo)
    check("nothing changed: nothing is rebuilt", calls, [])

    (repo / "src/shims/keyshim.c").write_text("/* v2 */\n")
    calls.clear()
    build.refresh_stale(cfg, repo)
    check("a shim changed: the shims AND the bridge (it shares rb_state.h)",
          calls, ["host", "shims", "install"])

    (repo / "src/host/flx4-bridge.c").write_text("int main(void){return 1;}\n")
    calls.clear()
    build.refresh_stale(cfg, repo)
    check("only the bridge changed: only the bridge", calls, ["host"])

    util.have = lambda name: False
    (repo / "src/shims/keyshim.c").write_text("/* v3 */\n")
    calls.clear()
    build.refresh_stale(cfg, repo)
    check("no cross compiler: the bridge still rebuilds, the shims are left",
          calls, ["host"])

print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("stale builds are rebuilt before a run")
