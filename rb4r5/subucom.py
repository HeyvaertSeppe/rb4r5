"""The panel link: draining it, capturing it, and working out what it says.

The XDJ-RX3's application talks to the front panel over an SPI link it calls
subucom.  Everything the panel shows comes down it - every button LED, the
pad colours, the level meters - and everything the panel does goes back up
it.  On a Pi there is no panel, so the chroot stubs the four device nodes
with FIFOs.

Two reasons this module exists.

**Nothing was reading them.**  A FIFO with no reader accepts one pipe buffer
(64 KiB on Linux) and then blocks the writer.  The player writes panel frames
continuously, so sooner or later its panel thread stops in write() and stays
there - which from the outside looks like the UI freezing for no reason.
Draining them is not optional.

**The LED state is in there.**  It is the only place the player says what it
thinks the lights should be doing, and decoding it is what would make the
launcher's button bar and the FLX4's own LEDs show the truth rather than a
guess.  Nobody has decoded it for this player, so this module does the part
that can be done without guessing: it captures the stream, splits it into
frames, and tells you which bytes and bits changed while you pressed
something.  That is the whole trick - press CUE on deck 1, see exactly one
bit move, write it down.

    sudo python3 launch.py subucom --watch          # live frames, changes marked
    sudo python3 launch.py subucom --learn "cue 1"  # diff across one action
    sudo python3 launch.py subucom --replay <file>  # the same over a capture
"""
from __future__ import annotations

import os
import select
import struct
import time
from pathlib import Path

from . import config, util

# the four nodes chroot.py stubs, in the chroot's /dev
NODES = ["subucom_spi1.0", "subucom_spi2.0", "subucom_spi_rdy3.0",
         "subucom_spi_rdy4.0"]

CAPTURE = "/var/log/rb4r5/subucom.bin"
STATE = "/tmp/rb-panel.dat"
MAX_CAPTURE = 8 << 20            # 8 MiB, then it wraps


class Frame:
    """One record off the link, with when it arrived and where from."""

    __slots__ = ("node", "at", "data")

    def __init__(self, node: str, data: bytes):
        self.node = node
        self.at = time.time()
        self.data = data

    def hex(self, limit: int = 32) -> str:
        blob = self.data[:limit]
        text = " ".join(f"{b:02x}" for b in blob)
        return text + (" ..." if len(self.data) > limit else "")


def open_nodes(root: Path) -> dict[int, str]:
    """Open every stub for reading without blocking on a missing writer."""
    handles = {}
    for name in NODES:
        path = root / "dev" / name
        if not path.exists():
            continue
        try:
            # O_RDWR: a fifo opened read-only reports EOF whenever the player
            # closes it, and the player opens and closes these repeatedly
            fd = os.open(str(path), os.O_RDWR | os.O_NONBLOCK)
        except OSError as exc:
            util.warn(f"subucom: cannot open {path} ({exc})")
            continue
        handles[fd] = name
    return handles


def split_frames(node: str, blob: bytes, carry: bytes = b"") -> tuple[list[Frame], bytes]:
    """Split a read into frames.

    The framing is not known, so this does the only honest thing: it treats
    each read as one frame when the reads are small (which is what a panel
    protocol looks like - one message per write), and when a read is large it
    leaves it whole rather than inventing boundaries.  `carry` is kept for a
    future decoder that does know the framing.
    """
    data = carry + blob
    if not data:
        return [], b""
    return [Frame(node, data)], b""


def changed_bits(before: bytes, after: bytes) -> list[tuple[int, int, int]]:
    """(offset, old, new) for every byte that differs."""
    out = []
    for index in range(min(len(before), len(after))):
        if before[index] != after[index]:
            out.append((index, before[index], after[index]))
    if len(after) > len(before):
        for index in range(len(before), len(after)):
            out.append((index, -1, after[index]))
    return out


def describe_bits(old: int, new: int) -> str:
    if old < 0:
        return f"new byte {new:08b}"
    flipped = old ^ new
    bits = [f"bit{b}{'+' if new & (1 << b) else '-'}"
            for b in range(8) if flipped & (1 << b)]
    return f"{old:02x}->{new:02x} ({' '.join(bits)})"


