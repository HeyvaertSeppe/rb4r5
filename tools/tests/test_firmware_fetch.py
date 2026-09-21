#!/usr/bin/env python3
"""Offline checks for how the firmware is obtained (no network used).

Covers the part that replaced the prompt: where the .UPD comes from, in what
order, extracting it from AlphaTheta's zip, and pulling the key out of an
archive the user already has.  The download itself is stubbed - the point is
that it is only reached when nothing local exists.

Run:  python3 tools/tests/test_firmware_fetch.py
"""
import io
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from rb4r5 import config, firmware  # noqa: E402

failures = []


def check(label, got, want=True):
    if got != want:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label}")


FAKE_UPD = b"\0" * (4096 * firmware.SECTOR) + b"XDJRX3\0" + bytes(9)


def make_zip(path: Path, member: str = "XDJ-RX3_v120/XDJ-RX3.UPD") -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(member, FAKE_UPD)
    return path


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    cfg = config.load("/nonexistent-rb4r5.json")
    payload = tmp / "payload"
    cfg.set("paths.payload", str(payload))

    # ---------------------------------------------------------- the baked-in source
    check("the official URL is AlphaTheta's",
          firmware.OFFICIAL["url"].startswith(
              "https://downloads.support.alphatheta.com/"))
    check("the config carries the same URL",
          cfg.get("firmware.url"), firmware.OFFICIAL["url"])
    check("auto download is on by default", cfg.get("firmware.auto_download"))
    check("the v1.20 size is recorded", cfg.get("firmware.expect_upd_size"),
          69171216)

    # ---------------------------------------------------------- zip handling
    archive = make_zip(tmp / "XDJ-RX3_v120.zip")
    extracted = firmware.upd_from_zip(archive, tmp / "out")
    check("the .UPD comes out of the zip", extracted.name, "XDJ-RX3.UPD")
    check("and is intact", extracted.read_bytes(), FAKE_UPD)

    odd = make_zip(tmp / "odd.zip", member="somewhere/else/FIRMWARE.upd")
    check("a differently named member still works",
          firmware.upd_from_zip(odd, tmp / "out2").name, "FIRMWARE.upd")

    empty = tmp / "empty.zip"
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("readme.txt", "nothing here")
    try:
        firmware.upd_from_zip(empty, tmp / "out3")
        check("a zip with no firmware is rejected", False)
    except Exception as exc:
        check("a zip with no firmware is rejected", "contains no .UPD" in str(exc))

    # ---------------------------------------------------------- where it looks
    downloads = []

    def fake_fetch(cfg_, force=False):
        cache = Path(cfg_.get("paths.payload")) / "firmware"
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / "XDJ-RX3.UPD"
        target.write_bytes(FAKE_UPD)
        downloads.append(target)
        return target

    real_fetch = firmware.fetch_official
    firmware.fetch_official = fake_fetch
    # keep the machine-wide scan out of it: this test is about the order of
    # the deliberate sources, not about what happens to be lying in /tmp
    real_find = firmware.find_upd_files
    firmware.find_upd_files = lambda extra=None: []

    # 1. nothing anywhere -> the official download
    got = firmware.locate_upd(cfg)
    check("with nothing local it downloads", len(downloads), 1)
    check("and returns the downloaded file", got.name, "XDJ-RX3.UPD")

    # 2. already downloaded -> reused, no second download
    got = firmware.locate_upd(cfg)
    check("a second call reuses the download", len(downloads), 1)
    check("returning the cached file", got, payload / "firmware/XDJ-RX3.UPD")

    # 3. an explicit file wins over everything
    mine = tmp / "my-own.UPD"
    mine.write_bytes(FAKE_UPD)
    check("an explicit --upd wins", firmware.locate_upd(cfg, explicit=mine), mine)

    # 4. an explicit zip is unpacked
    got = firmware.locate_upd(cfg, explicit=archive)
    check("an explicit zip is unpacked", got.name, "XDJ-RX3.UPD")

    # 5. a zip sitting in the payload dir is used instead of downloading
    payload2 = tmp / "payload2"
    cfg.set("paths.payload", str(payload2))
    (payload2 / "firmware").mkdir(parents=True)
    make_zip(payload2 / "firmware/XDJ-RX3_v120.zip")
    before = len(downloads)
    got = firmware.locate_upd(cfg)
    check("a local zip is used, not the network", len(downloads), before)
    check("and unpacked to the cache", got.name, "XDJ-RX3.UPD")

    # 6. offline with nothing available must explain itself, not hang
    payload3 = tmp / "payload3"
    cfg.set("paths.payload", str(payload3))
    payload3.mkdir()
    cfg.set("firmware.auto_download", False)
    try:
        firmware.locate_upd(cfg, allow_download=False)
        check("offline with nothing available fails clearly", False)
    except Exception as exc:
        check("offline with nothing available fails clearly",
              "no firmware available" in str(exc) and
              "drop it in" in str(exc))
    cfg.set("firmware.auto_download", True)

    # 7. a file found on the machine is the last resort, after the download
    firmware.find_upd_files = lambda extra=None: [
        {"path": mine, "size": len(FAKE_UPD), "mtime": 0, "plausible": True,
         "note": ""}]
    cfg.set("firmware.auto_download", False)
    check("a found file is used when downloading is off",
          firmware.locate_upd(cfg, allow_download=False), mine)
    cfg.set("firmware.auto_download", True)

    firmware.fetch_official = real_fetch
    firmware.find_upd_files = real_find

    # ---------------------------------------------------------- key in an archive
    key_text = b"a-key-that-is-not-real-0123456\n"
    gpl = tmp / "pioneerdj_xdj_rx3.tar.gz"
    with tarfile.open(gpl, "w:gz") as tf:
        for name, payload_bytes in (
                ("src/README", b"gpl sources"),
                ("src/usr/local/pdj/aes256.key", key_text),
                ("src/other.bin", b"\0" * 1000)):
            info = tarfile.TarInfo(name)
            info.size = len(payload_bytes)
            tf.addfile(info, io.BytesIO(payload_bytes))

    upd_here = tmp / "XDJ-RX3.UPD"
    upd_here.write_bytes(FAKE_UPD)
    found = firmware.key_from_archives(upd_here, payload3)
    check("the key is found inside a GPL tarball", found is not None)
    check("and has the right contents", found.read_bytes(), key_text)

    none_here = firmware.key_from_archives(upd_here, tmp / "payload-empty")
    check("no archive, no key", none_here is None)

print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("all firmware fetch checks passed")
