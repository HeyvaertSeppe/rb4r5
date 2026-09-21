"""Audio: find the DDJ-FLX4 (or HDMI), and tell audioshim how to use it.

rbp opens three XDJ-RX3 devices that do not exist here; audioshim.so muxes
them onto one real PCM and takes every hardware detail from the environment,
so all the discovery lives here in Python where it can be inspected:

    DDJ-FLX4 : 4 playback channels - 1/2 MASTER out, 3/4 HEADPHONES out
    HDMI     : 2 channels, used as the fallback when no controller is present

Nothing here opens the PCM for real; the player does that.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import util

# ALSA pcm format ids (sound/asound.h), the ones a USB DJ controller can use.
FORMAT_IDS = {
    "S8": 0, "U8": 1,
    "S16_LE": 2, "S16_BE": 3, "U16_LE": 4, "U16_BE": 5,
    "S24_LE": 6, "S24_BE": 7, "U24_LE": 8, "U24_BE": 9,
    "S32_LE": 10, "S32_BE": 11, "U32_LE": 12, "U32_BE": 13,
    "FLOAT_LE": 14, "FLOAT_BE": 15,
    "S24_3LE": 32, "S24_3BE": 33, "U24_3LE": 34, "U24_3BE": 35,
}
FORMAT_NAMES = {v: k for k, v in FORMAT_IDS.items()}


def cards(path: str = "/proc/asound/cards") -> list[dict]:
    """Parse /proc/asound/cards into [{index, id, name, longname}]."""
    result = []
    text = util.read_text(path)
    current = None
    for line in text.splitlines():
        head = re.match(r"\s*(\d+)\s+\[(\S+)\s*\]:\s*(.*)", line)
        if head:
            current = {
                "index": int(head.group(1)),
                "id": head.group(2),
                "name": head.group(3).strip(),
                "longname": "",
            }
            result.append(current)
        elif current is not None and line.strip():
            current["longname"] = line.strip()
    return result


def find_card(fragment: str) -> dict | None:
    """First card whose id / name / longname contains `fragment` (case-insensitive)."""
    if not fragment:
        return None
    needle = fragment.lower()
    for card in cards():
        blob = f"{card['id']} {card['name']} {card['longname']}".lower()
        if needle in blob:
            return card
    return None


def find_controller(name_fragment: str = "FLX4") -> dict | None:
    """The DJ controller's card: exact model first, then any Pioneer DDJ."""
    return find_card(name_fragment) or find_card("DDJ") or find_card("AlphaTheta")


def find_hdmi() -> dict | None:
    return find_card("HDMI") or find_card("vc4")


def stream_caps(index: int, path: str | None = None) -> dict:
    """Playback capability of a USB audio card, from /proc/asound/cardN/stream0.

    Returns {channels, formats, rates} - empty when the card is not USB audio
    (the Pi's HDMI cards have no stream file; they are stereo).
    """
    caps = {"channels": 0, "formats": [], "rates": []}
    text = util.read_text(path or f"/proc/asound/card{index}/stream0")
    if not text:
        return caps
    section = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Playback:"):
            section = "play"
            continue
        if stripped.startswith("Capture:"):
            section = "capture"
            continue
        if section != "play":
            continue
        if stripped.startswith("Format:"):
            fmt = stripped.split(":", 1)[1].strip()
            if fmt and fmt not in caps["formats"]:
                caps["formats"].append(fmt)
        elif stripped.startswith("Channels:"):
            try:
                channels = int(stripped.split(":", 1)[1])
            except ValueError:
                continue
            caps["channels"] = max(caps["channels"], channels)
        elif stripped.startswith("Rates:"):
            for rate in re.findall(r"\d+", stripped.split(":", 1)[1]):
                value = int(rate)
                if value not in caps["rates"]:
                    caps["rates"].append(value)
    caps["rates"].sort()
    return caps


def pcm_status(index: int, device: int = 0) -> dict:
    """Live state of a playback PCM (who owns it, and at what parameters)."""
    base = Path(f"/proc/asound/card{index}/pcm{device}p/sub0")
    params, status = {}, {}
    for line in util.read_text(base / "hw_params").splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            params[key.strip()] = value.strip()
    for line in util.read_text(base / "status").splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            status[key.strip()] = value.strip()
    return {"hw_params": params, "status": status,
            "state": status.get("state", "closed")}


