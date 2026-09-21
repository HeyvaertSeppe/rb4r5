# 02 — Hardware

## The three machines that matter

| | Raspberry Pi 5 (target) | Pioneer XDJ-RX3 (source of the player) | ASUS Chromebit (the port this replaces) |
|---|---|---|---|
| SoC | Broadcom **BCM2712**, 4× Cortex-A76 @2.4 GHz | NXP i.MX6 Quad, 4× Cortex-A9 | Rockchip RK3288, 4× Cortex-A17 |
| Userland ABI | arm64 (or armhf); **runs 32-bit ARM at EL0** | armv7 **soft-float**, glibc 2.13 | armv7 hard-float (musl) |
| OS | Raspberry Pi OS Bookworm (systemd) | BusyBox + in-house init | postmarketOS edge |
| Display | HDMI ×2, `vc4-kms-v3d` → DRM + fbdev emulation | 1280×800 RGB565 panel (`mxcfb`) | HDMI, `rockchipdrmfb` |
| Touch | whatever the monitor provides (USB HID) | tsc2007 resistive | none |
| Audio | HDMI, plus any USB interface | 3× CS4344 stereo DAC + ESAI ADC | HDMI only |
| Controls | the DDJ-FLX4 (USB MIDI) | EUP/SUB microcontrollers over SPI | none (keyboard) |
| USB | 2× USB 3.0 + 2× USB 2.0, all host | 2× host + a sub-MCU | 1× USB 2.0 |
| RAM | 4–16 GB | — | 2 GB |

What the Pi 5 brings over the Chromebit, and why the port got simpler:

* **CPU headroom.** The convert-and-scale publish path cost ~17 ms/frame on the
  RK3288 and capped the UI at ~19 fps. On an A76 at 2.4 GHz the same work is a
  few milliseconds, so the UI should run at its own ~30 fps ceiling.
* **A sane framebuffer.** The RK3288's VOP would not present a panned buffer
  (the fix was to always write physical buffer 0). The Pi's DRM fbdev emulation
  exposes a single buffer, so "always write buffer 0" is simply correct here.
* **Four USB ports.** The Chromebit had one, so a controller *and* a stick *and*
  a keyboard needed a powered hub. On the Pi they all just plug in — though the
  FLX4 still wants the Pi's own port or a powered hub, not a bus-powered one.
* **Real audio outputs.** The Chromebit could only fold everything into HDMI
  stereo, which means no headphone cue. The FLX4 gives master *and* cue.
* **Debian.** `apt install gcc-arm-linux-gnueabi` — the whole build runs on the
  Pi itself instead of needing a cross-build workstation.

## The 22″ touchscreen

A typical 22″ HDMI touch monitor is **1920×1080** with a **USB HID multitouch**
panel. Both cables matter: HDMI carries the picture, USB carries the touch.

* The player's UI is 1280×800 (5:8 ≈ 1.6:1) and the panel is 16:9 ≈ 1.78:1, so
  the driver **stretches** it to fill the screen. It is a nearest-neighbour
  scale, iterating over destination pixels, so every pixel is written each
  frame and nothing stale is left around the edges. If you would rather keep the
  aspect ratio, see [04-display.md](04-display.md).
* Touch coordinates are normalised from the device's own axis range, so a panel
  reporting 0…4095 and one reporting 0…32767 both work with the same zone file.
  If touches land in the wrong place, `touch.swap_xy` / `invert_x` / `invert_y`
  in the config fix the orientation — `launch.py calibrate` tells you which.
* Panels that present themselves as single-touch (`ABS_X`/`ABS_Y` + `BTN_TOUCH`)
  are handled as well as protocol-B multitouch panels.

## The DDJ-FLX4

* **USB audio and MIDI class compliant** — no vendor driver on Linux. It shows
  up as one ALSA card (name `FLX4`) with a playback stream and a rawmidi node.
* **Playback channels 1/2 are the master output, 3/4 the headphones.** That is
  the layout `audioshim` uses, and the same one Mixxx documents.
* **24-bit, 44.1 kHz**, which is exactly what the engine produces — so no
  resampling in the normal case. The launcher checks the device's own
  `/proc/asound/cardN/stream0` and, if the format or rate differ, routes through
  ALSA's `plug` layer so the conversion happens in alsa-lib rather than failing.
* **Power.** The FLX4 draws up to 500 mA. Use a Pi port or a powered hub.
* The controls that have no XDJ-RX3 equivalent (Smart CFX/Smart Fader behaviour,
  keyboard and key-shift pad modes, stems pads) are logged and ignored rather
  than mapped to something surprising — [06-controller.md](06-controller.md).

## Power, thermals, storage

* Use the official **27 W USB-C** supply. Under-voltage shows up as audio
  dropouts long before anything else; `launch.py doctor` prints
  `vcgencmd get_throttled`.
* The player is not GPU-bound but it does push ~8 MB/frame to the framebuffer;
  an active cooler keeps the A76 at full clock.
* The runtime needs ~200 MB (chroot + DirectFB + shims); the DirectFB build tree
  wants ~1 GB while it runs and can be deleted afterwards (`/opt/rb4r5/work`).
