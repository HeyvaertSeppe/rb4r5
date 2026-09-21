# Documentation

| Doc | Contents |
|---|---|
| [00-overview](00-overview.md) | Goal, architecture, why a Pi 5 can run this player |
| [01-install](01-install.md) | From a blank SD card to a running player |
| [02-hardware](02-hardware.md) | Pi 5 vs XDJ-RX3 (vs the Chromebit port), screen, FLX4 |
| [03-payload](03-payload.md) | The firmware you must supply, and how to extract it |
| [04-display](04-display.md) | DirectFB on `/dev/fb0`, the scale/convert path, the five fixes |
| [05-audio](05-audio.md) | FLX4 master + headphone cue, `audioshim`, the HDMI fallback |
| [06-controller](06-controller.md) | The DDJ-FLX4 MIDI map, the bridge, the FIFO protocol |
| [07-touch](07-touch.md) | Touch zones, calibration, and the native-touch caveat |
| [08-usb-library](08-usb-library.md) | Mounting a rekordbox stick so DeviceSQL imports it |
| [09-launcher](09-launcher.md) | Every command, the config file, the boot service |
| [10-troubleshooting](10-troubleshooting.md) | Symptom → cause → fix |
| [11-porting-notes](11-porting-notes.md) | What changed from the Chromebit port, what is unverified |

Conventions:

* `PI$` / `PI#` — a shell on the Pi, as your user / as root
* `<payload>` — `/opt/rb4r5/payload` unless you changed `paths.payload`
* `<chroot>` — `/opt/rb4r5/chroot`, the soft-float XDJ-RX3 userland
* "the player" / `rbp` — the XDJ-RX3's own rekordbox application

Related projects, external to this one:

* [PrimeBox](https://github.com/erhan-/PrimeBox) — the same player on a Denon
  Prime GO. The firmware extraction and patch tooling, the shim approach and the
  DirectFB groundwork come from there.
* `rb2go` — the same player on a phone under postmarketOS. The `UpdateRegion`
  display fix, the keyboard input path and several crash guards come from there.
* `rbtv` — the Chromebit port this repository is a rework of.
