#!/usr/bin/env python3
"""Offline checks for the ALSA parsing in rb4r5/audio.py (no hardware needed).

Run:  python3 tools/tests/test_audio_parse.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from rb4r5 import audio, config  # noqa: E402

CARDS = """\
 0 [vc4hdmi0       ]: vc4-hdmi - vc4-hdmi-0
                      vc4-hdmi-0
 1 [vc4hdmi1       ]: vc4-hdmi - vc4-hdmi-1
                      vc4-hdmi-1
 2 [FLX4           ]: USB-Audio - DDJ-FLX4
                      Pioneer DJ DDJ-FLX4 at usb-xhci-hcd.1-2, high speed
"""

STREAM0 = """\
Pioneer DJ DDJ-FLX4 at usb-xhci-hcd.1-2, high speed : USB Audio

Playback:
  Status: Stop
  Interface 1
    Altset 1
    Format: S24_3LE
    Channels: 4
    Endpoint: 0x01 (1 OUT) (ASYNC)
    Rates: 44100
    Bits: 24

Capture:
  Status: Stop
  Interface 2
    Altset 1
    Format: S24_3LE
    Channels: 2
    Endpoint: 0x82 (2 IN) (ASYNC)
    Rates: 44100
"""

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label} = {got!r}")


with tempfile.TemporaryDirectory() as tmp:
    cards_file = Path(tmp, "cards")
    cards_file.write_text(CARDS)
    stream_file = Path(tmp, "stream0")
    stream_file.write_text(STREAM0)

    cards = audio.cards(str(cards_file))
    check("card count", len(cards), 3)
    check("flx4 index", cards[2]["index"], 2)
    check("flx4 id", cards[2]["id"], "FLX4")
    check("flx4 name", cards[2]["name"], "USB-Audio - DDJ-FLX4")
    check("hdmi id", cards[0]["id"], "vc4hdmi0")

    caps = audio.stream_caps(2, str(stream_file))
    check("playback channels", caps["channels"], 4)
    check("playback formats", caps["formats"], ["S24_3LE"])
    check("playback rates", caps["rates"], [44100])

    # selection: point the module at the fixtures and check what the shim
    # would be told to do
    real_cards, real_caps = audio.cards, audio.stream_caps
    audio.cards = lambda path=str(cards_file): real_cards(str(cards_file))
    audio.stream_caps = lambda index, path=None: (
        real_caps(index, str(stream_file)) if index == 2 else
        {"channels": 0, "formats": [], "rates": []})

    cfg = config.load("/nonexistent-rb4r5.json")
    chosen = audio.select(cfg)
    check("selected source", chosen["source"], "controller")
    check("selected card", chosen["card"]["id"], "FLX4")
    check("selected channels", chosen["channels"], 4)
    check("selected device", chosen["device"],
          "plughw:CARD=FLX4,DEV=0,plughw:2,0")
    env = audio.env(cfg, chosen)
    check("env RB_AUDIO_CH", env["RB_AUDIO_CH"], "4")
    check("env RB_AUDIO_FMT", env["RB_AUDIO_FMT"], "6")   # S24_LE, plug converts
    check("conversion note",
          any("converts from S24_LE" in n for n in chosen["notes"]), True)

    # no controller -> HDMI fallback, stereo
    audio.cards = lambda path=None: [c for c in real_cards(str(cards_file))
                                     if "FLX4" not in c["id"]]
    hdmi = audio.select(cfg)
    check("fallback source", hdmi["source"], "hdmi")
    check("fallback channels", hdmi["channels"], 2)
    check("fallback card", hdmi["card"]["id"], "vc4hdmi0")

    audio.cards, audio.stream_caps = real_cards, real_caps

print()
if failures:
    for line in failures:
        print("FAIL", line)
    sys.exit(1)
print("all audio parsing checks passed")
