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

if command -v arm-linux-gnueabi-gcc >/dev/null 2>&1; then
    # keyshim reads the player's memory at fixed ARM addresses and patches
    # one of its functions, so it is worth compiling as the ARM it will be
    arm-linux-gnueabi-gcc -O2 -march=armv5t -mfloat-abi=soft \
        -fno-stack-protector -fPIC -Wall -Wextra -Wno-unused-parameter \
        -Werror -shared -o "$TMP/keyshim.so" src/shims/keyshim.c -lpthread \
        && echo "  keyshim.c ok as soft-float ARM"
fi

echo "== C: keyshim's probes survive a wrong address"
cc -O2 -Wall -Wextra -Wno-unused-parameter -Wno-unused-function \
    -DRB_STATE_PATH="\"$TMP/state.dat\"" -DKLOG_PATH="\"$TMP/klog.txt\"" \
    -o "$TMP/probeguard" tools/tests/test_probe_guard.c -lpthread
"$TMP/probeguard" || fail=1
if command -v arm-linux-gnueabi-gcc >/dev/null 2>&1 && \
   command -v qemu-arm-static >/dev/null 2>&1; then
    # and as the soft-float ARM that runs inside the player, where the
    # compiler once moved a read above the marker that names its probe
    arm-linux-gnueabi-gcc -O2 -march=armv5t -mfloat-abi=soft -static \
        -Wall -Wno-unused-parameter -Wno-unused-function \
        -DRB_STATE_PATH="\"$TMP/state-arm.dat\"" \
        -DKLOG_PATH="\"$TMP/klog.txt\"" \
        -o "$TMP/probeguard-arm" tools/tests/test_probe_guard.c -lpthread \
        2>/dev/null
    qemu-arm-static "$TMP/probeguard-arm" > "$TMP/probeguard-arm.txt" || fail=1
    tail -1 "$TMP/probeguard-arm.txt"
fi

echo "== C: framebuffer publish path (src/directfb/rb4r5_scale.h)"
cc -O2 -Wall -Wextra -o "$TMP/fbscale" tools/tests/test_fbscale.c
"$TMP/fbscale" | tail -1
if command -v arm-linux-gnueabi-gcc >/dev/null 2>&1 && \
   command -v qemu-arm-static >/dev/null 2>&1; then
    # the same tests against the real NEON expander, which is what runs on the
    # Pi: every RGB565 value through both channel orders, bit for bit
    arm-linux-gnueabi-gcc -O2 -Wall -Wextra -march=armv7-a -mfpu=neon \
        -mfloat-abi=softfp -static -o "$TMP/fbscale-neon" tools/tests/test_fbscale.c
    qemu-arm-static "$TMP/fbscale-neon" | tail -1
else
    echo "  NEON path not checked (needs arm-linux-gnueabi-gcc and qemu-arm-static)"
fi

echo "== C: the audio rate governor (src/shims/rate_gate.h)"
cc -O2 -Wall -Wextra -o "$TMP/rategate" tools/tests/test_rate_gate.c
"$TMP/rategate" | tail -1

echo "== patch integrity"
python3 - <<'PY'
import re, sys
bad = 0
for path in ("src/directfb/directfb-pi5.patch",):
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
