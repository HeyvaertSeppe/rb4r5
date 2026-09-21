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
    check("AlphaTheta's own URL is recorded",
          firmware.OFFICIAL["url"].startswith(
              "https://downloads.support.alphatheta.com/"))
    check("the configured source is a direct .UPD",
          cfg.get("firmware.url").lower().endswith(".upd"))
    check("AlphaTheta's download is kept as a fallback",
          any("alphatheta.com" in u for u in firmware.mirrors(cfg)))
    check("auto download is on by default", cfg.get("firmware.auto_download"))
    check("the v1.20 size is recorded", cfg.get("firmware.expect_upd_size"),
          69171216)

    # ---------------------------------------------------------- mirrors
    order = firmware.mirrors(cfg)
    check("the configured source is tried first",
          order[0], cfg.get("firmware.url"))
    check("with fallbacks behind it", len(order) >= 3)
    check("the Drive mirror is expressed as gdrive:<id>",
          any(u.startswith("gdrive:") for u in order))
    cfg.set("firmware.mirrors", ["https://example.invalid/fw.zip"])
    check("configured mirrors come before the built-in ones",
          firmware.mirrors(cfg)[1], "https://example.invalid/fw.zip")
    cfg.set("firmware.mirrors", ["gdrive:1FvztdfmpOvzqSXHDSo0eWhxe4RaEP5Ul"])

    # the downloaded file keeps a meaningful name
    check("a .UPD URL keeps its name",
          firmware.download_name("https://example.invalid/XDJRX3.UPD", cfg),
          "XDJRX3.UPD")
    check("a .zip URL keeps its name",
          firmware.download_name("https://example.invalid/XDJ-RX3_v120.zip", cfg),
          "XDJ-RX3_v120.zip")
    check("a query string does not confuse it",
          firmware.download_name("https://x.invalid/fw.UPD?token=abc", cfg),
          "fw.UPD")
    check("an opaque URL falls back to the configured name",
          firmware.download_name("https://example.invalid/download?id=7", cfg),
          "XDJ-RX3_v120.zip")
    check("a gdrive source falls back too",
          firmware.download_name("gdrive:abc123", cfg), "XDJ-RX3_v120.zip")

    # an HTML consent page must never be mistaken for firmware
    page = tmp / "consent.html"
    page.write_bytes(b"<!DOCTYPE html>\n<html><body>Google Drive</body></html>")
    check("an HTML page is recognised", firmware._looks_like_html(page))
    real = tmp / "real.bin"
    real.write_bytes(b"\x00\x01\x02" * 1000)
    check("a binary is not", firmware._looks_like_html(real), False)

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

    # ------------------------------------------- when every source fails
    payload4 = tmp / "payload4"
    payload4.mkdir()
    cfg.set("paths.payload", str(payload4))
    real_download = firmware.download_file

    def dead_download(url, dest, expect_size=0):
        raise firmware.util.Fail(f"could not download {url}")

    firmware.download_file = dead_download
    try:
        firmware.fetch_official(cfg)
        check("every source failing is reported clearly", False)
    except Exception as exc:
        check("every source failing is reported clearly",
              "none of the firmware sources worked" in str(exc) and
              "alphatheta.com" in str(exc))
    firmware.download_file = real_download

    # a source that returns a web page is discarded, not decrypted
    def html_download(url, dest, expect_size=0):
        Path(dest).write_bytes(b"<!DOCTYPE html><html>nope</html>")
        return Path(dest)

    firmware.download_file = html_download
    try:
        firmware.fetch_official(cfg)
        check("a web page is not accepted as firmware", False)
    except Exception as exc:
        check("a web page is not accepted as firmware",
              "none of the firmware sources worked" in str(exc))
    firmware.download_file = real_download

    # a download that is the bare .UPD (not a zip) is handled too
    def upd_download(url, dest, expect_size=0):
        Path(dest).write_bytes(FAKE_UPD)
        return Path(dest)

    firmware.download_file = upd_download
    got = firmware.fetch_official(cfg, force=True)
    check("a bare .UPD download is accepted (under one canonical name)",
          got.name, "XDJ-RX3.UPD")
    check("and is what was downloaded", got.read_bytes(), FAKE_UPD)

    # something that is not firmware at all is rejected before decryption
    def junk_download(url, dest, expect_size=0):
        Path(dest).write_bytes(b"\x00" * 4096)
        return Path(dest)

    firmware.download_file = junk_download
    try:
        firmware.fetch_official(cfg, force=True)
        check("junk is rejected before decryption", False)
    except Exception as exc:
        check("junk is rejected before decryption",
              "not firmware" in str(exc))
    firmware.download_file = real_download

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
