# rb4r5 — the XDJ-RX3 rekordbox player on a Raspberry Pi 5

Run the **Pioneer XDJ-RX3 standalone rekordbox player** (`rbp`) natively on a
**Raspberry Pi 5** with Raspberry Pi OS, driven by a **Pioneer DDJ-FLX4** over
USB (master out *and* headphone cue) on a **22″ touchscreen**, started by one
Python launcher that sets the machine up and brings the player up full screen.

```
sudo python3 launch.py
```

That single command provisions the Pi, builds what is missing, and runs the
player. After the first run it is also a boot service, so the Pi comes up as a
standalone rekordbox player with nothing plugged in but the FLX4 and the screen.

This is a rework of [`rbtv`](docs/11-porting-notes.md), which ran the same
player on an ASUS Chromebit under postmarketOS. Every device-specific layer —
display, audio, controller, input, init — was rewritten for the Pi 5; the parts
that are about the *player* rather than the *board* were kept, because they were
verified on hardware there.

## What it does

| Subsystem | How |
|---|---|
| **Display** | DirectFB 1.4 fbdev → `/dev/fb0` (the DRM fbdev emulation of `vc4-kms-v3d`). The RX3's 1280×800 RGB565 UI is converted and scaled to the panel's real mode (1920×1080 on a 22″ screen) inside the driver, with a NEON fast path. [docs/04](docs/04-display.md) |
| **Audio** | `audioshim` maps the RX3's three virtual DAC devices onto the FLX4's 4-channel USB PCM: **master → ch 1/2**, **headphone cue → ch 3/4**. HDMI audio is the automatic fallback. [docs/05](docs/05-audio.md) |
| **Controller** | `flx4-bridge` translates DDJ-FLX4 USB-MIDI into the engine's own `IKeyManager::sendKey` calls: transport, browse, load, 3-band EQ, trim, faders, crossfader, tempo, filter, jog (scratch and search), the 8 pads with automatic bank switching, and Beat FX. [docs/06](docs/06-controller.md) |
| **Touchscreen** | `rbtouchd` maps screen regions onto engine controls — on-screen buttons, drag-to-scroll the browse list, drag-to-scrub each deck. Zones are a JSON file; `launch.py calibrate` shows what you are hitting. [docs/07](docs/07-touch.md) |
| **Library** | A real rekordbox USB stick is mounted the way the RX3 mounts its own USB1 slot, so the native DeviceSQL import of `PIONEER/rekordbox/export.pdb` runs and the UI shows the full library. [docs/08](docs/08-usb-library.md) |
| **Launcher** | One Python program: provision, build, run, supervise, diagnose. No dependencies beyond a stock Raspberry Pi OS. [docs/09](docs/09-launcher.md) |

## Status — read this first

The port is **complete in code and tested offline**; it has **not yet been run
on real hardware by the author**. Everything that could be verified without a
Pi 5, an FLX4 and the firmware was verified, and everything that could not is
listed as such:

| | State |
|---|---|
| Launcher, supervisor, config, diagnostics | ✅ run and exercised (`tools/tests/`) |
| Firmware decryption (`.UPD` → ISO) | ✅ byte-for-byte against an independent AES implementation |
| Firmware unpacking (cramfs rootfs, gui, key discovery) | ✅ end-to-end on a synthetic firmware image |
| FLX4 MIDI → engine translation | ✅ end-to-end tested with synthetic MIDI (`tools/flx4-selftest.sh`) |
| Touch gestures → engine controls | ✅ 18 offline checks over synthetic evdev frames |
| Boot-config edits (`config.txt`, `cmdline.txt`) | ✅ idempotent + reversible, tested on fixtures |
| ALSA discovery and device choice | ✅ tested against captured `/proc/asound` data |
| Pi 5 runs 32-bit ARM soft-float binaries | ✅ `CONFIG_COMPAT=y` in the stock `bcm2712` kernel; the launcher proves it at runtime |
| Display path on real Pi 5 hardware | ⚠️ not yet run — the driver patch is inherited working code, re-aimed at the Pi |
| FLX4 4-channel audio on Linux | ⚠️ class-compliant, 4 ch reported by users; the launcher verifies and falls back |
| Native RX3 touch (`touch.native`) | ❌ off by default: the tsc2007 record layout is unverified ([docs/07](docs/07-touch.md)) |
| A few FLX4 controls (channel CUE/PFL, loop halve/double) | ⚠️ unbound — no verified keycode; bind them in `config/flx4-map.conf` |

