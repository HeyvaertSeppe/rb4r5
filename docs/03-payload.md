# 03 — The firmware

rb4r5 contains **no Pioneer/AlphaTheta firmware, no `rbp` binary, no decryption
key and no music database**. What it does contain is the *address* of the
firmware this port is built around, so there is nothing for you to fetch or
pick:

```python
# rb4r5/firmware.py
OFFICIAL = {
    "version": "1.20",
    "url": "https://downloads.support.alphatheta.com/firmwares/"
           "all-in-one-dj-systems/XDJ-RX3/XDJ-RX3_v120.zip",
    "member": "XDJ-RX3_v120/XDJ-RX3.UPD",
    "upd_size": 69_171_216,
    "rbp_md5": "4f2efcfc0c9e3f539289f863acfddcc6",
}
```

On a first run — including the very first `sudo python3 launch.py` — the
launcher downloads that package from AlphaTheta's own server, pulls the `.UPD`
out of the zip, decrypts it, unpacks it and checks that the player inside is
the stock v1.20 binary. You are not asked anything.

```sh
PI$ sudo python3 launch.py firmware
[==] downloading the XDJ-RX3 v1.20 firmware (~66 MB) from AlphaTheta
[ok] downloaded /opt/rb4r5/payload/firmware/XDJ-RX3_v120.zip (66.0 MB)
[==] extracting XDJ-RX3_v120/XDJ-RX3.UPD from XDJ-RX3_v120.zip
[ok] firmware: /opt/rb4r5/payload/firmware/XDJ-RX3.UPD (69.2 MB)
[==] decrypting XDJ-RX3.UPD (69.2 MB)
[ok] ISO 9660 signature found; wrote /opt/rb4r5/payload/XDJRX3.iso
...
     player md5 matches the stock v1.20 binary
```

**Why the file itself is not committed here.** It is AlphaTheta's copyrighted
firmware; putting 69 MB of it in a public repository is redistribution, which
is both unlawful and the fastest way to get the repository taken down. The
upstream projects take the same line (PrimeBox ships a download script, not the
firmware). Fetching it from the vendor at run time gives you the identical
result with none of that risk.

## Doing it without a network

Everything works offline as long as the file is somewhere deliberate. Put
either AlphaTheta's zip or the `.UPD` from it in the payload directory:

```sh
PI$ sudo mkdir -p /opt/rb4r5/payload
PI$ sudo cp /media/usb/XDJ-RX3_v120.zip /opt/rb4r5/payload/
PI$ sudo python3 launch.py firmware          # uses it, downloads nothing
```

Or name it explicitly, which always wins:

```sh
PI$ sudo python3 launch.py firmware --upd /media/usb/XDJ-RX3.UPD
PI$ sudo python3 launch.py firmware --offline      # never touch the network
PI$ sudo python3 launch.py firmware --ask          # show the file picker
```

The order it uses: `--upd` → whatever is already in `/opt/rb4r5/payload`
(including a previous download) → the picker if you asked for it → the official
download → any plausible `.UPD` elsewhere on the machine. A stray file in `/tmp`
deliberately does *not* outrank the firmware the port was built for.

## What happens after you choose## What happens next

```
XDJ-RX3.UPD  ──decrypt──▶ XDJRX3.iso ──unpack──▶ XDJRX3/
  (69 MB, AES-256-CBC)                             ├── lib/  usr/     ISO libraries
                                                   └── images/
                                                       ├── pdj.tar.gz ─┐
                                                       ├── gui.tar.gz ─┤
                                                       └── rootfs.cramfs
                                                                       │
                       the three payloads inside are unpacked in turn: ┘
                                                 XDJRX3/pdj/rbp     the player
                                                 XDJRX3/gui/        fonts + images
                                                 XDJRX3-rootfs/     the soft-float
                                                                    userland
```

