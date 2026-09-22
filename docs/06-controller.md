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

## The jog wheel: measure it, do not guess

A jog wheel needs two numbers, and only one of them was ever right here.

| | what it is | where it comes from |
|---|---|---|
| `controller.jog_ppr` | the units the **engine** counts one platter revolution in | the RX3's own wheel: 1800 |
| `controller.jog_ticks_per_rev` | how many MIDI messages the **FLX4** sends for one turn of its wheel | nobody documents it — measure it. The default (600) is an estimate; using the RX3's own 1800 makes the deck crawl |

The bridge used to divide the FLX4's messages by the *engine's* number, which
makes every turn look far slower than it is (the wheel feels dead), and let
the platter position run to 16 bits, which hands the engine an angle it cannot
mean (the track skips). Both are fixed: the wheel's own resolution turns
messages into revolutions per second, and the position wraps inside one
revolution the way a platter angle has to.

So measure the wheel:

```sh
sudo python3 launch.py jogtest
```

It asks you to touch the plate (checking the touch notes arrive at all — the
engine cannot tell scratching from bending without them) and then to turn the
wheel exactly one full revolution. It reports ticks per revolution and which
way it counts, and prints the two commands to set:

```sh
sudo python3 launch.py config --set controller.jog_ticks_per_rev=<measured>
sudo python3 launch.py config --set controller.jog_reverse=true   # if needed
```

`controller.jog_scale` is the last knob: it multiplies how far a turn pushes
the deck, for when the units are right but the feel is not.

### Tuning it while the wheel is in your hand

Going through a rebuild and a restart for each guess takes minutes. The bridge
re-reads `/tmp/rb-jog.conf` twice a second instead, so a change lands before
you have finished turning:

```sh
sudo python3 launch.py jogtest --tpr 400      # the deck moves further
sudo python3 launch.py jogtest --tpr 900      # ... and less far
sudo python3 launch.py jogtest --scale 1.5    # or scale the lot
sudo python3 launch.py jogtest --bend 0.4     # the rim, relative to the plate
```

**Lower `tpr` means the deck moves further for the same turn.** It is the
number of messages the wheel sends per revolution, so dividing by less makes
each message worth more.

When it feels right, the command prints the `config --set` line that makes it
the default.

**A stuck plate touch** leaves the deck in scratch mode, and then every nudge
of the wheel seeks the track instead of bending it — which is what "the music
skips" looks like. The FLX4 sends the touch on one note and the shifted touch
on another, so a missed note-off is possible; `controller.jog_touch_timeout_ms`
(4 s) releases a touch that has been held with no movement at all.

## The rim bends, the plate scratches

The FLX4 reports *where* the wheel was touched, on different CCs, and that is
what decides what a turn means:

| CC | where | what it should do |
|---|---|---|
| `0x21` | the rim | **bend** — a nudge, the deck keeps playing |
| `0x22` | the plate, vinyl mode | **scratch** |
| `0x23` | the plate, non-vinyl mode | bend |
| `0x29` | SHIFT + plate | search through the track |

All four used to be treated the same, which is why the wheel behaved
identically whether or not the plate was held. Now a bend is scaled down by
`controller.jog_bend_scale` (0.25 — a quarter as far as the plate) and does
not claim the plate is held; a scratch does.

**The plate-touch note matters more than it looks.** The engine tells
scratching from bending by whether the plate is held, so if that note never
arrives every nudge seeks the track instead of bending it. The bridge now
says the plate is held on the wheel's behalf when the plate CC moves, and
takes it back when the rim does — so the distinction works even if the note
is missing. `launch.py jogtest` reports whether your unit sends it.

## Why the speed used to come and go

The speed was worked out from the gap between two MIDI messages. Those arrive
in bursts, so the gap is sometimes a millisecond and sometimes twenty, and the
speed swung by the same factor while the wheel turned perfectly steadily —
which feels like momentum appearing and disappearing.

