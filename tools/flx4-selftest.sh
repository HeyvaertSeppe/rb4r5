#!/bin/sh
# flx4-selftest.sh - drive flx4-bridge with synthetic DDJ-FLX4 MIDI and show
# what came out, without a controller (and without the player, if you like).
#
# It feeds the bridge the exact byte sequences a DDJ-FLX4 sends for every class
# of control, then decodes the 24-byte records the bridge wrote.  This is how
# the map is verified offline; with the player running, point it at the real
# control FIFO instead and watch /tmp/keyshim.log.
#
#   tools/flx4-selftest.sh                 # isolated: its own FIFOs, decoded here
#   tools/flx4-selftest.sh --live          # write into /tmp/rb-ctrl.fifo (the player)
#   BRIDGE=/usr/local/bin/flx4-bridge tools/flx4-selftest.sh
set -eu

REPO=$(cd "$(dirname "$0")/.." && pwd)
BRIDGE=${BRIDGE:-}
LIVE=0
[ "${1:-}" = "--live" ] && LIVE=1

if [ -z "$BRIDGE" ]; then
    for candidate in /usr/local/bin/flx4-bridge /opt/rb4r5/work/host/flx4-bridge \
                     "$REPO/work/host/flx4-bridge"; do
        [ -x "$candidate" ] && { BRIDGE=$candidate; break; }
    done
fi
if [ -z "$BRIDGE" ]; then
    echo "building flx4-bridge into a temporary directory"
    TMPB=$(mktemp -d)
    make -s -C "$REPO/src/host" OUT="$TMPB" >/dev/null
    BRIDGE=$TMPB/flx4-bridge
fi
echo "bridge: $BRIDGE"

WORK=$(mktemp -d)
MIDI=$WORK/midi.fifo
if [ "$LIVE" = 1 ]; then
    CTRL=/tmp/rb-ctrl.fifo
    [ -p "$CTRL" ] || { echo "$CTRL does not exist - is the player running?" >&2; exit 1; }
    echo "writing into the live control FIFO: watch /tmp/keyshim.log"
else
    CTRL=$WORK/ctrl.fifo
    mkfifo "$CTRL"
fi
mkfifo "$MIDI"

