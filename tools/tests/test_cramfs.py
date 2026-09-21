#!/usr/bin/env python3
"""Offline checks for the pure-Python cramfs reader.

Builds a small cramfs image byte by byte (the format is frozen, so the writer
here is a fixture, not production code), then extracts it with rb4r5/cramfs.py
and checks every file, mode, symlink and nested directory came back.

Run:  python3 tools/tests/test_cramfs.py
"""
import os
import stat
import struct
import sys
import tempfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from rb4r5 import cramfs  # noqa: E402

BLOCK = cramfs.BLOCK_SIZE
failures = []


def check(label, got, want):
    if got != want:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        shown = got if not isinstance(got, bytes) or len(got) < 40 else f"{len(got)} bytes"
        print(f"ok   {label} = {shown!r}")


# ---------------------------------------------------------------- the writer
def inode(mode, uid, gid, size, offset, name):
    """12-byte inode + its NUL-padded name."""
    raw = name.encode()
    padded = (len(raw) + 3) // 4 * 4
    assert offset % 4 == 0, "data offsets are counted in 4-byte units"
    return struct.pack("<III",
                       (uid << 16) | (mode & 0xFFFF),
                       (gid << 24) | (size & 0xFFFFFF),
                       ((offset // 4) << 6) | (padded // 4)) + \
        raw.ljust(padded, b"\0")


def blocks_for(payload):
    """A cramfs block table + zlib streams for one file's contents."""
    chunks = [payload[i:i + BLOCK] for i in range(0, len(payload), BLOCK)] or []
    compressed = [zlib.compress(c, 9) for c in chunks]
    return compressed


def build_image():
    files = {
        "hello.txt": (0o100644, b"Hello cramfs!\n"),
        "empty":     (0o100600, b""),
        "big.bin":   (0o100644, bytes(range(256)) * 40),      # 10240 = 3 blocks
        "link":      (0o120777, b"hello.txt"),
    }
    nested = {"inner.dat": (0o100640, b"nested payload\n" * 10)}

    # --- lay out the inode/name area -------------------------------------
    root_entries_at = 76
    root_names = ["big.bin", "dir1", "empty", "hello.txt", "link"]   # sorted
    root_size = 0
    for name in root_names:
        root_size += 12 + (len(name) + 3) // 4 * 4
    dir1_entries_at = root_entries_at + root_size
    dir1_size = sum(12 + (len(n) + 3) // 4 * 4 for n in nested)
    data_at = dir1_entries_at + dir1_size

    # --- lay out the data area -------------------------------------------
    data = bytearray()
    placements = {}          # name -> (offset, size)

    def place(name, payload):
        if not payload:
            placements[name] = (0, 0)
            return
        compressed = blocks_for(payload)
        start = data_at + len(data)
        table_size = len(compressed) * 4
        cursor = start + table_size
        table = []
        body = b""
        for chunk in compressed:
            cursor += len(chunk)
            table.append(cursor)
            body += chunk
        data.extend(struct.pack("<" + "I" * len(table), *table))
        data.extend(body)
        while len(data) % 4:                  # keep the next offset aligned
            data.extend(b"\0")
        placements[name] = (start, len(payload))

    for name in ("big.bin",):
        place(name, files[name][1])
    for name in ("inner.dat",):
        place(name, nested[name][1])
    for name in ("empty", "hello.txt", "link"):
        place(name, files[name][1])

    # --- inodes -----------------------------------------------------------
    root_blob = b""
    for name in root_names:
        if name == "dir1":
            root_blob += inode(0o040755, 0, 0, dir1_size, dir1_entries_at, name)
        else:
            mode, _ = files[name]
            offset, size = placements[name]
            root_blob += inode(mode, 0, 0, size, offset, name)
    assert len(root_blob) == root_size

    dir1_blob = b""
    for name, (mode, _payload) in nested.items():
        offset, size = placements[name]
        dir1_blob += inode(mode, 0, 0, size, offset, name)
    assert len(dir1_blob) == dir1_size

    total = data_at + len(data)
    super_block = struct.pack("<III I", cramfs.MAGIC_LE, total, 0, 0)
    super_block += cramfs.SIGNATURE
    super_block += struct.pack("<IIII", 0, 0, (total + BLOCK - 1) // BLOCK,
                               len(root_names) + len(nested) + 1)
    super_block += b"rb4r5-test".ljust(16, b"\0")
    super_block += inode(0o040755, 0, 0, root_size, root_entries_at, "")
    assert len(super_block) == 76, len(super_block)

    return super_block + root_blob + dir1_blob + bytes(data), files, nested


# ---------------------------------------------------------------- the checks
image, files, nested = build_image()

with tempfile.TemporaryDirectory() as tmp:
    img_path = Path(tmp, "rootfs.cramfs")
    img_path.write_bytes(image)

    check("is_cramfs recognises it", cramfs.is_cramfs(img_path), True)
    check("is_cramfs rejects other data", cramfs.is_cramfs(__file__), False)

    fs = cramfs.Cramfs(image)
    check("volume name", fs.name, "rb4r5-test")
    check("root is a directory", fs.root.is_dir, True)
    check("root entries", sorted(i.name for i in fs.listdir(fs.root)),
          ["big.bin", "dir1", "empty", "hello.txt", "link"])
    check("walk finds the nested file",
          sorted(p for p, _ in fs.walk()),
          ["big.bin", "dir1", "dir1/inner.dat", "empty", "hello.txt", "link"])

    out = Path(tmp, "out")
    stats = cramfs.extract(img_path, out)
    check("extracted dirs", stats["dirs"], 1)
    check("extracted files", stats["files"], 4)   # 3 in root + 1 nested
    check("extracted links", stats["links"], 1)

    check("small file contents", (out / "hello.txt").read_bytes(),
          files["hello.txt"][1])
    check("empty file", (out / "empty").read_bytes(), b"")
    check("multi-block file", (out / "big.bin").read_bytes(), files["big.bin"][1])
    check("multi-block file size", (out / "big.bin").stat().st_size, 10240)
    check("nested file", (out / "dir1/inner.dat").read_bytes(),
          nested["inner.dat"][1])
    check("symlink is a symlink", (out / "link").is_symlink(), True)
    check("symlink target", os.readlink(out / "link"), "hello.txt")
    check("file mode preserved",
          stat.S_IMODE((out / "empty").stat().st_mode), 0o600)
    check("nested mode preserved",
          stat.S_IMODE((out / "dir1/inner.dat").stat().st_mode), 0o640)
    check("dir mode preserved",
          stat.S_IMODE((out / "dir1").stat().st_mode), 0o755)

    # a corrupt image must be reported, not silently half-extracted
    broken = bytearray(image)
    broken[0:4] = b"XXXX"
    try:
        cramfs.Cramfs(bytes(broken))
        check("bad magic raises", False, True)
    except cramfs.CramfsError as exc:
        check("bad magic raises", "not a cramfs image" in str(exc), True)

print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("all cramfs checks passed")
