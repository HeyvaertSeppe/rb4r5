# 05 — Audio: master and headphone cue through the DDJ-FLX4

The player opens three XDJ-RX3 devices that do not exist here. `audioshim.so`
presents them as virtual handles and muxes them onto one real PCM:

| Player's device | Goes to (FLX4, 4 channels) | Goes to (HDMI fallback, 2 channels) |
|---|---|---|
| `hw:cs4344audiorev8,0` — master | channels **1/2** → MASTER out | channels 1/2 |
| `hw:cs4344audiorev8,1` — headphones/cue | channels **3/4** → HEADPHONES | dropped (or mixed, see below) |
| `hw:cs4344audiorev8,2` — booth | dropped (the FLX4 has no booth) | dropped |
| capture (mic) | silence | silence |

## Why the shim is not optional

Two reasons, and the second one surprises people:

* The player also opens the **control** device
  (`snd_ctl_open("hw:cs4344audiorev8")`). If that fails, JUCE cannot enumerate
  rates, the sample rate defaults to 0, and
  `DjEngineIF::audioDeviceAboutToStart()` aborts with `sampleRate:0 != 44100`.
  The shim fakes success for the RX3 card names and passes real names through.
* **The transport is clocked by the ALSA callback.** `PlayEngine::update()` runs
  only from `DjEngineIF::audioDeviceIOCallback()`. With no running PCM the UI
  still repaints but nothing plays, the playhead does not move and the scrolling
  waveform stays blank. "No audio device" is therefore a *functional* failure,
  not a cosmetic one — which is why `doctor` reports it as a problem.

## Device discovery happens in Python, not in C

The shim has no card numbers compiled into it. `rb4r5/audio.py` enumerates
`/proc/asound/cards` and the USB device's own
`/proc/asound/cardN/stream0` (which lists the real channel count, formats and
rates), decides, and passes the result in the environment:

```
RB_AUDIO_DEV=plughw:CARD=FLX4,DEV=0|plughw:2,0|default   '|' separated, tried in order
RB_AUDIO_CH=4                                    4 on the FLX4, 2 on HDMI
RB_AUDIO_RATE=44100
RB_AUDIO_FMT=6                                   SND_PCM_FORMAT_S24_LE
RB_AUDIO_CUE_MIRROR=0                            mirror master into the cue?
RB_AUDIO_CUE_ON_2CH=0                            stereo sink follows the cue?
STARTUP_MUTE_MS=1500  STARTUP_FADE_MS=300
```

See exactly what it decided, without starting anything:

```sh
PI$ python3 launch.py audio --env
card 0 [vc4hdmi0] vc4-hdmi - vc4-hdmi-0
card 2 [FLX4] USB-Audio - DDJ-FLX4 [4ch S24_3LE 44100Hz]
chosen: plughw:CARD=FLX4,DEV=0 (4ch @44100 Hz, S24_LE, source=controller)
routing: master -> FLX4 ch 1/2 (MASTER out), cue -> ch 3/4 (HEADPHONES)
note: FLX4 is S24_3LE; alsa-lib converts from S24_LE
```

**Why `plughw:` and not `hw:`.** The engine produces S24_LE samples in 32-bit
containers at 44.1 kHz. The FLX4's own format is S24_3LE (packed 3-byte). Going
through ALSA's `plug` layer lets alsa-lib do that conversion — and resample, if
you ever attach a device that will not do 44.1 kHz — instead of the open simply
failing. Set `audio.plug: false` to use `hw:` directly when you know the
parameters match exactly; the launcher warns if that cannot work.

## Headphone cue

With a 4-channel sink the cue pair carries what the engine sends to its
headphone device, so the FLX4's **CUE** buttons and **HEADPHONES MIXING** knob
work as they do on the real RX3.

Two options exist for the awkward moments:

* `audio.cue_mirror: true` — until the engine has produced any cue audio, mirror
  the master into channels 3/4, so the headphones are not silent while you are
  finding your way around. Off by default, because it defeats cueing.
* `audio.cue_on_stereo: true` — on a **stereo** sink (HDMI), send the cue mix
  instead of master once the cue has audio. This is what the Chromebit port did,
  because it had nowhere else to put it. Off by default: on a TV you want the
  master.

## The start-up pop

The engine's first output buffers contain about 200 ms of full-scale garbage
(`0x800000`, the most negative 24-bit value) — an uninitialised filter
settling. Through a DJ controller's master out that is a loud pop.

