# 06 — The DDJ-FLX4 as the control surface

The player has **no MIDI input at all**. It is the XDJ-RX3 application: it reads
keycodes from Pioneer front-panel microcontrollers through its internal
`IKeyManager::sendKey(keycode, op, ch, param, f, l)`. A DDJ-FLX4 is a plain
USB-MIDI class device, so nothing in the engine can see it.

So the FLX4 is **translated, not emulated**:

```
DDJ-FLX4 ──USB MIDI──▶ flx4-bridge ──24-byte records──▶ /tmp/rb-ctrl.fifo
                        (the Pi's own rootfs)                   │
                                                                ▼
                  keyshim.so ──▶ IKeyManager::sendKey(key, op, ch, param, f, l)
                  (LD_PRELOAD inside the soft-float chroot)     │
                                                                ▼
                                                      the UI and the engine
```

`flx4-bridge` reads the ALSA rawmidi character device directly and parses MIDI
itself — no libasound — so it builds with the Pi's own gcc in a second and has
no runtime dependencies. It waits for the controller, survives a replug, and
reopens the FIFO if the player restarts.

## The FIFO protocol

Two FIFOs, because a FIFO has no framing and the record size must be fixed:

```c
/tmp/rb-keys.fifo   12 bytes  { int32 key, ch, down }
/tmp/rb-ctrl.fifo   24 bytes  { int32 key, ch, op, param; float f; int32 l }
```

| `op` | Meaning | Payload |
|---|---|---|
| 0 / 2 | press / release | — |
| 4 `ROTATE` | 10-bit **absolute** for faders/EQ/trim/crossfader, **relative** ±1 for the browse knob, and jog rotation | `param`, `f` = normalised or rev/s, `l` = jog position |
| 5 `VALUE` | Sound Color FX, Beat FX depth, tempo slider | `param` = 10-bit, `f` = normalised (tempo −1…+1) |

While something is feeding `rb-ctrl.fifo`, `keyshim` **stops re-applying the
synthetic mixer defaults** — otherwise it would snap your physical faders back
every 10 seconds. (Those defaults exist because with no sub-MCU the engine's
faders come up at zero, i.e. silent.)

## The map

Source: the Mixxx `Pioneer-DDJ-FLX4.midi.xml` mapping, which encodes
AlphaTheta's own MIDI message list. MIDI channels below are 1-based, as
`flx4-bridge -s` prints them. Keycodes are the ones verified on hardware by the
Prime GO and Chromebit ports.

### Browser (MIDI channel 7)

| Control | MIDI | Engine |
|---|---|---|
| rotary selector | CC `0x40`, relative | `SELECTOR 0x420c` op 4 ±1 |
| rotary push | Note `0x41` | `SELECTOR` press |
| SHIFT + rotary push | Note `0x42` | `BACK 0x420d` |
| LOAD deck 1 / 2 | Note `0x46` / `0x47` | `LOAD 0x4311` ch 1 / 2 |
| SHIFT + LOAD 1 / 2 | Note `0x68` / `0x7A` | `SOURCE 0x0201` / `MENU 0x0206` |

### Deck (MIDI channels 1 and 2)

| Control | MIDI | Engine |
|---|---|---|
| PLAY/PAUSE | Note `0x0B` | `PLAY 0x4101` |
| SHIFT + PLAY (censor) | Note `0x0E` | `REV 0x410f` |
| CUE | Note `0x0C` | `CUE 0x4102` |
| BEAT SYNC | Note `0x58` | `SYNC 0x4112` |
| BEAT SYNC (long press) | Note `0x5C` | `MASTER 0x4111` |
| SHIFT + SYNC | Note `0x60` | `TEMPO_RANGE 0x4107` |
| LOOP IN / 4 BEAT | Note `0x10` | `LOOPIN 0x410c` |
| LOOP OUT | Note `0x11` | `LOOPOUT 0x410d` |
| RELOOP/EXIT | Note `0x4D` | `RELOOP 0x410e` |
| jog plate touch | Note `0x36` (SHIFT: `0x67`) | `JOG_TOUCH 0x4306` |
| jog platter, vinyl mode | CC `0x22`, relative | `JOG_ROT 0x4305` op 4, `f` = rev/s |
| jog platter, non-vinyl | CC `0x23` | `JOG_ROT` |
| jog side | CC `0x21` | `JOG_ROT` |
| SHIFT + platter (search) | CC `0x29` | `JOG_ROT` |
| tempo slider | CC `0x00` + `0x20` (14-bit) | `TEMPO_SLIDER 0x4109` op 5, `f` = −1…+1 |
| channel fader | CC `0x13` + `0x33` | `FADER 0x501e` op 4 |
| TRIM | CC `0x04` + `0x24` | `TRIM 0x5019` op 4 |
| EQ HI / MID / LOW | CC `0x07`/`0x0B`/`0x0F` (+`0x20`) | `EQH 0x501a` / `EQM 0x501b` / `EQL 0x501c` op 4 |

### Global mixer (MIDI channel 7)

| Control | MIDI | Engine |
|---|---|---|
| CROSSFADER | CC `0x1F` + `0x3F` | `XFADER 0x6017` op 4 |
| HEADPHONES MIXING | CC `0x0C` + `0x2C` | `HPMIX 0x4405` op 4 |
| HEADPHONES LEVEL | CC `0x0D` + `0x2D` | `HPLEVEL 0x4406` op 4 |
| FILTER ch 1 / ch 2 | CC `0x17` / `0x18` (+`0x20`) | `COLOR 0x509d` op 5 (Sound Color FX) |

### Performance pads (MIDI channels 8–11)

