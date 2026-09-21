"""A pure-Python cramfs reader.

The XDJ-RX3's root filesystem ships as `rootfs.cramfs`.  The usual ways to open
one are to mount it or to run `cramfsck -x`, and on a Raspberry Pi neither is
available: the Pi kernel is built without CONFIG_CRAMFS, and Debian no longer
ships cramfsprogs.  (PrimeBox's tutorial works around this with a privileged
Docker container running fusecram - too much to ask of a launcher.)

cramfs is a small, frozen, well-documented format, so rb4r5 reads it directly:

    superblock            magic 0x28cd3d45, then the root inode
    inode (12 bytes)      mode:16 uid:16 | size:24 gid:8 | namelen:6 offset:26
                          namelen and offset are both counted in 4-byte units
    directory             its data is a run of (inode, name) entries
    file / symlink        its data is a block-pointer table of u32 end-offsets,
                          followed by zlib streams of up to 4096 bytes each

Everything is extracted: permissions, symlinks, hard-link-free device nodes
(when running as root), and empty files.
"""
from __future__ import annotations

import os
import stat
import struct
import zlib
from pathlib import Path

MAGIC_LE = 0x28CD3D45
SIGNATURE = b"Compressed ROMFS"
BLOCK_SIZE = 4096
SUPER_SIZE = 76          # 4*4 + 16 + 16 + 16 + 12 (root inode)
INODE_SIZE = 12

FLAG_HOLES = 0x00000100


class CramfsError(Exception):
    pass


class Inode:
    __slots__ = ("mode", "uid", "gid", "size", "name", "offset")

    def __init__(self, mode, uid, gid, size, offset, name):
        self.mode = mode
        self.uid = uid
        self.gid = gid
        self.size = size
        self.offset = offset
        self.name = name

    @property
    def is_dir(self) -> bool:
        return stat.S_ISDIR(self.mode)

    @property
    def is_file(self) -> bool:
        return stat.S_ISREG(self.mode)

    @property
    def is_link(self) -> bool:
        return stat.S_ISLNK(self.mode)

    def __repr__(self) -> str:
        return (f"<cramfs {self.name!r} mode={self.mode:o} size={self.size} "
                f"offset={self.offset}>")


