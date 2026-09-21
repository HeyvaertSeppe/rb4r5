# 10 — Troubleshooting

Start here:

```sh
PI# python3 launch.py doctor
```

It checks every subsystem and prints the fix for anything it finds. What follows
is organised by what you are seeing.

## Nothing on the screen

First: `sudo python3 launch.py verify` samples the framebuffer and saves a
screenshot, so you can see what the player is actually putting out before
guessing.


| Symptom | Likely cause | Fix |
|---|---|---|
| Console text, no player | the player is not running | `launch.py status`, then `launch.py logs rbp.log` |
| A desktop | the boot target went back to `graphical.target` | `sudo systemctl set-default multi-user.target`, reboot |
| Black screen, the player *is* running | the driver is not publishing frames | `RB_DFB_DEBUG=1 launch.py run`, then `head /tmp/flipdbg.log`: `FLIP` followed by `UPDATE` lines is healthy ([04](04-display.md)) |
| Black screen, `/dev/fb0` missing | KMS is off | `dtoverlay=vc4-kms-v3d` in `config.txt`, reboot |
| One frame, then frozen | an unpatched DirectFB module (no `UpdateRegion`) | `launch.py build --fast-directfb` |
| The UI in the top-left corner, junk around it | the scale path is missing (same cause) | as above |
| Text or `[ OK ]` lines flickering over the UI | `display.quiet_console` is 0, or `getty@tty1` came back | `launch.py config --set display.quiet_console=2`; `systemctl disable getty@tty1` |
| The screen blanks after ten minutes | console blanking | `setup` adds `consoleblank=0`; check `cmdline.txt` |
| Colours look wrong / the image is torn | the framebuffer is not 16 or 32 bpp | `launch.py doctor` prints the geometry; pin a mode with `display.force_mode` |

## The firmware will not unpack

| Symptom | Cause | Fix |
|---|---|---|
| `none of the firmware sources worked` | no network, or every mirror is unreachable | fetch the zip on another machine, put it in `/opt/rb4r5/payload`, and re-run — or `--upd /path/to/file` |
| `that source returned a web page, not a file` | a mirror needs a login or a consent click | it moves on to the next source by itself; supply the file with `--upd` if they all do it |
| It downloads every time | the payload directory is not writable, so nothing is cached | check `/opt/rb4r5/payload/firmware` |
| It used the wrong file | a `.UPD` was already in the payload directory | remove it, or name the one you want with `--upd` |
| `downloaded N bytes, expected 69171216` | a truncated download, or a different firmware version | delete `/opt/rb4r5/payload/firmware` and re-run |
| `the player's md5 is …, not the stock v1.20` | a different firmware version | the published patch sets were derived from v1.20; expect the build to fail |
| Your file is listed as "does not look right" | the body is not 512-byte aligned plus a 16-byte trailer | it is probably not an XDJ-RX3 `.UPD` — a partial download does this too |
| `no working firmware key found` | no `aes256.key` anywhere it looked, or the one it found is for something else | get it from AlphaTheta's GPL source distribution ([03-payload](03-payload.md)) and drop it next to the `.UPD` |
| `decryption produced something that is not an ISO image` | wrong key, or a corrupt download | check the `.UPD` size (v1.20 is 69,171,216 bytes) |
| `no AES implementation available` | no `python3-cryptography` and no `openssl` | `sudo apt-get install python3-cryptography` |
| `cannot unpack the ISO on this machine` | not root, and no `bsdtar`/`7z` | run it with `sudo`, or `sudo apt-get install libarchive-tools` |
| `rootfs.cramfs is not in this ISO` | not XDJ-RX3 firmware, or an extraction that produced nothing | the message now lists what the ISO *did* contain — compare it with docs/03 |
| `the player (pdj/rbp) is not in this ISO` | the player lives inside `images/pdj.tar.gz`, which an older version never opened; or the ISO extracted without Rock Ridge names (`PDJ/RBP;1`) | both are handled now: `git pull`, then `sudo python3 launch.py firmware --force`. The message also prints the tree and where it found a file called `rbp` |
| The payload looks complete but the build cannot find libraries | an upper-cased tree that was not normalised (an old version) | `sudo python3 launch.py firmware --force` re-extracts and normalises |
| `not a cramfs image` / a block "does not decompress" | truncated or corrupt firmware | re-download and re-run with `--force` |
| It asks about the key every time | the payload directory is not writable, so the key was not remembered | check `/opt/rb4r5/payload` |
| `could not clone …/PrimeBox` | the Pi has no network | clone it elsewhere and `launch.py config --set build.primebox=/path/to/PrimeBox` |

## The player will not start

| Symptom | Cause | Fix |
|---|---|---|
| `cannot run the player: the kernel refused to execute a 32-bit ARM binary` | a kernel without `CONFIG_COMPAT` | boot the stock Raspberry Pi OS kernel |
| `the runtime is incomplete - missing: …` | the payload or the build is not there | [03-payload](03-payload.md), then `launch.py build` |
| `no such file or directory` running `rbp` | the chroot's loader is missing | `ls <chroot>/lib/ld-linux.so.3`; re-run `launch.py payload` |
| `GLIBC_2.28 not found` / `dlopen` failures in `rbp.log` | something was built against a modern glibc | rebuild: the build fails loudly on this, so a stale artefact is in the chroot |
| The player exits about a second after starting | the `getPcController` guard is missing | `python3 src/patch/patch-rbp-crashguards.py --check <chroot>/root/pdj/rbp` |
| It starts, then dies repeatedly | the launcher gives up after 10 restarts | `launch.py logs rbp.log`; a core dump loop can thrash the box — `echo /dev/null > /proc/sys/kernel/core_pattern` while debugging |
| `UiMain` crashes with `SIGSEGV` at `NULL+0x10` | `openDevice()` called more than once | fixed in `keyshim.c` (it opens the device at most once per episode); rebuild the shims |
| Two players fighting | a leftover process | `launch.py stop`, then `run`; the launcher also does this itself |

