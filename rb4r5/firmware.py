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
import re
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

# The firmware this port is built around.  AlphaTheta publish the update
# package themselves; rb4r5 fetches it from them rather than redistributing it
# (it is their copyrighted firmware - see NOTICE.md), so nothing has to be
# downloaded by hand and nothing has to be chosen.  Every published patch set
# for this player was derived from v1.20.
OFFICIAL = {
    "version": "1.20",
    "url": "https://downloads.support.alphatheta.com/firmwares/"
           "all-in-one-dj-systems/XDJ-RX3/XDJ-RX3_v120.zip",
    "zip_name": "XDJ-RX3_v120.zip",
    "member": "XDJ-RX3_v120/XDJ-RX3.UPD",
    "upd_size": 69_171_216,          # documented by the upstream tutorial
    "rbp_md5": "4f2efcfc0c9e3f539289f863acfddcc6",   # the stock v1.20 player
}

# Where the firmware is fetched from, in order, until one yields a usable file.
# Nothing is served from this repository, which holds no vendor firmware at all
# (see NOTICE.md); these are locations the operator of this installation keeps
# a copy at, plus AlphaTheta's own download.
#
#   * a plain URL ending in .UPD is taken as the update file itself
#   * a plain URL ending in .zip is unpacked
#   * "gdrive:<file id>" goes through Google Drive's confirmation page, which
#     is how it serves anything large
MIRRORS = [
    "https://balvansintlievens.qzz.io/XDJRX3.UPD",   # direct, no unpacking
    OFFICIAL["url"],                                  # AlphaTheta's own zip
    "gdrive:1FvztdfmpOvzqSXHDSo0eWhxe4RaEP5Ul",
]

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
# getting the firmware itself
# --------------------------------------------------------------------------
GDRIVE_PREFIX = "gdrive:"