class Link:
    """Reads every stub, keeps the last frame per node, optionally captures."""

    def __init__(self, cfg, capture: str | None = None):
        self.cfg = cfg
        self.root = Path(cfg.chroot)
        self.handles = {}
        self.last: dict[str, bytes] = {}
        self.counts: dict[str, int] = {}
        self.bytes: dict[str, int] = {}
        self.capture_path = capture
        self.capture = None
        self.captured = 0

    def open(self) -> bool:
        self.handles = open_nodes(self.root)
        if not self.handles:
            return False
        if self.capture_path:
            util.ensure_dir(Path(self.capture_path).parent)
            self.capture = open(self.capture_path, "wb")
        return True

    def close(self) -> None:
        for fd in list(self.handles):
            try:
                os.close(fd)
            except OSError:
                pass
        self.handles = {}
        if self.capture:
            self.capture.close()
            self.capture = None

    def poll(self, timeout: float = 0.2) -> list[Frame]:
        if not self.handles:
            return []
        ready, _, _ = select.select(list(self.handles), [], [], timeout)
        frames = []
        for fd in ready:
            node = self.handles[fd]
            try:
                blob = os.read(fd, 65536)
            except (BlockingIOError, OSError):
                continue
            if not blob:
                continue
            self.counts[node] = self.counts.get(node, 0) + 1
            self.bytes[node] = self.bytes.get(node, 0) + len(blob)
            got, _ = split_frames(node, blob)
            for frame in got:
                self.last[node] = frame.data
                if self.capture and self.captured < MAX_CAPTURE:
                    # node, timestamp, length, payload - enough to replay
                    header = struct.pack("<BdI", NODES.index(node),
                                         frame.at, len(frame.data))
                    self.capture.write(header + frame.data)
                    self.captured += len(header) + len(frame.data)
            frames += got
        return frames

    def publish(self) -> None:
        """Last frame per node, for anything that learns to read it."""
        try:
            blob = b""
            for name in NODES:
                data = self.last.get(name, b"")
                blob += struct.pack("<BI", NODES.index(name), len(data)) + data
            tmp = STATE + ".tmp"
            Path(tmp).write_bytes(blob)
            os.replace(tmp, STATE)
            os.chmod(STATE, 0o666)
        except OSError:
            pass


def read_capture(path: str):
    """Yield (node, at, data) from a capture file."""
    blob = Path(path).read_bytes()
    at = 0
    while at + 13 <= len(blob):
        node, when, length = struct.unpack_from("<BdI", blob, at)
        at += 13
        if length > len(blob) - at:
            break
        yield NODES[node] if node < len(NODES) else str(node), when, blob[at:at + length]
        at += length


# --------------------------------------------------------------------------
# the daemon: drain, so the player never blocks writing to the panel
# --------------------------------------------------------------------------
def run(cfg, capture: bool = False) -> int:
    link = Link(cfg, CAPTURE if capture else None)
    util.info("panel link: draining the subucom stubs "
              f"({'capturing to ' + CAPTURE if capture else 'no capture'})")
    backoff = 1.0
    while True:
        if not link.handles and not link.open():
            util.warn(f"panel link: no stubs under {link.root}/dev "
                      f"(retrying in {backoff:.0f}s)")
            time.sleep(backoff)
            backoff = min(backoff * 2, 15.0)
            continue
        backoff = 1.0
        last_publish = 0.0
        try:
            while True:
                frames = link.poll(0.2)
                now = time.monotonic()
                if frames and now - last_publish > 0.1:
                    link.publish()
                    last_publish = now
        except OSError as exc:
            util.warn(f"panel link: {exc}; reopening")
            link.close()
            time.sleep(1.0)


