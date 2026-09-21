# 01 — Install

From a blank SD card to a Pi that boots into the rekordbox player.

## 1. Raspberry Pi OS

Flash **Raspberry Pi OS Bookworm** (64-bit is what this was written against;
32-bit works too). Either *Lite* or the desktop image is fine — the launcher
switches the Pi to console mode, which is what frees the framebuffer for the
player.

In Raspberry Pi Imager, pre-set a hostname, your user and SSH; you will want SSH
because the screen belongs to the player once it starts.

```sh
PI$ sudo apt update && sudo apt full-upgrade     # a current kernel and firmware
PI$ sudo reboot
```

## 2. Get rb4r5

```sh
PI$ git clone https://github.com/HeyvaertSeppe/rb4r5
PI$ cd rb4r5
```

## 3. Provision the Pi

```sh
PI$ sudo python3 launch.py setup
```

This is idempotent and every change it makes is reversible
(`sudo python3 launch.py setup --undo`). It:

* installs the runtime packages (`alsa-utils`, `exfatprogs`, …),
* makes sure the KMS driver is enabled in `config.txt`, so `/dev/fb0` exists,
* quiets the console: `loglevel=3`, `consoleblank=0`, `logo.nologo`,
  `kernel.printk`, systemd's `[ OK ]` lines,
* **switches the boot target to `multi-user.target` and disables `getty@tty1`**
  — a compositor or a login prompt would own (or paint over) the framebuffer,
* installs the udev rules and the `rb4r5.service` boot service,
* writes `/etc/rb4r5/config.json`, `/etc/rb4r5/touch-zones.json` and
  `/etc/rb4r5/flx4-map.conf` if they are not there yet.

Reboot if it says a desktop is still running.

## 4. The firmware

Nothing to do. The launcher downloads the official XDJ-RX3 v1.20 update package
from AlphaTheta and unpacks it by itself:

```sh
PI$ sudo python3 launch.py firmware        # or just let step 5 do it
```

If the Pi has no network, put AlphaTheta's zip (or the `.UPD` from it) in
`/opt/rb4r5/payload` and it is used instead. The firmware **key** is the only
thing that may need your help — it is searched for automatically, including
inside AlphaTheta's GPL archives if you have one on the Pi, and you are asked
only if nothing works. See [03-payload.md](03-payload.md).

The build also needs the PrimeBox tooling (the player's patch set and the
DirectFB base diff); it is cloned into `/opt/rb4r5/PrimeBox` automatically.

## 5. Build

```sh
PI$ sudo python3 launch.py build
```

On a Pi 5 this takes roughly:

| Step | Time |
|---|---|
| downloading the firmware (66 MB) | depends on your line |
| decrypting and unpacking it | under a minute |
| host daemons (`flx4-bridge`, `rbkeyd`, `fakekbd`) | seconds |
| assembling the chroot from the payload | 1–2 min |
| the four LD_PRELOAD shims | seconds |
| patching the player | seconds |
| DirectFB 1.4.16 (clone + patch + build) | 10–20 min, once |

Later runs are much faster; `--fast-directfb` rebuilds only the fbdev driver,
and `--no-directfb` / `--no-player` skip those stages.

Every artefact's md5 is printed. The build **fails loudly** if a shim ends up
referencing a glibc symbol version the RX3's glibc 2.13 cannot resolve — that
is the mistake that otherwise shows up much later as "the player will not
start".

## 6. Run

```sh
PI$ sudo python3 launch.py run        # foreground, Ctrl-C stops everything
PI$ sudo python3 launch.py doctor     # check every subsystem
PI$ sudo python3 launch.py status     # what is running right now
```

The service was enabled by `setup`, so a reboot comes up in the player with
nothing else needed:

```sh
PI$ sudo systemctl start rb4r5        # same thing, via systemd
PI$ sudo systemctl stop rb4r5
PI$ journalctl -u rb4r5 -f            # or: sudo python3 launch.py logs -f
```

## 7. Plug the hardware in

* **22″ touchscreen**: HDMI into the port nearest the USB-C socket (HDMI0), and
  the panel's USB cable into any USB port. `launch.py doctor` lists the touch
  device it found and its axis ranges.
* **DDJ-FLX4**: straight into the Pi, or through a **powered** hub. A
  bus-powered hub cannot supply it (that failure is a stream of
  `device not accepting address … error -71` in `dmesg`).
* **rekordbox stick**: any USB port; the watcher finds it, mounts it and tells
  the engine. The Pi's own boot disk is never a candidate.

## What "working" looks like

```sh
PI$ sudo python3 launch.py doctor
...
=== display
  framebuffer          /dev/fb0 present
  geometry             1920x1080 @32bpp (vc4drmfb), stride 7680, pan 0,0
  scaling              1280x800 RGB565 (RX3 UI) -> 1920x1080 @32bpp, scaled in …
  content              398412 non-zero bytes in the first 400k (something is drawn)
=== audio
  card 2 [FLX4] USB-Audio - DDJ-FLX4 [4ch S24_3LE 44100Hz]
  chosen: plughw:CARD=FLX4,DEV=0,plughw:2,0 (4ch @44100 Hz, S24_LE, source=controller)
  routing: master -> FLX4 ch 1/2 (MASTER out), cue -> ch 3/4 (HEADPHONES)
=== controller (DDJ-FLX4)
  card                 2 [FLX4] USB-Audio - DDJ-FLX4
  midi                 /dev/snd/midiC2D0
...
no blocking problems found.
```

If something is missing, [10-troubleshooting.md](10-troubleshooting.md) is
organised by symptom.