def _looks_like_html(path: Path) -> bool:
    """Drive and friends serve an error/consent page with a 200 status."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(512).lstrip().lower()
    except OSError:
        return False
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


def download_from_gdrive(file_id: str, dest: Path) -> bool:
    """Fetch a large file from Google Drive, past its confirmation page.

    Drive does not serve anything over ~100 MB at a plain URL: the first
    request returns an HTML interstitial carrying a confirm token, and the file
    comes from a second request that quotes it.  Both shapes of that dance (the
    old cookie one and the current drive.usercontent one) are tried.
    """
    if not util.have("curl"):
        util.warn("Google Drive downloads need curl")
        return False
    util.ensure_dir(dest.parent)
    partial = dest.with_suffix(dest.suffix + ".part")
    cookies = dest.with_suffix(".cookies")

    attempts = [
        ["curl", "-sSL", "--fail", "-o", str(partial),
         f"https://drive.usercontent.google.com/download"
         f"?id={file_id}&export=download&confirm=t"],
        ["curl", "-sSL", "-c", str(cookies), "-b", str(cookies),
         "-o", str(partial),
         f"https://drive.google.com/uc?export=download&confirm=t&id={file_id}"],
    ]
    try:
        for cmd in attempts:
            if sys.stderr.isatty():
                cmd = [cmd[0], "--progress-bar"] + cmd[1:]
            proc = util.run(cmd, check=False, capture=False, timeout=3600)
            if proc.returncode == 0 and partial.exists() and \
                    partial.stat().st_size > (1 << 20) and \
                    not _looks_like_html(partial):
                partial.replace(dest)
                return True

            # the interstitial: pull the confirm token out of it and retry
            if partial.exists() and _looks_like_html(partial):
                page = partial.read_text(errors="replace")
                token = re.search(r'name="confirm"\s+value="([^"]+)"', page) or \
                    re.search(r"confirm=([0-9A-Za-z_-]+)", page)
                uuid = re.search(r'name="uuid"\s+value="([^"]+)"', page)
                if token:
                    url = (f"https://drive.usercontent.google.com/download"
                           f"?id={file_id}&export=download"
                           f"&confirm={token.group(1)}")
                    if uuid:
                        url += f"&uuid={uuid.group(1)}"
                    proc = util.run(["curl", "-sSL", "--fail", "-o", str(partial),
                                     url], check=False, capture=False,
                                    timeout=3600)
                    if proc.returncode == 0 and partial.exists() and \
                            not _looks_like_html(partial):
                        partial.replace(dest)
                        return True
        return False
    finally:
        partial.unlink(missing_ok=True)
        cookies.unlink(missing_ok=True)


def download_file(url: str, dest: Path, expect_size: int = 0) -> Path:
    """Fetch a URL to a file, resuming and showing progress where possible."""
    util.ensure_dir(dest.parent)
    partial = dest.with_suffix(dest.suffix + ".part")

    if url.startswith(GDRIVE_PREFIX):
        if not download_from_gdrive(url[len(GDRIVE_PREFIX):], dest):
            raise util.Fail(f"could not download {url}")
        return dest

    if util.have("curl"):
        cmd = ["curl", "-L", "--fail", "--retry", "3", "--retry-delay", "2",
               "-C", "-", "-o", str(partial), url]
        if sys.stderr.isatty():
            cmd.insert(1, "--progress-bar")
        else:
            cmd.insert(1, "-sS")
        proc = util.run(cmd, check=False, capture=False, timeout=3600)
    elif util.have("wget"):
        proc = util.run(["wget", "-c", "-O", str(partial), url],
                        check=False, capture=False, timeout=3600)
    else:
        proc = _download_with_python(url, partial)

    if proc.returncode != 0 or not partial.exists() or partial.stat().st_size == 0:
        partial.unlink(missing_ok=True)
        raise util.Fail(
            f"could not download {url}\n"
            "Check the Pi's network, or fetch the file on another machine and "
            "drop it next to the launcher - it is picked up automatically.")
    size = partial.stat().st_size
    if expect_size and size != expect_size:
        util.warn(f"downloaded {size} bytes, expected {expect_size} "
                  "- continuing, but check the result")
    partial.replace(dest)
    return dest


def _download_with_python(url: str, dest: Path):
    """Last resort when neither curl nor wget is installed."""
    import urllib.request

    class Result:
        returncode = 0

    try:
        with urllib.request.urlopen(url, timeout=60) as response, \
                open(dest, "wb") as handle:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                if total and sys.stderr.isatty():
                    print(f"\r  {done / 1e6:6.1f} / {total / 1e6:.1f} MB", 
                          end="", file=sys.stderr)
        if sys.stderr.isatty():
            print(file=sys.stderr)
    except Exception as exc:                      # noqa: BLE001
        util.warn(f"download failed: {exc}")
        Result.returncode = 1
    return Result()


def upd_from_zip(archive: Path, dest_dir: Path) -> Path:
    """Pull the .UPD out of AlphaTheta's zip."""
    import zipfile

    util.ensure_dir(dest_dir)
    with zipfile.ZipFile(archive) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".upd")]
        if not names:
            raise util.Fail(f"{archive} contains no .UPD file "
                            f"(members: {', '.join(zf.namelist()[:5])})")
        member = names[0]
        target = dest_dir / Path(member).name
        util.step(f"extracting {member} from {archive.name}")
        with zf.open(member) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)
    return target


def mirrors(cfg) -> list[str]:
    configured = cfg.get("firmware.url")
    extra = cfg.get("firmware.mirrors") or []
    ordered = ([configured] if configured else []) + list(extra)
    for url in MIRRORS:
        if url not in ordered:
            ordered.append(url)
    return ordered


def download_name(url: str, cfg) -> str:
    """What to call the downloaded file: keep the server's own name if it is
    meaningful, so a .UPD does not end up masquerading as a .zip."""
    fallback = cfg.get("firmware.zip_name") or OFFICIAL["zip_name"]
    if url.startswith(GDRIVE_PREFIX):
        return fallback
    base = url.rsplit("/", 1)[-1].split("?")[0].strip()
    if base.lower().endswith((".zip", ".upd")):
        return base
    return fallback