| Step | How |
|---|---|
| **Decrypt** | AES-256-CBC per 512-byte sector, IV = `LE32(sector)` padded with zeroes, the last 16 bytes a plaintext trailer — the scheme PrimeBox's `rx3dec` documents, reimplemented in Python so no Rust toolchain is needed. It is done as one ECB pass plus an XOR (CBC decryption *is* ECB decryption XOR the previous ciphertext block), which is why 69 MB takes a second or two. |
| **Verify** | The ISO 9660 signature `CD001` must appear at sector 64. A wrong key cannot fake that, so a bad key is caught before anything is written. |
| **Unpack the ISO** | A loop mount when running as root (keeps Rock Ridge symlinks and modes), otherwise `bsdtar`, otherwise `7z`. |
| **Normalise the layout** | An ISO9660 image without Rock Ridge extracts as `PDJ/RBP;1` rather than `pdj/rbp`, and some extractors add a wrapper directory. The version suffixes are stripped, a wholly upper-cased tree is lower-cased (the player's loader wants `libc.so.6`, not `LIBC.SO.6`), a wrapper directory is flattened, and the player, the root filesystem image and the gui archive are located case-insensitively wherever they ended up — including inside the root filesystem, if the ISO tree has no copy. |
| **Unpack `images/pdj.tar.gz`** | The player is not a loose file in the image — `pdj/` is shipped as a tarball, and `rbp` is inside it, along with what sits beside it on the real device. |
| **Unpack `images/gui.tar.gz`** | The same for `gui/`: fonts, psets and image data. Without them the UI does not start. |
| **Unpack `rootfs.cramfs`** | rb4r5's own cramfs reader ([`rb4r5/cramfs.py`](../rb4r5/cramfs.py)). The Pi's kernel is built **without** `CONFIG_CRAMFS` and Debian no longer ships `cramfsprogs`, so neither mounting nor `cramfsck` is available — PrimeBox's tutorial needs a privileged Docker container here, and rb4r5 does not. |

Everything lands under `/opt/rb4r5/payload` and is skipped on later runs;
`--force` redoes it.

## The key

The one thing that cannot be automated away. AlphaTheta published the firmware
key themselves, inside their own GPL source distribution:

> <https://www.pioneerdj.com/en/support/open-source-code-distribution/gnu-open-source-license/>

Download the XDJ-RX3 archives from that page, unpack them, and look for a file
called **`aes256.key`** (`find . -name 'aes256.key'`). It is the same key the
device's own updater uses — the decrypted rootfs even carries a copy at
`/usr/local/pdj/aes256.key`.

You are not asked *where* it is. rb4r5 looks in the places it could sensibly
be, tries each candidate against your firmware file, and uses the one that
actually decrypts it:

```
<payload>/aes256.key            ← where it is remembered after the first run
/etc/rb4r5/aes256.key
<primebox>/keys/aes256.key
next to the .UPD you picked      ← the easy one: just drop it in the same folder
~/aes256.key
<payload>/XDJRX3-rootfs/usr/local/pdj/aes256.key
anything named *.key nearby
```

Only if none of those decrypts the file does it ask, with the link above. Once
it has one it copies it to `<payload>/aes256.key` (mode 600), so it never asks
again.

```sh
# if you would rather be explicit
PI$ sudo python3 launch.py firmware --upd ~/XDJ-RX3.UPD --key ~/aes256.key
```

## Checking what you got

```sh
PI$ sudo python3 launch.py firmware --show          # -v shows the ISO tree too
payload dir: /opt/rb4r5/payload
firmware ISO: present
player (pdj/rbp): present
gui assets: present
rootfs (soft-float userland): present
firmware version: 1.20
```

```sh
# the player is a 32-bit ARM soft-float binary
PI$ file /opt/rb4r5/payload/XDJRX3/pdj/rbp
  ELF 32-bit LSB executable, ARM, EABI5 version 1 (SYSV), dynamically linked,
  interpreter /lib/ld-linux.so.3

# and your kernel can run it (what `doctor` checks, too)
PI$ /opt/rb4r5/payload/XDJRX3-rootfs/lib/ld-linux.so.3 --version | head -2
  ld.so (GNU libc) stable release version 2.13
```

The stock v1.20 player is md5 `4f2efcfc0c9e3f539289f863acfddcc6`. `launch.py
build` prints the md5 at each stage:

| Artefact | What it is |
|---|---|
| stock `rbp` | your extracted binary, unmodified |
| `work/rbp-audio` | + PrimeBox's patch set (68 patches: panel, USB, keys, audio, waveform) — md5 `3706c68f7242779d46afa09f35a39acf` on v1.20 |
| `work/rbp-pi5` | + the `getPcController` NULL guard (`src/patch/patch-rbp-crashguards.py`) |

## The PrimeBox tooling

`launch.py build` needs two files from [PrimeBox](https://github.com/erhan-/PrimeBox):
the player's patch set (`tools/patch-rbp/rbp_patch.py`) and the DirectFB base
diff. rb4r5 does not duplicate them — it clones the repository into
`/opt/rb4r5/PrimeBox` on the first build. If the Pi has no network, clone it
anywhere and point `build.primebox` at it:

```sh
PI$ sudo python3 launch.py config --set build.primebox=/media/pi/USB/PrimeBox
```

## Why that last player patch

PrimeBox's patched player still SIGSEGVs about a second after start, in the JUCE
`NetworkMonitor` timer, dereferencing an uninitialised singleton
(`ui::IUiObjManager::getPcController()`, `pc=0x0031df70 addr=0x9c`). The guard
makes that function return NULL. It is two 4-byte writes and nothing else.

**Do not apply rb2go's `patch-rbp-debug.py` wholesale.** Besides that guard it
forces `playengine::Player::getTotalLength()` to the "no data" sentinel — a
phone-only workaround. With it, a track loads and shows its title, but PLAY does
nothing and the scrolling waveform never renders, because the deck never reports
a duration. The quirk is available behind `--with-length-quirk` if you ever need
it, and the reasoning is in the patcher's own docstring.

## Doing it by hand

Nothing above is magic, and the manual route still works if you prefer it (or if
you already have an extracted firmware tree from another project): put it at

```
/opt/rb4r5/payload/XDJRX3/           the ISO tree (pdj/rbp, gui/, lib/, usr/)
/opt/rb4r5/payload/XDJRX3-rootfs/    the soft-float rootfs
```

and `launch.py build` will use it as-is. PrimeBox's `TUTORIAL.md` parts A1–A5
describe that route with `rx3dec`, `7z`, `tar` and a Docker container for the
cramfs.

## Disk space and time (Raspberry Pi 5)

| | |
|---|---|
| the `.UPD` you supply | ~69 MB |
| decrypted ISO (kept, so re-unpacking needs no key) | ~69 MB |
| unpacked payload (ISO tree + gui + rootfs) | ~210 MB |
| decrypt + unpack | under a minute |
| DirectFB build afterwards | 10–20 min, once |

Delete `/opt/rb4r5/payload/XDJRX3.iso` if you want the space back; only
`--force` needs it again.