# --------------------------------------------------------------------------
# the decoding tools
# --------------------------------------------------------------------------
def watch(cfg, seconds: float = 30.0, width: int = 32) -> int:
    """Print frames as they arrive, marking the bytes that changed."""
    link = Link(cfg)
    if not link.open():
        raise util.Fail(f"no subucom stubs under {link.root}/dev - is the "
                        "chroot assembled? (launch.py payload)")
    print(f"watching {len(link.handles)} node(s) for {seconds:.0f}s; "
          "changed bytes are marked with ^\n")
    previous: dict[str, bytes] = {}
    deadline = time.monotonic() + seconds
    seen = 0
    try:
        while time.monotonic() < deadline:
            for frame in link.poll(0.2):
                seen += 1
                before = previous.get(frame.node, b"")
                diff = changed_bits(before, frame.data)
                print(f"{frame.node} {len(frame.data):4d}B  {frame.hex(width)}")
                if before and diff:
                    marks = [" "] * min(len(frame.data), width)
                    for offset, _old, _new in diff:
                        if offset < len(marks):
                            marks[offset] = "^"
                    print(f"{' ' * (len(frame.node) + 7)}{'  '.join(marks)}")
                    for offset, old, new in diff[:6]:
                        print(f"      byte {offset:3d}: {describe_bits(old, new)}")
                previous[frame.node] = frame.data
    except KeyboardInterrupt:
        pass
    finally:
        link.close()
    print(f"\n{seen} frame(s):")
    for node in NODES:
        if node in link.counts:
            print(f"  {node:22} {link.counts[node]:6d} reads, "
                  f"{link.bytes[node]:8d} bytes")
    if not seen:
        print("  nothing at all - the player writes to these only when it is "
              "running; start it first (launch.py run)")
    return 0


def learn(cfg, what: str, settle: float = 2.0, act: float = 4.0) -> int:
    """Diff the panel state across one action, to find its bit.

    Hold still, let it settle, then do the thing once.  Whatever moved is the
    thing - press CUE on deck 1 and one bit should change.
    """
    link = Link(cfg)
    if not link.open():
        raise util.Fail("no subucom stubs - assemble the chroot first")
    print(f"\nlearning: {what}")
    print(f"  1. hold still for {settle:.0f}s ...")
    deadline = time.monotonic() + settle
    baseline: dict[str, bytes] = {}
    while time.monotonic() < deadline:
        for frame in link.poll(0.2):
            baseline[frame.node] = frame.data
    if not baseline:
        link.close()
        raise util.Fail("no frames arrived - is the player running?")

    print(f"  2. NOW: {what}  (you have {act:.0f}s)")
    deadline = time.monotonic() + act
    moved: dict[str, list] = {}
    while time.monotonic() < deadline:
        for frame in link.poll(0.2):
            diff = changed_bits(baseline.get(frame.node, b""), frame.data)
            if diff:
                moved.setdefault(frame.node, []).extend(diff)
                baseline[frame.node] = frame.data
    link.close()

    print()
    if not moved:
        print("  nothing changed.  Either that control does not light "
              "anything,\n  or the frame carrying it did not repeat - try "
              "again and hold it.")
        return 1
    for node, diffs in moved.items():
        counts: dict[int, int] = {}
        for offset, _old, _new in diffs:
            counts[offset] = counts.get(offset, 0) + 1
        print(f"  {node}:")
        for offset in sorted(counts):
            examples = [d for d in diffs if d[0] == offset][:2]
            shown = "; ".join(describe_bits(o, n) for _i, o, n in examples)
            print(f"    byte {offset:3d} changed {counts[offset]:3d}x   {shown}")
        print(f"\n  A control usually sits in ONE byte that changes a couple "
              f"of times\n  (press and release).  Bytes changing constantly "
              f"are the meters or a clock.")
    return 0


def replay(path: str, width: int = 32) -> int:
    """The same diff view, over a capture taken earlier."""
    previous: dict[str, bytes] = {}
    count = 0
    start = None
    for node, when, data in read_capture(path):
        count += 1
        start = start if start is not None else when
        diff = changed_bits(previous.get(node, b""), data)
        print(f"{when - start:8.3f}s {node} {len(data):4d}B  "
              f"{' '.join(f'{b:02x}' for b in data[:width])}")
        for offset, old, new in diff[:4]:
            if previous.get(node):
                print(f"          byte {offset:3d}: {describe_bits(old, new)}")
        previous[node] = data
    print(f"\n{count} frame(s) in {path}")
    return 0
