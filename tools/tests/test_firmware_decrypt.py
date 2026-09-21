#!/usr/bin/env python3
"""Offline checks for the firmware pipeline: decryption, key choice, detection.

Builds a synthetic .UPD with the documented scheme (AES-256-CBC per 512-byte
sector, IV = LE32(sector), 16-byte plaintext trailer), then checks that rb4r5
decrypts it byte for byte, picks the right key out of several candidates, and
refuses a wrong one.  No firmware and no real key involved.

Run:  python3 tools/tests/test_firmware_decrypt.py
"""
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from rb4r5 import firmware  # noqa: E402

SECTOR = firmware.SECTOR
failures = []


def check(label, got, want=True):
    if got != want:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label}")


def _openssl(mode: str, key: bytes, iv: bytes, data: bytes, decrypt: bool) -> bytes:
    """One independent AES implementation: the openssl binary."""
    cmd = ["openssl", "enc", "-d" if decrypt else "-e", f"-aes-256-{mode}",
           "-nopad", "-K", key.hex()]
    if iv is not None:
        cmd += ["-iv", iv.hex()]
    proc = subprocess.run(cmd, input=data, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=True)
    return proc.stdout


def reference_encrypt(plain: bytes, key: bytes) -> bytes:
    """The scheme as rx3dec documents it, sector by sector (the obvious way)."""
    out = b""
    for sector in range(len(plain) // SECTOR):
        iv = struct.pack("<I", sector) + bytes(12)
        out += _openssl("cbc", key, iv,
                        plain[sector * SECTOR:(sector + 1) * SECTOR], False)
    return out


def reference_decrypt(cipher: bytes, key: bytes) -> bytes:
    out = b""
    for sector in range(len(cipher) // SECTOR):
        iv = struct.pack("<I", sector) + bytes(12)
        out += _openssl("cbc", key, iv,
                        cipher[sector * SECTOR:(sector + 1) * SECTOR], True)
    return out


# ---------------------------------------------------------------- fixtures
SECTORS = 96
plain = bytearray(os.urandom(SECTORS * SECTOR))
# an ISO 9660 primary volume descriptor lives at sector 64
pvd = bytearray(SECTOR)
pvd[0] = 1
pvd[1:6] = b"CD001"
pvd[6] = 1
pvd[8:40] = b"XDJRX3".ljust(32)
plain[64 * SECTOR:65 * SECTOR] = pvd
plain = bytes(plain)

KEY_TEXT = b"thisisnotarealkey_0123456789abcdef\nignored second line\n"
key = firmware.derive_key(KEY_TEXT)
check("key is 32 bytes", len(key), 32)
check("key is the first 31 bytes of line 1, NUL padded",
      key, KEY_TEXT[:31].ljust(32, b"\0"))

cipher = reference_encrypt(plain, key)
upd_bytes = cipher + b"XDJRX3\0" + bytes(9)      # 16-byte plaintext trailer

# --------------------------------------------------- the decryption itself
check("our CBC matches a block-by-block reference",
      firmware.decrypt_body(cipher, key), reference_decrypt(cipher, key))
check("round trip is exact", firmware.decrypt_body(cipher, key), plain)
check("single-sector decrypt matches",
      firmware.decrypt_sector(cipher[64 * SECTOR:65 * SECTOR], key, 64),
      bytes(pvd))

wrong = firmware.derive_key(b"a completely different key\n")
check("a wrong key does not produce the ISO signature",
      firmware.decrypt_sector(cipher[64 * SECTOR:65 * SECTOR], wrong, 64)[1:6]
      != b"CD001")

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    upd = tmp / "XDJ-RX3.UPD"
    upd.write_bytes(upd_bytes)

    # looks_like_upd also insists on a realistic size, so check it against a
    # full-size stand-in rather than the little crypto fixture
    realistic = tmp / "XDJ-RX3-fullsize.UPD"
    realistic.write_bytes(b"\0" * (4096 * SECTOR) + b"XDJRX3\0" + bytes(9))
    ok, note = firmware.looks_like_upd(realistic)
    check("a real-sized .UPD is recognised as 512-aligned + trailer", ok)
    check("the size is reported", note.endswith("MB"))
    bad = tmp / "truncated.UPD"
    bad.write_bytes(upd_bytes[:-3])
    check("a misaligned file is rejected", firmware.looks_like_upd(bad)[0], False)
    small = tmp / "small.UPD"
    small.write_bytes(b"x" * 1024)
    check("a tiny file is rejected", firmware.looks_like_upd(small)[0], False)

    # ------------------------------------------- picking the key by itself
    (tmp / "decoy1.key").write_bytes(b"nope\n")
    (tmp / "aes256.key").write_bytes(KEY_TEXT)
    (tmp / "decoy2.key").write_bytes(b"also wrong, and much longer than 31 bytes\n")

    check("the right key validates", firmware.key_works(upd, tmp / "aes256.key"))
    check("a decoy key does not",
          firmware.key_works(upd, tmp / "decoy1.key"), False)
    check("an empty file is not a key",
          firmware.key_works(upd, bad), False)

    payload = tmp / "payload"
    payload.mkdir()
    found = firmware.resolve_key(upd, payload, tmp / "PrimeBox", ask=False)
    check("resolve_key finds the working key among decoys",
          found.name, "aes256.key")

    # a wrong explicit key must fail loudly rather than produce garbage
    try:
        firmware.resolve_key(upd, payload, tmp / "PrimeBox",
                             explicit=tmp / "decoy1.key", ask=False)
        check("an explicit wrong key is refused", False)
    except Exception as exc:
        check("an explicit wrong key is refused", "does not decrypt" in str(exc))

    # ------------------------------------------------- decrypt to a file
    iso = payload / "XDJRX3.iso"
    result = firmware.decrypt_upd(upd, tmp / "aes256.key", iso)
    check("the ISO was written", iso.exists())
    check("the ISO is the plaintext", iso.read_bytes(), plain)
    check("the trailer was read", result["trailer"], "XDJRX3")

    # and a wrong key is caught before anything is written
    iso2 = payload / "wrong.iso"
    (tmp / "wrong.key").write_bytes(b"still not the key\n")
    try:
        firmware.decrypt_upd(upd, tmp / "wrong.key", iso2)
        check("decrypting with a wrong key fails", False)
    except Exception as exc:
        check("decrypting with a wrong key fails", "not an ISO image" in str(exc))
    check("nothing was written for the bad attempt", iso2.exists(), False)

print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("all firmware decryption checks passed")
