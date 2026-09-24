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
with velocity `0x7f` for on and `0x00` for off. The bridge opens the MIDI node
read-write, runs a lamp test when the controller appears, and from then on
**redraws every lamp from state**, writing only the ones that changed.

The state is the **player's own**. `keyshim.so` reads, from inside rbp, the
same things the RX3's panel is lit from and publishes them forty times a
second to `/tmp/rb-state.dat` ([`src/shims/rb_state.h`](../src/shims/rb_state.h)):

| What | Where in rbp | FLX4 lamp |
|---|---|---|
| playing / loaded / sync / looping | `PlayEngine::isPlaying()` etc. (`0x011497d0`) | PLAY, CUE, SYNC, LOOP IN/OUT, RELOOP |
| PLAY (2 = paused, blink), SYNC (2 = nudged off beat) | `uif::LedStat` ids 49 / 4 | PLAY `0x0B`, SYNC `0x58` |
| the eight pads, in whatever bank | `uif::LedStat` ids 18–25 | pads, plain and SHIFT channel |
| BEAT FX ON/OFF (blinks while on) | `uif::LedStat` id 48 | `ch5/6 0x47` |
| headphone CUE per channel | `MixerEngine::getMixerChHeadphoneCue` | `0x54` |
| channel meters, 0–11 segments, pre-fader | hook on `MonoLvMeter::getLedValue` | `CC 0x02` per deck |
| pad bank and its second function | `ui::PlayerInnards` +0x74 / +0x7a | pad-mode buttons |
| the Beat FX the player is on | `getBeatEffectType()` | the overlay's effect list |

Every address is the SC Live 4 port's (rblive4 `knobshim2.c`), live-verified
against this same XDJ-RX3 v1.20 rbp. So a track loaded from the touchscreen,
a loop that ends by itself or a hot cue stored last week all show — none of
which echoing button presses could do.

The lamps then behave like the RX3's:

* **PLAY** lit while playing, blinking while paused on a track, dark empty.
* **CUE** lit on the cue point, blinking when paused elsewhere, lit while held.
* **LOOP IN / OUT** flash while a loop plays (IN alone once its point is set),
  **RELOOP/EXIT** lit while there is a loop to exit — and all three go out
  when the loop does.
* **pads** show what the player holds (stored hot cues, the running beat
  loop), on both the plain and the SHIFT channel.
* **pad-mode buttons** are lit on the *deck* channel (`0x90/0x91`), where
  their buttons are; they used to be sent to the pad channel and never lit.

Every probe runs under a fault guard: if an address is wrong for this build,
the read faults, that one probe switches itself off (`keyshim: probe FAULTED
and is now off: …` in `/tmp/keyshim.log`) and the player carries on. The pad
bank scan and the Beat FX getter are the least proven and are off unless
`RB_STATE_PADBANK=1` / `RB_STATE_BFX=1`. The meter hook runs in the player's
own thread and cannot be guarded, so if the player dies of a memory fault the
launcher restarts it without the hook, and after a second crash without the
reader at all — for that run only, with keyshim's log printed so the cause
is on screen.

Two lamps do not follow rbp's table: **BEAT FX ON/OFF** blinks while the
effect is on and is dark when it is off (rbp's id 48 reads "blink" with the
effect off on this build), and **hot cue pads** light only for a stored cue
— rbp keeps an empty slot "dim", which an on/off lamp would show as lit.

If the state is missing (an old shim, the player still starting,
`controller.engine_state=false`) the same lamps are drawn from a model of the
deck kept from the buttons, and the bridge log says which it is using.
`RB_ENGINE_STATE=0` in the player's environment turns the reader off, and
`RB_METER_HOOK=0` just the meter hook.

The bridge only ever writes to a real MIDI character device. A FIFO or a file
would send the bytes straight back as input, where they would parse as button
presses nobody made.

### Headphone CUE

The RX3 has no keycode for a channel's CUE button — its PFL buttons go
straight to the mixer — so the bridge asks keyshim to toggle the channel's cue
in the player's `MixerEngine` (control record key `0x7e54`, channel 1/2).
Without the player's state it falls back to MASTER CUE, as before.

