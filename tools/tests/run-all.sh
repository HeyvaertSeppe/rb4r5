#!/bin/sh
# Run every offline test.  No hardware, no firmware, no root needed.
set -eu
REPO=$(cd "$(dirname "$0")/../.." && pwd)
cd "$REPO"

fail=0
for test in tools/tests/test_*.py; do
    echo "== $test"
    python3 "$test" || fail=1
    echo
done

echo "== compile everything"
python3 -m compileall -q rb4r5 launch.py tools/tests && echo "python ok"

echo "== C: host daemons"
TMP=$(mktemp -d)
make -s -C src/host OUT="$TMP" && echo "host tools built"

echo "== C: shims (syntax only - the real build needs the armel cross compiler)"
for src in src/shims/memshim.c src/shims/audioshim.c src/shims/keyshim.c; do
    cc -fsyntax-only -Wall -Wextra -Wno-unused-parameter \
       -DSYS_mmap2=192 -DSYS_poll=168 "$src" && echo "  $src ok"
done

echo "== patch integrity"
python3 - <<'PY'
import re, sys
bad = 0
for path in ("src/directfb/directfb-pi5.patch",
             "src/directfb/directfb-pi5-neon.patch"):
    lines = open(path).read().split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    i = hunks = 0
    while i < len(lines):
        head = re.match(r"^@@ -(\d+),?(\d*) \+(\d+),?(\d*) @@", lines[i])
        if not head:
            i += 1
            continue
        hunks += 1
        want = (int(head.group(2) or 1), int(head.group(4) or 1))
        i += 1
        old = new = 0
        while i < len(lines) and not lines[i].startswith(("@@", "--- ", "+++ ")):
            line = lines[i]
            if line.startswith("-"):
                old += 1
            elif line.startswith("+"):
                new += 1
            else:
                old += 1
                new += 1
            i += 1
        if (old, new) != want:
            print(f"  {path}: hunk ending at line {i} counts {old},{new}, "
                  f"header says {want}")
            bad += 1
    print(f"  {path}: {hunks} hunks ok" if not bad else "")
sys.exit(1 if bad else 0)
PY

echo "== selftest (synthetic MIDI through the real bridge)"
BRIDGE="$TMP/flx4-bridge" tools/flx4-selftest.sh | tail -5
rm -rf "$TMP"

echo
[ "$fail" = 0 ] && echo "ALL OFFLINE TESTS PASSED" || { echo "SOME TESTS FAILED"; exit 1; }