def fetch_official(cfg, force: bool = False) -> Path:
    """Download (once) the firmware this port is built around.

    Each source in turn until one yields a file that is actually firmware; a
    download that comes back as an HTML page, the wrong size, or something that
    is not 512-byte-aligned firmware is discarded rather than fed to the
    decryptor.
    """
    cache = util.ensure_dir(Path(cfg.get("paths.payload")) / "firmware")
    version = cfg.get("firmware.version") or OFFICIAL["version"]

    existing = sorted(cache.glob("*.UPD")) + sorted(cache.glob("*.upd"))
    if existing and not force:
        util.info(f"using the firmware already downloaded: {existing[0]}")
        return existing[0]

    sources = mirrors(cfg)
    zip_path = None
    util.step(f"downloading the XDJ-RX3 v{version} firmware (~66 MB)")
    last_error = None
    for index, url in enumerate(sources, 1):
        target = cache / download_name(url, cfg)
        if target.exists() and not force:
            util.info(f"using the file already downloaded: {target}")
            zip_path = target
            break
        label = "Google Drive" if url.startswith(GDRIVE_PREFIX) else url
        util.info(f"[{index}/{len(sources)}] {label}")
        try:
            download_file(url, target)
        except util.Fail as exc:
            last_error = exc
            util.warn(f"  that source did not work: {str(exc).splitlines()[0]}")
            continue
        if _looks_like_html(target):
            target.unlink(missing_ok=True)
            util.warn("  that source returned a web page, not a file")
            continue
        util.ok(f"downloaded {target} ({target.stat().st_size / 1e6:.1f} MB)")
        zip_path = target
        break
    if zip_path is None:
        raise util.Fail(
            "none of the firmware sources worked"
            + (f" (last error: {last_error})" if last_error else "") +
            f"\nFetch this on another machine and drop it in {cfg.payload}:\n"
            f"    {OFFICIAL['url']}")

    # The download is either AlphaTheta's zip or the .UPD itself, depending on
    # the source.  Either way it ends up under one canonical name, so a later
    # run finds it whichever source produced it.
    if zipfile_is_zip(zip_path):
        upd = upd_from_zip(zip_path, cache)
    else:
        upd = cache / "XDJ-RX3.UPD"
        if zip_path.resolve() != upd.resolve():
            shutil.move(str(zip_path), str(upd))

    expect = int(cfg.get("firmware.expect_upd_size") or OFFICIAL["upd_size"])
    size = upd.stat().st_size
    ok, note = looks_like_upd(upd)
    if not ok:
        raise util.Fail(f"what came down is not firmware: {note}\n"
                        f"Delete {cache} and try again, or supply the file "
                        f"yourself with --upd.")
    if expect and size != expect:
        util.warn(f"{upd.name} is {size} bytes; v{version} is {expect}.  "
                  "A different firmware version may not patch.")
    util.ok(f"firmware: {upd} ({size / 1e6:.1f} MB)")
    return upd


def zipfile_is_zip(path: Path) -> bool:
    import zipfile
    try:
        return zipfile.is_zipfile(path)
    except OSError:
        return False


def locate_upd(cfg, explicit=None, ask: bool = False,
               allow_download: bool = True) -> Path:
    """Decide which .UPD to use.  Deliberate choices beat accidental ones.

        1. --upd, if you passed one
        2. what was downloaded or placed in the payload directory before
        3. a .UPD or AlphaTheta's zip sitting in the payload directory
        4. the picker, but only with --ask
        5. the official download (the normal path - nothing to choose)
        6. failing that, a .UPD found elsewhere on this machine

    A stray file in /tmp or an old download in ~ deliberately does *not* win
    over the firmware this port is built around; you have to put it in the
    payload directory or name it to mean it.
    """
    cache = cfg.payload / "firmware"

    if explicit:
        path = Path(os.path.expanduser(str(explicit)))
        if not path.is_file():
            raise util.Fail(f"no such file: {path}")
        if path.suffix.lower() == ".zip":
            return upd_from_zip(path, util.ensure_dir(cache))
        return path

    # 2 + 3: the payload directory is where a deliberate copy lives
    for directory in (cache, cfg.payload):
        if not directory.is_dir():
            continue
        for found in sorted(directory.glob("*.UPD")) + \
                sorted(directory.glob("*.upd")):
            util.info(f"firmware: {found}")
            return found
        for found in sorted(directory.glob("*.zip")):
            if "xdj" in found.name.lower() or "rx3" in found.name.lower():
                util.info(f"firmware archive: {found}")
                return upd_from_zip(found, util.ensure_dir(cache))

    # 4: only when explicitly asked for
    if ask and interactive():
        picked = choose_file(
            "Select your XDJ-RX3 firmware file", find_upd_files([cfg.payload]),
            what="firmware file",
            hint="Leave this and rb4r5 downloads the official v1.20 firmware "
                 "from AlphaTheta by itself.")
        if picked:
            return picked

    # 5: the normal path
    if allow_download and cfg.get("firmware.auto_download", True):
        try:
            return fetch_official(cfg)
        except util.Fail as exc:
            util.warn(str(exc).splitlines()[0])
            util.info("looking for a firmware file on this machine instead")

    # 6: anything plausible lying around
    candidates = [c for c in find_upd_files([cfg.payload]) if c["plausible"]]
    if candidates:
        chosen = candidates[0]["path"]
        util.info(f"firmware: {chosen} (found on this machine)")
        return chosen

    raise util.Fail(
        "no firmware available.\n"
        f"Either let rb4r5 download it (needs network):\n"
        f"    {cfg.get('firmware.url') or OFFICIAL['url']}\n"
        f"or fetch that zip on another machine and drop it in {cfg.payload}.")


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


