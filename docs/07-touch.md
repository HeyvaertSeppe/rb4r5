# 07 — Touchscreen

The 22″ panel is a USB HID touchscreen; the player is an application written for
the RX3's tsc2007 resistive panel. rb4r5 gives the player the finger through
the RX3's own touch device, so the screen works like an RX3's; a zone map that
only presses keys remains as a fallback.

## Native touch (the default): the player gets the finger

The player was written for the RX3's own touch panel, a tsc2007 that it reads
through `/dev/tsc2007_2-0048` in 6-byte records. rb4r5 hands it the 22″
panel's finger through that same device, so the player does with a touch
exactly what an RX3 does: tap a track or a menu row, drop the needle on the
waveform, the on-screen buttons and tabs — everything the RX3's screen does,
with nothing to learn.

```
22" panel --evdev--> rbtouchd --> /tmp/rb-touch.dat --> memshim --> rbp
                                  (the finger, in UI      (the RX3's touch
                                   coordinates)            device, 6 bytes)
```

What makes the player accept it, all verified on this same player by the
Prime GO and SC Live 4 ports (rblive4 `fbshim-tsc.c`):

| Piece | Where |
|---|---|
| the record `{u8 flag, u8 0, u16 x, u16 y}`, little-endian, 1280×800 | `memshim.c` `touch_pack` (`rx3`) |
| X mirrored: the firmware computes `calX = 1280 - rawX` (its panel is wired `invertX`) | `memshim.c` (`touch.native_invert_x`) |
| the touch device's ioctls answered: max X `3`, max Y `3900` (a stub file cannot) | `fbshim.c` |
| identity calibration `0 0 320 200 1280 800` in `root/settings/TouchCalib_User.dat` and `_Factory.dat` (the originals are kept as `*.rb4r5-orig`) | `chroot.touch_calibration` |
| a tap held at least 90 ms: the player samples ~60×/s and drops the first "down" as debounce | `touchd.py` (`touch.native_min_tap_ms`) |

Touches on the black bars and the top bar are not the player's: the overlay
handles the top buttons and the effect list itself.

If touches land mirrored, `touch.native_invert_x=false`; if the whole panel is
rotated or mirrored, `touch.swap_xy` / `invert_x` / `invert_y` as below, then
`launch.py calibrate`.

## The zone map (the fallback)

`touch.native=false` goes back to the older way: `rbtouchd` maps **regions of
the screen** onto controls through the same `IKeyManager::sendKey` path the
DDJ-FLX4 bridge uses. It only presses keys, so the list is scrolled rather
than tapped - it is kept for a panel the native path does not suit.

| Type | Gesture | What it sends |
|---|---|---|
| `key` | touch down / up | press / release of a keycode (with a deck channel) |
| `scroll` | vertical drag | the browse knob, ±1 per `touch.scroll_step` of screen height; a **tap** sends the zone's `tap_key` (SELECT) |
| `jog` | horizontal drag | `JOG_TOUCH` press, then `JOG_ROT` with a speed in rev/s, then a stop on release |
| `value` | drag inside the zone | a 10-bit absolute value (a fader, EQ, filter) |
| `none` | — | ignore touches here (status areas) |

The layout is `/etc/rb4r5/touch-zones.json`; `launch.py zones` prints and
validates it, and `launch.py calibrate` shows what each touch hits without
driving the player.

## Tuning the feel (zones)

```json
"touch": {
  "tap_ms": 400,
  "tap_slop": 0.02,
  "scroll_step": 0.035,
  "jog_scale": 3.0
}
```

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

**With black bars, the panel and the UI are different rectangles.** Since
`display.fit` defaults to `aspect`, the picture does not reach the edges of
the glass: on a 2880×1620 panel the frame is 2592×1620 with 144 px of black
each side. The daemon reads the framebuffer geometry at start-up, works out
the same rectangle the driver uses (`fb.frame_rect()`, tied to the C by a
test), and maps every contact through it — so a touch two thirds across the
glass is two thirds across the *UI*, and a press on a bar presses nothing.
`launch.py calibrate` prints the rectangle it is using:

```
the UI covers 90% x 100% of the panel at 5%,0% (black bars) - touches are mapped through that
```

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

## Browse lists: why tapping a playlist did nothing

The engine has no "open the thing at this pixel". It has "turn the browse
knob" and "press it". So a tap that only presses SELECT opens whatever was
already highlighted — which is almost never the row you just touched, and
looks exactly like touch not working at all.

The `list` zone type fixes that. A tap works out which row was touched, turns
the selector by the difference between that row and where the highlight is,
and then presses it:

```json
{"name": "list", "rect": [0.00, 0.09, 1.00, 0.70],
 "type": "list", "key": "selector", "select_key": "select", "rows": 9}
```

* **drag** still scrolls the list, and the tracked highlight follows;
* **tap a row** moves the highlight there and opens it;
* **long-press a row** says "the highlight is already here" and re-syncs
  without sending anything — the repair when the controller's own knob has
  moved it behind our back;
* pressing BROWSE, BACK, SOURCE, MENU or TAG LIST puts the tracked highlight
  back at the top, because those all open a fresh list.

`rows` is how many rows the list shows on screen. If tapping consistently
lands a row or two off, that number is wrong for your firmware's layout:
count the rows on screen and set it.