cleanup() {
    [ -n "${BPID:-}" ] && kill "$BPID" 2>/dev/null || true
    [ -n "${RPID:-}" ] && kill "$RPID" 2>/dev/null || true
    rm -rf "$WORK" "${TMPB:-}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Decode the records, unless we are feeding the real player.
if [ "$LIVE" = 0 ]; then
    python3 - "$CTRL" <<'PY' &
import os, struct, sys, time
NAMES = {0x4101: "PLAY", 0x4102: "CUE", 0x4112: "SYNC", 0x4111: "MASTER",
         0x4107: "TEMPO RANGE", 0x4109: "TEMPO", 0x410c: "LOOP IN",
         0x410d: "LOOP OUT", 0x410e: "RELOOP", 0x410f: "REVERSE",
         0x4305: "JOG", 0x4306: "JOG TOUCH", 0x420c: "SELECTOR",
         0x420d: "BACK", 0x4311: "LOAD", 0x0201: "SOURCE", 0x0206: "MENU",
         0x4113: "bank HOT CUE", 0x4114: "bank AUTO LOOP",
         0x4115: "bank SLIP LOOP", 0x4116: "bank BEAT JUMP",
         0x501e: "FADER", 0x5019: "TRIM", 0x501a: "EQ HI", 0x501b: "EQ MID",
         0x501c: "EQ LOW", 0x6017: "CROSSFADER", 0x509d: "COLOUR FX",
         0x4405: "PHONES MIX", 0x4406: "PHONES LEVEL", 0x448b: "BFX TYPE",
         0x448c: "BFX CHANNEL", 0x448d: "BFX ON/OFF", 0x448f: "BFX DEPTH",
         0x4490: "BEAT <", 0x4491: "BEAT >"}
NAMES.update({0x4117 + i: f"PAD {i + 1}" for i in range(8)})
OPS = {0: "press", 2: "release", 4: "rotate", 5: "value"}
fd = os.open(sys.argv[1], os.O_RDWR | os.O_NONBLOCK)
time.sleep(4.0)
data = b""
while True:
    try:
        chunk = os.read(fd, 24 * 512)
    except BlockingIOError:
        break
    if not chunk:
        break
    data += chunk
print(f"\n{len(data) // 24} records reached the control FIFO:\n")
for off in range(0, len(data) // 24 * 24, 24):
    key, ch, op, param, val, pos = struct.unpack("<iiiifi", data[off:off + 24])
    print(f"  0x{key:04x} {NAMES.get(key, '?'):<16} {OPS.get(op, op):<8} "
          f"deck {ch}  param={param:<5} f={val:+.3f} l={pos}")
PY
    RPID=$!
    sleep 0.3
fi

"$BRIDGE" -v -d "$MIDI" -f "$CTRL" -M "$WORK/master.dat" -P "$WORK/state.dat" \
    > "$WORK/bridge.log" 2>&1 &
BPID=$!
sleep 0.5

# Each line is one MIDI message (or a 14-bit MSB+LSB pair), exactly as a
# DDJ-FLX4 sends it - see docs/06-controller.md for the map.
python3 - "$MIDI" <<'PY'
import os, sys, time
fd = os.open(sys.argv[1], os.O_WRONLY)
def send(label, *bytes_):
    print(f"  -> {label}")
    os.write(fd, bytes(bytes_))
    time.sleep(0.05)

send("deck 1 PLAY press",            0x90, 0x0B, 0x7F)
send("deck 1 PLAY release",          0x90, 0x0B, 0x00)
send("deck 2 CUE",                   0x91, 0x0C, 0x7F)
send("deck 1 SHIFT+PLAY (censor)",   0x90, 0x0E, 0x7F)
send("deck 2 BEAT SYNC",             0x91, 0x58, 0x7F)
send("deck 1 LOOP IN",               0x90, 0x10, 0x7F)
send("browse knob +2",               0xB6, 0x40, 0x02)
send("browse knob -1",               0xB6, 0x40, 0x7F)
send("browse push (select)",         0x96, 0x41, 0x7F)
send("SHIFT+browse push (back)",     0x96, 0x42, 0x7F)
send("LOAD deck 1",                  0x96, 0x46, 0x7F)
send("deck 1 fader centre",          0xB0, 0x13, 0x40, 0xB0, 0x33, 0x00)
send("deck 1 EQ HI max",             0xB0, 0x07, 0x7F, 0xB0, 0x27, 0x7F)
send("deck 2 trim min",              0xB1, 0x04, 0x00, 0xB1, 0x24, 0x00)
send("deck 2 tempo centre",          0xB1, 0x00, 0x40, 0xB1, 0x20, 0x00)
send("crossfader hard left",         0xB6, 0x1F, 0x00, 0xB6, 0x3F, 0x00)
send("filter ch1 3/4 up",            0xB6, 0x17, 0x60, 0xB6, 0x37, 0x00)
send("phones mixing centre",         0xB6, 0x0C, 0x40, 0xB6, 0x2C, 0x00)
send("deck 1 jog touch",             0x90, 0x36, 0x7F)
send("deck 1 jog forward",           0xB0, 0x22, 0x05)
send("deck 1 jog back",              0xB0, 0x22, 0x7B)
send("deck 1 jog release",           0x90, 0x36, 0x00)
send("deck 1 pad 3, HOT CUE mode",   0x97, 0x02, 0x7F)
send("deck 1 pad 3 release",         0x97, 0x02, 0x00)
send("deck 2 pad 5, BEAT LOOP mode", 0x99, 0x64, 0x7F)
send("deck 1 pad 1, BEAT JUMP mode", 0x97, 0x20, 0x7F)
send("deck 2 pad 2, SAMPLER mode",   0x99, 0x31, 0x7F)
send("deck 1 pad 8, KEY SHIFT (no engine equivalent)", 0x98, 0x77, 0x7F)
send("BEAT FX select",               0x94, 0x63, 0x7F)
send("BEAT FX channel -> CH1",       0x94, 0x10, 0x7F)
send("BEAT FX on/off",               0x94, 0x47, 0x7F)
send("BEAT FX depth half",           0xB4, 0x02, 0x40)
send("BEAT >",                       0x94, 0x4B, 0x7F)
time.sleep(0.6)
os.close(fd)
PY

sleep 1.5
kill "$BPID" 2>/dev/null || true

if [ "$LIVE" = 0 ]; then
    wait "$RPID" 2>/dev/null || true
    echo
    echo "ignored-by-design events (no XDJ-RX3 equivalent):"
    grep -iE "ignored|unmapped" "$WORK/bridge.log" | sed 's/^/  /' || echo "  (none)"
else
    echo
    echo "last lines of /tmp/keyshim.log (what the engine received):"
    tail -12 /tmp/keyshim.log 2>/dev/null || echo "  (no keyshim log - is the player running?)"
fi
echo
echo "bridge log: $WORK/bridge.log (removed on exit; copy it if you need it)"
