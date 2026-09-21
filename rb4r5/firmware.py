"""From a `.UPD` file to a ready payload, with one prompt.

You pick the XDJ-RX3 firmware update file you downloaded; everything after that
is automatic:

    XDJ-RX3.UPD  ──decrypt──▶  XDJRX3.iso  ──unpack──▶  XDJRX3/
                                                          ├── pdj/rbp
                                                          ├── lib/ usr/
                                                          ├── gui/         (gui.tar.gz)
                                                          └── images/
                                            ──unpack──▶  XDJRX3-rootfs/   (rootfs.cramfs)

The decryption is the scheme PrimeBox's `rx3dec` documents - AES-256-CBC per
512-byte sector, IV = LE32(sector) padded with zeroes, key = the first 31 bytes
of the key file's first line NUL-padded to 32, with a 16-byte plaintext trailer
- reimplemented here so no Rust toolchain is needed.

The key itself is **not** shipped and never will be: AlphaTheta published it in
their own GPL source distribution and you obtain it from there.  rb4r5 looks for
it in the obvious places, tries every candidate it finds against the file
(a wrong key is obvious immediately - the ISO signature does not appear), and
only asks if it cannot find one.
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from . import cramfs, util

SECTOR = 512
TRAILER = 16
ISO_PVD_SECTOR = 64          # where the "CD001" signature must appear

# Where to look for a .UPD or a key, in order of likelihood.
SCAN_ROOTS = [
    ".", "~", "~/Downloads", "~/Desktop", "~/firmware", "/root", "/root/Downloads",
    "/home", "/media", "/mnt", "/opt/rb4r5/payload", "/boot/firmware", "/srv",
    "/tmp", "/var/tmp",
]
SCAN_DEPTH = 3
SKIP_DIRS = {"/proc", "/sys", "/dev", "/run", "/opt/rb4r5/chroot",
             "/opt/rb4r5/work", ".git", "__pycache__", "node_modules"}


# --------------------------------------------------------------------------
# finding files
# --------------------------------------------------------------------------
def _expand_roots(extra=None) -> list[Path]:
    roots = []
    for entry in (list(extra or []) + SCAN_ROOTS):
        path = Path(os.path.expanduser(str(entry)))
        try:
            if path.is_dir() and path.resolve() not in [r.resolve() for r in roots]:
                roots.append(path)
        except OSError:
            continue
    return roots


def scan(suffixes: tuple[str, ...], extra_roots=None, names=(),
         max_hits: int = 60, depth: int = SCAN_DEPTH) -> list[Path]:
    """Find files by suffix (case-insensitive) or exact name, breadth first."""
    hits: list[Path] = []
    seen: set = set()
    for root in _expand_roots(extra_roots):
        base_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root, topdown=True,
                                                    onerror=lambda e: None):
            here = Path(dirpath)
            if len(here.parts) - base_depth >= depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames
                           if d not in SKIP_DIRS
                           and not d.startswith(".")
                           and str(here / d) not in SKIP_DIRS]
            for filename in filenames:
                lower = filename.lower()
                if not (lower.endswith(suffixes) or filename in names or
                        lower in names):
                    continue
                candidate = here / filename
                try:
                    key = candidate.resolve()
                except OSError:
                    continue
                if key in seen:
                    continue
                seen.add(key)
                hits.append(candidate)
                if len(hits) >= max_hits:
                    return hits
    return hits


def looks_like_upd(path) -> tuple[bool, str]:
    """A quick structural check: the body must be 512-aligned plus a trailer."""
    try:
        size = Path(path).stat().st_size
    except OSError as exc:
        return False, str(exc)
    if size < 1 << 20:
        return False, "too small to be firmware"
    if (size - TRAILER) % SECTOR != 0:
        return False, "not 512-byte aligned (not an RX3 .UPD?)"
    return True, f"{size / 1e6:.1f} MB"


def find_upd_files(extra_roots=None) -> list[dict]:
    found = []
    for path in scan((".upd",), extra_roots):
        ok, note = looks_like_upd(path)
        try:
            stat = path.stat()
        except OSError:
            continue
        found.append({
            "path": path,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "plausible": ok,
            "note": note,
        })
    found.sort(key=lambda f: (not f["plausible"], -f["mtime"]))
    return found


# --------------------------------------------------------------------------
# the prompt
# --------------------------------------------------------------------------
def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def choose_file(title: str, candidates: list[dict], what: str = "file",
                hint: str = "") -> Path | None:
    """Show a numbered menu and return the chosen path (or None)."""
    print()
    print(title)
    print("=" * len(title))
    if candidates:
        print()
        for index, item in enumerate(candidates, 1):
            path = item["path"]
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(item["mtime"]))
            flag = "" if item.get("plausible", True) else "  (does not look right)"
            print(f"  {index:>2}) {str(path):<52} {item['size'] / 1e6:>8.1f} MB  "
                  f"{when}{flag}")
    else:
        print(f"\n  (no {what} found automatically)")
    if hint:
        print(f"\n{hint}")
    print()
    print("   p) type a path")
    print("   q) cancel")
    print()

    default = "1" if candidates else "p"
    while True:
        try:
            reply = input(f"Select the {what} [{default}]: ").strip() or default
        except EOFError:
            return None
        if reply.lower() in ("q", "quit", "exit"):
            return None
        if reply.lower() == "p":
            try:
                typed = input("Path: ").strip()
            except EOFError:
                return None
            if not typed:
                continue
            path = Path(os.path.expanduser(typed))
            if path.is_file():
                return path
            print(f"  {path} is not a file")
            continue
        if reply.isdigit() and 1 <= int(reply) <= len(candidates):
            return candidates[int(reply) - 1]["path"]
        # allow pasting a path straight in
        path = Path(os.path.expanduser(reply))
        if path.is_file():
            return path
        print("  not a valid choice")


# --------------------------------------------------------------------------
# the key
# --------------------------------------------------------------------------
KEY_HINT = """\
rb4r5 does not ship the firmware key, and cannot: AlphaTheta published it
themselves in their GPL source distribution, and you obtain it from there.

  https://www.pioneerdj.com/en/support/open-source-code-distribution/gnu-open-source-license/