The shim holds every output channel at zero for `STARTUP_MUTE_MS` after the
first write, then fades in over `STARTUP_FADE_MS`:

```
audioshim: startup mute=1500ms fade=300ms (66150+13230 frames @44100 Hz)
audioshim: startup mute active: 66150+13230 frames
audioshim: startup mute released after 79380 frames
```

`audio.startup_mute_ms: 0` disables it. This was written for the Chromebit port,
documented there as an open item, and is enabled here by default.

## Scheduling

The player asks for `SCHED_FIFO` and pins threads to CPUs the RX3 had. Left
alone, its RT threads starve the UI. The shim neutralises
`sched_setscheduler`, `pthread_setschedparam`, the affinity calls and friends,
and the service instead runs at `Nice=-5`. Four A76 cores cope easily.

## Checking it

```sh
PI$ python3 launch.py audio --env          # what will be used
PI$ python3 launch.py doctor               # + the live PCM state
PI$ cat /proc/asound/card*/pcm0p/sub0/hw_params
PI$ cat /proc/asound/card*/pcm0p/sub0/status
PI$ grep -E "opened real|set_|hw_params|writei" /tmp/audioshim.log | tail
```

A healthy log looks like:

```
audioshim: cfg dev='plughw:CARD=FLX4,DEV=0,…' ch=4 rate=44100 fmt=6 …
audioshim: opened real 'plughw:CARD=FLX4,DEV=0' for Master (mode=0), res=0 …
audioshim: real set_format(req=6 -> 6) res=0
audioshim: real set_channels(req=2 -> 4) res=0
audioshim: real set_rate_near res=0 rate=44100
audioshim: real hw_params res=0
audioshim: writei #501 frames=64 written=64 ch=4 peak_m=16777215 peak_cue=0
```

`peak_m` climbing off zero once a track plays is the proof that audio is really
flowing; `hw_params` showing `state: RUNNING` is the proof the device is open.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ALSA lib pcm_hw.c: … Invalid value for card` in `rbp.log` | `audioshim` not preloaded | it is in `LD_PRELOAD` by default; check `chroot.player_env` / `/tmp/audioshim.log` exists |
| `sampleRate:0 != 44100` and the engine aborts | the control device interception is missing | you are running an old `audioshim.so`; rebuild |
| PCM never `RUNNING`, write counter static | the real open failed | `grep 'opened real' /tmp/audioshim.log`; check `aplay -l` shows the card |
| No sound, `peak_m` non-zero | FLX4 master knob down, or the cue/master swap | turn it up; check `audio.cue_on_stereo` |
| Headphones silent, master fine | no cue audio until PFL is pressed | press CUE on a channel, or set `audio.cue_mirror: true` |
| Only 2 channels offered | the controller is in a 2-channel USB mode | `cat /proc/asound/card*/stream0`; the launcher falls back to stereo and says so |
| Crackling / xruns | under-voltage, or too small a period | use the 27 W supply; check `vcgencmd get_throttled` |
| Playhead frozen, UI alive | no running PCM (see above) | fix the device; this is the single most common cause |

## The controller plugged in after the player started

The engine opens its PCM **once**, at startup, and there is no way to hand a
running engine a different card. Plug the FLX4 in a minute later and nothing
moves: the sound stays on HDMI and the controller looks undetected.

The supervisor now watches for it. When a controller appears after startup it
says so and restarts the player, which takes a few seconds and is what you
were about to do by hand:

```
[!!] the controller appeared (DDJ-FLX4) after the player had already chosen
     its audio device - restarting so master and cue go to it
