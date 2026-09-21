# 04 — Display: the rekordbox UI full screen on the Pi 5

Status: running on a real Pi 5. The first run there found a real bug — see
[F8](#f8--the-framebuffers-pixel-format-is-read-not-assumed) — which is fixed;
what is still unconfirmed on hardware is the frame rate and the colour
rendition, both of which `launch.py fbtest` answers in one photograph.

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
   └── systems/libdirectfb_fbdev.so   (patched: src/directfb/directfb-pi5.patch
                                       + src/directfb/rb4r5_scale.h)
          • the primary layer surface lives in system RAM (rot_surface)
          • every FlipRegion AND UpdateRegion runs the publish path:
            RGB565 → whatever the fb's format is, scaled to the real mode,
            into framebuffer offset 0
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
| Real framebuffer | `mxcfb`, 1280×800, RGB565 | `vc4drmfb`, 1920×1080, **RGB565 _or_ XRGB8888** |
| Rotation | none | none needed |
| Panning | works | single buffer; irrelevant |

The player cannot be told the screen is anything other than the RX3 panel, so
the driver rescales **1280×800 → the panel's mode** on every frame, and
converts the pixels as well *if* the panel's framebuffer is not RGB565. Which
it is depends on the kernel — see F8.

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

**F4 — scale onto the screen.** Without scaling the 1280×800 layer landed in
the top-left corner of a 1920×1080 framebuffer with junk around it. The patch
scales by iterating over *destination* pixels, so every pixel of the picture
is written every frame and nothing stale survives. How it scales is F9.

**F5 — the loader details.** Two of them: a modern build of `fbdev.c` references
`fcntl@GLIBC_2.28`, which glibc 2.13 cannot resolve, so `dlopen()` of the module
fails — the patch uses `syscall(SYS_fcntl, …)` instead (`SYS_fcntl`, not
`SYS_fcntl64`: `F_SETFD` involves no file offsets, and it is the one number
that exists on every target). And DirectFB's
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

## F8 — the framebuffer's pixel format is read, not assumed

This is the one the Pi found, and it is worth spelling out because the symptom
looks like anything but its cause.

`/dev/fb0` on a Pi 5 is the DRM fbdev emulation of `vc4-kms-v3d`, and **its
depth is not fixed**. Depending on the kernel it hands out RGB565 at 16 bpp or
XRGB8888 at 32 bpp, and a `video=HDMI-A-1:1920x1080-32` on the kernel command
line changes it again. The publish path used to write 32-bit pixels
unconditionally. Into a 16 bpp framebuffer that means:

* each write covers **two** screen pixels, so the picture is double width and
  only its left half is on the panel;
* each row overruns into the next, so the vertical geometry collapses too;
* the colours are mangled in a very specific way — every second pixel carries
  the low half-word `0xGGBB` and the next the high half-word `0xffRR`, so a
  dark blue-grey background reads as **olive yellow**, blue-grey panels read
  as **lavender**, and pure white stays white.

That last detail is the tell: a straightforward channel swap cannot turn black
into yellow, and nothing can make white anything but white. If you ever see
those colours again, the destination format is wrong.

The fix is `src/directfb/rb4r5_scale.h`. The layout is classified **once** from
the `fb_var_screeninfo` bitfields (`FBIOGET_VSCREENINFO`, through a raw
syscall so the fb shim cannot lie about it) and the writer is chosen from that:
RGB565, BGR565, RGB555, XRGB8888, XBGR8888, RGB888 and BGR888 are all handled,
and the row write is clamped to the reported line length so even a misdetected
mode cannot scribble into the next row. An unrecognised layout falls back to
the usual one for its depth and says so in the log.

RGB565 is the *good* case, incidentally: the source is RGB565 too, so the
publish becomes a pure scaled copy with no colour conversion at all.

The driver prints what it decided, once, at startup:

```
(*) FBDev/rb4r5: fb 1920x1080 RGB565 (16 bpp, pitch 3840) <- frame 1280x800
    at 0,0 1920x1080 [fill]
```

The header is unit tested away from the Pi — `tools/tests/test_fbscale.c`,
which is also compiled for armv7 with NEON and run under `qemu-arm-static` by
`tools/tests/run-all.sh`, so the vectorised expander is checked against the
reference for all 65536 RGB565 values in both channel orders.

## F9 — aspect ratio and filtering

Two separate decisions, both of which were wrong to begin with.

**Shape.** The RX3's screen is 1280×800, i.e. 16:10. A 16:9 monitor is not.
Stretching one onto the other makes everything 8% wide — noticeably so on the
round jog displays and the text. The default is now `fit: aspect`: the frame
keeps its shape and is centred, with black down each side, so **the UI is
never distorted**.

| panel | frame | bars |
|---|---|---|
| 1920×1080 | 1728×1080 | 96 px each side |
| 2880×1620 | 2592×1620 | 144 px each side |
| 1024×768 | 1024×640 | 64 px top and bottom |
| 1280×800 | 1280×800 | none — an exact 1:1 copy |

`fit: fill` brings the stretch back. **The touch daemon maps through the same
rectangle** (`fb.frame_rect()`, which is the same arithmetic as
`rb4r5_dst_init()`, with a test pinning the two together): with bars, a touch
two thirds across the glass is *not* two thirds across the UI, and a press on
a bar presses nothing.

**Filtering.** 1280→1728 is 1.35×, 1280→2592 is 2.03×, and nearest neighbour
at either duplicates some columns and rows and leaves the others alone. On
text and on the waveform's single-pixel lines that reads as a coarse, uneven,
low-resolution picture — because it is one. The default is now
`scale: bilinear`, which interpolates:

| | nearest | bilinear |
|---|---|---|
| 1px lines at 8, 4, 2 px spacing | uneven — some doubled, some dropped | even, softened consistently |
| a smooth ramp over 64 px | 31 distinct values | 248 distinct values |
| accuracy vs a floating-point reference | n/a | worst channel error **0.94/255** |
| cost per frame, 2592×1620, x86, no NEON | ~2.8 ms | ~13 ms |

`scale: nearest` is there if a particular machine disagrees.

The work is arranged so the expensive part happens as few times as possible:
**the horizontal pass runs once per source row and the vertical pass once per
destination row.** Enlarging 800 rows to 1620 that is 800 horizontal passes
instead of 1620, and what is left per destination row — a two-row blend and a
pack to the framebuffer's format — both vectorise (16 bytes per iteration for
the blend, 8 pixels for the RGB565 pack).

