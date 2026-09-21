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
          "plughw:CARD=FLX4,DEV=0")
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


def check2(label, cond):
    if not cond:
        failures.append(label)
        print(f"FAIL {label}")
    else:
        print(f"ok   {label}")


print("\n== the device name is one device, and the candidates are a list")

_real_controller = audio.find_controller
_real_caps = audio.stream_caps
_real_hdmi = audio.find_hdmi


def _fake_card(ident, index, channels=4):
    audio.find_controller = lambda *a, **k: {
        "id": ident, "index": index, "name": ident, "longname": ident}
    audio.find_hdmi = lambda *a, **k: None
    audio.stream_caps = lambda *a, **k: {
        "channels": channels, "formats": ["S24_3LE"], "rates": [44100]}


try:
    for ident, index in (("DDJFLX4", 2), ("vc4hdmi0", 0)):
        _fake_card(ident, index)
        cfg = config.load("/nonexistent-audio.json")
        chosen = audio.select(cfg)

        check2(f"{ident}: the device is one name",
              chosen["device"].count("hw:") == 1)
        check2(f"{ident}: it names the card",
              chosen["device"] == f"plughw:CARD={ident},DEV=0")
        check2(f"{ident}: the candidates are a list",
              isinstance(chosen["candidates"], list) and
              len(chosen["candidates"]) >= 2)
        for name in chosen["candidates"]:
            check2(f"{ident}: candidate {name!r} is one device",
                  name.count("hw:") <= 1)

        # what audioshim receives: separated by something that is NOT a comma,
        # and every piece has to survive the split intact
        blob = audio.env(cfg, chosen)["RB_AUDIO_DEV"]
        check2(f"{ident}: the shim gets them '|' separated", "|" in blob)
        parts = blob.split("|")
        check2(f"{ident}: they split back to exactly the candidates",
              parts == chosen["candidates"])
        check2(f"{ident}: splitting on a comma would destroy them",
              any("," in part for part in parts))
finally:
    audio.find_controller = _real_controller
    audio.stream_caps = _real_caps
    audio.find_hdmi = _real_hdmi

# and the shim must agree about the separator
shim = (Path(__file__).resolve().parents[2] / "src/shims/audioshim.c").read_text()
check2("audioshim splits the candidate list on '|'",
      'strtok_r(list, "|", &save)' in shim)
check2("audioshim does not split it on ','",
      'strtok_r(list, ",", &save)' not in shim)

print()
if failures:
    for line in failures:
        print("FAIL", line)
    sys.exit(1)
print("all audio parsing checks passed")


# ---------------------------------------------------------------- device names
# An ALSA device name contains commas of its own - "plughw:CARD=FLX4,DEV=0" is
# ONE device - so a list of candidates can never be joined with a comma.  It
# was, and the result was a name that could not open on any card, which is why
# there was no sound at all: aplay said
#     Parameter SUBDEV must be an integer
#     Unknown PCM plughw:CARD=DDJFLX4,DEV=0,plughw:2,0
