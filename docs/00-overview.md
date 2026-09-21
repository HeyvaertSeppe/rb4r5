# 00 — Overview

## Goal

Run the **XDJ-RX3's own rekordbox player** (`rbp`, the standalone application
from Pioneer's firmware) on a **Raspberry Pi 5**, natively — no emulation —
with:

* the UI **full screen** on a 22″ HDMI panel,
* **audio out of the DDJ-FLX4**: master on its RCA/main outputs and a real
  **headphone cue** on its headphone jack,
* the **DDJ-FLX4 as the control surface** (it replaces the RX3's front panel),
* the **touchscreen** driving the UI,
* the rekordbox **library from a USB stick**, imported by the engine's own
  DeviceSQL,
* one **Python launcher** that sets the machine up and starts everything,
  on demand and at boot.

## Why this works at all

The XDJ-RX3 firmware ships its player as a **32-bit ARM soft-float EABI5**
binary against **glibc 2.13**. Two facts make that runnable here:

1. **The Pi 5 executes 32-bit ARM userspace.** Cortex-A76 supports AArch32 at
   EL0 and Raspberry Pi's `bcm2712` arm64 kernel is built with
   `CONFIG_COMPAT=y`, so a 32-bit ELF runs on the stock 64-bit image. The
   launcher proves this at runtime rather than assuming it (it executes a tiny
   ARM binary it builds itself, and later the chroot's own `ld-linux.so.3`).
2. **The float ABI of the host userland does not matter.** The kernel does not
   care whether userspace is soft- or hard-float; the player just needs to be
   given the RX3's own soft-float userland, which is what the chroot is for.

This is the same approach that already works on the Denon Prime GO (PrimeBox),
on a POCO X3 phone (rb2go) and on an ASUS Chromebit (rbtv). The Pi 5 is the
easiest of the four: four fast cores, a normal Debian userland with cross
compilers in `apt`, four USB ports, and a DRM driver that hands out a plain
framebuffer.

## The pieces

```
┌───────────────────────────── Raspberry Pi 5 (BCM2712) ─────────────────────────────┐
│  Raspberry Pi OS Bookworm, console only (no compositor - the player owns fb0)      │
│                                                                                    │
│  launch.py  (supervisor: prepares, starts, restarts, diagnoses)                    │
│     │                                                                              │
│     ├── flx4-bridge ── USB MIDI ─────────────┐                                     │
│     ├── rbtouchd ───── evdev (touch panel) ──┤ 12/24-byte records                  │
│     ├── rbkeyd ─────── evdev (keyboard) ─────┤ via /tmp/rb-keys.fifo               │
│     │                                        │     and /tmp/rb-ctrl.fifo           │
│     ├── usbwatch ───── mounts the stick, writes /tmp/udev_usb1                     │
│     │                                        │                                     │
│  ┌──┴───────────────── /opt/rb4r5/chroot ────┼──────────────────────────────────┐  │
│  │  soft-float glibc 2.13 + RX3 libraries + DirectFB 1.4.16 (rebuilt)           │  │
│  │                                           │                                  │  │
│  │   rbp  ── the XDJ-RX3 rekordbox player ◄──┘                                  │  │
│  │     ▲  ▲  ▲  ▲                                                               │  │
│  │     │  │  │  └── keyshim.so    FIFO records → IKeyManager::sendKey            │  │
│  │     │  │  └───── audioshim.so  RX3 DAC devices → the FLX4's 4-ch PCM          │  │
│  │     │  └──────── fbshim.so     "the panel is 1280x800 RGB565"                 │  │
│  │     └─────────── memshim.so    /dev/mem, mmap, GPIO poll, touch record        │  │
│  │                                                                              │  │
│  │   libdirectfb_fbdev.so (patched) ── RGB565 → XRGB8888 + scale → fb0           │  │
│  └──────────────────────────────────────────────────────────────────────────────┘  │
│        ▲                  ▲                    ▲                  ▲                │
│    /dev/fb0        /dev/snd (FLX4)      /dev/input/event*     /media/usb1          │
│  (vc4-kms-v3d)     USB audio+MIDI       touch + keyboard      rekordbox stick      │
└────────────────────────────────────────────────────────────────────────────────────┘
```

Four shims are `LD_PRELOAD`ed into the player, in this order (the first
definition of a symbol wins, and `memshim` must be able to see every `open`):

```
/usr/lib/memshim.so : /usr/lib/fbshim.so : /usr/lib/audioshim.so : /usr/lib/keyshim.so
```

## What each layer is for

* **`memshim`** — the player was written for an i.MX6. It reads i.MX6 registers
  through `/dev/mem`, polls a GPIO driver that does not exist here, and reads a
  tsc2007 touch device. The shim denies `/dev/mem` (and fixes the broken `mmap`
  that follows), parks the GPIO poll loops so two threads do not spin a core
  each, and answers the touch device — either "no touch" or, optionally, the
  contact the touch daemon published.
* **`fbshim`** — the player and DirectFB must believe the framebuffer is the
  RX3's 1280×800 RGB565 panel. The shim answers the `FBIOGET_*` ioctls with
  exactly that and swallows mode changes. The real geometry is read by the
  driver with a raw ioctl that bypasses the shim.
* **`audioshim`** — the player opens `hw:cs4344audiorev8,0|1|2` (three stereo
  DACs) and the matching control device. The shim presents those as virtual
  handles and muxes them onto one real PCM. It matters more than it sounds:
  the transport is clocked by the ALSA callback, so **no running PCM means no
  playback and a frozen waveform**, even though the UI still repaints.
* **`keyshim`** — the RX3 gets its button presses from front-panel
  microcontrollers. The shim reads records from two FIFOs and calls
  `IKeyManager::sendKey` directly, starts the message-pump thread that
  dispatches them, clears a stale caution that would gate browsing, and pushes
  sane mixer defaults (with no sub-MCU the engine's faders start at zero).
* **the patched DirectFB fbdev driver** — converts and scales every frame into
  the real framebuffer, and publishes on both `FlipRegion` *and* `UpdateRegion`
  (the player redraws through the blit path, so a driver that only handles
  flips shows exactly one frame).

## What is not emulated

Nothing. The player runs as the ARM binary it is, on this kernel, at full
speed. What is *translated* is the hardware it expects: a MIDI controller
becomes front-panel keycodes, a USB panel becomes touch events, a USB audio
interface becomes three Pioneer DACs, an HDMI monitor becomes a 1280×800
RGB565 panel.

Next: [01-install](01-install.md), then the subsystem docs.