```

`audio.restart_on_controller=false` turns that off. Plugging it in *before*
`launch.py run` is still the quickest path.

## The master level meter

`audioshim` is the only thing in the stack that sees the audio — the engine's
own meters are drawn into the panel link this port does not decode — so it
measures the peak of each channel as it mixes, and writes it to
`/tmp/rb-levels.dat` twenty times a second. The launcher draws that in the
black bar to the right of the picture (see [12-overlay](12-overlay.md)):
two columns, green to about -12 dBFS, amber above that, red over the line at
-3 dBFS, which is where the RX3 puts its own.

It needs the black bar to live in, so it appears with `display.fit=aspect`
(the default) and not with `fill`. `overlay.meter=false` turns it off.

## Why the song played too fast, and the seconds counted too fast with it

The player's transport is clocked by `snd_pcm_writei`. The deck plays, and the
time display counts, at **exactly the rate that call returns at** — there is no
other clock. So anything that lets it return early makes the music run fast
and the seconds run fast with it, in perfect step, which is what made it look
like a timing bug rather than an audio one.

Two things let it return early.

**A failed write reported as a success.** If the device refused the audio,
the shim retried once and then returned "wrote it all" anyway. The engine was
told it had output when it had none, and raced through the track at whatever
speed the CPU managed. Now the whole period is written, underruns recover, and
a device that has genuinely stopped taking audio is marked dead and said so in
`rbp.log`.

**The streams with nothing behind them.** The engine writes several streams —
master, headphones, booth — and only master goes to hardware. The others were
mixed in memory and returned instantly. If the engine clocks itself off any of
them, nothing was pacing it at all.

So every stream now goes through a governor (`src/shims/rate_gate.h`): it may
run at real time, never faster. A stream the hardware already paces never
trips it, because it is never ahead; a stream with nothing behind it comes out
at real time anyway. A device that is genuinely slow is allowed to be slow —
the engine catches up its deficit and is then governed again — and a stall of
more than two seconds restarts the clock rather than sprinting to make up for
it.

It reports itself every five seconds, which is what to look for in `rbp.log`:

```
audioshim: the master stream has written 60.0s of audio in 60.1s of real time (1.00x)
audioshim: the headphone stream has written 60.0s of audio in 60.0s of real time (1.00x) [governed]
```

Anything other than about `1.00x` there is the transport running wrong, and
the stream named is the one doing it.

## Finding out where the silence is

Two commands, and between them they say which half of the problem it is:

```sh
sudo python3 launch.py stop
sudo python3 launch.py audio --test      # a tone straight at the device
sudo python3 launch.py run
sudo python3 launch.py audio --levels    # what the player is producing
```

* **The tone plays but the player is silent** — the device, the channel
  mapping and the volume are all fine, so the problem is the player's side:
  check `--levels`.
* **The tone does not play** — `aplay`'s own error is the real problem. "Device
  busy" means something still has it open; a format error means the channel
  count or format is wrong for this card.
* **`--levels` never moves** — the player is not writing audio periods at all.
* **`--levels` moves but every period is silent** — the engine's audio loop is
  running, so the deck is not playing, the fader or trim is down, or no track
  is loaded.
* **`--levels` shows audio and you hear nothing** — it is between the shim and
  the speakers: the `audioshim:` lines in `rbp.log` say what the device did.

## Which stream is the master

The RX3 has one sound card with three output subdevices, and the engine opens
them by name:

| device | stream |
|---|---|
| `hw:cs4344audiorev8,0` | Master |
| `hw:cs4344audiorev8,1` | Headphones / cue |
| `hw:cs4344audiorev8,2` | Booth |
| `hw:esaics4344audio,0` | mic / return — not an output |

audioshim maps the master onto the real hardware and answers for the rest
itself. It decides **by name**. It used to decide by counting opens — first
playback open is the master, second is the headphones, and so on — which is
wrong because the engine opens and closes these repeatedly while it
enumerates devices. By the time it opened the master for real, the count had
drifted past it, so the master got a dummy handle: its audio was dropped, the
meters never moved, and nothing in the log looked out of place. The name is
stable; the count is not.

## The card is opened once and kept

`snd_pcm_close()` on the master does **not** close the card. It drops what is
queued and frees the hardware parameters, returning the handle to the OPEN
state so the next open can reconfigure it, but the descriptor stays ours for
the life of the process.

This is deliberate. The engine closes and reopens the master several times
during startup, and a card that was genuinely closed is often still busy when
the next open arrives:

```
opened real 'plughw:CARD=DDJFLX4,DEV=0' for Master, res=0    <- fine
snd_pcm_close(...)
opened real 'plughw:CARD=DDJFLX4,DEV=0' for Master, res=-16  <- EBUSY
opened real 'plughw:0,0'                for Master, res=-16
opened real 'default'                   for Master, res=-16
NO usable playback device - the transport will not advance
```

Holding the descriptor also stops anything else taking the card in between.
An open that does still come back `-EBUSY` is retried for two seconds, which
covers a previous player that is still shutting down.

The open also clears `SND_PCM_NONBLOCK`. The hardware is the transport clock,
and a non-blocking handle returns `EAGAIN` instead of pacing the engine. (The
code meant to clear it but masked off bit 2, which is `SND_PCM_ASYNC`, so
NONBLOCK stayed set — and a momentarily busy card then failed outright
instead of waiting.)

## Reading a sample the way the engine meant it

`SND_PCM_FORMAT_S24_LE` keeps a 24-bit sample in the **low three bytes** of a
32-bit container and leaves the top byte alone. A negative sample therefore
arrives looking like a large positive `int32`:

| sample | container | read raw as int32 |
|---|---|---|
| −1 | `0x00FFFFFF` | 16777215 |
| −16 | `0x00FFFFF0` | 16777200 |

against a full scale of 8388607. That is why the meter sat pinned at the top
with nothing playing, and why `peak_m` in the log read a steady 16777200 when
the engine was in fact sending near-silence. `engine_sample()` sign-extends
from 24 bits before anything looks at the value — metering, the cue mix and
the startup fade all depend on it being the number the engine meant. A 32-bit
format already fills the container, so it is left alone.

## Access: writei cannot use an mmap PCM

The engine does not always ask for an access type; on the RX3 it takes
whatever its own device defaults to. Left alone, `snd_pcm_hw_params()` picks
the first access the hardware offers, which for a USB card is mmap — and then
every `snd_pcm_writei()` returns `-EINVAL`:

```
writei #1 frames=64 written=-22
the output device stopped accepting audio (Invalid argument after 1 writes)
```

This shim reaches the hardware through `writei`, so the real device is forced
to `SND_PCM_ACCESS_RW_INTERLEAVED` — both in `set_access()` when the engine
does ask, and again in `hw_params()` for when it never does. After
`hw_params()` succeeds the shim logs what the device actually settled on
(access, format, channels, rate, period, buffer), because every "no sound" in
this port so far has been one of those numbers not being what the calls
leading up to it suggested.

A device that fails a write is no longer written off for the whole run: a
later `hw_params()` that succeeds clears the flag and gives it another chance.

## Software parameters have to match the real buffer

Every *hardware* parameter is overridden for the real device — access,
format, channels, period, periods — so the buffer it ends up with is nothing
like the 128 frames the engine believes in. Its *software* parameters were
being forwarded verbatim all the same, and that is fatal:

```
the device was in state 1 (not ready to be written); prepare() res=0
the output device stopped accepting audio (File descriptor in bad state,
errno 77, after 1 writes, state 1)
```

A `stop_threshold` of 128 frames on a 2048-frame buffer stops the stream the
moment it is prepared — `avail` is the whole empty buffer, already past the
threshold. So `prepare()` reported success, the state fell straight back to
SETUP, and the first `writei()` returned `EBADFD`.

`apply_sw_params()` now sets them from the buffer the hardware actually has,
right after `hw_params()` succeeds:

| parameter | value | why |
|---|---|---|
| `stop_threshold` | the boundary | an underrun must never stop the stream; the write loop recovers from `EPIPE` itself |
| `start_threshold` | one period | start playing as soon as there is a period to play |
| `avail_min` | one period | wake the writer when a period is free |
| `silence_threshold` / `silence_size` | 0 | the shim fills the buffer itself |

The engine's own `sw_params` calls are **not** forwarded to the real device,
for the same reason its `hw_params` are overridden: they describe a device it
is not actually writing to.

`EBADFD` is now treated like `EPIPE` in the write loop — prepare and retry —
rather than as a reason to give up on the device.

## The real device is configured by the shim, not by the engine

The engine's `snd_pcm_hw_params_t` describes the **RX3's own output**. Every
value in it — access, format, channels, period, periods, and the software
parameters derived from them — is right for local I2S hardware that is not
present, and wrong for a USB controller that is. Applying it failed three
separate times, each for a different reason, and the last failure could not be
named at all: `hw_params` returned 0, `sw_params` returned 0, `prepare()`
returned 0, and the device sat in SETUP refusing every write with `EBADFD`.

So the real device is no longer configured from that struct. When it opens,
`configure_real_device()` allocates its own `hw_params`, fills it from the
device's actual capabilities with `hw_params_any()`, sets only what this shim
needs, applies it once, applies matching software parameters, prepares, and
verifies the state reached PREPARED.

The engine's own calls — `set_access`, `set_format`, `set_channels`,
`set_rate_near`, `set_period_size_near`, `set_periods_near`, `hw_params`, and
every `sw_params` setter — are accepted and **not** applied. They must still
*succeed*, because the engine checks the return value and will not open its
audio otherwise; `set_rate_near` still reports back the rate, because the
engine clocks itself from it.

If the device is ever found outside PREPARED or RUNNING, the whole setup runs
again: from `hw_params()` when the engine reconfigures, and from the write
path when `prepare()` alone will not recover it.

## The shim intercepts alsa-lib's own calls to itself

This is the single most important thing to know about `audioshim.c`, and it
was the cause behind four separate "no sound" repairs that each looked
correct and changed nothing.

The shim exports public ALSA symbols. **alsa-lib calls those same public
symbols internally.** `plughw:` is a *plug* PCM wrapping a *hw* PCM, and the
plug implements its operations by calling the public function on its slave:

```
snd_pcm_prepare(plug)    ->  snd_pcm_prepare(slave)
snd_pcm_hw_params(plug)  ->  snd_pcm_hw_params(slave)
snd_pcm_writei(plug)     ->  snd_pcm_writei(slave)
```

Under `LD_PRELOAD` every one of those internal calls lands **in this shim**,
carrying a handle it has never seen. Swallowing them — "not one of mine,
return 0" — leaves the slave unconfigured and unprepared while every call
reports success:

```
setup hw_params res=0
our own sw_params (start=512, stop=boundary 1073741824, ...) res=0
prepare after setup res=0, state now 1
writei #1 frames=64 written=-77          (EBADFD)
```

It also explains the boundary of 0 — the plug's own setup never completed,
so `pcm->boundary` was never computed — and why forcing the access type,
enlarging the buffer and rewriting the software parameters all changed
nothing: none of it was reaching the hardware.

`whose()` classifies every handle into three kinds, and every exported
function begins by asking:

| kind | handle | what happens |
|---|---|---|
| `H_ALSA` | anything the shim did not create | **passed straight through, with its own handle** |
| `H_MASTER` | the real device, as the engine holds it | the shim's own policy (configured once, at open) |
| `H_VIRTUAL` | headphones, booth, dummy, capture | answered by the shim |

A new interposed function must make that check its first line. Forgetting it
does not fail loudly — it returns success and silently breaks the layer
underneath.

There is a fourth kind, `H_NONE`, for a **null** handle. The engine really
does call `snd_pcm_close(NULL)` — it is in the logs. That was harmless while
anything unrecognised was swallowed; the moment unrecognised came to mean
"hand it to alsa-lib", a null handle became a `SIGSEGV` inside alsa-lib. It
belongs to nobody, so nothing happens to it.

Two accessors still cannot be told apart, because they take only a
`snd_pcm_hw_params_t` and no handle: `snd_pcm_hw_params_get_channels_min()`
and `..._max()`, which always answer 2 for the stereo stream the engine
believes in. If alsa-lib is ever seen making its own channel decisions
through those public accessors rather than its internal ones, that is the
place to look.

## The streams that are not hardware are still real PCMs

The engine opens five playback/capture streams; only one is the controller.
The other four — headphones, booth, a spare output, and capture — used to be
the addresses of five `static int`s, recognised by pointer comparison.

That holds only while **every** ALSA function the engine calls on them is one
this shim exports. It exports 23. `snd_pcm_state()`, `snd_pcm_avail_update()`,
`snd_pcm_delay()`, `snd_pcm_drain()`, `snd_pcm_start()`, `snd_pcm_hw_free()`,
`snd_pcm_poll_descriptors()` and a dozen more go straight to alsa-lib, which
dereferences the pointer as a `snd_pcm_t`. A pointer to an `int` is not one,
and the player dies with `SIGSEGV`.

It only started firing once the interception bug above was fixed: until then
the engine gave up early, and never reached the point of doing real playback
work on the streams it was not going to hear anything from.

Each virtual stream is now backed by alsa-lib's own **`null`** PCM — a real,
complete PCM that needs no hardware and is defined by `alsa.conf` itself.
Every ALSA function works on it, exported here or not. Parameter and prepare
calls are passed through to it so it behaves like the working device the
engine expects; only `writei` is intercepted, which is the part that actually
needs doing something about (the cue mix, the meters, the pacing).

If the runtime has no `null` PCM the shim falls back to the old sentinels and
says so in the log — the player runs, with the old hazard.
