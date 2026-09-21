"""Build a small cramfs image from a dict tree - a fixture for the tests.

    build({"lib": {"ld-linux.so.3": b"...", "libc.so.6": b"..."},
           "etc": {"hostname": b"rx3\n"}})

Only what the tests need: directories, regular files and symlinks
(as ("symlink", "target")).  The format is frozen, so this stays valid.
"""
import struct
import zlib

MAGIC = 0x28CD3D45
SIGNATURE = b"Compressed ROMFS"
BLOCK = 4096


def _inode(mode, size, offset, name):
    raw = name.encode()
    padded = (len(raw) + 3) // 4 * 4
    assert offset % 4 == 0
    return struct.pack("<III", mode & 0xFFFF, size & 0xFFFFFF,
                       ((offset // 4) << 6) | (padded // 4)) + \
        raw.ljust(padded, b"\0")


def _entry_len(name):
    return 12 + (len(name.encode()) + 3) // 4 * 4


def build(tree: dict, volume: str = "rb4r5-fixture") -> bytes:
    """Lay the whole image out in two passes: sizes first, then bytes."""
    # pass 1: how big is each directory's entry block, and in what order
    dirs = []               # (path, node, entries_offset, size)

    def measure(node):
        return sum(_entry_len(name) for name in sorted(node))

    root_size = measure(tree)
    cursor = 76 + root_size
    queue = [("", tree)]
    layout = {"": (76, root_size)}
    while queue:
        path, node = queue.pop(0)
        for name in sorted(node):
            child = node[name]
            if isinstance(child, dict):
                child_path = f"{path}/{name}" if path else name
                size = measure(child)
                layout[child_path] = (cursor, size)
                cursor += size
                queue.append((child_path, child))
    data_at = cursor

    # pass 2: data blocks
    data = bytearray()
    placed = {}

    def place(path, payload: bytes):
        if not payload:
            placed[path] = (0, 0)
            return
        chunks = [payload[i:i + BLOCK] for i in range(0, len(payload), BLOCK)]
        compressed = [zlib.compress(c, 6) for c in chunks]
        start = data_at + len(data)
        cursor_ = start + len(compressed) * 4
        table = []
        for chunk in compressed:
            cursor_ += len(chunk)
            table.append(cursor_)
        data.extend(struct.pack("<" + "I" * len(table), *table))
        for chunk in compressed:
            data.extend(chunk)
        while len(data) % 4:
            data.extend(b"\0")
        placed[path] = (start, len(payload))

    def walk_place(path, node):
        for name in sorted(node):
            child = node[name]
            child_path = f"{path}/{name}" if path else name
            if isinstance(child, dict):
                walk_place(child_path, child)
            elif isinstance(child, tuple) and child[0] == "symlink":
                place(child_path, child[1].encode())
            else:
                place(child_path, child)

    walk_place("", tree)

    # pass 3: emit the inode blocks in the same order as pass 1
    blocks = {}

    def emit(path, node):
        blob = b""
        for name in sorted(node):
            child = node[name]
            child_path = f"{path}/{name}" if path else name
            if isinstance(child, dict):
                offset, size = layout[child_path]
                blob += _inode(0o040755, size, offset, name)
                emit(child_path, child)
            elif isinstance(child, tuple) and child[0] == "symlink":
                offset, size = placed[child_path]
                blob += _inode(0o120777, size, offset, name)
            else:
                offset, size = placed[child_path]
                blob += _inode(0o100755, size, offset, name)
        blocks[path] = blob

    emit("", tree)

    total = data_at + len(data)
    image = struct.pack("<III I", MAGIC, total, 0, 0) + SIGNATURE
    image += struct.pack("<IIII", 0, 0, (total + BLOCK - 1) // BLOCK, len(layout))
    image += volume.encode().ljust(16, b"\0")[:16]
    image += _inode(0o040755, root_size, 76, "")
    assert len(image) == 76, len(image)

    ordered = [""] + [p for p in layout if p]
    for path in ordered:
        image += blocks[path]
    assert len(image) == data_at, (len(image), data_at)
    return image + bytes(data)
