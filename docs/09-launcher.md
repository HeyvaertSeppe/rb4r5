# 09 — The launcher

One Python program does everything: provision the Pi, build the runtime, run and
supervise the player, and diagnose it. It needs nothing but `python3` from a
stock Raspberry Pi OS image.

```sh
sudo python3 launch.py            # do whatever is missing, then run
```

## Commands

| Command | What it does |
|---|---|
| *(none)* / `auto` | Provision if this is a first run, build if the runtime is incomplete, then run and supervise. |
| `setup` | Provision only: packages, `config.txt`/`cmdline.txt`, console handover, sysctl, udev, the boot service, default config files. `--no-service` skips enabling the service; `--undo` reverts everything; `-y` does not ask. |
| `firmware` | Get the XDJ-RX3 firmware and turn it into a ready payload: download it from AlphaTheta (or use a local copy), decrypt, unpack the ISO, the fonts and the rootfs. `--upd FILE` uses your own file or zip, `--key FILE` names the key, `--offline` never touches the network, `--ask` shows a file picker, `--show` just reports, `--force` redoes it. |
| `payload` | Assemble `<chroot>` from the unpacked firmware (runs `firmware` first if needed). `--force` overwrites. |
| `build` | Host daemons, LD_PRELOAD shims, the patched player, DirectFB, then install into the chroot. `--no-directfb`, `--no-player`, `--fast-directfb`. |
| `run` | Prepare the screen and the chroot, start everything, supervise it. `--detach` starts and returns. |
| `stop` | Stop the player and its daemons, release the bind mounts. `--restore-console` gives the text console back. |
| `status` | What is running, plus whether the framebuffer has content. |
| `doctor` | Check every subsystem and print what is wrong and how to fix it. `-v` adds the zone list. Exit 1 if anything is broken. |
| `verify` | Prove it works on this machine: screenshot the player, check it fills the screen, check audio is running through the FLX4, then walk **every** FLX4 control and report which ones reached the engine. `--quick` does seven instead of 33, `--timeout` sets the wait per control, `--no-controller` / `--no-touch` skip a section. |
| `probe` | Read the player's live USB/database state out of its memory. Exit 0 when USB1 is ready. |
| `keys` | Inject a key: `keys play 1`, `keys 0x4101 2`, `keys selector 1 --op rotate --value -1`, `keys --list`. |
| `calibrate` | Show which zone each touch hits (does not drive the engine). |
| `zones` | Print and validate the touch layout; `--write-default` restores it. |
| `audio` | Show the ALSA cards, the device that will be used and why; `--env` prints what the shim gets. |
| `logs` | Tail the logs: `logs -f`, `logs rbp.log`, `logs -n 100`. |
| `screenshot` | Screenshot the player right now, into `/var/log/rb4r5/screenshots`. `-n 5 -i 2` takes a burst, which is how you capture the UI reacting to a control. |
| `fbdump` | The same thing to an exact path (`/tmp/rb4r5-screen.png` by default). |
| `service` | `install`, `remove`, `start`, `stop`, `restart`, `status`, `enable`, `disable`. |
| `config` | Print the effective configuration, or change it: `config --set audio.channels=2`. |
| `touchd`, `usbwatch` | The daemons themselves; the supervisor runs these, and you can run one by hand to watch it work. |

Global options: `-v` (show every command run), `-c FILE` (use another config
file), `--version`.

## What `run` actually does

1. **Preflight** — platform checks (is this a Pi 5, does the kernel run 32-bit
   ARM, is there a framebuffer, is a compositor in the way) and the runtime's
   completeness. It stops with an explanation rather than failing halfway.
2. **Stop anything stale** — two players fight over the screen and the subucom
   FIFOs, so any leftover `rbp`/`edb_streamd` is killed first. Processes are
   matched by whole argv entry, not by substring, so this cannot kill your own
   shell (which is exactly what `pkill -f "rbp -a"` used to do).
3. **Prepare the screen** — silence kernel messages, stop the cursor, disable
   console blanking, detach `fbcon` (`display.quiet_console`).
4. **Prepare the runtime** — create the RX3 device stubs (FIFOs for what the
   player polls, plain files for ioctl-only devices, and `/dev/paudiog0`
   *removed*, because its presence makes JUCE take a dead gadget-audio path),
   create the `/tmp` FIFOs, bind-mount `/dev /proc /sys /tmp` into the chroot.
5. **Choose the audio device** and unmute whatever mixer controls exist.
6. **Start the children**, each with its own log in `/var/log/rb4r5`:

   | Child | Why in this order |
   |---|---|
   | `edb_streamd` | DeviceSQL; the player looks for its sockets at startup |
   | `rbp` | the player |
   | `flx4-bridge` | waits for the controller by itself |
   | `rbtouchd` | waits for the panel by itself |
   | `rbkeyd` | optional keyboard control |
   | `usbwatch` | mounts and announces the library stick |