Three more details make the filtered path worth trusting:

* **32.32 fixed point.** At 16.16 the truncated step `(1280<<16)/1920` loses
  0.667 per pixel; by the right-hand edge of a 1920-wide panel that has
  drifted far enough to pick visibly wrong weights — measured against a
  floating-point reference it was out by up to 6.6/255 on a high-contrast
  edge. A 64-bit accumulator costs an add-with-carry per pixel and takes that
  under 1/255.
* **Pixel centres, not corners.** `dst centre -> src centre` with the −0.5
  offset, which is what makes a 1:1 scale a bit-exact copy rather than a
  half-pixel blur, and makes the edges clamp instead of sampling past the
  frame.
* **The weights are precomputed per column**, so no 64-bit arithmetic
  survives in the inner loop.
* **The blend rounds, it does not truncate.** A bilinear sample is two blends
  chained, one per axis, so truncation loses up to a whole level twice over —
  enough to show against the reference (it was the difference between 2.08 and
  0.94). The NEON path uses a rounding narrowing shift to match the scalar one
  exactly: both come out at 0.94.

Sharpest of all, if the monitor accepts it: run the panel at 1280×800
(`force_mode: "1280x800@60"`). The scale becomes an exact 1:1 copy — the
identity case is a unit test — and the monitor's own scaler does the
enlarging.

## Is the player even using this driver?

A display driver left over from an older build is the one fault that looks
like broken hardware, so it is checked rather than assumed. The built module
carries two strings (`FBDev/rb4r5:` and `bilinear`); `chroot.module_report()`
looks for them in the copy inside the chroot and compares its md5 with the one
in the build directory. `launch.py doctor` prints the result:

```
usr/lib/directfb-1.4-6/systems/libdirectfb_fbdev.so: md5 6b1f…
  yes  reads the framebuffer's pixel format instead of assuming it
  yes  can interpolate when scaling (not just nearest neighbour)
```

and `launch.py run` refuses to start with an older one, naming the fix
(`build --fast-directfb`) — `run --force` runs it anyway. A `build` where the
DirectFB step failed now exits non-zero instead of printing a note among forty
other lines, which is how a failed rebuild got missed.

## Configuration

`/etc/rb4r5/config.json`:

