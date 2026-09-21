# Notice, copyright and legal

This is an independent **interoperability / preservation** project. It is not
affiliated with, endorsed by, or sponsored by Pioneer DJ, AlphaTheta, the
Raspberry Pi Foundation or Raspberry Pi Ltd, Broadcom, Denon DJ / inMusic, or
any of their subsidiaries.

## What this repository contains

* Original code and documentation written for this project — the Python
  launcher, the LD_PRELOAD shims, the DDJ-FLX4 bridge, the touch daemon, the
  DirectFB patches, the build and service glue. **MIT** licensed (see
  [LICENSE](LICENSE)).
* Code adapted, with attribution, from sibling projects that run the same
  player on other hardware: [PrimeBox](https://github.com/erhan-/PrimeBox)
  (Denon Prime GO), `rb2go` (postmarketOS phone) and `rbtv` (ASUS Chromebit).
  Where a file came from one of those, its header says so.
* Patches *against* DirectFB 1.4.16, which is **LGPL-2.1**. The patches are
  ours; DirectFB itself is fetched at build time from its own upstream and
  keeps its own licence.

## What this repository does **not** contain

* **No XDJ-RX3 firmware** (`.UPD`), no decrypted firmware image, no `rootfs`,
  no `gui/` assets and **no `rbp` player binary**. These are
  Pioneer/AlphaTheta property.

  The launcher *downloads* the official update package from AlphaTheta's own
  server (the same URL upstream's `get-firmware.sh` uses) and unpacks it on
  your machine, which is convenience, not redistribution: no vendor bytes are
  stored in this repository or served from it, and you can supply the file
  yourself instead (`--upd`, or drop it in the payload directory).
  See [docs/03-payload.md](docs/03-payload.md).
* **No firmware decryption key.** AlphaTheta published it in their own GPL
  source distribution; you obtain it yourself. It is gitignored here.
* **No rekordbox music, playlists, analysis data or databases** (`export.pdb`),
  and no Denon or Engine OS files.
* No Raspberry Pi OS components — you install that yourself from
  raspberrypi.com.

The `.gitignore` is deliberately aggressive about all of the above: firmware
images, keys, `rbp` binaries and `*.pdb` files cannot be committed by accident.

## Legal caveats

* Firmware decryption and reverse engineering may be restricted where you live
  (for example DMCA §1201 in the US, the EUCD in the EU). Check your local law.
* Patching and running a vendor application on third-party hardware is very
  likely to violate that vendor's EULA. This project is offered for research,
  repair, preservation and personal interoperability only.
* Nothing here is a commercial product, and none of it is fit for a paid gig
  without you testing it yourself first.
* Running this is at your own risk. It modifies your Pi's boot configuration
  (reversibly — `launch.py setup --undo`), takes over the screen, and drives
  audio hardware at full scale.

## Trademarks

*Pioneer DJ*, *AlphaTheta*, *rekordbox*, *XDJ-RX3*, *DDJ-FLX4*, *DDJ-400* and
*CDJ* are trademarks of AlphaTheta Corporation. *Denon DJ* and *Prime GO* are
trademarks of inMusic. *Raspberry Pi* is a trademark of Raspberry Pi Ltd.
*Broadcom* is a trademark of Broadcom Inc. *Rockchip* is a trademark of
Rockchip. *Mixxx* is a trademark of the Mixxx project. All are used here
descriptively and nominatively only.

## Credits

* [PrimeBox](https://github.com/erhan-/PrimeBox) — the firmware extraction and
  player patch tooling, the soft-float chroot recipe, the DirectFB groundwork
  and the original shims. This port would not exist without it.
* `rb2go` — the `UpdateRegion` display fix, the keyboard input daemon and
  several crash guards.
* `rbtv` — the Chromebit port this repository reworks: the display fixes, the
  audio shim, the USB library chain and the input FIFO protocol were all
  verified on hardware there.
* The [Mixxx](https://mixxx.org) project, whose `Pioneer-DDJ-FLX4.midi.xml`
  mapping is the source for the controller's MIDI addresses.
* DirectFB, JUCE, ALSA and the other open-source components inside the RX3
  firmware.
