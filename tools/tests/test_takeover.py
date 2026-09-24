#!/usr/bin/env python3
"""A launcher ends OTHER launchers - never the sudo it was started with.

`sudo python3 launch.py run` puts "launch.py run" in sudo's own command
line, and sudo can sit two levels up.  The first take-over took it for
another launcher, killed it, and with it the run and the SSH session.

Run:  python3 tools/tests/test_takeover.py
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
failures = []


def check(label, got, want=True):
    if got != want:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label}")


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    # a stand-in "other launcher": python running a launch.py with `run`
    fake = tmp / "launch.py"
    fake.write_text("import time\ntime.sleep(30)\n")
    other = subprocess.Popen([sys.executable, str(fake), "run"])

    # the checker runs the way sudo runs us: under a NON-python process whose
    # command line names launch.py run, with another process in between
    probe = tmp / "probe.py"
    probe.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from rb4r5.supervisor import Supervisor\n"
        "print(' '.join(str(p) for p in Supervisor.other_supervisors()))\n"
        "print(' '.join(str(p) for p in Supervisor.ancestors()))\n")
    time.sleep(0.3)
    wrapper = subprocess.run(
        ["sh", "-c", f'sh -c "{sys.executable} {probe}"; : launch.py run',
         "launch.py", "run"], capture_output=True, text=True, timeout=30)
    found_line, mine_line = (wrapper.stdout.splitlines() + ["", ""])[:2]
    found = {int(p) for p in found_line.split()}
    mine = {int(p) for p in mine_line.split()}
    check("another launcher is found", other.pid in found)
    check("the wrappers above this one (like sudo) are not",
          found & mine, set())
    check("and nothing but the other launcher is",
          found - {other.pid}, set())
    other.kill()
    other.wait()

print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("a launcher never ends the sudo it runs under")