def key_from_archives(upd_path: Path, payload: Path) -> Path | None:
    """Look for aes256.key inside archives the user already downloaded.

    AlphaTheta's GPL source distribution is a set of very large tarballs with
    the key somewhere inside.  Unpacking one by hand to find a 32-byte file is
    miserable, so if such an archive is sitting next to the firmware (or in the
    payload directory) we read it out directly.
    """
    import tarfile as tar_module
    import zipfile

    searched = []
    for directory in {upd_path.parent, payload, Path.cwd()}:
        try:
            searched += [p for p in directory.iterdir()
                         if p.is_file() and p.suffix.lower() in
                         (".gz", ".bz2", ".xz", ".zip", ".tar", ".tgz")]
        except OSError:
            continue
    if not searched:
        return None

    out = payload / "aes256.key"
    for archive in searched:
        util.info(f"looking for the key inside {archive.name} "
                  f"({archive.stat().st_size / 1e6:.0f} MB)…")
        try:
            if zipfile.is_zipfile(archive):
                with zipfile.ZipFile(archive) as zf:
                    for name in zf.namelist():
                        if Path(name).name == "aes256.key":
                            out.write_bytes(zf.read(name))
                            util.ok(f"found {name} in {archive.name}")
                            return out
            elif tar_module.is_tarfile(archive):
                with tar_module.open(archive) as tf:
                    for member in tf:
                        if member.isfile() and \
                                Path(member.name).name == "aes256.key":
                            handle = tf.extractfile(member)
                            if handle:
                                out.write_bytes(handle.read())
                                util.ok(f"found {member.name} in {archive.name}")
                                return out
        except (OSError, ValueError, tar_module.TarError) as exc:
            util.debug(f"  {archive.name}: {exc}")
    return None


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

    # the GPL archives the key ships in may already be on this machine
    from_archive = key_from_archives(upd_path, payload)
    if from_archive and key_works(upd_path, from_archive):
        util.ok(f"key: {from_archive} (extracted from an archive)")
        return from_archive

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


# The names the firmware uses, and how an ISO9660 extraction can mangle them:
# without Rock Ridge every name comes out upper case with a ";1" version
# suffix, so "pdj/rbp" arrives as "PDJ/RBP;1".  Everything below is written to
# cope with that rather than to assume it did not happen.
ISO_VERSION_SUFFIX = re.compile(r";\d+$")
EXPECTED_FILES = {
    "pdj/rbp": ("rbp",),
    "images/rootfs.cramfs": ("rootfs.cramfs",),
    "images/gui.tar.gz": ("gui.tar.gz", "gui.tgz"),
}
EXPECTED_DIRS = ("pdj", "images", "lib", "usr", "gui", "etc")


def strip_version_suffixes(root: Path) -> list[str]:
    """Rename `NAME;1` to `NAME` throughout a tree (bottom up)."""
    renamed = []
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        clean = ISO_VERSION_SUFFIX.sub("", path.name)
        if clean == path.name:
            continue
        target = path.with_name(clean)
        try:
            if target.exists():
                continue
            path.rename(target)
            renamed.append(f"{path.name} -> {clean}")
        except OSError:
            continue
    return renamed


def looks_uppercase(root: Path, sample: int = 400) -> bool:
    """Does this tree look like an ISO9660 extraction with no Rock Ridge?

    Such an extraction upper-cases everything, and the firmware's real names
    are all lower case - so "no lower-case letter anywhere" is a reliable
    signal, and a safe condition for renaming the lot.
    """
    seen = lowered = 0
    for path in root.rglob("*"):
        name = ISO_VERSION_SUFFIX.sub("", path.name)
        if not any(c.isalpha() for c in name):
            continue
        seen += 1
        if any(c.islower() for c in name):
            lowered += 1
        if seen >= sample:
            break
    return seen >= 3 and lowered == 0