def select(cfg) -> dict:
    """Decide which device the player will use, and with what parameters.

    The result is what env() turns into RB_AUDIO_* for audioshim.
    """
    want_rate = int(cfg.get("audio.rate", 44100))
    want_fmt = int(cfg.get("audio.format", FORMAT_IDS["S24_LE"]))
    plug = bool(cfg.get("audio.plug", True))
    notes: list[str] = []

    override = cfg.get("audio.device")
    if override:
        channels = int(cfg.get("audio.channels") or 2)
        return {"device": override, "channels": channels, "rate": want_rate,
                "format": want_fmt, "source": "config", "card": None,
                "notes": ["audio.device set in the config file; no detection"]}

    card_frag = cfg.get("audio.card")
    card = find_card(card_frag) if card_frag else \
        find_controller(cfg.get("controller.name", "FLX4"))
    source = "controller"

    if card is None and cfg.get("audio.fallback_hdmi", True):
        card = find_hdmi()
        source = "hdmi"
        if card:
            notes.append("no DJ controller found - using HDMI audio; plug the "
                         "FLX4 in and restart for master + headphone cue")
    if card is None:
        return {"device": "default", "channels": 2, "rate": want_rate,
                "format": want_fmt, "source": "none", "card": None,
                "notes": ["no usable ALSA card found; the player will run "
                          "without audio and the transport will not advance"]}

    caps = stream_caps(card["index"])
    channels = cfg.get("audio.channels")
    if channels:
        channels = int(channels)
    elif source == "controller":
        channels = 4 if caps["channels"] >= 4 else 2
        if caps["channels"] and caps["channels"] < 4:
            notes.append(f"{card['id']} reports only {caps['channels']} playback "
                         "channels; headphone cue needs 4 (check the controller's "
                         "USB mode)")
    else:
        channels = 2

    if caps["rates"] and want_rate not in caps["rates"]:
        if plug:
            notes.append(f"{card['id']} does not offer {want_rate} Hz "
                         f"(only {caps['rates']}); alsa-lib will resample")
        else:
            notes.append(f"{card['id']} does not offer {want_rate} Hz and "
                         "audio.plug is false - enable plug or the open fails")

    if caps["formats"]:
        native = [FORMAT_IDS.get(name) for name in caps["formats"]]
        if want_fmt not in native:
            if plug:
                notes.append(f"{card['id']} is {'/'.join(caps['formats'])}; "
                             "alsa-lib converts from S24_LE")
            else:
                notes.append(f"{card['id']} cannot take "
                             f"{FORMAT_NAMES.get(want_fmt, want_fmt)} directly; "
                             "audio.plug must stay true")

    prefix = "plughw" if plug else "hw"
    device = f"{prefix}:CARD={card['id']},DEV=0"
    fallback = f"{prefix}:{card['index']},0"
    return {
        "device": f"{device},{fallback}" if device != fallback else device,
        "channels": channels,
        "rate": want_rate,
        "format": want_fmt,
        "source": source,
        "card": card,
        "caps": caps,
        "notes": notes,
    }


def env(cfg, chosen: dict | None = None) -> dict:
    """The RB_AUDIO_* / STARTUP_* environment audioshim reads."""
    chosen = chosen or select(cfg)
    return {
        "RB_AUDIO_DEV": chosen["device"],
        "RB_AUDIO_CH": str(chosen["channels"]),
        "RB_AUDIO_RATE": str(chosen["rate"]),
        "RB_AUDIO_FMT": str(chosen["format"]),
        "RB_AUDIO_CUE_MIRROR": "1" if cfg.get("audio.cue_mirror") else "0",
        "RB_AUDIO_CUE_ON_2CH": "1" if cfg.get("audio.cue_on_stereo") else "0",
        "STARTUP_MUTE_MS": str(int(cfg.get("audio.startup_mute_ms", 1500))),
        "STARTUP_FADE_MS": str(int(cfg.get("audio.startup_fade_ms", 300))),
    }


def unmute(card: dict | None) -> list[str]:
    """Open up whatever playback controls the card has (best effort)."""
    done = []
    if not card or not util.have("amixer"):
        return done
    listing = util.out(["amixer", "-c", str(card["index"]), "scontrols"])
    for match in re.finditer(r"'([^']+)'", listing):
        name = match.group(1)
        if name.lower() in ("pcm", "master", "speaker", "headphone", "hdmi"):
            proc = util.run(["amixer", "-q", "-c", str(card["index"]),
                             "sset", name, "100%", "unmute"], check=False)
            if proc.returncode == 0:
                done.append(f"{card['id']}: {name} -> 100% unmuted")
    return done


def describe(cfg) -> list[str]:
    """Lines for `doctor` / `setup` output."""
    lines = []
    for card in cards():
        caps = stream_caps(card["index"])
        extra = ""
        if caps["channels"]:
            extra = (f" [{caps['channels']}ch "
                     f"{'/'.join(caps['formats'][:3]) or '?'} "
                     f"{','.join(str(r) for r in caps['rates'][:3]) or '?'}Hz]")
        lines.append(f"card {card['index']} [{card['id']}] {card['name']}{extra}")
    if not lines:
        lines.append("no ALSA cards (is snd_usb_audio loaded? is a monitor "
                     "connected for HDMI audio?)")
    chosen = select(cfg)
    lines.append(f"chosen: {chosen['device']} "
                 f"({chosen['channels']}ch @{chosen['rate']} Hz, "
                 f"{FORMAT_NAMES.get(chosen['format'], chosen['format'])}, "
                 f"source={chosen['source']})")
    if chosen["channels"] == 4:
        lines.append("routing: master -> FLX4 ch 1/2 (MASTER out), "
                     "cue -> ch 3/4 (HEADPHONES)")
    else:
        lines.append("routing: master -> ch 1/2 (stereo sink; no headphone cue)")
    lines.extend(f"note: {note}" for note in chosen["notes"])
    return lines
