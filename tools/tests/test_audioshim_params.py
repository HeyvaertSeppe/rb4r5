#!/usr/bin/env python3
"""audioshim's virtual streams must leave hw_params READABLE.

The XDJ-RX3 engine opens four playback streams and a capture stream.  Only the
first is real hardware; audioshim answers for the rest with virtual handles.
Those answers have to be complete, because `snd_pcm_hw_params_t` is opaque and
the caller allocates it ZEROED - every mask inside it empty.  alsa-lib does
not return an error when asked to read an empty mask, it asserts:

    rbp: mask_inline.h:282: snd_mask_value: Assertion `!snd_mask_empty(mask)'
    failed.

and the player aborts.  `snd_pcm_hw_params_any()` returning 0 without touching
`params` is therefore not a harmless stub - it is a crash, and it crashed the
player on every start once audio began opening devices at all.

This builds audioshim against a fake libasound, opens a virtual stream, and
checks that hw_params comes back filled in.

Run:  python3 tools/tests/test_audioshim_params.py
"""
import ctypes
import os
import subprocess
import sys
import struct
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
failures = []

# Pin the shim's configuration before it is loaded: cfg_init() reads these
# once, on the first call into it.
os.environ["RB_AUDIO_DEV"] = "plughw:CARD=TESTCARD,DEV=0"
os.environ["RB_AUDIO_CH"] = "4"
os.environ["RB_AUDIO_RATE"] = "44100"
os.environ["RB_AUDIO_PERIOD"] = "512"
os.environ["RB_AUDIO_PERIODS"] = "4"

FAKE_ASOUND = r"""
/* Just enough libasound for audioshim to talk to, and a params struct that
 * records whether anyone filled it. */
#include <string.h>
#include <stdio.h>

#define MAGIC 0x5A5AF00D

struct fake_pcm { char name[64]; int stream; };
static struct fake_pcm pcms[8];
static int npcm = 0;

/* every call is recorded here so the test can read back what happened */
static char opened[8][64];
static int  n_opened = 0;

const char *fake_opened(int i)
{
    return (i >= 0 && i < n_opened) ? opened[i] : 0;
}
int fake_opened_count(void) { return n_opened; }

int snd_pcm_open(void **pcm, const char *name, int stream, int mode)
{
    (void)mode;
    if (npcm >= 8) return -1;
    snprintf(pcms[npcm].name, sizeof(pcms[npcm].name), "%s", name ? name : "");
    pcms[npcm].stream = stream;
    if (n_opened < 8)
        snprintf(opened[n_opened++], 64, "%s", name ? name : "");
    *pcm = &pcms[npcm++];
    return 0;
}

static int n_closed = 0;
int snd_pcm_close(void *pcm) { (void)pcm; n_closed++; return 0; }
int fake_closed_count(void) { return n_closed; }
int snd_pcm_drop(void *pcm) { (void)pcm; return 0; }
int snd_pcm_hw_free(void *pcm) { (void)pcm; return 0; }

/* the real one fills the caller's struct with the device's capabilities;
 * the fake one writes a sentinel so the test can see that it happened */
int snd_pcm_hw_params_any(void *pcm, void *params)
{
    unsigned int magic = MAGIC;
    if (!pcm || !params) return -22;
    memcpy(params, &magic, sizeof(magic));
    return 0;
}

static int applied_access = -1;
int snd_pcm_hw_params(void *a, void *b) { (void)a; (void)b; return 0; }
int snd_pcm_hw_params_set_access(void *a, void *b, int c)
{ (void)a;(void)b; applied_access = c; return 0; }
int fake_access(void) { return applied_access; }
void fake_forget_access(void) { applied_access = -1; }
int snd_pcm_hw_params_set_format(void *a, void *b, int c) { (void)a;(void)b;(void)c; return 0; }
int snd_pcm_hw_params_set_channels(void *a, void *b, unsigned c) { (void)a;(void)b;(void)c; return 0; }
int snd_pcm_hw_params_set_rate_near(void *a, void *b, unsigned *c, int *d) { (void)a;(void)b;(void)c;(void)d; return 0; }
/* record what the shim actually asks the hardware for */
static unsigned long asked_period = 0;
static unsigned asked_periods = 0;
int snd_pcm_hw_params_set_period_size_near(void *a, void *b, unsigned long *c, int *d)
{ (void)a;(void)b;(void)d; if (c) asked_period = *c; return 0; }
int snd_pcm_hw_params_set_periods_near(void *a, void *b, unsigned *c, int *d)
{ (void)a;(void)b;(void)d; if (c) asked_periods = *c; return 0; }
unsigned long fake_period(void) { return asked_period; }
unsigned fake_periods(void) { return asked_periods; }
int snd_pcm_sw_params_current(void *a, void *b) { (void)a;(void)b; return 0; }
int snd_pcm_sw_params_get_boundary(const void *a, unsigned long *b) { (void)a; if (b) *b = 0; return 0; }
int snd_pcm_sw_params_set_silence_threshold(void *a, void *b, unsigned long c) { (void)a;(void)b;(void)c; return 0; }
int snd_pcm_sw_params_set_silence_size(void *a, void *b, unsigned long c) { (void)a;(void)b;(void)c; return 0; }
int snd_pcm_sw_params_set_start_threshold(void *a, void *b, unsigned long c) { (void)a;(void)b;(void)c; return 0; }
int snd_pcm_sw_params_set_stop_threshold(void *a, void *b, unsigned long c) { (void)a;(void)b;(void)c; return 0; }
int snd_pcm_sw_params(void *a, void *b) { (void)a;(void)b; return 0; }
int snd_pcm_prepare(void *a) { (void)a; return 0; }
long snd_pcm_writei(void *a, const void *b, unsigned long c) { (void)a;(void)b; return (long)c; }
int snd_pcm_state(void *a) { (void)a; return 3; }
int snd_ctl_open(void **a, const char *b, int c) { (void)b;(void)c; *a = (void *)1; return 0; }
int snd_ctl_close(void *a) { (void)a; return 0; }
"""


