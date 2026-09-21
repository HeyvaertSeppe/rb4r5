# 03 — The firmware: pick your `.UPD`, the launcher does the rest

rb4r5 contains **no Pioneer/AlphaTheta firmware, no `rbp` binary, no decryption
key and no music database**. It is the glue that runs *your* copy on *your*
hardware. What you supply is one file: the XDJ-RX3 firmware update you
downloaded.

```sh
PI$ sudo python3 launch.py firmware
```

```
Select your XDJ-RX3 firmware file
=================================

   1) /home/pi/Downloads/XDJ-RX3.UPD                  69.2 MB  2026-09-21 09:16
   2) /media/pi/USB-STICK/firmware/XDJ-RX3_v120.UPD   69.2 MB  2026-09-18 10:16

This is the .UPD you downloaded from AlphaTheta (XDJ-RX3 v1.20, about 69 MB).
Everything after this is automatic.

   p) type a path
   q) cancel

Select the firmware file [1]:
```

From there it decrypts, unpacks and lays out everything the build needs. The
same thing happens by itself the first time you run `sudo python3 launch.py`,
so in practice you never type this command — you just answer the question.

Read [NOTICE.md](../NOTICE.md) first: extracting firmware may be restricted
where you live, and running a vendor application on other hardware is very
likely against its EULA. This is for research, repair, preservation and
personal interoperability.

## What happens after you choose

```
XDJ-RX3.UPD  ──decrypt──▶ XDJRX3.iso ──unpack──▶ XDJRX3/
  (69 MB, AES-256-CBC)                             ├── pdj/rbp        the player
                                                   ├── lib/  usr/     ISO libraries
                                                   ├── gui/           fonts + images
                                                   └── images/
                                      ──unpack──▶ XDJRX3-rootfs/      the soft-float
                                                                      userland
```

| Step | How |
|---|---|
| **Decrypt** | AES-256-CBC per 512-byte sector, IV = `LE32(sector)` padded with zeroes, the last 16 bytes a plaintext trailer — the scheme PrimeBox's `rx3dec` documents, reimplemented in Python so no Rust toolchain is needed. It is done as one ECB pass plus an XOR (CBC decryption *is* ECB decryption XOR the previous ciphertext block), which is why 69 MB takes a second or two. |
| **Verify** | The ISO 9660 signature `CD001` must appear at sector 64. A wrong key cannot fake that, so a bad key is caught before anything is written. |
| **Unpack the ISO** | A loop mount when running as root (keeps Rock Ridge symlinks and modes), otherwise `bsdtar`, otherwise `7z`. |
| **Unpack `gui.tar.gz`** | Python's `tarfile`. Without these fonts the UI does not start. |
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
PI$ sudo python3 launch.py firmware --show
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
