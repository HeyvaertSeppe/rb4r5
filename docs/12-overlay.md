# 12 — The bar, the effect picker and the splash

Three things the launcher draws itself, in the parts of the screen the player
does not own.

## Why there is anything to draw

The XDJ-RX3 has a row of buttons **above** its display — SOURCE, BROWSE, TAG
LIST, INFO, MENU, BACK — and a Pi with one touch panel has nowhere to put
them. So the display driver keeps a strip of rows at the top clear
(`RB_FB_TOP`, see [04-display](04-display.md)) and `rb4r5/overlay.py` owns it.

Nothing composites. The player never writes above `RB_FB_TOP` — not even to
clear — so the bar can be drawn once and left alone. The two full-screen
overlays (picker, splash) simply cover the player's frames; taking one down
means letting the player's next redraw show through, which it does
continuously anyway.

```
 ┌──────────────────────────────────────────────┐  ← RB_FB_TOP rows: ours
 │  SOURCE  BROWSE  TAG  INFO  MENU  BACK   FX  │
 ├──────────────────────────────────────────────┤
 │ ▓▓▓                                      ▓▓▓ │  ← black bars (aspect fit)
 │ ▓▓▓        the player's 1280x800         ▓▓▓ │
 │ ▓▓▓         frame, scaled up             ▓▓▓ │
 └──────────────────────────────────────────────┘
```

## The button bar

`display.top_bar` turns it on (default), `display.top_bar_height` sets its
height in pixels — `0` means 8% of the panel, which is 130 rows on a
2880×1620 screen. Each button sends the same keycode the RX3's own button
sends (`rb4r5/keys.py`), so SOURCE really is SOURCE.

Edit `/etc/rb4r5/top-bar.json` to change them:

```json
[ {"label": "SOURCE", "key": "source"},
  {"label": "BROWSE", "key": "browse"},
  {"label": "FX",     "action": "fx"} ]
```

`key` is any name from `launch.py keys --list`; `action` is ours (`fx` opens
the effect picker).

**About the lighting.** The buttons light amber while pressed, and the FX
button stays lit while the picker is open. That is the launcher's own state,
not the player's: the engine reports its LED state over the subucom serial
link, which this port stubs out and does not decode. So the bar shows what
*you* did, not what the player thinks. Decoding those frames would make the
bar (and the controller's own LEDs) exact; it is the single most useful
unfinished piece of reverse engineering left here.

## The effect picker

The FLX4 has one FX knob and no screen, so there is no way to see the list of
Beat FX or jump to one. Pressing **SHIFT + the FX SELECT knob** opens a grid
covering 70% of the screen; tapping an effect selects it. The top bar's FX
button does the same thing.

Selection is relative — the engine takes "turn the FX type selector one step",
not "select ECHO" — so the picker tracks which effect it believes is current
and sends the difference. Two consequences:

* the **order** in `/etc/rb4r5/fx-list.json` matters, not the spelling. If
  tapping REVERB lands on TRANS, the list is in the wrong order for your
  firmware: fix the file, not the code.
* if something else moves the selection (the knob itself), the two drift
  apart. **Long-press an effect** to say "it is already on this one" — that
  re-syncs without sending anything.

## The boot splash

`overlay.splash` (default on) covers the screen while the player starts, with
a progress bar and what it is doing. Nothing is delayed to make this happen:
rbp starts, loads and draws *underneath* the splash, exactly as it would
without it.

The hand-off is not on a timer. The daemon fingerprints a few pixels of the
area it painted inside the player's rectangle, and watches for them to
change — when the player draws its first real frame, those pixels stop being
the splash's, and the splash comes down. `overlay.splash_min_seconds` keeps it
up long enough to read; `overlay.splash_max_seconds` gives up and shows the
player anyway.

## Seeing it without a panel

```sh
python3 launch.py overlay --preview /tmp/ui     # three PNGs, real panel size
sudo python3 launch.py overlay --fx             # toggle the picker
sudo python3 launch.py overlay --splash 0.5 --message "loading"
```

## How touches are split

Two daemons read the same touch device — evdev gives every reader its own
copy — and they divide the screen:

| where | who handles it |
|---|---|
| the top bar | `rboverlay` |
| the player's frame | `rbtouchd` (zones, [07-touch](07-touch.md)) |
| the black bars | nobody: a press there presses nothing |
| anywhere, while the picker or splash is up | `rboverlay` only |

The last row is what `/tmp/rb-overlay.state` is for: `rbtouchd` reads it and
keeps off the UI while a modal is up.

## The effect list

Every Beat FX is listed down the **left black border**, with the selected one
lit in the RX3's amber. It is always there — the FLX4 has one FX knob and no
way to show what it is set to, and a list you can see beats a box you have to
open.

It mirrors the master meter on the right, and neither touches the player's
picture, so nothing ever draws over the UI.

| what | does |
|---|---|
| **BEAT FX SELECT** | one effect **down** the list |
| **SHIFT + BEAT FX SELECT** | one effect **up** |
| a tap on a row | that effect |
| a long press on a row | "it is already on this one" — re-syncs without sending anything |

The button lights for a moment each time it moves the selection. It never lit
at all before: the branch in the bridge that handles it returns before
`led_for_press()` is reached.

### How the effect actually changes

BEAT FX SELECT is a **selector knob** on the RX3 — not a button, and not an
endless encoder. The engine therefore wants an absolute position:

```
op 4 ROTATE: param = 10-bit absolute (faders, EQ, trim, crossfader)
             or relative delta (browse knob)
```

Sending a delta of `+1` reads as position **1 out of 1023** — the bottom of
the knob's travel, which is the first effect in the list. That is why it sat
on DELAY however many times it was pressed. A press and release does nothing
at all, because it is not a button.

One message puts the knob where it belongs, so there is no stepping and no
wrapping: up and down cost the same.

| effect | position |
|---|---|
| first (DELAY) | 0 |
| middle | ~511 |
| last | 1023 |

If that reading of the control turns out to be wrong too, the other two can
be tried without a rebuild:

```
sudo python3 launch.py config --set overlay.fx_mode=delta   # or: tap
```

### There is no popup any more

The picker was a 70% box in the middle of the screen. It held the player off
while it was up, the master meter drew straight over it twenty times a
second, and it had to be opened before it could tell you anything. All of
that is gone: `Overlay.mode` is now only `none` or `splash`.
