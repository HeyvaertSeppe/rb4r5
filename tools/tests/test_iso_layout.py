#!/usr/bin/env python3
"""The ISO layouts a real extraction can produce, and normalising them.

An ISO9660 image without Rock Ridge extracts as upper case with ";1" version
suffixes, and some extractors add a wrapper directory.  This is what made
"pdj/rbp is not in this ISO" happen on a perfectly good firmware image, so all
three shapes are checked here.

Run:  python3 tools/tests/test_iso_layout.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from rb4r5 import firmware  # noqa: E402

failures = []


def check(label, got, want=True):
    if got != want:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label}")


def build(root: Path, style: str) -> Path:
    """Lay out an extracted ISO the way a given extractor would."""
    root.mkdir(parents=True)
    if style == "rockridge":                      # loop mount / bsdtar + RR
        names = {"pdj/rbp": b"PLAYER", "images/rootfs.cramfs": b"CRAMFS",
                 "images/gui.tar.gz": b"GUI", "images/release.txt": b"1.20\n",
                 "lib/libc.so.6": b"LIBC", "usr/lib/libstdc++.so.6": b"STDC"}
    elif style == "iso9660":                      # no Rock Ridge: UPPER + ;1
        names = {"PDJ/RBP;1": b"PLAYER", "IMAGES/ROOTFS.CRAMFS;1": b"CRAMFS",
                 "IMAGES/GUI.TAR.GZ;1": b"GUI", "IMAGES/RELEASE.TXT;1": b"1.20\n",
                 "LIB/LIBC.SO.6;1": b"LIBC", "USR/LIB/LIBSTDC++.SO.6;1": b"STDC"}
    elif style == "wrapped":                      # everything one level down
        names = {"XDJRX3/pdj/rbp": b"PLAYER",
                 "XDJRX3/images/rootfs.cramfs": b"CRAMFS",
                 "XDJRX3/images/gui.tar.gz": b"GUI",
                 "XDJRX3/lib/libc.so.6": b"LIBC"}
    elif style == "elsewhere":                    # the player somewhere odd
        names = {"firmware/player/rbp": b"PLAYER",
                 "images/rootfs.cramfs": b"CRAMFS",
                 "images/gui.tar.gz": b"GUI"}
    else:
        names = {"readme.txt": b"not firmware at all"}
    for relative, payload in names.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return root


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)

    # 1. a clean extraction must be left alone
    tree = build(tmp / "clean", "rockridge")
    notes = firmware.normalise_iso_tree(tree)
    check("a Rock Ridge extraction needs no changes", notes, [])
    check("and the player is where it should be",
          (tree / "pdj/rbp").read_bytes(), b"PLAYER")

    # 2. the case that broke: upper case with version suffixes
    tree = build(tmp / "iso9660", "iso9660")
    check("before: pdj/rbp is missing", (tree / "pdj/rbp").exists(), False)
    notes = firmware.normalise_iso_tree(tree)
    print("     " + "\n     ".join(notes))
    check("the player is found and put in place",
          (tree / "pdj/rbp").read_bytes(), b"PLAYER")
    check("the root filesystem image too",
          (tree / "images/rootfs.cramfs").read_bytes(), b"CRAMFS")
    check("the gui archive too",
          (tree / "images/gui.tar.gz").read_bytes(), b"GUI")
    check("the library directories are lower-cased",
          (tree / "lib/libc.so.6").read_bytes(), b"LIBC")
    check("nested names lose their ;1 as well",
          (tree / "usr/lib/libstdc++.so.6").read_bytes(), b"STDC")
    check("release.txt is readable, so the version check works",
          (tree / "images/release.txt").read_text().strip(), "1.20")

    # 3. a wrapper directory
    tree = build(tmp / "wrapped", "wrapped")
    notes = firmware.normalise_iso_tree(tree)
    check("a wrapper directory is flattened",
          (tree / "pdj/rbp").read_bytes(), b"PLAYER")
    check("and reported", any("flattened" in n for n in notes))

    # 4. the player hiding somewhere unexpected
    tree = build(tmp / "elsewhere", "elsewhere")
    firmware.normalise_iso_tree(tree)
    check("a player in an unexpected place is still found",
          (tree / "pdj/rbp").read_bytes(), b"PLAYER")

    # 5. something that is not firmware: say what it is instead of guessing
    tree = build(tmp / "junk", "junk")
    firmware.normalise_iso_tree(tree)
    check("no player is invented", (tree / "pdj/rbp").exists(), False)
    summary = firmware.tree_summary(tree)
    check("the summary names what was there",
          any("readme.txt" in line for line in summary))
    try:
        firmware.extract_rootfs(tree, tmp / "out")
        check("a missing rootfs is reported with the tree", False)
    except Exception as exc:
        check("a missing rootfs is reported with the tree",
              "rootfs.cramfs is not in this ISO" in str(exc) and
              "readme.txt" in str(exc))

    # 6. case-insensitive lookups on their own
    tree = build(tmp / "lookup", "iso9660")
    firmware.strip_version_suffixes(tree)
    check("find_file ignores case",
          firmware.find_file(tree, ("rbp",)).name, "RBP")
    check("find_dir ignores case", firmware.find_dir(tree, "images").name,
          "IMAGES")
    check("find_file returns None when there is nothing",
          firmware.find_file(tree, ("nothing-here",)), None)

    # ---------------------------------------------------------------------
    # 7. the real XDJ-RX3 layout: the player and the fonts are tarballs in
    #    images/, not directories.  This is the exact shape that produced
    #    "the player (pdj/rbp) is not in this ISO".
    # ---------------------------------------------------------------------
    import io
    import tarfile

    def make_tar(path: Path, contents: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(path, "w:gz") as tar:
            for name, payload in contents.items():
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                info.mode = 0o755
                tar.addfile(info, io.BytesIO(payload))

    # (a) the tarball carries its own pdj/ directory
    tree = tmp / "real-wrapped"
    (tree / "images").mkdir(parents=True)
    make_tar(tree / "images/pdj.tar.gz",
             {"pdj/rbp": b"PLAYER", "pdj/kill_daemon": b"HELPER"})
    make_tar(tree / "images/gui.tar.gz",
             {"pset/fontdata/f.bin": b"FONT", "imagedata/i.bin": b"IMG"})
    note = firmware.extract_player_tar(tree)
    check("pdj.tar.gz is unpacked", (tree / "pdj/rbp").read_bytes(), b"PLAYER")
    check("what sits beside the player comes too",
          (tree / "pdj/kill_daemon").exists())
    check("and it notices the archive's own directory",
          "carried its own directory" in note)
    firmware.extract_gui(tree)
    check("gui.tar.gz still lands in gui/",
          (tree / "gui/pset/fontdata/f.bin").read_bytes(), b"FONT")

    # (b) the tarball has no top directory: a bare rbp
    tree = tmp / "real-bare"
    (tree / "images").mkdir(parents=True)
    make_tar(tree / "images/pdj.tar.gz", {"rbp": b"PLAYER", "edb": b"DB"})
    firmware.extract_player_tar(tree)
    check("a bare rbp lands in pdj/ anyway",
          (tree / "pdj/rbp").read_bytes(), b"PLAYER")
    check("and so does its neighbour", (tree / "pdj/edb").exists())

    # (c) gui.tar.gz that carries its own gui/ must not become gui/gui/
    tree = tmp / "gui-wrapped"
    (tree / "images").mkdir(parents=True)
    make_tar(tree / "images/gui.tar.gz", {"gui/pset/f.bin": b"FONT"})
    firmware.extract_gui(tree)
    check("a wrapped gui.tar.gz does not nest",
          (tree / "gui/pset/f.bin").read_bytes(), b"FONT")
    check("and gui/gui/ was not created", (tree / "gui/gui").exists(), False)

    # (d) no tarball at all: say so, do not crash
    tree = tmp / "no-tars"
    (tree / "images").mkdir(parents=True)
    check("a missing pdj.tar.gz is reported",
          "not found" in firmware.extract_player_tar(tree))

print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("all ISO layout checks passed")