7. **Supervise** — restart anything that dies. The player gets a budget
   (`player.max_restarts`, default 10 per hour); past that the launcher gives up
   and says where to look, instead of hiding a real fault in a restart loop.
   `SIGTERM`/`SIGINT` stops all children and releases the mounts.

## The boot service

`setup` installs and enables `systemd/rb4r5.service`, which is simply
`launch.py run` as root, with:

* `Conflicts=getty@tty1.service` — a login prompt would paint over the player
* `Restart=on-failure`, `StartLimitBurst=5` in 10 minutes
* `Nice=-5` and best-effort I/O priority
* output appended to `/var/log/rb4r5/supervisor.log`

```sh
PI$ sudo systemctl status rb4r5
PI$ sudo journalctl -u rb4r5 -f
PI$ sudo systemctl disable rb4r5        # stop starting at boot
```

## The configuration file

`/etc/rb4r5/config.json`, created by `setup`; `config/config.example.json` in
the repository is the same content. Anything not mentioned keeps its default, so
the file can be as small as you like. Change values with `launch.py config --set
key=value` (JSON values: `true`, `4`, `"1920x1080@60"`, `null`) or edit it.

| Section | Keys that matter |
|---|---|
| `paths` | `chroot`, `payload`, `work`, `bin`, `logs`, `media` |
| `firmware` | `auto_download`, `url`, `version`, `expect_upd_size`, `verify_rbp_md5` ([03](03-payload.md)) |
| `display` | `force_mode`, `hdmi_port`, `rotate`, `quiet_console` ([04](04-display.md)) |
| `audio` | `card`, `device`, `channels`, `rate`, `format`, `plug`, `cue_mirror`, `cue_on_stereo`, `startup_mute_ms`, `fallback_hdmi` ([05](05-audio.md)) |
| `controller` | `enabled`, `name`, `jog_ppr`, `filter_init`, `map_file` ([06](06-controller.md)) |
| `touch` | `enabled`, `device`, `swap_xy`, `invert_x/y`, `zones_file`, `tap_ms`, `tap_slop`, `scroll_step`, `jog_scale`, `native`, `native_format` ([07](07-touch.md)) |
| `keyboard` | `enabled`, `device`, `name` |
| `usb` | `enabled`, `poll`, `confirm_tries`, `confirm_interval` ([08](08-usb-library.md)) |
| `player` | `args`, `restart`, `restart_delay`, `max_restarts`, `ulimit_procs`, `env` |
| `build` | `primebox`, `neon`, `jobs` |

Two other editable files live beside it: `touch-zones.json` (the touch layout)
and `flx4-map.conf` (extra controller bindings).

## Logs

| File | What is in it |
|---|---|
| `/var/log/rb4r5/rbp.log` | the player's own stdout/stderr |
| `/var/log/rb4r5/flx4-bridge.log` | every MIDI event it mapped (with `controller.verbose`) |
| `/var/log/rb4r5/touchd.log` | the touch device it found, and gestures with `touch.verbose` |
| `/var/log/rb4r5/usbwatch.log` | mounts, announcements, the confirm loop |
| `/var/log/rb4r5/edb_streamd.log` | DeviceSQL |
| `/var/log/rb4r5/supervisor.log` | the launcher itself, when run as a service |
| `/var/log/rb4r5/screenshots/` | PNGs of the player: one captured 25 s after every start, plus whatever `verify` and `fbdump` save |
| `/var/log/rb4r5/verify-report.txt` | the last verification run |
| `/tmp/audioshim.log` | ALSA negotiation and periodic peak levels — the first place to look for audio |
| `/tmp/keyshim.log` | every key/control the engine actually received |
| `/tmp/flipdbg.log` | the display driver's publish log (with `RB_DFB_DEBUG=1`) |

The shim logs are in `/tmp` because that is the one writable path shared between
the chroot and the host.

## Offline tests

```sh
$ tools/tests/run-all.sh                       # everything below, in one go
$ python3 tools/tests/test_firmware_decrypt.py # .UPD decryption vs openssl
$ python3 tools/tests/test_firmware_fetch.py   # where the firmware comes from
$ python3 tools/tests/test_control_chain.py   # all 33 FLX4 controls, end to end
$ python3 tools/tests/test_verify.py          # the full-screen probe, log watching
$ python3 tools/tests/test_firmware_pipeline.py# .UPD -> complete payload
$ python3 tools/tests/test_cramfs.py           # the cramfs reader
$ python3 tools/tests/test_audio_parse.py      # ALSA parsing and device choice
$ python3 tools/tests/test_touch_gestures.py   # gestures -> engine controls
$ python3 tools/tests/test_provision_edits.py  # boot-config edits are safe
$ tools/flx4-selftest.sh                       # synthetic MIDI through the bridge
```

None of them need hardware, the firmware, or root (the selftest needs a built
`flx4-bridge`). They are what makes it possible to change this code without a
Pi on the desk.
