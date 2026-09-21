# 08 — The rekordbox library on a USB stick

A real rekordbox stick is presented to the player exactly the way the XDJ-RX3
presents its own USB1 slot, so the engine's **own DeviceSQL import** of
`PIONEER/rekordbox/export.pdb` runs and the UI shows the full library — songs,
playlists, categories — rather than a folder listing.

This is the device path, not "emulate a folder as a stick": the volume label,
capacity and free space come from the filesystem, and the song and playlist
counts come from Pioneer's own database import.

## The chain

```
stick plugged in
  → usbwatch mounts /dev/sdX1 at /media/usb1/sda1   (the RX3's own vfat options)
  → bind-mounts it into the chroot at <chroot>/media/usb1/sda1
  → writes "umount /media/usb1/sda1" then
           "mount  /media/usb1/sda1"   to  /tmp/udev_usb1
       │
       ▼  ui::UsbMountManager::run()            (the player's FIFO reader thread)
       ▼  ui::UsbStorageManager::notify_usb_mount()
       ▼  ui::DbProxy::reqAttach()
       ▼  db::DbIF::mount(path, type=3)
       ▼  Total_MainUsbMessageProc → SetMountInfo_DriveLetter / DeviceDetectFlg
       ▼  DeviceSQL (edb_streamd) imports PIONEER/rekordbox/export.pdb
       ▼  the detect flag for media kind 2 becomes 2 (ready)
       ▼  the SOURCE screen shows "USB1 <label>" with Songs / Playlists / …
```

`/tmp` is bind-mounted into the chroot, which is how a daemon outside it can
write a FIFO the player reads inside it.

## Three things that are easy to get wrong

* **The unmount must come first.** The player's `PathDecider` ignores a `mount`
  that was not preceded by an `umount` ("UNMOUNT was not executed before").
  `usbwatch` always sends the pair.
* **A mount event sent too early is dropped silently.** The FIFO reader thread
  exists long before the database subsystem is ready, so the write succeeds and
  nothing happens. `usbwatch` therefore **re-announces** the stick every
  `usb.confirm_interval` seconds (default 5) up to `usb.confirm_tries` times
  (default 18 → 90 s), stopping as soon as the engine reports the database
  ready. That is read straight out of the player's memory — see below.
* **Never write to `/tmp/udev_usbctn*`.** A "connect" event on those FIFOs
  raises the cosmetic caution *"USB Error. Remove the device."* The mount event
  alone is enough, and `usbwatch` never touches them.

## Which device gets used

Several storage devices can be present at once (a stick *and* a card reader, and
the Pi's own boot disk). The watcher:

1. considers only `sd*` devices **on a USB bus** with a non-zero size (an empty
   card reader reports size 0 and is skipped),
2. **never** considers a disk carrying `/`, `/boot` or `/boot/firmware`,
3. prefers a device that actually carries `PIONEER/rekordbox/export.pdb` —
   checked with a read-only probe mount, or by looking at where it is already
   mounted,
4. otherwise takes the first device with a recognised filesystem (the player
   then shows the FOLDER view),
5. re-evaluates whenever the set of devices changes, so plugging the stick in
   *after* the card reader still works.

```sh
PI# python3 launch.py doctor     # includes the candidate list and the choice
  /dev/sda    58600 MB  Ultra Fit            part=/dev/sda1 fs=vfat label=MY MUSIC
  would use: /dev/sda1 (rekordbox library)
```

Mount options are the RX3's own for FAT:
`flush,rw,noatime,shortname=mixed,dmask=000,fmask=000,codepage=437,iocharset=iso8859-1,usefree,utf8`,
with `exfat` and `hfsplus` handled too.

## Watching the engine's own state

`launch.py probe` reads the player's mount-info tables out of
`/proc/<pid>/mem` (root only) and prints what the engine believes:

```sh
PI# python3 launch.py probe
player pid 2214
host mount:  /media/usb1/sda1 <- /dev/sda1 (vfat)
export.pdb:  3448832 bytes
chroot bind: yes
kind 2 USB1: detect=2 (ready) label='My Music' songs=2174 playlists=160 db_ready=1 cap=61.5GB free=12.8GB
kind 3 USB2: detect=0 (absent)
uiConnectedMedia = 0x2 (bit1 USB1: set)   uiBrowse = 12
```

* `detect` is `0` absent, `1` analysing, `2` ready. The exit status is 0 when
  USB1 is ready, so this works as a health check in a script.
* **USB1 is "media kind 2"** — the engine inherited the CDJ-3000 media model, in
  which kind 2 is the SD slot and kind 3 is USB2. Only one port is watched here,
  so kind 3 stays absent and there is no phantom USB2.
* `uiConnectedMedia` bit 1 is only recomputed while the UI is on the source
  screen (`uiBrowse == 12`); it reads 0 on the deck view even when the drive is
  ready. That is not a fault.

## Configuration

```json
"usb": {
  "enabled": true,
  "poll": 1.0,              ← seconds between scans
  "confirm_tries": 18,      ← how many times to re-announce
  "confirm_interval": 5.0,
  "require_usb": true       ← only removable USB devices are candidates
}
```

`"enabled": false` turns the watcher off entirely (the player still runs; you
just have no library).

## What the engine needs on the stick

Formatted **FAT32** (or exFAT), with a rekordbox export:

```
PIONEER/rekordbox/export.pdb      the database (what makes it a library)
PIONEER/USBANLZ/…                 waveform / beatgrid analysis
Contents/…                        the audio files
```

`export.pdb` is optional — without it the player shows a plain FOLDER view and
can still load tracks — but with it you get playlists, categories, waveforms and
beatgrids, which is the whole point of the RX3 engine.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `detect=0` while the stick is mounted | the mount event arrived before the DB subsystem was ready | the confirm loop re-sends it; watch `usbwatch.log` for "database ready" |
| `detect` never leaves `1` | `edb_streamd` is not running or crashed | `launch.py logs edb_streamd.log`; remove stale `/tmp/{guard,req}_LocalDBServer` and restart |
| The drive appears with 0 songs | no `export.pdb`, or it is unreadable | check the path above; re-export from rekordbox |
| "USB Error. Remove the device." | something wrote to `/tmp/udev_usbctn*` | not us; do not echo into those FIFOs |
| The stick is not found | it is not on a USB bus, or reports size 0 | `launch.py doctor` lists candidates and says why |
| The Pi's own disk got mounted | cannot happen: disks carrying `/` or `/boot` are excluded | — |
| The library disappears after a player restart | the watcher re-announces automatically | check `usbwatch` is running (`launch.py status`) |
