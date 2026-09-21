# 11 — Porting notes: what changed, what was kept, what to check first

This repository is a rework of **rbtv**, which ran the same player on an ASUS
Chromebit (Rockchip RK3288) under postmarketOS. That port got the player to
display, play audio, accept input and import a rekordbox library on real
hardware. This one targets a Raspberry Pi 5 with a DDJ-FLX4 and a touchscreen.

The rule followed throughout: **anything that is about the player was kept,
anything that is about the board was rewritten.** The player is the same binary
with the same addresses, so its quirks and their fixes carry over exactly; the
display controller, the audio hardware, the control surface, the init system and
the input devices do not.

## Kept, because it is the player's behaviour, not the board's

| | Why it carries over |
|---|---|
| The four-shim structure and the `LD_PRELOAD` order | it is dictated by the player's own `open`/`ioctl`/`snd_*` calls |
| `fbshim`: "the panel is 1280×800 RGB565" | the UI is built for that panel, whatever the screen is |
| `keyshim`: the `sendKey` path, the `UiMain` pump, the stale-caution clear, the mixer defaults, the mixer input routing | all of it is engine behaviour with no front-panel microcontroller |
| The RX3 keycodes and the `op` semantics | live-verified on the Prime GO and Chromebit |
| The device stubs (FIFOs vs plain files, `/dev/paudiog0` absent, `/dev/mem` denied) | the player polls and opens exactly these |
| The DirectFB fixes F1–F4 and F7 (convert with rotation off, publish on `UpdateRegion`, write buffer 0, scale, NEON) | F2 in particular is pure player behaviour: it redraws through the blit path |
| `no-vt` in `directfbrc` (the `fstat`/`struct stat` stack smash) | a glibc-2.13-vs-modern-headers mismatch, board-independent |
| The `getPcController` guard, and the deliberate *absence* of the `getTotalLength` quirk | the second one breaks playback; that was learned the hard way |
| The USB library chain: umount-before-mount, the confirm loop, never writing `udev_usbctn*` | the engine's own mount state machine |
| The start-up pop suppression | the engine's own transient; it was written for rbtv and is enabled here |

## Rewritten for the Pi 5

| Area | Chromebit | Here |
|---|---|---|
| **Audio** | one HDMI stereo PCM, everything folded into it, no cue, `hw:0,0` hard-coded in C | the FLX4's 4-channel PCM: master → 1/2, cue → 3/4; device, channels, rate and format are discovered in Python and passed in the environment; HDMI is the automatic fallback |
| **Controller** | DDJ-400 bridge, 736 lines, fixed map | DDJ-FLX4 bridge from the current Mixxx FLX4 mapping: different shift notes, per-mode pad note bases, 14-bit filter knobs, Beat FX on two channels, a runtime-loadable map file for the rest, and sysex-safe MIDI parsing |
| **Touch** | none (the Chromebit had no touchscreen; `memshim` answered "no touch") | a full touch daemon: zones, drag-to-scroll, drag-to-scrub, calibration; plus the optional native record path |
| **Init / launcher** | four shell scripts and four systemd units | one Python launcher and one service; the shell logic (`start-rb.sh`, `fix-dev.sh`, `usb-watch.sh`, `usb-probe.sh`, `quiet-console.sh`) became `supervisor.py`, `chroot.py`, `usbwatch.py`, `probe.py`, `display.py` |
| **Provisioning** | manual, documented | `setup` does it, idempotently and reversibly (`config.txt`, `cmdline.txt`, boot target, `getty@tty1`, sysctl, udev, service) |
| **Display** | RK3288 VOP quirks: would not present a panned buffer, needed the pan pinned at 0 | the Pi's fbdev emulation has one buffer, so the same "always buffer 0" code is simply correct; the RK3288 pixel-clock investigation is gone |
| **Build** | cross-build on a workstation, deploy over SSH | builds on the Pi itself (`apt install gcc-arm-linux-gnueabi`), and the chroot *is* the sysroot |
| **Process matching** | `pkill -f "rbp -a"`, which could kill its own SSH session | whole-argv matching (`util.pgrep_arg`) |
| **`keyshim` `openDevice` race** | known latent crash, fix written and reverted | fixed: the device is opened at most once per "pump not running" episode, and the wait loops sleep |

## Dropped

* Everything about the Chromebit as a *machine*: the postmarketOS image fixes,
  the initramfs patches, the eMMC install, the Chromebit survey, the
  developer-mode/`Ctrl+U` boot path, the DirectFB probe's Chromebit specifics.