Ticks are now accumulated and turned into one speed at a fixed rate
(`controller.jog_emit_ms`, 10 ms). Same wheel, same turn, same number.

## The LEDs

Pioneer controllers light a button by being sent the note that button sends,
with velocity `0x7f` for on and `0x00` for off. So the bridge opens the MIDI
node **read-write** now — it was read-only, which is why nothing on the
controller ever lit — echoes every button it handles, and runs a lamp test at
startup so you can see at a glance whether the output path works.

Pads take their own path through the bridge — the note is computed from the
pad mode and the pad number rather than looked up in the table — which is how
they came to be the one thing that never lit. They light now too.

That is not the same as mirroring the player. The player's own LED state goes
down the panel link, which is not decoded yet
([13-panel-link](13-panel-link.md)) — so what lights is what *you* pressed,
not what the RX3 thinks. `controller.leds=false` turns it off.

The bridge only ever writes to a real MIDI character device. A FIFO or a file
would send the bytes straight back as input, where they would parse as button
presses nobody made.

## "It responds slowly"

That has to be somebody's microseconds. `-v` (or `controller.verbose=true`)
makes the bridge say how long it took, from the MIDI byte arriving to the
record reaching the player's FIFO:

```
  HOT CUE pad 1 handled in 84us
```

Tens of microseconds is the bridge doing nothing wrong, and the delay is the
player's side — which, until the audio clock was fixed, it invariably was: the
engine's transport, and everything timed against it, ran off a clock that was
not being paced at all ([05-audio](05-audio.md)).

## Finding a button's note

```sh
sudo python3 launch.py sniff        # press the control; its note is printed
```

Then bind it in `/etc/rb4r5/flx4-map.conf` — for example to move the effect
picker onto a different button:

```
note ch5 0x63 0xf001 global   FX select opens the picker
```

`0xf001` is not an engine key: it is the launcher's effect picker.

## Finding out which message lights which lamp

The FLX4's lamps are lit by the host, and Pioneer does not publish the
mapping. `sniff` shows what the controller **sends**; `ledsweep` shows what it
**listens to**:

```
sudo python3 launch.py stop          # the bridge must not hold the port
sudo python3 launch.py ledsweep --channel 5
```

It lights one lamp at a time, printing each message before it sends it, and
turns everything back off on the way out. Watch the controller, note what
lights, and put it in `/etc/rb4r5/flx4-map.conf`.

`--cc` sweeps control changes instead of notes, which is how level-meter LEDs
are usually driven on Pioneer hardware. Narrow the walk with `--first` and
`--last`, and slow it down with `--hold`.

## The level meter lamps

A DDJ-FLX4's channel meter is lit by **one message whose value is the
level** — `ch1 CC 0x02` lights the whole left column — not by one message per
segment. Found with `ledsweep`.

The bridge reads the master peak audioshim publishes and sends it twenty
times a second, scaled over 48 dBFS so the top of the travel behaves like a
meter rather than a volume control. It only sends on a change, so an idle
deck costs nothing.

The defaults are `left = ch1 CC 0x02` and `right = ch2 CC 0x02`. A controller
that numbers them differently needs a map file line, not a rebuild:

```
meter <left|right> ch<n> <cc|note> <number>
meter <left|right> off
```

On other hardware, find the numbers with:

```
sudo python3 launch.py stop
sudo python3 launch.py ledsweep --channel 1 --cc
```

It lights one lamp at a time, naming each message before it sends it, and
turns everything back off on the way out.

## Two meters from one mixed stream

The audio this port can see is the **master mix** — one stereo stream, after
the mixer — so there is no per-deck level in it to show. The channel faders
are the closest real signal there is, and the bridge already sees them: the
left meter is scaled by deck 1's fader and the right by deck 2's, so pulling
one channel down drops its own meter.

It is an approximation, and worth being clear about: it shows what you are
**sending**, not what the deck is playing. A deck with its fader up and
nothing loaded will still show the master level.

## The MASTER level knob

