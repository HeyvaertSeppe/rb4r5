# 04 — Display: the rekordbox UI full screen on the Pi 5

Status: the code path is the one that was made to work on the Chromebit,
re-aimed at the Pi's framebuffer. It has **not yet been run on a real Pi 5**
(see [11-porting-notes](11-porting-notes.md)); this page describes how it works
and how to check it.

## The stack

```
 rbp (32-bit ARM soft-float, the XDJ-RX3 binary)
   │  renders a 1280x800 RGB565 "layer"
   ▼
 DirectFB 1.4.16 core (rebuilt soft-float, modules in /usr/lib/directfb-1.4-6)
   │
   ├── LD_PRELOAD fbshim.so
   │      FBIOGET_VSCREENINFO → 1280x800, 16 bpp, RGB565, line_length 2560
   │      FBIOPUT_VSCREENINFO → accepted silently (no real modeset)
   │
   ├── LD_PRELOAD memshim.so
   │      denies /dev/mem, fixes the mmap that follows, parks the GPIO polls,
   │      answers the tsc2007 touch device
   │
   └── systems/libdirectfb_fbdev.so   (patched: src/directfb/directfb-pi5.patch)
          • the primary layer surface lives in system RAM (rot_surface)
          • every FlipRegion AND UpdateRegion runs the publish path:
            RGB565 → XRGB8888, scaled to the real mode, into framebuffer offset 0
          • never pans
   ▼
 /dev/fb0  =  vc4drmfb, the DRM fbdev emulation of vc4-kms-v3d
   ▼
 HVS → HDMI0 → the 22" panel
```

## The mismatch it papers over

| | XDJ-RX3 | Pi 5 + 22″ panel |
|---|---|---|
| Logical UI | 1280×800 **RGB565** | — |
| Real framebuffer | `mxcfb`, 1280×800, RGB565 | `vc4drmfb`, **1920×1080, XRGB8888** |
| Rotation | none | none needed |
| Panning | works | single buffer; irrelevant |

The player cannot be told the screen is anything other than the RX3 panel, so
two conversions happen in the driver on every frame: **16 → 32 bpp** and
**1280×800 → the panel's mode**.

## The five fixes (inherited, and why they are still needed)

These were each found the hard way on the earlier ports. They are in
`src/directfb/directfb-pi5.patch`.

**F1 — run the convert path with rotation off.** PrimeBox's driver reuses one
switch (`rot_deg`) for "rotate" and for "convert + publish". With
`DFB_ROTATE=off` the whole publish block was skipped: DirectFB rendered into its
own buffers, reported success, and the screen never changed. The patch adds
`rot_enable`, always on, and leaves `rot_deg` to mean only the angle.

**F2 — publish on `UpdateRegion`, not only `FlipRegion`.** DirectFB calls
`FlipRegion` for full-surface swaps only; the player redraws mostly through the
blit path, which calls `UpdateRegion`. Without a handler, exactly one frame (the
first, black one) is ever presented. The patch registers
`.UpdateRegion = primaryUpdateRegion` and both paths call the same
`publish_rotated_frame()`.

**F3 — always write physical buffer 0, never pan.** On the RK3288 the display
controller ignored a panned buffer, so the UI sat in buffer 1 while the screen
showed black. The fix — convert into `framebuffer_base + 0` and leave the pan at
zero — is also exactly right on the Pi, where fbdev emulation has one buffer.

**F4 — scale to fill the screen.** Without scaling the 1280×800 layer landed in
the top-left corner of a 1920×1080 framebuffer with junk around it. The patch
adds a nearest-neighbour 16.16 fixed-point scale that iterates over
*destination* pixels, so every pixel is written every frame and nothing stale
survives.