[docs/11-porting-notes.md](docs/11-porting-notes.md) has the full list of what
changed, what was kept, and what to check first on hardware.

## What you must supply

One file: the **XDJ-RX3 firmware update** (`.UPD`) you downloaded. On the first
run the launcher shows you every `.UPD` it can find and asks which one — after
that it decrypts it, unpacks the ISO, the fonts and the soft-float root
filesystem, patches the player and builds everything, by itself.

```
Select your XDJ-RX3 firmware file
=================================

   1) /home/pi/Downloads/XDJ-RX3.UPD                  69.2 MB  2026-09-21 09:16
   2) /media/pi/USB-STICK/firmware/XDJ-RX3_v120.UPD   69.2 MB  2026-09-18 10:16

   p) type a path      q) cancel

Select the firmware file [1]:
```

The firmware key (`aes256.key`, published by AlphaTheta in their own GPL source
distribution) is found automatically if it is anywhere sensible — next to the
`.UPD` is easiest — and you are only asked for it if it is not.

This repository contains **no Pioneer/AlphaTheta firmware, no `rbp` binary, no
decryption key and no music database**; see
[docs/03-payload.md](docs/03-payload.md) and [NOTICE.md](NOTICE.md).

## Hardware

* Raspberry Pi 5 (4 GB is plenty), Raspberry Pi OS Bookworm (64-bit is fine),
  an SD card or NVMe with ~2 GB free
* a 22″ HDMI touchscreen — HDMI for the picture, USB for the touch panel
* a Pioneer DDJ-FLX4 on USB (plug it into the Pi directly, or into a
  **powered** hub; a bus-powered hub cannot feed it)
* optionally a rekordbox USB stick, and a keyboard for the fallback control path

## Quick start

```sh
git clone https://github.com/HeyvaertSeppe/rb4r5 && cd rb4r5
sudo python3 launch.py
```

That is the whole thing: it provisions the Pi, asks which `.UPD` to use,
unpacks and builds everything, and starts the player full screen. It also
installs a boot service, so from then on the Pi comes up as a player by itself.

The individual steps exist too, if you would rather watch them one at a time:

```sh
sudo python3 launch.py setup       # packages, KMS, console handover, service
sudo python3 launch.py firmware    # pick the .UPD; decrypt and unpack it
sudo python3 launch.py build       # shims, DirectFB, daemons, patched player
sudo python3 launch.py run         # run and supervise it
sudo python3 launch.py doctor      # check every subsystem
```

## Layout

```
launch.py                 the launcher / supervisor entry point
rb4r5/                    its implementation (one module per subsystem)
src/shims/                LD_PRELOAD shims that run inside the player
src/host/                 daemons that run outside it (FLX4 bridge, rbkeyd)
src/directfb/             the DirectFB fbdev patches and build scripts
src/patch/                the player's crash-guard patcher
src/probe/                a DirectFB bring-up probe
config/                   editable defaults: touch zones, controller map
systemd/  udev/           the boot service and device rules
docs/                     how each subsystem works, and how to fix it
tools/tests/              offline tests (no hardware needed)
```

## Licence

Our code is MIT ([LICENSE](LICENSE)). Third-party components keep their own
licences; nothing vendor-owned is included. [NOTICE.md](NOTICE.md) has the
details, the trademarks and the legal caveats.