## No sound

| Symptom | Cause | Fix |
|---|---|---|
| The UI repaints but the playhead does not move | **no running PCM** — the transport is clocked by the ALSA callback | this is the big one; see [05-audio](05-audio.md) |
| `sampleRate:0 != 44100` in `rbp.log` | the control-device interception failed | rebuild `audioshim.so` |
| `Invalid value for card` in `rbp.log` | `audioshim` was not preloaded | check `/tmp/audioshim.log` exists at all |
| The FLX4 is not listed by `aplay -l` | not enumerated, or under-powered | plug it into the Pi or a **powered** hub; `dmesg \| tail` |
| Master works, headphones silent | no cue audio until PFL is pressed | press CUE on a channel, or `audio.cue_mirror: true` |
| Sound comes out of the TV instead | the controller was absent at startup | plug it in and restart the service |
| A loud pop when the player starts | the engine's power-on transient | already suppressed; `audio.startup_mute_ms` tunes it |
| Crackles and dropouts | under-voltage or thermal throttling | 27 W supply, active cooling; `vcgencmd get_throttled` |

## The controller does nothing

| Symptom | Cause | Fix |
|---|---|---|
| `flx4: waiting for a DDJ-FLX4…` | no matching ALSA card | `cat /proc/asound/cards`; `dmesg` for `error -71` (power) |
| The bridge logs events, the UI ignores them | the player or `keyshim` is missing | `ls -l /tmp/rb-ctrl.fifo`; `tail /tmp/keyshim.log` |
| It worked, then went dead after a restart | the FIFOs were recreated under the writers | the supervisor restarts the writers with the player; if you started them by hand, restart them |
| Faders snap back every 10 s | the synthetic mixer defaults are still being applied | that stops as soon as the control FIFO is fed; check the bridge is really running |
| Channel 2's fader moves deck 1 | mixer routing stuck on player 0 | `keyshim` pins it at startup; look for `mixer defaults sent` in `/tmp/keyshim.log` |
| Some buttons do nothing | deliberately unbound (no verified keycode) | [06-controller](06-controller.md) and `flx4-map.conf` |

## Touch does nothing / the wrong thing

| Symptom | Cause | Fix |
|---|---|---|
| `waiting for a touchscreen` | the panel's USB cable is not connected | HDMI alone carries no touch |
| Touches land mirrored or rotated | axis orientation | `touch.swap_xy` / `invert_x` / `invert_y`; verify with `launch.py calibrate` |
| Every touch reads `(no zone)` | orientation, or a broken zone file | `launch.py zones` validates it |
| Buttons work, the list will not scroll | you are tapping, or `scroll_step` is too big | drag further; lower `scroll_step` |
| The list scrolls the wrong way | the selector's sign | `"invert": true` on the list zone |
| The UI reacts in the wrong place | `touch.native` is on with the wrong record format | set `touch.native: false` ([07-touch](07-touch.md)) |

## The library does not appear

| Symptom | Cause | Fix |
|---|---|---|
| `detect=0` while the stick is mounted | the mount event was delivered too early | the confirm loop re-sends it; `launch.py logs usbwatch.log` |
| `detect` stays at `1` | `edb_streamd` missing or crashed | `launch.py logs edb_streamd.log`; delete `/tmp/{guard,req}_LocalDBServer` |
| The drive shows 0 songs | no `export.pdb` | re-export from rekordbox ([08](08-usb-library.md)) |
| "USB Error. Remove the device." | something wrote to `/tmp/udev_usbctn*` | not us |
| `uiConnectedMedia = 0` although it is ready | the mask is only recomputed on the SOURCE screen | press SOURCE; not a fault |

## Performance

| Symptom | Cause | Fix |
|---|---|---|
| The waveform scrolls sluggishly | the scalar convert path, or a throttled CPU | `build.neon: true` (default); check throttling |
| High idle load | the GPIO poll loops are spinning | `memshim` parks them; a stale shim is the usual reason |
| The whole box is slow after crashes | `systemd-coredump` compressing big cores | `echo /dev/null > /proc/sys/kernel/core_pattern` while debugging |

## Getting back to a normal Pi

```sh
PI# python3 launch.py stop --restore-console   # release the screen now
PI# python3 launch.py setup --undo             # undo every system change
PI# sudo reboot
```

`setup --undo` removes the service, the udev rules, the sysctl and systemd
drop-ins, restores `graphical.target` and `getty@tty1`, and takes our marked
block out of `config.txt` and `cmdline.txt` (backups are kept beside them as
`*.rb4r5.bak`). It does not delete `/opt/rb4r5`, so the runtime you built is
still there if you change your mind.