def check(label, got, want=True):
    if got != want:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label}")


def cc(src: Path, out: Path, *extra) -> bool:
    cmd = ["cc", "-shared", "-fPIC", "-o", str(out), str(src), *extra]
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        print(done.stderr.strip()[:2000])
    return done.returncode == 0


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)

    fake = tmp / "fake_asound.c"
    fake.write_text(FAKE_ASOUND)
    # The SONAME matters: audioshim dlopen()s "libasound.so.2" by plain name,
    # and the loader only matches an already-loaded object by its SONAME.
    check("the fake libasound builds",
          cc(fake, tmp / "libasound.so.2", "-Wl,-soname,libasound.so.2"))

    shim = tmp / "audioshim.so"
    levels = tmp / "levels.dat"
    built = cc(REPO / "src/shims/audioshim.c", shim,
               "-DRB_NO_SYMVER", "-DSYS_mmap2=9", "-DSYS_poll=7",
               f'-DRB_LEVELS_PATH="{levels}"',
               f"-I{REPO / 'src/shims'}", "-O1", "-w", "-ldl")
    check("audioshim builds for the host", built)
    if not built:
        print("\ncannot run the rest without it")
        sys.exit(1)

    # dlopen (not LD_PRELOAD): the shim defines mmap(), and interposing that
    # on the test process itself would take malloc down with it.
    # load the fake FIRST, so the shim's dlopen("libasound.so.2") finds it
    # already present under that SONAME instead of searching the system.
    asound = ctypes.CDLL(str(tmp / "libasound.so.2"), mode=ctypes.RTLD_GLOBAL)
    lib = ctypes.CDLL(str(shim), mode=ctypes.RTLD_LOCAL)

    # audioshim dlopen()s "libasound.so.2" by name, so it has to be findable.
    # ctypes already loaded it under its full path, which satisfies the
    # loader's cache for the plain name.
    handle = ctypes.c_void_p()
    params = (ctypes.c_ubyte * 256)()

    SND_PCM_STREAM_CAPTURE = 1
    rc = lib.snd_pcm_open(ctypes.byref(handle), b"rx3-capture",
                          SND_PCM_STREAM_CAPTURE, 0)
    check("a capture stream opens as a virtual handle", rc, 0)
    check("and it is not NULL", bool(handle.value))

    rc = lib.snd_pcm_hw_params_any(handle, ctypes.byref(params))
    check("hw_params_any on a virtual stream succeeds", rc, 0)

    magic = int.from_bytes(bytes(params[:4]), sys.byteorder)
    check("and it FILLED the params struct, rather than leaving it zeroed "
          "for alsa-lib to assert on", hex(magic), hex(0x5A5AF00D))

    # the donor must be alsa-lib's own null device: no hardware, always there
    asound.fake_opened.restype = ctypes.c_char_p

    def opens():
        """Every device name libasound has actually been asked to open."""
        return [asound.fake_opened(i).decode()
                for i in range(asound.fake_opened_count())]

    names = opens()
    check("the params come from alsa-lib's null device, which needs no "
          "hardware", "null" in names, True)

    # --- which stream is the master -----------------------------------
    #
    # The engine opens and closes these repeatedly while it enumerates
    # devices.  Deciding by open ORDER meant that once the count had drifted
    # the master was handed a dummy handle: its audio went nowhere, the
    # meters never moved, and nothing in the log looked wrong.  Map by name.
    SND_PCM_STREAM_PLAYBACK = 0

    def open_out(name):
        h = ctypes.c_void_p()
        rc = lib.snd_pcm_open(ctypes.byref(h), name.encode(),
                              SND_PCM_STREAM_PLAYBACK, 0)
        return rc, h

    rc, master = open_out("hw:cs4344audiorev8,0")
    check("the master output opens", rc, 0)
    rc, hp = open_out("hw:cs4344audiorev8,1")
    check("the headphone output opens", rc, 0)
    rc, booth = open_out("hw:cs4344audiorev8,2")
    check("the booth output opens", rc, 0)

    check("master, headphones and booth are three different streams",
          len({master.value, hp.value, booth.value}), 3)

    # close and reopen in a different order, exactly as the engine does
    lib.snd_pcm_close(master)
    lib.snd_pcm_close(hp)
    lib.snd_pcm_close(booth)
    rc, booth2 = open_out("hw:cs4344audiorev8,2")
    rc, hp2 = open_out("hw:cs4344audiorev8,1")
    rc, master2 = open_out("hw:cs4344audiorev8,0")

    check("the master is still the master after a close and a reopen "
          "out of order", master2.value, master.value)
    check("and the headphones are still the headphones", hp2.value, hp.value)
    check("and the booth is still the booth", booth2.value, booth.value)

    # the mic/return card is never an output
    rc, mic = open_out("hw:esaics4344audio,0")
    check("the mic card is not mistaken for the master",
          mic.value != master.value, True)

    # The master must be the REAL device, not a virtual stand-in.
    check("the master went to the configured real device",
          "plughw:CARD=TESTCARD,DEV=0" in opens(), True)

    # And the card must not be closed out from under us: the engine closes
    # the master several times during startup, and a card that was really
    # closed comes back EBUSY on the next open - which is how the transport
    # lost its device and stopped advancing.
    check("closing the master never closes the card",
          asound.fake_closed_count(), 0)

    # --- the hardware buffer must be big enough for USB ----------------
    #
    # The engine asks for two periods of 64 frames: 2.9 ms, which suits the
    # RX3's local I2S output and underruns on every period over USB, until
    # the shim gives up on the device and there is no sound at all.
    period = ctypes.c_ulong(64)
    periods = ctypes.c_uint(2)
    lib.snd_pcm_hw_params_set_period_size_near(
        master2, ctypes.byref(params), ctypes.byref(period), None)
    lib.snd_pcm_hw_params_set_periods_near(
        master2, ctypes.byref(params), ctypes.byref(periods), None)
    asound.fake_period.restype = ctypes.c_ulong
    check("a 64-frame period is raised to something USB can hold",
          asound.fake_period(), 512)
    check("and two periods become four", asound.fake_periods(), 4)

    # a request that is already generous is left alone
    period = ctypes.c_ulong(2048)
    lib.snd_pcm_hw_params_set_period_size_near(
        master2, ctypes.byref(params), ctypes.byref(period), None)
    check("a period that is already large enough is not shrunk",
          asound.fake_period(), 2048)

    # --- access ---------------------------------------------------------
    #
    # This shim reaches the hardware through snd_pcm_writei(), and writei on
    # a PCM configured for mmap access returns -EINVAL on every call:
    #
    #   writei #1 frames=64 written=-22
    #   the output device stopped accepting audio (Invalid argument)
    #
    # The engine does not always ask for an access type at all, so setting it
    # only when it does is not enough.
    SND_PCM_ACCESS_MMAP_INTERLEAVED = 0
    SND_PCM_ACCESS_RW_INTERLEAVED = 3

    lib.snd_pcm_hw_params_set_access(master2, ctypes.byref(params),
                                     SND_PCM_ACCESS_MMAP_INTERLEAVED)
    check("an mmap access request is overridden, because writei cannot use it",
          asound.fake_access(), SND_PCM_ACCESS_RW_INTERLEAVED)

    # and it is set even when the engine never asks
    asound.fake_forget_access()
    lib.snd_pcm_hw_params(master2, ctypes.byref(params))
    check("access is set by hw_params even when the engine never asked",
          asound.fake_access(), SND_PCM_ACCESS_RW_INTERLEAVED)

    # --- what a sample actually says ------------------------------------
    #
    # S24_LE puts a 24-bit sample in the low three bytes of a 32-bit
    # container and leaves the top byte alone, so a negative sample arrives
    # looking like a large positive int32.  Read raw, -16 (0x00FFFFF0) comes
    # out as 16777200 against a full scale of 8388607 - which is why the
    # meter sat pinned at the top with nothing playing.
    FRAMES = 64

    def write_master(sample):
        """Feed the master FRAMES stereo frames of one S24_LE value."""
        raw = (sample & 0x00FFFFFF).to_bytes(4, "little")
        buf = ctypes.create_string_buffer(raw * 2 * FRAMES, 4 * 2 * FRAMES)
        lib.snd_pcm_writei.restype = ctypes.c_long
        lib.snd_pcm_writei(master2, buf, ctypes.c_ulong(FRAMES))

    def published():
        blob = levels.read_bytes()
        _seq, left, right, _phones, full = struct.unpack("<5i", blob[:20])
        return left, right, full

    write_master(-16)
    left, right, full = published()
    check("full scale is signed 24-bit", full, 8388607)
    check("near-silence reads as near-silence, not as full scale",
          left < 1000 and right < 1000, True)

    time.sleep(0.06)            # publish_levels only writes 20x a second
    write_master(-8388608)      # the most negative sample there is
    left, _right, full = published()
    check("a loud negative sample reads as loud",
          left > full * 9 // 10, True)

print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("audioshim's virtual streams hand back usable hw_params")