def lowercase_tree(root: Path) -> int:
    """Lower-case every name in a tree, deepest first.  Returns the count.

    Only called when looks_uppercase() says the extraction mangled the case:
    the player's loader looks for `libc.so.6`, not `LIBC.SO.6`, so leaving the
    tree upper case breaks the chroot in a way that is hard to diagnose later.
    """
    renamed = 0
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        lower = path.name.lower()
        if lower == path.name:
            continue
        target = path.with_name(lower)
        if target.exists():
            continue                    # a genuine case clash: leave both
        try:
            path.rename(target)
            renamed += 1
        except OSError:
            continue
    return renamed


def find_file(root: Path, names, max_depth: int = 6) -> Path | None:
    """Case-insensitive search for a file with any of `names`, nearest first."""
    wanted = {n.lower() for n in names}
    best = None
    best_depth = 10 ** 6
    base = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        here = Path(dirpath)
        depth = len(here.parts) - base
        if depth >= max_depth:
            dirnames[:] = []
        for filename in filenames:
            plain = ISO_VERSION_SUFFIX.sub("", filename).lower()
            if plain in wanted and depth < best_depth:
                best, best_depth = here / filename, depth
    return best


def find_dir(root: Path, name: str) -> Path | None:
    """A directory called `name`, whatever its case, at the top of the tree."""
    try:
        for entry in root.iterdir():
            if entry.is_dir() and entry.name.lower() == name.lower():
                return entry
    except OSError:
        pass
    return None


def normalise_iso_tree(iso_tree: Path) -> list[str]:
    """Make an extracted ISO look the way the rest of rb4r5 expects.

    Three things go wrong depending on which extractor ran and whether the ISO
    carries Rock Ridge or Joliet names:

      * `RBP;1` instead of `rbp` (ISO9660 version suffixes),
      * `PDJ/` instead of `pdj/` (ISO9660 upper case),
      * everything one level down inside a wrapper directory.

    All three are fixed here rather than left for the build to trip over.
    """
    notes = []

    stripped = strip_version_suffixes(iso_tree)
    if stripped:
        notes.append(f"removed ISO9660 version suffixes from {len(stripped)} "
                     f"names (e.g. {stripped[0]})")

    # a wrapper directory: one entry at the top which itself holds the firmware
    try:
        entries = [e for e in iso_tree.iterdir() if not e.name.startswith(".")]
    except OSError:
        entries = []
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        if find_dir(inner, "images") or find_dir(inner, "pdj"):
            for item in list(inner.iterdir()):
                target = iso_tree / item.name
                if not target.exists():
                    item.rename(target)
            notes.append(f"flattened the wrapper directory {inner.name}/")
            try:
                inner.rmdir()
            except OSError:
                pass

    # a wholesale upper-case extraction: the whole tree has to come down,
    # not just the directories we happen to look for by name
    if looks_uppercase(iso_tree):
        count = lowercase_tree(iso_tree)
        notes.append(f"the ISO extracted upper case (no Rock Ridge names): "
                     f"lower-cased {count} entries")

    # upper-case directories -> the lower-case names the firmware uses
    for name in EXPECTED_DIRS:
        wanted = iso_tree / name
        if wanted.exists():
            continue
        found = find_dir(iso_tree, name)
        if found:
            found.rename(wanted)
            notes.append(f"{found.name}/ -> {name}/")

    # and the three files everything depends on, wherever they ended up
    for relative, names in EXPECTED_FILES.items():
        target = iso_tree / relative
        if target.exists():
            continue
        found = find_file(iso_tree, names)
        if not found:
            continue
        util.ensure_dir(target.parent)
        try:
            found.rename(target)
        except OSError:
            shutil.copy2(found, target)
        notes.append(f"{found.relative_to(iso_tree)} -> {relative}")

    return notes


def tree_summary(iso_tree: Path, limit: int = 24) -> list[str]:
    """What is actually in the extracted ISO - for when something is missing."""
    lines = []
    try:
        for entry in sorted(iso_tree.iterdir())[:limit]:
            if entry.is_dir():
                children = sorted(c.name for c in entry.iterdir())[:8]
                lines.append(f"  {entry.name}/  ({', '.join(children)}"
                             f"{', …' if len(children) == 8 else ''})")
            else:
                lines.append(f"  {entry.name}  "
                             f"({entry.stat().st_size / 1e6:.1f} MB)")
    except OSError as exc:
        lines.append(f"  (cannot read {iso_tree}: {exc})")
    return lines or ["  (the extracted ISO is empty)"]