**F5 — the loader details.** Two of them: a modern build of `fbdev.c` references
`fcntl@GLIBC_2.28`, which glibc 2.13 cannot resolve, so `dlopen()` of the module
fails — the patch uses `syscall(SYS_fcntl64, …)` instead. And DirectFB's
`vt_set_fb()` calls `fstat()` into a `struct stat` laid out by modern headers
while linking glibc 2.13, smashing the stack canary — so the chroot's
`/etc/directfbrc` sets `no-vt`.

Plus **F6**: `/dev/gpiodrv` is a regular-file stub, and a regular file is always
`poll`-ready, so two `GpioManager` threads used to spin at ~11k polls/s each.
`memshim` intercepts `poll()` for those descriptors, sleeps, and returns 0.

And **F7**: the RGB565 → XRGB8888 row expansion is NEON (8 px/iteration) with
the expanded row cached across the destination rows that share it, and no
whole-buffer `memcpy`. On the RK3288 that took the publish from 25.6 ms to
16.7 ms per frame. On an A76 it is a fraction of that; the module is built
`-march=armv7-a -mfpu=neon -mfloat-abi=softfp`, because `softfp` keeps the base
calling convention and stays link-compatible with the soft-float core, while
`-mfloat-abi=soft` rejects NEON intrinsics. `build.neon=false` in the config
builds the portable scalar version.

## Configuration

`/etc/rb4r5/config.json`:

```json
"display": {
  "fbdev": "/dev/fb0",
  "ui_width": 1280, "ui_height": 800,   ← the RX3 panel; do not change
  "force_mode": null,                   ← e.g. "1920x1080@60" to pin the mode
  "hdmi_port": 0,                       ← 0 = the HDMI next to USB-C
  "rotate": "off",
  "quiet_console": 2,
  "blank_timeout": 0
}
```

* `force_mode` writes an `hdmi_cvt`/`hdmi_group`/`hdmi_mode` block into
  `config.txt` (inside our marked block, with a backup). Leave it `null` unless
  the monitor's preferred mode is wrong — the driver adapts to whatever mode is
  live.
* `quiet_console`: `0` keeps the text console (useful while bringing things up),
  `1` silences kernel messages, `2` (default) also detaches `fbcon` so nothing
  can overdraw DirectFB. Get the console back with
  `echo 1 | sudo tee /sys/class/vtconsole/vtcon1/bind`, or
  `launch.py stop --restore-console`.
* Want letterboxing instead of a stretch? Set the monitor to 1280×800 if it
  supports it (`force_mode: "1280x800@60"`), which makes the scale a 1:1 copy.

## Checking it

```sh
PI# python3 launch.py doctor            # geometry, connectors, non-zero bytes
PI# python3 launch.py fbdump /tmp/screen.png   # what is on screen, over SSH
PI# cat /sys/class/graphics/fb0/{name,virtual_size,bits_per_pixel,stride,pan}
PI# RB_DFB_DEBUG=1 python3 launch.py run       # driver prints FLIP/UPDATE lines
PI# head -20 /tmp/flipdbg.log
```

A healthy run shows `FLIP 0` once and then `UPDATE 0..n`, `pan=0,0`, and a
framebuffer whose first 400 kB is mostly non-zero. `fbdump` needs
`python3-pil` for PNG output and otherwise writes raw BGRA.

## If the screen is black

Full table in [10-troubleshooting](10-troubleshooting.md); the short version:

| Symptom | Cause |
|---|---|
| `/dev/fb0` missing | KMS not enabled — `dtoverlay=vc4-kms-v3d`, then reboot |
| Screen black, the player is running, `fb0` all zero | the driver is not publishing: the wrong module got installed, or `DFB_ROTATE` is set to something odd |
| One frame then frozen | an `UpdateRegion`-less driver — you are running an unpatched module |
| UI in the top-left corner with junk around it | the scale path is missing (same cause) |
| Text or a login prompt flickering over the UI | `quiet_console` is 0, or `getty@tty1` got re-enabled |
| A desktop is on the screen instead | the boot target went back to `graphical.target` |