```json
"display": {
  "fbdev": "/dev/fb0",
  "ui_width": 1280, "ui_height": 800,   ← the RX3 panel; do not change
  "force_mode": null,                   ← e.g. "1920x1080@60" to pin the mode
  "hdmi_port": 0,                       ← 0 = the HDMI next to USB-C
  "rotate": "off",
  "fit": "aspect",                      ← never distort; "fill" to stretch
  "scale": "bilinear",                  ← "nearest" is faster and coarser
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
* `fit`: `aspect` (default) keeps the RX3's 16:10 shape and centres it with
  black bars — 1728×1080 with 96 px each side on a 1080p panel. `fill`
  stretches it over the whole panel, which makes it 8% too wide. Reaches the
  driver as `RB_FB_FIT`, so `RB_FB_FIT=fill` works for a one-off test.
* `scale`: `bilinear` (default) interpolates; `nearest` duplicates pixels.
  Reaches the driver as `RB_FB_SCALE`. See F9.

## Checking it

```sh
PI# python3 launch.py fbtest            # test pattern: mode, format, colours
PI# python3 launch.py fbtest --ui       # a frame through the driver's own scaler
PI# python3 launch.py fbtest --ui --nearest    # ... to compare the filtering
PI# python3 launch.py verify            # + does the frame fill the panel?
PI# python3 launch.py doctor            # geometry, format, connectors, content
PI# python3 launch.py fbdump /tmp/screen.png   # what is on screen, over SSH
PI# cat /sys/class/graphics/fb0/{name,virtual_size,bits_per_pixel,stride,pan}
PI# RB_DFB_DEBUG=1 python3 launch.py run       # driver prints FLIP/UPDATE lines
PI# head -20 /tmp/flipdbg.log
```

**`fbtest --ui` answers "is the player getting this?".** It publishes a
1280×800 frame through `rb4r5_scale565()` — the same header that is compiled
into the module — using `src/host/fbpublish.c`. The frame is built out of the
things that show scaling faults: single-pixel lines at 8, 4 and 2 px spacing, a
1 px checkerboard, 2 px text-sized strokes, smooth ramps, and coloured corner
blocks. If that picture is right and the player's is not, the player is
loading an older module, which `doctor` will confirm.

**`fbtest` first.** It writes a test pattern straight to `/dev/fb0` in the
format the driver reports, which takes rbp, the chroot, the shims and DirectFB
out of the picture completely. Photograph it and check three things: the thin
white frame touches all four edges (the mode and the panel agree, no
overscan), the eight colour bars are in the order the command prints (the
pixel format is right), and there are eight ticks along the top, one per bar
(nothing is doubled or halved). If the pattern is right, everything below the
player is right.

A healthy run shows `FLIP 0` once and then `UPDATE 0..n`, `pan=0,0`, and a
framebuffer whose first 400 kB is mostly non-zero. `fbdump` writes a real PNG
with no Pillow needed, decoding the pixels according to the format the driver
reports — a screenshot taken with the wrong assumption makes a working screen
look broken, so it uses the same classification the driver does.

Screenshots are also saved automatically as the player comes up — at 10 s,
30 s and 90 s after every start — in `/var/log/rb4r5/screenshots/` — so if the screen ever looks wrong there is a
record of what it looked like. `verify` additionally samples the framebuffer's
four corners and its centre: a frame that leaves corners black is not covering
the panel, which is exactly what a missing scale path (F4) looks like.

## If the screen is black

Full table in [10-troubleshooting](10-troubleshooting.md); the short version:

| Symptom | Cause |
|---|---|
| `/dev/fb0` missing | KMS not enabled — `dtoverlay=vc4-kms-v3d`, then reboot |
| Screen black, the player is running, `fb0` all zero | the driver is not publishing: the wrong module got installed, or `DFB_ROTATE` is set to something odd |
| One frame then frozen | an `UpdateRegion`-less driver — you are running an unpatched module |
| UI in the top-left corner with junk around it | the scale path is missing (same cause) |
| Half the UI stretched across the panel, olive background, lavender panels | F8: a module built before the pixel format was read from the driver. `launch.py build` again |
| Colours right but red and blue swapped | the fb is BGR and the bitfields say otherwise — send the output of `launch.py fbtest` |
| The UI is stretched wide | `display.fit` is `fill`; the default is `aspect` |
| The UI looks coarse or blocky | `display.scale` is `nearest`, or the module predates F9 — `doctor` says which |
| `fbtest` is right but the player is wrong | the player is loading an older module: `build --fast-directfb` |
| Text or a login prompt flickering over the UI | `quiet_console` is 0, or `getty@tty1` got re-enabled |
| A desktop is on the screen instead | the boot target went back to `graphical.target` |