def extract_gui(iso_tree: Path, dest: Path) -> str:
    """gui.tar.gz holds the fonts and images - the UI will not start without it."""
    archive = iso_tree / "images/gui.tar.gz"
    if not archive.exists():
        found = find_file(iso_tree, ("gui.tar.gz", "gui.tgz"))
        if not found:
            return "gui.tar.gz not found in the ISO (fonts will be missing)"
        archive = found
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
        found = find_file(iso_tree, ("rootfs.cramfs",))
        if not found:
            raise util.Fail(
                "rootfs.cramfs is not in this ISO.  What it does contain:\n"
                + "\n".join(tree_summary(iso_tree)) +
                "\nIf that does not look like XDJ-RX3 firmware, the .UPD is "
                "for another model or the download is damaged.")
        image = found
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
            ask: bool = False) -> list[str]:
    """Pick the .UPD (if needed) and turn it into a complete payload."""
    payload = util.ensure_dir(cfg.payload)
    primebox = Path(cfg.get("build.primebox"))
    notes: list[str] = []

    if ready(cfg) and not force:
        state = status(cfg)
        return [f"the payload is already unpacked in {payload}"
                + (f" (firmware {state['release']})" if state["release"] else ""),
                "pass --force to unpack it again"]

    # 1. the firmware itself - downloaded from AlphaTheta unless one is already
    #    here.  Nothing to choose, nothing to ask.
    upd_path = locate_upd(cfg, explicit=upd, ask=ask,
                          allow_download=cfg.get("firmware.auto_download", True))

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
    for note in normalise_iso_tree(iso_tree):
        notes.append(f"layout: {note}")

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

    # 5. the player.  Normally it is in the ISO tree; some dumps only carry it
    #    inside the root filesystem, so look there before concluding anything.
    player = iso_tree / "pdj/rbp"
    if not player.exists():
        from_rootfs = find_file(rootfs, ("rbp",))
        if from_rootfs:
            util.ensure_dir(player.parent)
            shutil.copy2(from_rootfs, player)
            os.chmod(player, 0o755)
            notes.append(f"player taken from the root filesystem "
                         f"({from_rootfs.relative_to(rootfs)})")

    if player.exists():
        import hashlib
        digest = hashlib.md5(player.read_bytes()).hexdigest()
        expected = cfg.get("firmware.verify_rbp_md5") or OFFICIAL["rbp_md5"]
        notes.append(f"player: {player} "
                     f"({player.stat().st_size / 1e6:.1f} MB, md5 {digest})")
        if expected and digest != expected:
            util.warn(f"the player's md5 is {digest}, not the stock v1.20 "
                      f"{expected} - the published patch sets may not apply")
        else:
            notes.append("player md5 matches the stock v1.20 binary")
    else:
        util.error("the player (pdj/rbp) is not in this ISO.")
        print("\nWhat the extracted ISO does contain:")
        for line in tree_summary(iso_tree):
            print(line)
        print(f"\nLook for the player yourself with:\n"
              f"    sudo find {iso_tree} -iname 'rbp*'\n"
              f"If it is there under another name, copy it to "
              f"{iso_tree}/pdj/rbp and re-run the build.\n"
              f"If it is not there at all, the .UPD is for a different model "
              f"or the download is damaged - delete\n"
              f"    {cfg.payload}/firmware\n"
              f"and run `launch.py firmware --force` to fetch it again.")
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


def describe(cfg, verbose: bool = False) -> list[str]:
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

    cache = state["payload"] / "firmware"
    downloads = sorted(cache.glob("*")) if cache.is_dir() else []
    if downloads:
        rows.append("downloaded:")
        rows += [f"  {d.name}  ({d.stat().st_size / 1e6:.1f} MB)"
                 for d in downloads if d.is_file()]

    if state["iso_tree"].is_dir() and (verbose or not state["have_player"]):
        rows.append(f"inside {state['iso_tree']}:")
        rows += tree_summary(state["iso_tree"])
        if not state["have_player"]:
            found = find_file(state["iso_tree"], ("rbp",))
            rows.append(f"  a file called 'rbp' was "
                        + (f"found at {found.relative_to(state['iso_tree'])}"
                           if found else "NOT found anywhere in the tree"))
    if not ready(cfg):
        rows.append("run: sudo python3 launch.py firmware --force")
    return rows
