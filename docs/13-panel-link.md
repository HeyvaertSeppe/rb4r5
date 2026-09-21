# 13 — The panel link (subucom), and what it would unlock

## What it is

The XDJ-RX3's application talks to its front panel over an SPI link it calls
**subucom**. Everything the panel shows goes down it — every button LED, every
pad colour, the level meters, the end-of-track flash — and everything the panel
does comes back up it. On a Pi there is no panel, so the chroot stubs the four
device nodes with FIFOs:

```
<chroot>/dev/subucom_spi1.0      <chroot>/dev/subucom_spi_rdy3.0
<chroot>/dev/subucom_spi2.0      <chroot>/dev/subucom_spi_rdy4.0
```

## Why it is drained now

A FIFO with no reader takes one pipe buffer — 64 KiB on Linux — and then
**blocks the writer**. The player writes panel frames continuously, so sooner
or later its panel thread stops inside `write()` and stays there. From the
outside that is the UI freezing for no reason and nothing in any log.

`rbpanel` (`rb4r5/subucom.py`, started by the supervisor) reads and discards
them, which is not optional. If the screen has ever frozen on you, this is a
candidate.

## Why it is captured

It is the only place the player says what it thinks the lights should be
doing. Decoding it is what would make:

* the launcher's top bar light from the player's state instead of from ours;
* the **FLX4's own LEDs** — every button and pad — mirror the RX3;
* the end-of-track behaviour work, because the flash the RX3 does on its pads
  is a panel frame, and so is the track position that drives it.

Nobody has decoded it for this player. This module does the part that can be
done without guessing: capture the stream, split it into frames, and say which
bytes and bits moved while you did one thing.

## Decoding it, one control at a time

The whole method is: hold still, do one thing, see what moved.

```sh
sudo python3 launch.py run                       # the player must be running
sudo python3 launch.py subucom --watch           # live frames, changes marked
sudo python3 launch.py subucom --learn "cue 1"   # settle, then press CUE once
```

`--learn` prints something like:

```
  subucom_spi1.0:
    byte  14 changed   2x   0a->0b (bit0+); 0b->0a (bit0-)
    byte  31 changed  88x   3f->41 (bit0+ bit1+ bit6+)
```

Byte 14 bit 0 went on when you pressed and off when you let go: **that is CUE
on deck 1**. Byte 31 changing 88 times is a meter or a clock, not a control.
Write the mapping down, do the next control.

To record a session for later:

```sh
sudo python3 launch.py config --set panel.capture=true
sudo python3 launch.py run                       # ... use it for a while
sudo python3 launch.py subucom --replay /var/log/rb4r5/subucom.bin
```

The capture stops at 8 MiB, and each frame carries its node, timestamp and
payload, so a replay shows exactly what a live watch would have.

## What to do with a decoded byte

`/tmp/rb-panel.dat` carries the last frame from each node, refreshed as they
arrive — so once a byte's meaning is known, both the top bar and (with MIDI
output added to `flx4-bridge`) the controller's LEDs can read it directly.
Until then the bar lights from the launcher's own state, which is honest but
is not the player's.