Channel 8 = deck 1, 9 = deck 1 + SHIFT, 10 = deck 2, 11 = deck 2 + SHIFT. The
note is **mode base + (pad − 1)**, and the bridge switches the engine's pad bank
before the first pad of a new mode:

| FLX4 pad mode | Note base | Engine bank |
|---|---|---|
| HOT CUE | `0x00` | `HOTCUE 0x4113` |
| BEAT JUMP | `0x20` | `BEATJUMP 0x4116` |
| SAMPLER | `0x30` | `SLIPLOOP 0x4115` (no sampler in this engine) |
| BEAT LOOP | `0x60` | `ALOOP 0x4114` |
| KEYBOARD `0x40`, KEY SHIFT `0x70`, PAD FX | — | no equivalent: logged and ignored |

Pads themselves are `PAD1…PAD8` = `0x4117…0x411e`.

### Beat FX (MIDI channels 5 and 6)

| Control | MIDI | Engine |
|---|---|---|
| BEAT FX SELECT (+SHIFT) | Note `0x63` / `0x64` | `BFXTYPE 0x448b` |
| BEAT ◀ / ▶ | Note `0x4A` / `0x4B` | `BEATPREV 0x4490` / `BEATNEXT 0x4491` |
| FX channel select CH1 / CH2 | Note `0x10` (ch 5) / `0x11` (ch 6) | `BFXCH 0x448c` op 5 = 1 / 2 |
| FX ON/OFF (+SHIFT) | Note `0x47` / `0x43` | `BFX 0x448d` |
| FX LEVEL/DEPTH | CC `0x02` | `DEPTH 0x448f` op 5 |

### Not bound

A few FLX4 controls have no keycode we could verify, so they are deliberately
left unbound rather than guessed at — pressing them logs a line with `-v` and
does nothing:

* **channel CUE / PFL** (note `0x54` on the deck channels) — headphone cueing
  still works through the audio path and the HEADPHONES MIXING knob
* **CUE/LOOP CALL ◀ ▶** (notes `0x51` / `0x53`), which would be loop halve and
  double
* SHIFT + CUE (jump to start), SHIFT + RELOOP (reloop and stop), the loop-in and
  loop-out adjusts, quantize, Smart CFX / Smart Fader behaviour

Bind any of them yourself in `/etc/rb4r5/flx4-map.conf` — no recompiling:

```
# note <midi_ch> <note> <keycode> <deck|global|1|2> [name]
note 1 0x54 0x5024 deck  channel cue deck 1

# cc14 <midi_ch> <msb_cc> <keycode> <rotate|value> <deck|global|1|2> [signed]
cc14 7 0x0D 0x4406 rotate global
```

A `note` line replaces an existing binding for the same channel and note;
keycode `0` removes one. `cc14` works the same way. The file is read at startup
and its entries are listed by `flx4-bridge -l -m <file>`.

## Finding a keycode on real hardware

```sh
PI# systemctl stop rb4r5                    # free the controller
PI# /usr/local/bin/flx4-bridge -s           # sniff: press the control
MIDI ch1  NOTE  84 (0x54) on
PI# systemctl start rb4r5
# then, with the player running, try candidate keycodes and watch the UI:
PI# python3 launch.py keys 0x5024 1
PI# tail -f /tmp/keyshim.log
```

`python3 launch.py keys --list` prints every keycode rb4r5 knows by name.

## Jog feel

`controller.jog_ppr` (default 1800) is the controller's pulses per revolution;
it scales the speed the engine is told. To calibrate, run the bridge with `-v`,
turn the platter exactly one revolution and add up the deltas. A too-low value
makes scratching hypersensitive, too high makes it sluggish.

## Checking every control on real hardware

```sh
PI# python3 launch.py verify            # all 33, one at a time
PI# python3 launch.py verify --quick    # seven of them
```

It asks you to move each control and watches what the engine received, so a
control that is mapped but not arriving shows up as a failure rather than as a
puzzle. The result is written to `/var/log/rb4r5/verify-report.txt`.

## Testing without a controller

```sh
PI$ tools/flx4-selftest.sh                       # synthetic MIDI, decoded
PI$ python3 tools/tests/test_control_chain.py    # all 33 controls, end to end
```

It creates a FIFO, runs `flx4-bridge` against it, feeds it the byte sequences a
FLX4 sends for every class of control, and prints the records that came out the
other end (or, with the player running, what reached `/tmp/keyshim.log`). This
is how the map was verified offline.

## Checking a live controller

```sh
PI$ python3 launch.py doctor                # card, rawmidi node, bridge state
PI$ python3 launch.py logs flx4-bridge.log -f
PI$ tail -f /tmp/keyshim.log                # what the engine actually received
```

`keyshim.log` lines like `keyshim: ctrl key=0000501e op=4 ch=1 param=512` are
the far end of the chain: MIDI arrived, was translated, and the engine was
called.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `flx4: waiting for a DDJ-FLX4 to be plugged in...` | no card matching FLX4/DDJ | `cat /proc/asound/cards`; is it powered? |
| `device not accepting address … error -71` in `dmesg` | bus-powered hub | plug it into the Pi, or use a powered hub |
| Bridge logs events, the UI does nothing | the player or `keyshim` is not there | `ls -l /tmp/rb-ctrl.fifo`; check `/tmp/keyshim.log` exists |
| Everything works but faders snap back | a stale `keyshim` that still applies defaults | rebuild the shims |
| Channel 2's fader moves deck 1 | mixer input routing stuck on player 0 | `keyshim` pins it; check `/tmp/keyshim.log` for `mixer defaults sent` |
| Pads do nothing in one mode | a mode with no engine equivalent | expected; see the pad table |
| Jog scratches wildly | `controller.jog_ppr` too low | calibrate it |