* The Prime GO rotation path (nothing here is portrait), and `knobshim2`
  (superseded by the FLX4 bridge).
* The HDMI-only downmix as the *primary* audio path — it is the fallback now.

## Verified here, and how

Without a Pi 5, an FLX4 or the firmware, these could still be tested properly,
and were:

* **The FLX4 bridge, end to end.** Built natively, fed the exact byte sequences
  a FLX4 sends (transport, browse, load, 14-bit fader/EQ/tempo/crossfader/filter,
  jog touch + rotate, pads in two modes, Beat FX), and the 24-byte records it
  wrote were decoded and checked — including the automatic pad-bank switch and
  the jog "stop" record. `tools/flx4-selftest.sh` reruns it.
* **The touch gesture logic**, over synthetic multitouch and single-touch evdev
  frames: buttons press/release, taps versus drags, scroll direction and step
  count, jog speed sign and the stop on release, long-press suppression, extra
  fingers being ignored, axis flips. 18 checks in
  `tools/tests/test_touch_gestures.py`.
* **The ALSA discovery and device choice**, against captured `/proc/asound`
  content for a real FLX4 (4 ch, S24_3LE, 44100): it picks the 4-channel plughw
  device, keeps S24_LE for the shim, notes that alsa-lib will convert, and falls
  back to HDMI stereo when the controller is absent.
* **The boot-config edits**: idempotent, one marked block, backups, and `--undo`
  leaves `config.txt` and `cmdline.txt` exactly as they were. This one matters —
  a botched `cmdline.txt` does not boot.
* **The shims and daemons compile clean** (`-Wall -Wextra`), and the build
  refuses to install a shim that references a glibc version newer than 2.13.
* **The Pi 5 kernel really does run 32-bit ARM**: `CONFIG_COMPAT=y` in
  `arch/arm64/configs/bcm2712_defconfig`, and the launcher proves it at runtime
  with an ARM ELF it builds itself, then again with the chroot's own loader.
* **The DirectFB patches' integrity**: every hunk header's line counts were
  re-verified after the edits, so the patch applies the same way the original
  did.

## Not verified — check these first on real hardware

In the order I would check them:

1. **The display.** `RB_DFB_DEBUG=1 launch.py run`, then `/tmp/flipdbg.log`
   should show `FLIP 0` and then `UPDATE` lines, and `launch.py fbdump` should
   show the UI. This is inherited working code aimed at a new framebuffer; the
   most likely surprise is the fbdev emulation's pitch or format differing from
   what the driver assumes.
2. **The DirectFB build.** Our patch applies on top of PrimeBox's
   `directfb-full.diff`, which is not in this repository, so hunk offsets cannot
   be checked here. `patch -F3` is used and `.rej` files are left behind;
   [03-payload](03-payload.md) says what to do.
3. **FLX4 audio.** Whether it really offers 4 playback channels, at S24_3LE
   44.1 kHz, and whether channels 3/4 are the headphones. `launch.py audio`
   prints what it found; the fallbacks are in place either way.
4. **The FLX4 map on hardware.** The Mixxx mapping is a good source, but
   SHIFT+LOAD (notes `0x68`/`0x7A`, inherited from the DDJ-400) is the one entry
   not in it — if those do nothing, sniff and fix them in `flx4-map.conf`.
5. **The touch zone layout.** The zones are a sensible guess at where the UI puts
   its own controls; `launch.py calibrate` plus ten minutes of moving rectangles
   is what turns that into a good layout.
6. **Native touch.** Off by default, and the record format is a guess. If
   someone works it out, the zone map becomes a fallback rather than the
   main path.

## Things that will bite whoever works on this next

* Two players fight over the subucom FIFOs and the screen; always check
  `launch.py status` first.
* Running `rbp` by hand without the device stubs crashes it in `openDevice`.
* Restarting the player recreates `/tmp/rb-*.fifo`; writers holding the old
  inode go silent. The supervisor handles it, hand-started daemons do not.
* A crash loop plus `systemd-coredump` can make the Pi unreachable; set
  `core_pattern=/dev/null` while debugging.
* `/dev/gpiodrv` is a regular file, and a regular file is always `poll`-ready:
  without `memshim`'s `poll()` interception two threads spin a core each.
* The engine reports no duration (and so refuses to play) if the
  `getTotalLength` quirk is ever applied. Do not apply it.