class Cramfs:
    def __init__(self, data: bytes):
        self.data = data
        if len(data) < SUPER_SIZE:
            raise CramfsError("file is too small to be a cramfs image")
        magic = struct.unpack_from("<I", data, 0)[0]
        if magic == MAGIC_LE:
            self.endian = "<"
        elif struct.unpack_from(">I", data, 0)[0] == MAGIC_LE:
            self.endian = ">"
        else:
            raise CramfsError(
                f"not a cramfs image (magic 0x{magic:08x}, expected "
                f"0x{MAGIC_LE:08x})")
        endian = self.endian
        self.size, self.flags = struct.unpack_from(endian + "II", data, 4)
        self.signature = data[16:32]
        if self.signature != SIGNATURE:
            raise CramfsError(f"bad cramfs signature {self.signature!r}")
        self.crc, self.edition, self.blocks, self.files = \
            struct.unpack_from(endian + "IIII", data, 32)
        self.name = data[48:64].split(b"\0")[0].decode("ascii", "replace")
        self.root = self._inode_at(64)
        if not self.root.is_dir:
            raise CramfsError("the root inode is not a directory")

    # -- parsing -----------------------------------------------------------
    def _inode_at(self, offset: int) -> Inode:
        if offset + INODE_SIZE > len(self.data):
            raise CramfsError(f"inode at {offset} is past the end of the image")
        w0, w1, w2 = struct.unpack_from(self.endian + "III", self.data, offset)
        mode, uid = w0 & 0xFFFF, w0 >> 16
        size, gid = w1 & 0xFFFFFF, w1 >> 24
        namelen = (w2 & 0x3F) * 4
        data_offset = (w2 >> 6) * 4
        name = self.data[offset + INODE_SIZE:offset + INODE_SIZE + namelen]
        return Inode(mode, uid, gid, size, data_offset,
                     name.split(b"\0")[0].decode("utf-8", "surrogateescape"))

    def entry_size(self, inode: Inode) -> int:
        """Bytes this inode occupies in its parent directory."""
        return INODE_SIZE + ((len(inode.name.encode("utf-8", "surrogateescape"))
                              + 3) // 4) * 4

    def listdir(self, inode: Inode) -> list[Inode]:
        if not inode.is_dir:
            raise CramfsError(f"{inode.name} is not a directory")
        entries = []
        offset = inode.offset
        end = inode.offset + inode.size
        while offset < end:
            child = self._inode_at(offset)
            entries.append(child)
            step = self.entry_size(child)
            if step <= 0:
                raise CramfsError(f"zero-length directory entry at {offset}")
            offset += step
        return entries

    def read(self, inode: Inode) -> bytes:
        """Decompress a file's (or symlink's) contents."""
        if inode.size == 0:
            return b""
        nblocks = (inode.size + BLOCK_SIZE - 1) // BLOCK_SIZE
        table = struct.unpack_from(self.endian + "I" * nblocks,
                                   self.data, inode.offset)
        out = bytearray()
        start = inode.offset + nblocks * 4
        for index, block_end in enumerate(table):
            if block_end == 0 and (self.flags & FLAG_HOLES):
                # a hole: this block is all zeroes
                remaining = inode.size - len(out)
                out.extend(b"\0" * min(BLOCK_SIZE, remaining))
                continue
            if block_end < start or block_end > len(self.data):
                raise CramfsError(
                    f"{inode.name}: block {index} ends at {block_end}, "
                    f"outside the image")
            chunk = self.data[start:block_end]
            start = block_end
            if not chunk:
                remaining = inode.size - len(out)
                out.extend(b"\0" * min(BLOCK_SIZE, remaining))
                continue
            try:
                out.extend(zlib.decompress(chunk))
            except zlib.error as exc:
                raise CramfsError(f"{inode.name}: block {index} "
                                  f"does not decompress ({exc})") from exc
        if len(out) != inode.size:
            raise CramfsError(f"{inode.name}: expected {inode.size} bytes, "
                              f"decompressed {len(out)}")
        return bytes(out)

    def walk(self, inode: Inode | None = None, path: str = ""):
        """Yield (path, inode) for every entry, depth first."""
        inode = inode or self.root
        for child in self.listdir(inode):
            child_path = f"{path}/{child.name}" if path else child.name
            yield child_path, child
            if child.is_dir:
                yield from self.walk(child, child_path)


def is_cramfs(path) -> bool:
    try:
        with open(path, "rb") as handle:
            head = handle.read(32)
    except OSError:
        return False
    if len(head) < 32:
        return False
    magic = struct.unpack_from("<I", head, 0)[0]
    swapped = struct.unpack_from(">I", head, 0)[0]
    return (magic == MAGIC_LE or swapped == MAGIC_LE) and head[16:32] == SIGNATURE


def extract(image, dest, log=None) -> dict:
    """Extract a cramfs image into `dest`.  Returns a small summary."""
    image, dest = Path(image), Path(dest)
    data = image.read_bytes()
    fs = Cramfs(data)
    dest.mkdir(parents=True, exist_ok=True)

    stats = {"dirs": 0, "files": 0, "links": 0, "devices": 0, "skipped": 0,
             "bytes": 0, "name": fs.name, "image_size": fs.size}
    can_mknod = os.geteuid() == 0

    for rel, inode in fs.walk():
        target = dest / rel
        if inode.is_dir:
            target.mkdir(parents=True, exist_ok=True)
            os.chmod(target, stat.S_IMODE(inode.mode))
            stats["dirs"] += 1
        elif inode.is_link:
            link_target = fs.read(inode).decode("utf-8", "surrogateescape")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() or target.exists():
                target.unlink()
            os.symlink(link_target, target)
            stats["links"] += 1
        elif inode.is_file:
            payload = fs.read(inode)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            os.chmod(target, stat.S_IMODE(inode.mode))
            stats["files"] += 1
            stats["bytes"] += len(payload)
        elif stat.S_ISCHR(inode.mode) or stat.S_ISBLK(inode.mode) or \
                stat.S_ISFIFO(inode.mode) or stat.S_ISSOCK(inode.mode):
            # device numbers live in the size field for special files
            if can_mknod:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    target.unlink()
                try:
                    os.mknod(target, inode.mode, os.makedev(
                        (inode.size >> 8) & 0xFF, inode.size & 0xFF))
                    stats["devices"] += 1
                except OSError:
                    stats["skipped"] += 1
            else:
                stats["skipped"] += 1
        else:
            stats["skipped"] += 1
        if log and (stats["files"] + stats["dirs"]) % 500 == 0:
            log(f"  {stats['dirs']} dirs, {stats['files']} files…")
    return stats
