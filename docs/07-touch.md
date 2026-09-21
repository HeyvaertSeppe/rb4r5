# 07 — Touchscreen

The 22″ panel is a USB HID touchscreen; the player is an application written for
a tsc2007 resistive panel on an i.MX6. There are two ways to connect the two,
and rb4r5 ships both — one that is designed to work, and one that is honest
about not being verifiable yet.

## The supported path: zones

`rbtouchd` reads the panel's evdev stream and maps **regions of the screen** onto
controls the engine definitely accepts — the same `IKeyManager::sendKey` path the
DDJ-FLX4 bridge uses:

```
22" panel --evdev--> rbtouchd --> /tmp/rb-keys.fifo   (buttons)
                              --> /tmp/rb-ctrl.fifo   (browse knob, jog)
                              --> /tmp/rb-touch.dat   (the raw contact)
```

Four zone types:

| Type | Gesture | What it sends |
|---|---|---|
| `key` | touch down / up | press / release of a keycode (with a deck channel) |
| `scroll` | vertical drag | the browse knob, ±1 per `touch.scroll_step` of screen height; a **tap** sends the zone's `tap_key` (SELECT) |
| `jog` | horizontal drag | `JOG_TOUCH` press, then `JOG_ROT` with a speed in rev/s, then a stop on release — i.e. scrubbing a deck |
| `value` | drag inside the zone | a 10-bit absolute value (a fader, EQ, filter) |
| `none` | — | ignore touches here (status areas) |

Why a *drag* for the list and not a tap on a row: the engine has **no
menu-up/down keycode at all**. The XDJ-RX3 navigates both the track list and its
menus with the rotary selector (`SELECTOR`, op 4, ±1), so scrolling is the only
thing a touchscreen can do to a list — and a tap is SELECT. Dragging down turns
the knob clockwise, which moves the highlight down; `"invert": true` on the zone
flips that.

## The default layout

Zones are fractions of the screen, so the same file works on any panel size.
`/etc/rb4r5/touch-zones.json` (and `config/touch-zones.json` in the repo):

```
 0.00                                                             1.00
 ┌──────┬──────┬──────┬──────┬───────────────────────┬──────┐ 0.00
 │source│browse│ menu │ back │      (status bar)     │ info │
 ├──────┴──────┴──────┴──────┴───────────────────────┴──────┤ 0.09
 │                                                          │
 │   list:  drag = browse knob,  tap = SELECT               │
 │                                                          │
 ├───────────────────────────┬──────────────────────────────┤ 0.70
 │   deck 1 scrub (jog)      │   deck 2 scrub (jog)         │
 ├──────┬──────┬──────┬──────┼──────┬──────┬──────┬─────────┤ 0.82
 │LOAD 1│PLAY 1│CUE 1 │SYNC 1│SYNC 2│CUE 2 │PLAY 2│ LOAD 2  │
 └──────┴──────┴──────┴──────┴──────┴──────┴──────┴─────────┘ 1.00
```

That gives browse, load, play, cue, sync and scrub from touch alone. The zones
are **invisible** — the player draws the whole screen and knows nothing about
them — so the layout is a convention you learn (or change to match what the UI
actually shows on your screen).

Show it, and check it against real touches:

```sh
PI$ python3 launch.py zones                 # print the layout, validate it
PI# python3 launch.py calibrate             # touch the screen, see what you hit
  touch  x=0.184 y=0.902  (UI  235, 721)  -> play1 [key]
  touch  x=0.512 y=0.331  (UI  655, 264)  -> list [scroll]
```

`calibrate` does **not** drive the engine, so it is safe while the player runs.
If every touch reads `(no zone)` or lands in the wrong place, fix the
orientation in the config:

```json
"touch": { "swap_xy": false, "invert_x": false, "invert_y": false }
```

## Editing the layout

Each zone is `{"name", "rect": [x0,y0,x1,y1], "type", ...}`; the **first** zone
containing the touch wins, so put small zones before large ones. Examples:

```json
{"name": "play1", "rect": [0.125, 0.82, 0.25, 1.0],
 "type": "key", "key": "play", "ch": 1}

{"name": "list", "rect": [0.0, 0.09, 1.0, 0.70],
 "type": "scroll", "key": "selector", "tap_key": "select"}

{"name": "deck1-scrub", "rect": [0.0, 0.70, 0.5, 0.82], "type": "jog", "ch": 1}

{"name": "fader1", "rect": [0.02, 0.30, 0.08, 0.68],
 "type": "value", "key": "fader", "ch": 1, "axis": "y"}
```

`key` is a name from `launch.py keys --list` or a raw `0x…` code. The file is
validated on load; if it is broken, the built-in layout is used and the reason is
logged rather than leaving you with a dead screen.

## Tuning the feel