### Pad banks: PAD FX1 is RELEASE FX, SAMPLER is SLIP LOOP

The RX3 has four banks (`0x4113`–`0x4116`), and pressing a bank key again
flips the bank to its **second function** rather than selecting it. RELEASE
FX and SLIP LOOP share `0x4115`. So the bridge sends a bank key only when the
deck is not already there, reads which function the bank opened on from the
player, and flips it once if needed: PAD FX1 lands on RELEASE FX, SAMPLER on
SLIP LOOP, and pressing either again changes nothing.

### CUE/LOOP CALL < >

With a beat loop running in the BEAT LOOP bank, `<` halves it and `>` doubles
it by pressing the neighbouring beat-loop pad (rbp's sizes run 4, 2, 1 … 1/32
beats from pad 1 to 8). They used to send BEAT < / >, which is the Beat FX's
beat, not the loop's. The RX3's own CUE/LOOP CALL keycodes are not verified,
so a manual (IN/OUT) loop cannot be resized from here. `loopcall reverse` in
the map file flips the direction. SHIFT + `<` / `>` are SEARCH.

### SHIFT + RELOOP/EXIT is KEY LOCK

Master tempo (`0x4108`) for that deck: the tempo changes and the pitch does
not. Its lamp is on the SHIFT layer of RELOOP/EXIT (`0x50`).

### Backspin

Letting go of the plate while it is flung backwards (or spun hard forwards)
keeps the engine "held" while the spin runs down — about 95% gone after
`spindown` ms (`/tmp/rb-jog.conf`, default 900) — and hands the deck back to
the motor only when it has stopped. It used to stop dead, because letting go
of the plate is what hands it back. If the FLX4's own wheel is still turning
its ticks drive the spin.

### Where the faders are

At startup the bridge sends Pioneer's "report every control" sysex
(`F0 00 40 05 00 00 02 06 00 03 01 F7`, from the DDJ-400 / FLX4 Mixxx
scripts) and repeats it for half a minute once the player is up, so a fader
that was already up plays at its real level instead of reading "down" until
it is moved.

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

## The FLX4's own numbers

From the DDJ-FLX4 controller mapping. Several of these were wrong here, and
each one was a reported fault.

### The jog wheel has two platter CCs, not one

```
CC 0x22   PLATTER, vinyl mode ON    - the top
CC 0x23   PLATTER, vinyl mode OFF   - still the top
CC 0x21   SIDE                      - the rim
CC 0x29   PLATTER + SHIFT           - search
```

`0x23` was being treated as the rim. With vinyl mode off — which is the
default — touching the top and turning was therefore a quarter-speed nudge.
That is what "captive touch feels like the side" was, and no amount of
tuning the wheel could have fixed it.

### Buttons that were never bound

| button | note | goes to |
|---|---|---|
| headphone CUE | `0x54` | `K_MASTERCUE` |

The headphone CUE buttons were not in the table at all, so they did nothing
and never lit. The RX3's **per-channel** PFL keycode is not among the ones
verified so far, so they drive MASTER CUE for now: the headphones follow,
which is most of what the button is for. Bind it properly from the map file
when the right keycode turns up.

### The pad modes

```
0x1B HOT CUE    0x1E PAD FX1    0x20 BEAT JUMP   0x22 SAMPLER
0x69 KEYBOARD   0x6B PAD FX2    0x6D BEAT LOOP   0x6F KEY SHIFT
```

The RX3's fourth bank is release FX, and it now sits on **PAD FX1** (`0x1E`)
where the RX3 puts it, rather than behind two presses of SAMPLER.

### A pad's lamp needs both channels

The FLX4 keeps a separate lamp state per MIDI channel. A pad lit on `0x97`
goes dark the moment SHIFT is held unless `0x98` was told as well — which is
why hot cues and loops appeared to "stop working" with a finger on SHIFT.
Every pad light is now sent on both.

Loop in, loop out and headphone cue also stay lit while they are on, instead
of only flashing on the press.
