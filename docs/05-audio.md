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
RB_AUDIO_DEV=plughw:CARD=FLX4,DEV=0,plughw:2,0   comma-separated, tried in order
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
chosen: plughw:CARD=FLX4,DEV=0,plughw:2,0 (4ch @44100 Hz, S24_LE, source=controller)
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
