# 03 — The firmware payload (what you must supply)

rb4r5 contains **no Pioneer/AlphaTheta firmware, no `rbp` binary, no decryption
key and no music database**. It is the glue that runs *your* copy on *your*
hardware. This page is what to produce before `launch.py build`.

Read [NOTICE.md](../NOTICE.md) first: extracting firmware may be restricted
where you live, and running a vendor application on other hardware is very
likely against its EULA. This is for research, repair, preservation and
personal interoperability.

## What the build needs

```
/opt/rb4r5/payload/
├── XDJRX3-rootfs/        the RX3's soft-float userland (glibc 2.13, libstdc++,
│   ├── lib/              DirectFB, freetype, ALSA, busybox)
│   ├── usr/
│   ├── bin/  sbin/  etc/
└── XDJRX3/               the firmware's ISO tree
    ├── pdj/rbp           the player itself
    ├── gui/              fonts, psets, image data
    ├── lib/  usr/        libraries the ISO adds on top of the rootfs
/opt/rb4r5/PrimeBox/      a checkout of github.com/erhan-/PrimeBox
```

`launch.py payload` copies that into `/opt/rb4r5/chroot`, and `launch.py build`
adds the pieces rb4r5 builds (shims, DirectFB, the patched player).

## Getting there

Use the tooling from [PrimeBox](https://github.com/erhan-/PrimeBox) — it already
does firmware decryption and the player patching, and rb4r5 deliberately does
not duplicate it.

```sh
PI$ sudo git clone https://github.com/erhan-/PrimeBox /opt/rb4r5/PrimeBox
PI$ cd /opt/rb4r5/PrimeBox
```

Then, following PrimeBox's own `TUTORIAL.md` (parts A3–A5):

1. **Get the firmware.** `./tools/get-firmware.sh ~/xdjrx3-fw` fetches the
   official XDJ-RX3 update package (`XDJ-RX3.UPD`, v1.20 is what every port has
   been verified against).
2. **Decrypt it.** `tools/rx3dec` turns the `.UPD` into an ISO. It needs the
   AES key, which AlphaTheta published in their own GPL source distribution —
   you obtain it yourself; it is not here and it is gitignored.
3. **Unpack the ISO** (`7z x`), which gives you `pdj/rbp`, `gui/`, `lib/`,
   `usr/` → that is `XDJRX3/`.
4. **Unpack the rootfs** (`rootfs.cramfs` from the ISO) → that is
   `XDJRX3-rootfs/`.

Copy both trees under `/opt/rb4r5/payload/` with the names above.

## Sanity checks

```sh
# the player is a 32-bit ARM soft-float binary
PI$ file /opt/rb4r5/payload/XDJRX3/pdj/rbp
  ELF 32-bit LSB executable, ARM, EABI5 version 1 (SYSV), dynamically linked,
  interpreter /lib/ld-linux.so.3

# the rootfs has the loader the player needs
PI$ ls -l /opt/rb4r5/payload/XDJRX3-rootfs/lib/ld-linux.so.3

# your kernel can execute it (this is what the launcher checks too)
PI$ /opt/rb4r5/chroot/lib/ld-linux.so.3 --version | head -2
  ld.so (GNU libc) stable release version 2.13
```

The stock v1.20 player is md5 `4f2efcfc0c9e3f539289f863acfddcc6`. The build
prints the md5 at each stage so you can see which patch sets were applied:

| Artefact | What it is |
|---|---|
| stock `rbp` | your extracted binary, unmodified |
| `work/rbp-audio` | + PrimeBox's patch set (68 patches: panel, USB, keys, audio, waveform) — md5 `3706c68f7242779d46afa09f35a39acf` on v1.20 |
| `work/rbp-pi5` | + the `getPcController` NULL guard (`src/patch/patch-rbp-crashguards.py`) |

## Why that last patch

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

## What DirectFB needs from PrimeBox

`launch.py build` clones DirectFB 1.4.16 and applies three layers:

1. `PrimeBox/tools/build-directfb/directfb-full.diff` — the soft-float/fbdev
   groundwork for running this player on non-Pioneer hardware,
2. `src/directfb/directfb-pi5.patch` — our changes (convert with rotation off,
   publish on `UpdateRegion`, write buffer 0, scale to the panel, no versioned
   `fcntl`),
3. `src/directfb/directfb-pi5-neon.patch` — the NEON row expander.

If the PrimeBox diff is missing, the build says so and points here.

## Disk space

| | Size |
|---|---|
| payload (both trees) | ~120 MB |
| assembled chroot | ~200 MB |
| DirectFB build tree | ~1 GB while building (delete `/opt/rb4r5/work/dfb-build` afterwards) |