Download the XDJ-RX3 archives, unpack them, and look for a file called
'aes256.key' (it is also at /usr/local/pdj/aes256.key inside an already
decrypted rootfs).  Put it next to the .UPD file, or point at it below -
rb4r5 will remember it in the payload directory."""


def derive_key(key_bytes: bytes) -> bytes:
    """First line, first 31 bytes, NUL-padded to 32 (the device's own rule)."""
    first_line = key_bytes.split(b"\n", 1)[0]
    return first_line[:31].ljust(32, b"\0")


class _quiet_stderr:
    """Silence fd 2 for the duration of a block.

    A broken `cryptography` install panics inside its Rust bindings, which
    prints a backtrace straight to fd 2 before Python ever sees an exception.
    We fall back cleanly, so the noise is worse than useless.
    """

    def __enter__(self):
        self.saved = os.dup(2)
        self.devnull = os.open(os.devnull, os.O_WRONLY)
        sys.stderr.flush()
        os.dup2(self.devnull, 2)
        return self

    def __exit__(self, *exc):
        sys.stderr.flush()
        os.dup2(self.saved, 2)
        os.close(self.devnull)
        os.close(self.saved)
        return False


def _ecb_cryptography(key: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return decryptor.update(data) + decryptor.finalize()


def _ecb_pycryptodome(key: bytes, data: bytes) -> bytes:
    from Crypto.Cipher import AES
    return AES.new(key, AES.MODE_ECB).decrypt(data)


def _aes_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    """ECB-decrypt a whole buffer, by whatever is available on this machine."""
    # Any of these can be broken on a given machine.  A half-installed
    # `cryptography` does not raise ImportError - its Rust bindings raise
    # pyo3_runtime.PanicException, which derives from BaseException - so catch
    # broadly and fall through to the next backend rather than dying.
    for name, attempt in (("cryptography", _ecb_cryptography),
                          ("pycryptodome", _ecb_pycryptodome)):
        try:
            with _quiet_stderr():
                return attempt(key, data)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:              # noqa: BLE001
            util.debug(f"{name} unusable ({exc.__class__.__name__}: {exc})")
    if not util.have("openssl"):
        raise util.Fail(
            "no AES implementation available.  Install one:\n"
            "    sudo apt-get install python3-cryptography\n"
            "(openssl would also do).")
    proc = subprocess.run(
        ["openssl", "enc", "-d", "-aes-256-ecb", "-nopad",
         "-K", key.hex()],
        input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0 or len(proc.stdout) != len(data):
        raise util.Fail(f"openssl could not decrypt the image: "
                        f"{proc.stderr.decode(errors='replace')[:200]}")
    return proc.stdout


def _xor(left: bytes, right: bytes) -> bytes:
    """XOR two equal-length buffers, in chunks so memory stays bounded."""
    out = bytearray(len(left))
    step = 1 << 20
    for start in range(0, len(left), step):
        end = min(start + step, len(left))
        chunk = int.from_bytes(left[start:end], "big") ^ \
            int.from_bytes(right[start:end], "big")
        out[start:end] = chunk.to_bytes(end - start, "big")
    return bytes(out)


def decrypt_body(body: bytes, key: bytes) -> bytes:
    """AES-256-CBC with a per-sector IV of LE32(sector index).

    Done as one ECB pass plus an XOR rather than 135k separate CBC objects:
    CBC decryption is ECB decryption XOR the previous ciphertext block, and for
    the first block of each sector that "previous block" is the IV.
    """
    if len(body) % SECTOR:
        raise util.Fail(f"the encrypted body is {len(body)} bytes, which is not "
                        f"a whole number of {SECTOR}-byte sectors")
    ecb = _aes_ecb_decrypt(key, body)

    # Build the XOR stream: per sector, the IV followed by that sector's
    # ciphertext blocks except the last one.
    mask = bytearray(len(body))
    for sector in range(len(body) // SECTOR):
        base = sector * SECTOR
        mask[base:base + 4] = struct.pack("<I", sector)
        # bytes 4..16 stay zero (the rest of the IV)
        mask[base + 16:base + SECTOR] = body[base:base + SECTOR - 16]
    return _xor(ecb, bytes(mask))


def decrypt_sector(chunk: bytes, key: bytes, sector: int) -> bytes:
    """Decrypt one 512-byte sector, given its index (which sets the IV)."""
    if len(chunk) != SECTOR:
        return b""
    ecb = _aes_ecb_decrypt(key, chunk)
    mask = bytearray(SECTOR)
    mask[0:4] = struct.pack("<I", sector)
    mask[16:SECTOR] = chunk[:SECTOR - 16]
    return _xor(ecb, bytes(mask))


def key_works(upd_path: Path, key_file: Path) -> bool:
    """Decrypt just the ISO primary volume descriptor and look for CD001.

    One sector is enough to prove a key: a wrong one gives noise.
    """
    try:
        raw = key_file.read_bytes()
    except OSError:
        return False
    if not raw.strip():
        return False
    try:
        with open(upd_path, "rb") as handle:
            handle.seek(ISO_PVD_SECTOR * SECTOR)
            chunk = handle.read(SECTOR)
    except OSError:
        return False
    if len(chunk) != SECTOR:
        return False
    try:
        plain = decrypt_sector(chunk, derive_key(raw), ISO_PVD_SECTOR)
    except util.Fail:
        return False
    return plain[1:6] == b"CD001"


def find_key_files(upd_path: Path, payload: Path, primebox: Path) -> list[Path]:
    """Every plausible key file, best guesses first."""
    ordered: list[Path] = []

    def add(path):
        try:
            path = Path(path)
            if path.is_file() and path.stat().st_size <= 4096 and \
                    path.resolve() not in [p.resolve() for p in ordered]:
                ordered.append(path)
        except OSError:
            pass

    add(payload / "aes256.key")
    add(Path("/etc/rb4r5/aes256.key"))
    add(primebox / "keys/aes256.key")
    if upd_path:
        add(upd_path.parent / "aes256.key")
        for sibling in sorted(upd_path.parent.glob("*.key")):
            add(sibling)
        for sibling in sorted(upd_path.parent.glob("*/aes256.key")):
            add(sibling)
    add(Path("~/aes256.key").expanduser())
    # an already-extracted rootfs carries the device's own copy
    add(payload / "XDJRX3-rootfs/usr/local/pdj/aes256.key")
    for path in scan((".key",), names=("aes256.key",), max_hits=25):
        add(path)
    return ordered


def resolve_key(upd_path: Path, payload: Path, primebox: Path,
                explicit: Path | None = None, ask: bool = True) -> Path:
    """Find a key that actually decrypts this file, asking only if we must."""
    if explicit:
        explicit = Path(os.path.expanduser(str(explicit)))
        if not explicit.is_file():
            raise util.Fail(f"key file not found: {explicit}")
        if not key_works(upd_path, explicit):
            raise util.Fail(f"{explicit} does not decrypt {upd_path.name} "
                            "(wrong key, or this is not an XDJ-RX3 .UPD)")
        return explicit

    candidates = find_key_files(upd_path, payload, primebox)
    if candidates:
        util.info(f"checking {len(candidates)} candidate key file(s)…")
    for candidate in candidates:
        if key_works(upd_path, candidate):
            util.ok(f"key: {candidate}")
            return candidate
        util.debug(f"  {candidate}: does not decrypt this file")

    if not ask or not interactive():
        raise util.Fail(
            "no working firmware key found.\n" + KEY_HINT +
            f"\n\nThen re-run, or pass it directly:\n"
            f"    sudo python3 launch.py firmware --upd {upd_path} "
            f"--key /path/to/aes256.key")

    listing = []
    for candidate in candidates:
        try:
            stat = candidate.stat()
        except OSError:
            continue        # it went away while we were looking
        listing.append({"path": candidate, "size": stat.st_size,
                        "mtime": stat.st_mtime, "plausible": False,
                        "note": "did not decrypt this file"})
    chosen = choose_file("Firmware key needed", listing, what="key file",
                         hint=KEY_HINT)
    if chosen is None:
        raise util.Fail("cancelled - no key, so the firmware cannot be unpacked")
    if not key_works(upd_path, chosen):
        raise util.Fail(f"{chosen} does not decrypt {upd_path.name}")
    util.ok(f"key: {chosen}")
    return chosen


# --------------------------------------------------------------------------
# unpacking
# --------------------------------------------------------------------------
def decrypt_upd(upd_path: Path, key_file: Path, out_iso: Path) -> dict:
    util.step(f"decrypting {upd_path.name} ({upd_path.stat().st_size / 1e6:.1f} MB)")
    raw = upd_path.read_bytes()
    if len(raw) <= TRAILER:
        raise util.Fail("the file is too small to be firmware")
    body, trailer = raw[:-TRAILER], raw[-TRAILER:]
    key = derive_key(key_file.read_bytes())
    started = time.monotonic()
    plain = decrypt_body(body, key)
    took = time.monotonic() - started

    signature_at = ISO_PVD_SECTOR * SECTOR
    if plain[signature_at + 1:signature_at + 6] != b"CD001":
        raise util.Fail("decryption produced something that is not an ISO image "
                        "- wrong key, or not an XDJ-RX3 .UPD")
    util.ensure_dir(out_iso.parent)
    out_iso.write_bytes(plain)
    util.ok(f"ISO 9660 signature found; wrote {out_iso} "
            f"({len(plain) / 1e6:.1f} MB in {took:.1f}s)")
    model = trailer.split(b"\0")[0].decode("ascii", "replace")
    return {"iso": out_iso, "bytes": len(plain), "trailer": model, "seconds": took}


def _mount_and_copy(image: Path, dest: Path, fstype: str) -> bool:
    """Mount an image read-only and copy it out (best fidelity, needs root)."""
    if os.geteuid() != 0 or not util.have("mount"):
        return False
    mountpoint = Path("/run/rb4r5-mnt")
    util.ensure_dir(mountpoint)
    proc = util.run(["mount", "-o", "loop,ro", "-t", fstype,
                     str(image), str(mountpoint)], check=False)
    if proc.returncode != 0:
        return False
    try:
        util.ensure_dir(dest)
        util.run(["cp", "-a", f"{mountpoint}/.", str(dest)], timeout=1800)
        return True
    finally:
        util.run(["umount", str(mountpoint)], check=False)


def extract_iso(iso: Path, dest: Path) -> str:
    """Unpack the ISO, preferring the method that keeps symlinks and modes."""
    util.step(f"unpacking {iso.name} -> {dest}")
    util.ensure_dir(dest)
    if _mount_and_copy(iso, dest, "iso9660"):
        return "loop mount (Rock Ridge preserved)"
    if util.have("bsdtar"):
        util.run(["bsdtar", "-x", "-f", str(iso), "-C", str(dest)], timeout=1800)
        return "bsdtar"
    for seven in ("7z", "7zz", "7za"):
        if util.have(seven):
            util.run([seven, "x", "-y", f"-o{dest}", str(iso)], timeout=1800)
            return seven
    raise util.Fail(
        "cannot unpack the ISO on this machine.  Either run as root (the "
        "kernel can loop-mount it) or install a tool:\n"
        "    sudo apt-get install libarchive-tools     # bsdtar\n"
        "    sudo apt-get install p7zip-full           # 7z")


def extract_gui(iso_tree: Path, dest: Path) -> str:
    """gui.tar.gz holds the fonts and images - the UI will not start without it."""
    archive = iso_tree / "images/gui.tar.gz"
    if not archive.exists():
        alternatives = list(iso_tree.rglob("gui.tar.gz"))
        if not alternatives:
            return "gui.tar.gz not found in the ISO (fonts will be missing)"
        archive = alternatives[0]
    util.step(f"unpacking {archive.name} -> {dest}")
    util.ensure_dir(dest)
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        _safe_extract(tar, members, dest)
    return f"{len(members)} entries"


def _safe_extract(tar: tarfile.TarFile, members, dest: Path) -> None:
    """Extract without letting a path escape `dest` (belt and braces)."""
    dest = dest.resolve()
    safe = []
    for member in members:
        target = (dest / member.name).resolve()
        if dest == target or dest in target.parents:
            safe.append(member)
    if hasattr(tarfile, "data_filter"):
        tar.extractall(dest, members=safe, filter="tar")
    else:                                        # Python < 3.11.4
        tar.extractall(dest, members=safe)


def extract_rootfs(iso_tree: Path, dest: Path) -> str:
    """rootfs.cramfs -> the soft-float userland the player runs inside."""
    image = iso_tree / "images/rootfs.cramfs"
    if not image.exists():
        found = list(iso_tree.rglob("rootfs.cramfs"))
        if not found:
            raise util.Fail("rootfs.cramfs is not in this ISO - is it really "
                            "XDJ-RX3 firmware?")
        image = found[0]
    util.step(f"unpacking {image.name} ({image.stat().st_size / 1e6:.1f} MB) "
              f"-> {dest}")
    if not cramfs.is_cramfs(image):
        raise util.Fail(f"{image} is not a cramfs image")
    # The Pi's kernel has no CONFIG_CRAMFS, so this normally uses our own
    # reader; mounting is tried first for the rare kernel that can.
    if _mount_and_copy(image, dest, "cramfs"):
        return "loop mount"
    stats = cramfs.extract(image, dest, log=util.debug)
    return (f"{stats['dirs']} dirs, {stats['files']} files, "
            f"{stats['links']} symlinks, {stats['devices']} device nodes"
            + (f", {stats['skipped']} skipped" if stats["skipped"] else "")
            + " (pure-Python cramfs reader)")


# --------------------------------------------------------------------------
# the pipeline
# --------------------------------------------------------------------------
def status(cfg) -> dict:
    payload = cfg.payload
    iso_tree = payload / "XDJRX3"
    rootfs = payload / "XDJRX3-rootfs"
    return {
        "payload": payload,
        "iso": payload / "XDJRX3.iso",
        "iso_tree": iso_tree,
        "rootfs": rootfs,
        "have_iso": (payload / "XDJRX3.iso").exists(),
        "have_player": (iso_tree / "pdj/rbp").exists(),
        "have_gui": (iso_tree / "gui").is_dir(),
        "have_rootfs": (rootfs / "lib/ld-linux.so.3").exists(),
        "release": util.read_text(iso_tree / "images/release.txt").strip(),
    }


def ready(cfg) -> bool:
    state = status(cfg)
    return state["have_player"] and state["have_rootfs"]


def prepare(cfg, upd=None, key=None, force: bool = False,
            ask: bool = True) -> list[str]:
    """Pick the .UPD (if needed) and turn it into a complete payload."""
    payload = util.ensure_dir(cfg.payload)
    primebox = Path(cfg.get("build.primebox"))
    notes: list[str] = []

    if ready(cfg) and not force:
        state = status(cfg)
        return [f"the payload is already unpacked in {payload}"
                + (f" (firmware {state['release']})" if state["release"] else ""),
                "pass --force to unpack it again"]

    # 1. the one thing you have to choose
    if upd:
        upd_path = Path(os.path.expanduser(str(upd)))
        if not upd_path.is_file():
            raise util.Fail(f"no such file: {upd_path}")
    else:
        util.step("looking for XDJ-RX3 firmware (.UPD) files")
        candidates = find_upd_files([payload])
        if len(candidates) == 1 and candidates[0]["plausible"] and not ask:
            upd_path = candidates[0]["path"]
        elif not interactive() and candidates and candidates[0]["plausible"]:
            upd_path = candidates[0]["path"]
            util.info(f"not a terminal; taking the only sensible candidate: "
                      f"{upd_path}")
        elif not interactive():
            raise util.Fail(
                "no firmware file given and no terminal to ask on.  Run:\n"
                "    sudo python3 launch.py firmware --upd /path/to/XDJ-RX3.UPD")
        else:
            chosen = choose_file(
                "Select your XDJ-RX3 firmware file",
                candidates, what="firmware file",
                hint="This is the .UPD you downloaded from AlphaTheta "
                     "(XDJ-RX3 v1.20, about 69 MB).\nEverything after this is "
                     "automatic.")
            if chosen is None:
                raise util.Fail("cancelled")
            upd_path = chosen

    ok, note = looks_like_upd(upd_path)
    if not ok:
        util.warn(f"{upd_path}: {note} - trying anyway")
    notes.append(f"firmware: {upd_path} ({upd_path.stat().st_size / 1e6:.1f} MB)")

    # 2. the key, found rather than asked for whenever possible
    key_file = resolve_key(upd_path, payload, primebox, explicit=key, ask=ask)
    notes.append(f"key: {key_file}")
    # keep a copy so later runs never have to look again
    stored = payload / "aes256.key"
    if key_file.resolve() != stored.resolve():
        shutil.copy2(key_file, stored)
        os.chmod(stored, 0o600)
        notes.append(f"remembered the key at {stored}")

    # 3. decrypt
    iso = payload / "XDJRX3.iso"
    if iso.exists() and not force:
        notes.append(f"reusing {iso}")
    else:
        result = decrypt_upd(upd_path, key_file, iso)
        notes.append(f"decrypted -> {iso} ({result['bytes'] / 1e6:.1f} MB, "
                     f"{result['seconds']:.1f}s)")

    # 4. unpack the ISO, the GUI assets and the root filesystem
    iso_tree = payload / "XDJRX3"
    if force and iso_tree.exists():
        shutil.rmtree(iso_tree)
    notes.append(f"ISO unpacked with {extract_iso(iso, iso_tree)}")

    release = util.read_text(iso_tree / "images/release.txt").strip()
    if release:
        notes.append(f"firmware version: {release}")
        if "1.20" not in release:
            util.warn(f"this is firmware {release}; every published patch set "
                      "was derived from v1.20, so the player may not patch")

    notes.append(f"gui assets: {extract_gui(iso_tree, iso_tree / 'gui')}")

    rootfs = payload / "XDJRX3-rootfs"
    if force and rootfs.exists():
        shutil.rmtree(rootfs)
    if (rootfs / "lib/ld-linux.so.3").exists() and not force:
        notes.append(f"reusing {rootfs}")
    else:
        notes.append(f"rootfs: {extract_rootfs(iso_tree, rootfs)}")

    # 5. what we ended up with
    player = iso_tree / "pdj/rbp"
    if player.exists():
        notes.append(f"player: {player} "
                     f"({player.stat().st_size / 1e6:.1f} MB)")
    else:
        util.warn("pdj/rbp is not in this ISO - the payload is incomplete")
    loader = rootfs / "lib/ld-linux.so.3"
    notes.append(f"loader: {loader} "
                 f"({'present' if loader.exists() else 'MISSING'})")

    # the device's own copy of the key, now that we can see it
    device_key = rootfs / "usr/local/pdj/aes256.key"
    if device_key.exists():
        notes.append(f"(the firmware's own key is at {device_key})")

    if not ready(cfg):
        raise util.Fail("the payload is still incomplete - see the warnings above")
    return notes


def describe(cfg) -> list[str]:
    state = status(cfg)
    rows = [
        f"payload dir: {state['payload']}",
        f"firmware ISO: {'present' if state['have_iso'] else 'absent'}",
        f"player (pdj/rbp): {'present' if state['have_player'] else 'absent'}",
        f"gui assets: {'present' if state['have_gui'] else 'absent'}",
        f"rootfs (soft-float userland): "
        f"{'present' if state['have_rootfs'] else 'absent'}",
    ]
    if state["release"]:
        rows.append(f"firmware version: {state['release']}")
    if not ready(cfg):
        rows.append("run: sudo python3 launch.py firmware")
    return rows