```json
"touch": {
  "tap_ms": 400,        ← held longer than this is not a tap
  "tap_slop": 0.02,     ← movement (fraction of the screen) still counted as a tap
  "scroll_step": 0.035, ← drag distance per browse-knob step (smaller = faster)
  "jog_scale": 3.0      ← jog revolutions per full screen width
}
```

Extra fingers are tracked but ignored: only the first contact drives the UI, and
lifting a second finger cannot strand a gesture. Both multitouch (protocol B,
`ABS_MT_SLOT`/`TRACKING_ID`) and single-touch (`ABS_X`/`ABS_Y` + `BTN_TOUCH`)
panels are handled.

## The experimental path: native RX3 touch

The player's own touch input comes from `/dev/tsc2007_2-0048`, which its
`TouchPanelComm` thread `read()`s in **6-byte records**. `memshim` answers that
device; by default it reports "no touch" (all zeroes) at ~60 Hz, which is what
the earlier ports did.

With `touch.native: true`, `rbtouchd` publishes the current contact to
`/tmp/rb-touch.dat` (16 bytes: `u32 seq, down, x, y` in UI coordinates) and
`memshim` packs it into that 6-byte record. If the layout is right, the engine
gets *real* touch — a tap on an actual list row, needle drops on the waveform,
everything the RX3's own screen does.

**The layout is unverified.** We have no XDJ-RX3 to sniff and the firmware's
driver is not public, so the record format is a guess. It is therefore off by
default and selectable:

| `touch.native_format` | Bytes |
|---|---|
| `fxy` (default) | `u16 down, u16 x, u16 y` |
| `xyf` | `u16 x, u16 y, u16 down` |
| `xyp` | `u16 x, u16 y, u16 pressure` (0 or 4095) |
| `bxy` | `u8 down, u8 pad, u16 x, u16 y` |

Trying it:

```sh
PI# python3 launch.py config --set touch.native=true \
                             --set touch.native_format=fxy
PI# systemctl restart rb4r5      # memshim reads the format at player start
# touch the screen and watch the UI.  Nothing happening is the expected
# outcome for three of the four formats; something *wrong* happening (the UI
# reacting in the wrong place) means the layout is close.
PI# python3 launch.py config --set touch.native=false   # back to zones
```

A wrong format cannot hurt anything — the engine either ignores the record or
reacts oddly — but it can look like a malfunction, which is why this is opt-in.
If you do work out the real layout, `src/shims/memshim.c` has one small function
(`touch_pack`) to change, and please open an issue: it would let the zone map
become a fallback rather than the main path.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `waiting for a touchscreen` | the panel's **USB** cable is not connected (HDMI alone is not enough) | plug it in; `launch.py doctor` lists input devices |
| Touches land mirrored or rotated | axis orientation | `swap_xy` / `invert_x` / `invert_y`, then `calibrate` |
| Nothing happens anywhere | the player is not running, so the FIFOs do not exist | `launch.py status`; `ls -l /tmp/rb-*.fifo` |
| Buttons work, the list does not scroll | `scroll_step` too large, or you are tapping | drag further; lower `scroll_step` |
| The list scrolls the wrong way | the selector's sign | `"invert": true` on the list zone |
| A tap selects when you meant to scroll | `tap_slop` too large | lower it |
| Scrubbing a deck does nothing | the deck has no track loaded | load one first |
| Multiple touches confuse it | expected: only the first contact is used | — |

## When touches land on the wrong control

Zones are defined in **normalised UI space** (0..1 of the RX3's 1280×800), and
the daemon converts panel coordinates into that space. So a touch lands where
it looks like it landed only if what the panel shows really is the UI.

That matters because of the display bug in
[04, F8](04-display.md): while the driver was writing 32-bit pixels into a
16 bpp framebuffer, the visible picture was the **left half of the UI stretched
over the whole panel**. Every touch then hit a control at roughly half the x it
appeared to be at — which looks exactly like "touch does not work". If the
screen is not showing the UI correctly, fix that first and re-test touch
afterwards.

Once the picture is right:

```sh
PI# python3 launch.py calibrate           # each touch prints its zone
PI# python3 launch.py calibrate --raw     # every evdev event, for a dead panel
```

`calibrate` now reports how many evdev events arrived, not just how many
touches it recognised, and distinguishes the three failures:

* **no device at all** — it lists every `/dev/input/event*` with the reason it
  was not taken as a touchscreen (no `ABS_MT_POSITION_X`, no `BTN_TOUCH`, …).
  A 22″ touch monitor needs its own USB lead; the video cable does not carry
  touch.
* **events but no contacts** — the panel reports `ABS_MT_*` without
  `BTN_TOUCH`; `--raw` output says which codes it does send.
* **contacts but every one outside every zone** — the axes are swapped or
  inverted: `touch.swap_xy`, `touch.invert_x`, `touch.invert_y`.

Name a panel explicitly if the wrong device is picked:

```sh
PI# sudo python3 launch.py config --set touch.device=/dev/input/event5
```