Most Pioneer controllers wire this straight to the output and send nothing
over MIDI, in which case the on-screen meter cannot follow it — it measures
the audio *before* the knob. Check whether yours sends anything:

```
sudo python3 launch.py stop
sudo python3 launch.py sniff        # then turn the MASTER knob
```

If a CC appears, map it and both the on-screen meter and the controller's own
level LEDs follow the knob:

```
masterlevel ch<n> cc <number>
```

The bridge publishes the position to `/tmp/rb-master.dat`, which is what the
launcher's meter reads.

## The keycodes, checked against a verified port

Every keycode this port uses was compared against the SC Live 4 / knobshim2
work, which drives the **same engine** and was verified on live hardware.
Thirty-nine agreed. Two did not, and both were real bugs:

### BEAT FX SELECT is a fourteen-position switch

```c
send_rx_key(K_BFXTYPE, OP_VALUE, CH_GLOBAL, g_fx_type_pos);
```

`onEv_BeatEffectType(SW_BFX_TYPE)` takes **op 5 VALUE** carrying the **switch
position, 0..13**. Not a rotate with a delta. Not a rotate with a 10-bit
absolute position. Not a button press. All three were tried on hardware and
the player stayed on DELAY every time.

Because what is sent *is* the position, the order of the effect list has to
be the selector's order. The list was also wrong — it carried DJM-900
effects (ENIGMA JET, MOBIUS SAW, MOBIUS TRI) and was missing four of the
RX3's. If the player lands on a different effect from the one tapped, fix the
order in `fx-list.json`; the mechanism is right.

### The tempo fader is 0x4107

`onKey_TempoSlider` → `DjEngineIF::setTempoSlider(ch, f)`, op 5, `f` in
[-1..+1] with 0 at the detent. This project had `0x4107` and `0x4109` the
other way round, so the pitch fader was driving whatever `0x4109` is.

### Aliases that sent the wrong control

`play1` / `play2` and `cue1` / `cue2` are gone. There is **one** play key and
the deck is the channel — `0x4102` is CUE, not deck 2's play, and `0x4104` is
VINYL, not deck 2's cue. Use `play 1` / `play 2`.

`tools/tests/test_keycodes.py` pins all of this so it cannot drift back.

## What the LEDs would take

rbp computes its real LED state into **`uif::LedStat`** and encodes it for the
panel's micons, sent to `/dev/subucom_spi1.0` — which this port stubs as a
FIFO and does not decode. So the lights here are *modelled* from what we
send, which is right until the player changes something by itself.

Matching the player exactly needs one of two things, and neither is a guess
that can be made from here:

* **decode the panel link** — `launch.py subucom --learn` captures it while a
  named control changes, which is the data that would make the decode
  possible; or
* **read `LedStat` in-process** — the pointer chain is
  `IUiObjManager::getLedManager()` → `LedManager+0x30`, with `Led` entries of
  `0x2c` bytes holding id, channel and state. The addresses are specific to
  each rbp build, so the RX3's have to be found in the RX3's binary.

## The jog: what makes a turn a scratch

The plate being **held** is what makes a turn a scratch — not which CC
carried it. Deciding on the CC alone meant that touching the top and turning
still counted as the rim, a quarter-speed nudge, whenever the FLX4 sent the
rim's CC. That is what "captive touch feels like the side" was.

A released wheel now runs down instead of stopping dead, so a backspin
carries on turning and the platter feels heavier than it is. `spindown` in
`/tmp/rb-jog.conf` is how long that takes (900 ms; `0` turns it off), and it
can be changed while the wheel is in your hand.

## SMART FADER

With it on, the player sets the tempo itself and the pitch faders should stop
fighting it — so the bridge holds them. One switch holds **both** decks,
which is how the controller works.

It is **not** mapped by default, because a wrong guess silently swallows the
pitch fader. `sniff` while flicking the switch gives the number:

```
smartfader ch<n> <cc|note> <number>
```
