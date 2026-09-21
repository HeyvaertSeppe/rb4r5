#!/usr/bin/env python3
"""End-to-end check of the firmware pipeline, with a synthetic firmware file.

Builds a fake .UPD whose plaintext contains an "ISO" carrying a gui.tar.gz, a
real cramfs rootfs and a player binary, then runs firmware.prepare() exactly as
the launcher does and checks the payload that comes out.  The ISO unpacking
step is stubbed (it needs bsdtar/7z or a loop mount, i.e. a real machine);
everything else is the production code path.

Run:  python3 tools/tests/test_firmware_pipeline.py
"""
import io
import os
import struct
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))

import _cramfs_fixture  # noqa: E402
from rb4r5 import config, firmware, util  # noqa: E402

SECTOR = firmware.SECTOR
failures = []


def check(label, got, want=True):
    if got != want:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label}")


def encrypt(plain: bytes, key: bytes) -> bytes:
    """The device's scheme, via openssl (independent of our implementation)."""
    out = b""
    for sector in range(len(plain) // SECTOR):
        iv = struct.pack("<I", sector) + bytes(12)
        proc = subprocess.run(
            ["openssl", "enc", "-e", "-aes-256-cbc", "-nopad",
             "-K", key.hex(), "-iv", iv.hex()],
            input=plain[sector * SECTOR:(sector + 1) * SECTOR],
            stdout=subprocess.PIPE, check=True)
        out += proc.stdout
    return out


# ------------------------------------------------------------------ fixtures
ROOTFS = {
    "lib": {
        "ld-linux.so.3": b"\x7fELF" + b"fake loader" * 100,
        "libc.so.6": b"\x7fELF" + b"fake libc" * 200,
        "libc.so": ("symlink", "libc.so.6"),
    },
    "usr": {
        "bin": {"edb_streamd": b"\x7fELF" + b"fake devicesql" * 50},
        "local": {"pdj": {"aes256.key": b"the device's own copy\n"}},
    },
}

GUI_FILES = {
    "pset/fontdata/font.bin": b"font data" * 100,
    "system/fontdata/sys.bin": b"system font" * 50,
    "imagedata/images.bin": b"image data" * 200,
}

KEY_TEXT = b"pipeline-test-key-not-a-real-one\nsecond line\n"
key = firmware.derive_key(KEY_TEXT)

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    iso_src = tmp / "iso-contents"
    (iso_src / "images").mkdir(parents=True)
    (iso_src / "pdj").mkdir(parents=True)

    # the firmware's own pieces
    (iso_src / "images/rootfs.cramfs").write_bytes(_cramfs_fixture.build(ROOTFS))
    (iso_src / "images/release.txt").write_text("1.20\n")
    (iso_src / "pdj/rbp").write_bytes(b"\x7fELF" + b"fake player" * 500)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, payload in GUI_FILES.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    (iso_src / "images/gui.tar.gz").write_bytes(buffer.getvalue())

    # a "firmware image": anything, as long as the ISO signature is at sector 64
    plain = bytearray(os.urandom(200 * SECTOR))
    plain[64 * SECTOR + 1:64 * SECTOR + 6] = b"CD001"
    plain = bytes(plain)
    upd = tmp / "XDJ-RX3.UPD"
    upd.write_bytes(encrypt(plain, key) + b"XDJRX3\0" + bytes(9))
    (tmp / "aes256.key").write_bytes(KEY_TEXT)

    # ------------------------------------------------------------ the run
    cfg = config.load("/nonexistent-rb4r5.json")
    cfg.set("paths.payload", str(tmp / "payload"))
    cfg.set("build.primebox", str(tmp / "PrimeBox"))

    def fake_extract_iso(iso, dest):
        """Stand in for bsdtar/7z/mount: the ISO contents are already on disk."""
        check("prepare() decrypted the ISO before unpacking it", Path(iso).exists())
        util.run(["cp", "-a", f"{iso_src}/.", str(util.ensure_dir(dest))])
        return "test stub"

    firmware.extract_iso = fake_extract_iso

    check("nothing is ready to begin with", firmware.ready(cfg), False)
    notes = firmware.prepare(cfg, upd=upd, ask=False)
    print("\n".join(f"     {n}" for n in notes))

    payload = Path(cfg.get("paths.payload"))
    check("prepare() reports ready", firmware.ready(cfg))
    check("the player was unpacked", (payload / "XDJRX3/pdj/rbp").exists())
    check("the ISO was kept", (payload / "XDJRX3.iso").exists())
    check("the decrypted ISO is the plaintext",
          (payload / "XDJRX3.iso").read_bytes(), plain)

    # gui.tar.gz
    check("gui fonts were unpacked",
          (payload / "XDJRX3/gui/pset/fontdata/font.bin").exists())
    check("gui image data was unpacked",
          (payload / "XDJRX3/gui/imagedata/images.bin").read_bytes(),
          GUI_FILES["imagedata/images.bin"])

    # rootfs.cramfs, through our own reader
    rootfs = payload / "XDJRX3-rootfs"
    check("the soft-float loader came out",
          (rootfs / "lib/ld-linux.so.3").read_bytes(),
          ROOTFS["lib"]["ld-linux.so.3"])
    check("a rootfs symlink survived", (rootfs / "lib/libc.so").is_symlink())
    check("the symlink target is right",
          os.readlink(rootfs / "lib/libc.so"), "libc.so.6")
    check("a nested rootfs file came out",
          (rootfs / "usr/bin/edb_streamd").exists())

    # the key is remembered so later runs never ask
    check("the key was stored in the payload", (payload / "aes256.key").exists())
    check("the stored key is the right one",
          (payload / "aes256.key").read_bytes(), KEY_TEXT)
    check("the stored key is not world readable",
          oct((payload / "aes256.key").stat().st_mode)[-3:], "600")

    # running it again must be a no-op, and must not need a key or a prompt
    again = firmware.prepare(cfg, ask=False)
    check("a second run is a no-op", any("already unpacked" in n for n in again))

    # and it finds the key by itself when asked to redo the work
    os.remove(payload / "XDJRX3.iso")
    notes = firmware.prepare(cfg, upd=upd, ask=False, force=True)
    check("a forced re-run finds the stored key without asking",
          any("aes256.key" in n for n in notes))
    check("and produces a ready payload again", firmware.ready(cfg))

    # describe() is what doctor prints
    described = "\n".join(firmware.describe(cfg))
    check("describe() reports the version", "1.20" in described)

    # ---- an ISO with no player in it, but one inside the root filesystem ----
    # (this is what "pdj/rbp is not in this ISO" used to die on)
    payload2 = tmp / "payload2"
    cfg.set("paths.payload", str(payload2))
    iso_src2 = tmp / "iso-no-player"
    (iso_src2 / "images").mkdir(parents=True)
    rootfs_with_player = dict(ROOTFS)
    rootfs_with_player["root"] = {"pdj": {"rbp": b"\x7fELF" + b"player" * 300}}
    (iso_src2 / "images/rootfs.cramfs").write_bytes(
        _cramfs_fixture.build(rootfs_with_player))
    (iso_src2 / "images/release.txt").write_text("1.20\n")
    (iso_src2 / "images/gui.tar.gz").write_bytes(buffer.getvalue())

    def fake_extract_iso2(iso, dest):
        util.run(["cp", "-a", f"{iso_src2}/.", str(util.ensure_dir(dest))])
        return "test stub (no pdj/rbp in the ISO)"

    firmware.extract_iso = fake_extract_iso2
    notes = firmware.prepare(cfg, upd=upd, ask=False)
    check("a player inside the rootfs is used when the ISO has none",
          (payload2 / "XDJRX3/pdj/rbp").exists())
    check("and it is the one from the rootfs",
          (payload2 / "XDJRX3/pdj/rbp").read_bytes(),
          rootfs_with_player["root"]["pdj"]["rbp"])
    check("the payload is then complete", firmware.ready(cfg))
    check("and it says where the player came from",
          any("root filesystem" in n for n in notes))

print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("all firmware pipeline checks passed")
