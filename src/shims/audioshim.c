/* audioshim.c - LD_PRELOAD: map the XDJ-RX3 audio devices onto the hardware
 * that is actually present on a Raspberry Pi 5.
 *
 *   rbp opens three stereo ALSA devices (master / headphones / booth) named
 *   hw:cs4344audiorev8,0|1|2 plus the control device hw:cs4344audiorev8.
 *   None of them exist here, and JUCE does more than fail: getDeviceProperties()
 *   cannot enumerate rates, the rate defaults to 0 and
 *   DjEngineIF::audioDeviceAboutToStart() aborts with "sampleRate:0 != 44100".
 *
 *   More importantly the transport is clocked by the ALSA callback:
 *   PlayEngine::update() only runs from DjEngineIF::audioDeviceIOCallback().
 *   No running PCM => no playhead, no waveform, even though the UI repaints.
 *
 * This shim presents the RX3 devices as virtual handles and muxes them onto one
 * real PCM:
 *
 *   DDJ-FLX4 (4 channels)   master -> ch 1/2 (MASTER out)
 *                           cue    -> ch 3/4 (HEADPHONES out)
 *                           booth  -> dropped
 *   HDMI / any stereo PCM   master (or cue, see RB_AUDIO_CUE_ON_2CH) -> ch 1/2
 *
 * Everything hardware-specific is passed in by the launcher (rb4r5/audio.py),
 * which enumerates /proc/asound and picks the device, so this file has no
 * card numbers compiled into it:
 *
 *   RB_AUDIO_DEV        ALSA device name, e.g. "plughw:CARD=FLX4,DEV=0".
 *                       Several may be given, to be tried in order, separated
 *                       by '|' - NOT by a comma, which is part of the names
 *                       themselves ("plughw:CARD=FLX4,DEV=0" is one device,
 *                       and splitting it on the comma produces four things
 *                       that are not devices at all).
 *                       (a comma-separated list is tried in order)
 *   RB_AUDIO_CH         real channel count: 4 (FLX4) or 2 (HDMI)   [default 2]
 *   RB_AUDIO_RATE       sample rate to negotiate                   [44100]
 *   RB_AUDIO_FMT        ALSA format id to request                  [6 = S24_LE]
 *   RB_AUDIO_CUE_ON_2CH 1 = a stereo sink carries the cue mix when the cue has
 *                       audio (the Chromebit behaviour); 0 = always master
 *   RB_AUDIO_CUE_MIRROR 1 = mirror master into ch 3/4 until the engine
 *                       produces a real cue mix (4-channel sinks only)
 *   STARTUP_MUTE_MS / STARTUP_FADE_MS   start-up pop suppression (see below)
 *
 * Prefer a "plughw:" device: alsa-lib then converts S24_LE -> whatever the
 * FLX4 actually accepts (S24_3LE/S32_LE) and resamples if the device will not
 * do 44100.  Use "hw:" only when the launcher has verified an exact match.
 *
 * Build: soft-float EABI5, GLIBC_2.4 only (see src/shims/Makefile).
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <stdarg.h>
#include <sys/mman.h>
#include <time.h>

/* the rate governor, shared with tools/tests/test_rate_gate.c */
#include "rate_gate.h"
#include <sys/syscall.h>

/* Enforce GLIBC_2.4 versioning for libdl on glibc 2.13.
 *
 * RB_NO_SYMVER lets the tests build this file against a modern host glibc,
 * which has no GLIBC_2.4 to bind to.  The real build never defines it. */
#ifndef RB_NO_SYMVER
__asm__(".symver dlsym, dlsym@GLIBC_2.4");
__asm__(".symver dlopen, dlopen@GLIBC_2.4");
__asm__(".symver dlerror, dlerror@GLIBC_2.4");
__asm__(".symver dlclose, dlclose@GLIBC_2.4");
#endif

#ifndef SYS_mmap2
#define SYS_mmap2 __NR_mmap2
#endif

/* rbp's user_space_rtc_init() calls mmap(MAP_SHARED, fd=-1) after the /dev/mem
 * open fails (memshim denies it).  That mmap returns MAP_FAILED and leaves a
 * dangling RTC pointer which later SIGSEGV-loops.  Redirect the broken
 * combination to anonymous memory so it succeeds with zeroes.  (memshim does
 * the same; whichever shim resolves first wins, both are equivalent.) */
void *mmap(void *addr, size_t length, int prot, int flags, int fd, off_t offset)
{
    if (fd < 0 && (flags & MAP_SHARED) && !(flags & MAP_ANONYMOUS)) {
        flags = (flags & ~MAP_SHARED) | MAP_PRIVATE | MAP_ANONYMOUS;
        fd = -1;
        offset = 0;
    }
    return (void *)syscall(SYS_mmap2, addr, length, prot, flags, fd,
                           (unsigned long)offset >> 12);
}

#define LOG_PATH "/tmp/audioshim.log"

/* The log is capped.  It gets a line per device call and one every 500
 * writes - about one a second - forever, across every restart, and a
 * crash-looping player writes it without end.  Unbounded, it filled the
 * Pi's disk, which then silently truncated whatever else was being written:
 * a source file, and a git object.  A log that costs you the filesystem is
 * worse than no log. */
#define LOG_MAX_BYTES (4 * 1024 * 1024)

static void alog(const char *fmt, ...)
{
    char buf[512];
    va_list ap;
    int fd;
    off_t size;

    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);

    fd = open(LOG_PATH, O_WRONLY | O_CREAT | O_APPEND, 0666);
    if (fd < 0)
        return;
    size = lseek(fd, 0, SEEK_END);
    if (size > LOG_MAX_BYTES) {
        /* start again rather than grow: the interesting part of this log is
         * always the most recent start, not the first one of the day */
        close(fd);
        fd = open(LOG_PATH, O_WRONLY | O_CREAT | O_TRUNC, 0666);
        if (fd < 0)
            return;
        {
            static const char note[] =
                "audioshim: --- log restarted (it had reached its size "
                "limit) ---\n";
            if (write(fd, note, sizeof(note) - 1) < 0) { /* nothing to do */ }
        }
    }
    if (write(fd, buf, strlen(buf)) < 0) { /* nothing to be done */ }
    close(fd);
}

/* Opaque ALSA types */
typedef void snd_pcm_t;
typedef void snd_pcm_hw_params_t;
typedef void snd_pcm_sw_params_t;
typedef unsigned long snd_pcm_uframes_t;
typedef long snd_pcm_sframes_t;

#define SND_PCM_STREAM_PLAYBACK 0
#define SND_PCM_STREAM_CAPTURE  1
#define SND_PCM_NONBLOCK        1
#define SND_PCM_ASYNC           2
#define SND_PCM_ACCESS_RW_INTERLEAVED 3
#define SND_PCM_FORMAT_S24_LE   6

/* Virtual handles for the streams that are not the real device */
static int g_h_master = 0;   /* only when no hardware could be opened */
static int g_h_hp     = 1;
static int g_h_booth  = 2;
static int g_h_dummy  = 3;
static int g_h_cap    = 4;

static snd_pcm_t *g_real_playback = NULL;
static int g_real_refs            = 0;

/* An open PCM kept only so hw_params_any() has something real to fill a
 * caller's params struct from.  See params_donor() for why that matters. */
static snd_pcm_t *g_donor      = NULL;
static int        g_donor_tried = 0;

/* ---- runtime configuration (env, filled by the launcher) --------------- */
static int   g_cfg_done     = 0;
static char  g_dev_list[256] = "";
static int   g_real_ch      = 2;
static int   g_rate         = 44100;
static int   g_format       = SND_PCM_FORMAT_S24_LE;
static int   g_cue_on_2ch   = 0;
static int   g_cue_mirror   = 0;
/* Floors for the REAL device's buffer.
 *
 * The RX3's own output is local I2S, so the engine asks for what suits that:
 * two periods of 64 frames, a 2.9 ms buffer.  A USB controller cannot hold
 * to that - it underruns on essentially every period, and after enough
 * underruns in a row the shim gives up on the device and there is no sound
 * at all.  The engine is not harmed by a larger hardware buffer: it still
 * writes whatever size it likes, and writei still blocks once the buffer is
 * full, which is what clocks its transport. */
static unsigned long g_min_period  = 512;
static unsigned long g_min_periods = 4;

/* What a full-scale sample from the engine looks like. */
static int32_t g_full_scale = 8388607;

/* Read one sample as the engine meant it.
 *
 * S24_LE keeps a 24-bit sample in the low three bytes of a 32-bit container
 * and leaves the top byte alone, so a NEGATIVE sample arrives looking like a
 * large positive int32: -16 is 0x00FFFFF0, which reads as 16777200.  Taking
 * the absolute value of that gives 16777200 out of a claimed full scale of
 * 8388607, which is why the meter sat pinned at the top with nothing playing
 * and why peak_m in the log was always about 16777200 instead of 0.
 *
 * Only S24_LE needs this; a 32-bit format already fills the container. */
static inline int32_t engine_sample(int32_t v)
{
    if (g_format != SND_PCM_FORMAT_S24_LE)
        return v;
    v &= 0x00FFFFFF;
    return (v & 0x00800000) ? v - 0x01000000 : v;
}

static void cfg_init(void)
{
    const char *s;

    if (g_cfg_done)
        return;
    g_cfg_done = 1;

    s = getenv("RB_AUDIO_DEV");
    if (s && *s)
        snprintf(g_dev_list, sizeof(g_dev_list), "%s", s);

    s = getenv("RB_AUDIO_CH");
    if (s && *s) g_real_ch = atoi(s);
    if (g_real_ch != 2 && g_real_ch != 4) g_real_ch = 2;

    s = getenv("RB_AUDIO_RATE");
    if (s && *s) g_rate = atoi(s);
    if (g_rate < 8000 || g_rate > 192000) g_rate = 44100;

    s = getenv("RB_AUDIO_FMT");
    if (s && *s) g_format = atoi(s);
    g_full_scale = (g_format == SND_PCM_FORMAT_S24_LE) ? 8388607 : 2147483647;

    s = getenv("RB_AUDIO_CUE_ON_2CH");
    if (s && *s && *s != '0') g_cue_on_2ch = 1;

    s = getenv("RB_AUDIO_CUE_MIRROR");
    if (s && *s && *s != '0') g_cue_mirror = 1;

    s = getenv("RB_AUDIO_PERIOD");
    if (s && *s) g_min_period = strtoul(s, NULL, 10);
    if (g_min_period < 32 || g_min_period > 8192) g_min_period = 512;

    s = getenv("RB_AUDIO_PERIODS");
    if (s && *s) g_min_periods = strtoul(s, NULL, 10);
    if (g_min_periods < 2 || g_min_periods > 32) g_min_periods = 4;

    alog("audioshim: cfg dev='%s' ch=%d rate=%d fmt=%d cue_on_2ch=%d "
         "cue_mirror=%d period>=%lu periods>=%lu\n",
         g_dev_list[0] ? g_dev_list : "(auto)", g_real_ch, g_rate, g_format,
         g_cue_on_2ch, g_cue_mirror, g_min_period, g_min_periods);
}

/* Real ALSA function pointers */
static int (*real_snd_pcm_open)(snd_pcm_t **, const char *, int, int) = NULL;
static int (*real_snd_pcm_close)(snd_pcm_t *) = NULL;
static int (*real_snd_pcm_hw_params)(snd_pcm_t *, snd_pcm_hw_params_t *) = NULL;
static int (*real_snd_pcm_hw_params_any)(snd_pcm_t *, snd_pcm_hw_params_t *) = NULL;
static int (*real_snd_pcm_hw_params_set_access)(snd_pcm_t *, snd_pcm_hw_params_t *, int) = NULL;
static int (*real_snd_pcm_hw_params_set_format)(snd_pcm_t *, snd_pcm_hw_params_t *, int) = NULL;
static int (*real_snd_pcm_hw_params_set_channels)(snd_pcm_t *, snd_pcm_hw_params_t *, unsigned int) = NULL;
static int (*real_snd_pcm_hw_params_set_rate_near)(snd_pcm_t *, snd_pcm_hw_params_t *, unsigned int *, int *) = NULL;
static int (*real_snd_pcm_hw_params_set_period_size_near)(snd_pcm_t *, snd_pcm_hw_params_t *, snd_pcm_uframes_t *, int *) = NULL;
static int (*real_snd_pcm_hw_params_set_periods_near)(snd_pcm_t *, snd_pcm_hw_params_t *, unsigned int *, int *) = NULL;
static int (*real_test_rate)(snd_pcm_t *, snd_pcm_hw_params_t *, unsigned int) = NULL;
static int (*real_snd_pcm_sw_params_current)(snd_pcm_t *, snd_pcm_sw_params_t *) = NULL;
static int (*real_snd_pcm_sw_params_get_boundary)(const snd_pcm_sw_params_t *, snd_pcm_uframes_t *) = NULL;
static int (*real_snd_pcm_sw_params_set_silence_threshold)(snd_pcm_t *, snd_pcm_sw_params_t *, snd_pcm_uframes_t) = NULL;
static int (*real_snd_pcm_sw_params_set_silence_size)(snd_pcm_t *, snd_pcm_sw_params_t *, snd_pcm_uframes_t) = NULL;
static int (*real_snd_pcm_sw_params_set_start_threshold)(snd_pcm_t *, snd_pcm_sw_params_t *, snd_pcm_uframes_t) = NULL;
static int (*real_snd_pcm_sw_params_set_stop_threshold)(snd_pcm_t *, snd_pcm_sw_params_t *, snd_pcm_uframes_t) = NULL;
static int (*real_snd_pcm_sw_params)(snd_pcm_t *, snd_pcm_sw_params_t *) = NULL;
static int (*real_snd_pcm_prepare)(snd_pcm_t *) = NULL;
static int (*real_snd_pcm_drop)(snd_pcm_t *) = NULL;
static int (*real_snd_pcm_hw_free)(snd_pcm_t *) = NULL;
/* read back what the device actually settled on, for the log */
static int (*real_get_access)(const snd_pcm_hw_params_t *, unsigned int *) = NULL;
static int (*real_get_format)(const snd_pcm_hw_params_t *, int *) = NULL;
static int (*real_get_channels)(const snd_pcm_hw_params_t *, unsigned int *) = NULL;
static int (*real_get_rate)(const snd_pcm_hw_params_t *, unsigned int *, int *) = NULL;
static int (*real_get_period_size)(const snd_pcm_hw_params_t *, snd_pcm_uframes_t *, int *) = NULL;
static int (*real_get_buffer_size)(const snd_pcm_hw_params_t *, snd_pcm_uframes_t *) = NULL;
static int (*real_hw_malloc)(snd_pcm_hw_params_t **) = NULL;
static void (*real_hw_free)(snd_pcm_hw_params_t *) = NULL;
static int (*real_sw_malloc)(snd_pcm_sw_params_t **) = NULL;
static void (*real_sw_free)(snd_pcm_sw_params_t *) = NULL;
static int (*real_sw_set_avail_min)(snd_pcm_t *, snd_pcm_sw_params_t *, snd_pcm_uframes_t) = NULL;

/* what the hardware actually settled on, for our own sw_params */
static snd_pcm_uframes_t g_hw_period = 512;
static snd_pcm_uframes_t g_hw_buffer = 2048;
static snd_pcm_sframes_t (*real_snd_pcm_writei)(snd_pcm_t *, const void *, snd_pcm_uframes_t) = NULL;
static int (*real_snd_pcm_state)(snd_pcm_t *) = NULL;

/* Control interface types */
typedef void snd_ctl_t;
typedef void snd_pcm_info_t;

static int (*real_snd_ctl_open)(snd_ctl_t **, const char *, int) = NULL;
static int (*real_snd_ctl_close)(snd_ctl_t *) = NULL;

static void init_real_alsa(void)
{
    static int initialized = 0;
    if (initialized) return;
    initialized = 1;

    cfg_init();

    void *lib = dlopen("libasound.so.2", RTLD_LAZY | RTLD_GLOBAL);
    if (!lib) {
        alog("audioshim: failed to dlopen libasound.so.2: %s\n", dlerror());
        return;
    }

    real_snd_pcm_open = dlsym(lib, "snd_pcm_open");
    real_snd_pcm_close = dlsym(lib, "snd_pcm_close");
    real_snd_pcm_hw_params = dlsym(lib, "snd_pcm_hw_params");
    real_snd_pcm_hw_params_any = dlsym(lib, "snd_pcm_hw_params_any");
    real_snd_pcm_hw_params_set_access = dlsym(lib, "snd_pcm_hw_params_set_access");
    real_snd_pcm_hw_params_set_format = dlsym(lib, "snd_pcm_hw_params_set_format");
    real_snd_pcm_hw_params_set_channels = dlsym(lib, "snd_pcm_hw_params_set_channels");
    real_snd_pcm_hw_params_set_rate_near = dlsym(lib, "snd_pcm_hw_params_set_rate_near");
    real_snd_pcm_hw_params_set_period_size_near = dlsym(lib, "snd_pcm_hw_params_set_period_size_near");
    real_snd_pcm_hw_params_set_periods_near = dlsym(lib, "snd_pcm_hw_params_set_periods_near");
    real_test_rate = dlsym(lib, "snd_pcm_hw_params_test_rate");
    real_snd_pcm_sw_params_current = dlsym(lib, "snd_pcm_sw_params_current");
    real_snd_pcm_sw_params_get_boundary = dlsym(lib, "snd_pcm_sw_params_get_boundary");
    real_snd_pcm_sw_params_set_silence_threshold = dlsym(lib, "snd_pcm_sw_params_set_silence_threshold");
    real_snd_pcm_sw_params_set_silence_size = dlsym(lib, "snd_pcm_sw_params_set_silence_size");
    real_snd_pcm_sw_params_set_start_threshold = dlsym(lib, "snd_pcm_sw_params_set_start_threshold");
    real_snd_pcm_sw_params_set_stop_threshold = dlsym(lib, "snd_pcm_sw_params_set_stop_threshold");
    real_snd_pcm_sw_params = dlsym(lib, "snd_pcm_sw_params");
    real_snd_pcm_prepare = dlsym(lib, "snd_pcm_prepare");
    real_snd_pcm_drop = dlsym(lib, "snd_pcm_drop");
    real_snd_pcm_hw_free = dlsym(lib, "snd_pcm_hw_free");
    real_get_access = dlsym(lib, "snd_pcm_hw_params_get_access");
    real_get_format = dlsym(lib, "snd_pcm_hw_params_get_format");
    real_get_channels = dlsym(lib, "snd_pcm_hw_params_get_channels");
    real_get_rate = dlsym(lib, "snd_pcm_hw_params_get_rate");
    real_get_period_size = dlsym(lib, "snd_pcm_hw_params_get_period_size");
    real_get_buffer_size = dlsym(lib, "snd_pcm_hw_params_get_buffer_size");
    real_hw_malloc = dlsym(lib, "snd_pcm_hw_params_malloc");
    real_hw_free = dlsym(lib, "snd_pcm_hw_params_free");
    real_sw_malloc = dlsym(lib, "snd_pcm_sw_params_malloc");
    real_sw_free = dlsym(lib, "snd_pcm_sw_params_free");
    real_sw_set_avail_min = dlsym(lib, "snd_pcm_sw_params_set_avail_min");
    real_snd_pcm_writei = dlsym(lib, "snd_pcm_writei");
    real_snd_pcm_state = dlsym(lib, "snd_pcm_state");
    real_snd_ctl_open = dlsym(lib, "snd_ctl_open");
    real_snd_ctl_close = dlsym(lib, "snd_ctl_close");

    alog("audioshim: real ALSA initialized\n");
}

/* Mix buffers.  The engine hands us S24_LE samples in int32 containers, which
 * is what we write out too (alsa-lib's plug layer converts if the device wants
 * S24_3LE or S32_LE). */
#define MAX_FRAMES 4096
static int32_t g_mix4ch[MAX_FRAMES * 4];    /* RX3 master L/R + cue L/R   */
static int32_t g_out[MAX_FRAMES * 4];       /* what goes to the hardware  */
static unsigned long g_write_count = 0;
/* Set once the device has refused audio for good: from then on the engine is
 * paced in software rather than left to spin. */
static int g_pcm_dead = 0;
static int g_pcm_fails = 0;

/* The chroot bind-mounts the host /tmp, so the launcher outside can read it. */
#ifndef RB_LEVELS_PATH
#define RB_LEVELS_PATH "/tmp/rb-levels.dat"
#endif

/* ---- Startup mute / fade-in --------------------------------------------
 * rb's first audio buffers contain a full-scale transient (0x800000, the
 * most-negative 24-bit value) for ~200 ms, which is a loud pop through the
 * FLX4's master out.  Hold the output at zero for STARTUP_MUTE_MS after the
 * first write, then fade in over STARTUP_FADE_MS so the unmute cannot click.
 *   STARTUP_MUTE_MS=0 -> disabled; defaults 1500 / 300 ms. */
static long g_startup_mute_frames = -1;
static long g_startup_fade_frames = -1;
static unsigned long long g_startup_frames_done = 0;
static int g_startup_logged_mute = 0;
static int g_startup_logged_open = 0;

static void startup_env_init(void)
{
    if (g_startup_mute_frames >= 0) return;
    long mute_ms = 1500, fade_ms = 300;
    const char *s = getenv("STARTUP_MUTE_MS");
    if (s && *s) mute_ms = atol(s);
    s = getenv("STARTUP_FADE_MS");
    if (s && *s) fade_ms = atol(s);
    if (mute_ms < 0) mute_ms = 0;
    if (fade_ms < 0) fade_ms = 0;
    g_startup_mute_frames = mute_ms * (long)g_rate / 1000L;
    g_startup_fade_frames = fade_ms * (long)g_rate / 1000L;
    alog("audioshim: startup mute=%ldms fade=%ldms (%ld+%ld frames @%d Hz)\n",
         mute_ms, fade_ms, g_startup_mute_frames, g_startup_fade_frames, g_rate);
}

static float startup_gain(unsigned long long t, unsigned long long mute,
                          unsigned long long fade)
{
    if (t < mute) return 0.0f;
    if (fade > 0 && t < mute + fade) return (float)(t - mute) / (float)fade;
    return 1.0f;
}

static inline int is_real(snd_pcm_t *pcm)
{
    return (pcm && pcm == g_real_playback);
}

/* Whose handle is this?
 *
 * THE thing to understand about this shim: it exports public ALSA symbols,
 * and alsa-lib CALLS THOSE SYMBOLS ITSELF.  A plug PCM - which is what
 * plughw: is - implements snd_pcm_prepare() by calling snd_pcm_prepare() on
 * its slave, snd_pcm_hw_params() by calling snd_pcm_hw_params() on its slave,
 * and so on down the chain.  With LD_PRELOAD in play those internal calls
 * land HERE, with a handle this shim has never seen.
 *
 * Swallowing them - "not one of mine, return 0" - leaves the slave
 * unconfigured and unprepared while every single call reports success.  That
 * is precisely what
 *
 *     setup hw_params res=0
 *     our own sw_params (...) res=0
 *     prepare after setup res=0, state now 1
 *     writei #1 written=-77   (EBADFD)
 *
 * was: the plug layer accepted everything and passed nothing on, because we
 * were intercepting it on the way.  It also explains the boundary of 0 - the
 * plug's own setup never completed - and every earlier repair that looked
 * correct and changed nothing.
 *
 * So: this shim owns its virtual streams, and the master handle the engine
 * was given.  EVERYTHING else is alsa-lib's own business and is passed
 * straight through, with its own handle, untouched.
 */
#define H_ALSA    0     /* alsa-lib's internal slave - pass through */
#define H_MASTER  1     /* the master, as the engine holds it */
#define H_VIRTUAL 2     /* one of our fake streams */
#define H_NONE    3     /* no handle at all - do nothing, touch nothing */

static int whose(snd_pcm_t *pcm)
{
    /* The engine really does call snd_pcm_close(NULL), among others.  That
     * used to land in "not one of mine, return 0" and was harmless; now that
     * anything unrecognised is passed through to alsa-lib, a null handle
     * would be dereferenced inside it.  It is nobody's, so nothing happens
     * to it. */
    if (!pcm)
        return H_NONE;
    if (pcm == (snd_pcm_t *)&g_h_master || pcm == (snd_pcm_t *)&g_h_hp ||
        pcm == (snd_pcm_t *)&g_h_booth  || pcm == (snd_pcm_t *)&g_h_dummy ||
        pcm == (snd_pcm_t *)&g_h_cap)
        return H_VIRTUAL;
    if (pcm == g_real_playback)
        return H_MASTER;
    return H_ALSA;
}

/* A real PCM to fill hw_params from, for the streams that have no hardware.
 *
 * snd_pcm_hw_params_t is opaque and version-specific, so a shim cannot build
 * a valid one by hand.  But it MUST be valid: the caller allocates it zeroed,
 * every mask inside it is therefore empty, and alsa-lib asserts rather than
 * returns when asked to read an empty mask
 *
 *     rbp: mask_inline.h:282: snd_mask_value: Assertion !snd_mask_empty(mask)
 *
 * which aborts the player.  So the virtual streams borrow a real device's
 * capabilities: the real output if it is open, otherwise alsa-lib's own
 * "null" PCM, which needs no hardware and is defined by alsa.conf itself.
 * Nothing is ever written through the donor - it exists only to make the
 * params struct readable.
 */
static snd_pcm_t *params_donor(void)
{
    if (g_real_playback)
        return g_real_playback;
    if (g_donor || g_donor_tried)
        return g_donor;
    g_donor_tried = 1;
    if (real_snd_pcm_open) {
        int err = real_snd_pcm_open(&g_donor, "null", SND_PCM_STREAM_PLAYBACK,
                                    0);
        alog("audioshim: params donor 'null' res=%d handle=%p\n", err, g_donor);
        if (err < 0)
            g_donor = NULL;
    }
    return g_donor;
}

/* Fallback device discovery, used only when the launcher did not set
 * RB_AUDIO_DEV (e.g. rbp started by hand).  Prefer the DJ controller, then any
 * HDMI sink, then ALSA's default. */
static int pick_device(char *out, size_t n)
{
    FILE *f = fopen("/proc/asound/cards", "r");
    char line[256];
    int card = -1, flx = -1, ddj = -1, hdmi = -1;

    if (!f)
        return 0;
    while (fgets(line, sizeof(line), f)) {
        int idx;
        if (sscanf(line, " %d [", &idx) == 1) {
            card = idx;
            continue;
        }
        if (card < 0)
            continue;
        if (strstr(line, "FLX4") && flx < 0)
            flx = card;
        if (strstr(line, "DDJ") && ddj < 0)
            ddj = card;
        if ((strstr(line, "HDMI") || strstr(line, "vc4")) && hdmi < 0)
            hdmi = card;
    }
    fclose(f);

    if (flx < 0) flx = ddj;
    if (flx >= 0) {
        snprintf(out, n, "plughw:%d,0", flx);
        g_real_ch = 4;
        return 1;
    }
    if (hdmi >= 0) {
        snprintf(out, n, "plughw:%d,0", hdmi);
        g_real_ch = 2;
        return 1;
    }
    return 0;
}

static int configure_real_device(void);

/* Open the first device of RB_AUDIO_DEV (comma separated) that works. */
static void open_real_playback(int mode)
{
    char list[256];
    char autodev[64];
    char *p, *save = NULL;
    int err = -1;

    cfg_init();
    if (!real_snd_pcm_open)
        return;

    /* "none" means: do not open anything.  The engine still runs, paced in
     * software, which is the quickest way to find out whether a crash is the
     * audio path or something else entirely. */
    if (!strcmp(g_dev_list, "none")) {
        alog("audioshim: RB_AUDIO_DEV=none - not opening any device; the "
             "engine will be paced in software and there will be no sound\n");
        return;
    }

    if (!g_dev_list[0]) {
        if (pick_device(autodev, sizeof(autodev)))
            snprintf(g_dev_list, sizeof(g_dev_list), "%s|default", autodev);
        else
            snprintf(g_dev_list, sizeof(g_dev_list), "%s", "default");
        alog("audioshim: no RB_AUDIO_DEV, auto-picked '%s'\n", g_dev_list);
    }

    /* Candidates are separated by '|'.  A comma cannot be the separator: it
     * is part of every card-name device ("plughw:CARD=FLX4,DEV=0"), and
     * splitting on it turns one real device into four names that are not
     * devices - which is exactly why nothing ever opened. */
    snprintf(list, sizeof(list), "%s", g_dev_list);
    for (p = strtok_r(list, "|", &save); p; p = strtok_r(NULL, "|", &save)) {
        while (*p == ' ') p++;
        if (!*p)
            continue;
        /* -EBUSY is worth waiting out.  A card the engine itself has just
         * closed can take a moment to come free, and an earlier player that
         * is still shutting down holds it for longer than that. */
        for (int try = 0; try < 20; try++) {
            err = real_snd_pcm_open(&g_real_playback, p,
                                    SND_PCM_STREAM_PLAYBACK, mode);
            if (err != -EBUSY)
                break;
            g_real_playback = NULL;
            if (try == 0)
                alog("audioshim: '%s' is busy; waiting for it\n", p);
            usleep(100000);
        }
        alog("audioshim: opened real '%s' for Master (mode=%d), res=%d handle=%p\n",
             p, mode, err, g_real_playback);
        if (err == 0 && g_real_playback) {
            /* Configure it now, from our own parameters, while nothing else
             * has touched it. */
            if (configure_real_device() < 0)
                alog("audioshim: the device opened but could not be set up; "
                     "the write path will try again\n");
            return;
        }
        g_real_playback = NULL;
    }
    alog("audioshim: NO usable playback device (last res=%d) - the transport "
         "will not advance\n", err);
}

/* Which of the RX3's outputs a device name is.
 *
 * The RX3 has one card with three output subdevices:
 *
 *     hw:cs4344audiorev8,0   Master
 *     hw:cs4344audiorev8,1   Headphones / cue
 *     hw:cs4344audiorev8,2   Booth
 *
 * and hw:esaics4344audio,0, which is the mic/return path, not an output.
 *
 * This used to be decided by counting opens: the first playback open was the
 * master, the second the headphones, and so on.  But the engine opens and
 * closes these repeatedly while it enumerates devices, so by the time it
 * opened the master for real the count had already drifted past it - the
 * master was handed a dummy handle, its audio was dropped on the floor, and
 * there was no sound and no meter movement, with nothing in the log that
 * looked wrong.  The name is stable; the count is not.
 */
static int rx3_output(const char *name)
{
    const char *comma;

    if (!name)
        return -1;
    if (strstr(name, "esaic"))          /* mic / return, never an output */
        return -1;
    comma = strrchr(name, ',');
    if (!comma)                          /* "default", "hw:card" */
        return 0;
    switch (atoi(comma + 1)) {
    case 0:  return 0;
    case 1:  return 1;
    case 2:  return 2;
    default: return -1;
    }
}

int snd_pcm_open(snd_pcm_t **pcm, const char *name, int stream, int mode)
{
    int which;

    init_real_alsa();
    alog("audioshim: snd_pcm_open(name='%s', stream=%d, mode=%d)\n",
         name ? name : "null", stream, mode);

    if (stream != SND_PCM_STREAM_PLAYBACK) {
        /* Capture / mic -> virtual handle, so nothing fights for the hardware */
        alog("audioshim: mapped virtual Capture device\n");
        *pcm = (snd_pcm_t *)&g_h_cap;
        return 0;
    }

    which = rx3_output(name);
    if (which == 1) {
        alog("audioshim: mapped virtual Headphone/cue device\n");
        *pcm = (snd_pcm_t *)&g_h_hp;
        return 0;
    }
    if (which == 2) {
        alog("audioshim: mapped virtual Booth device\n");
        *pcm = (snd_pcm_t *)&g_h_booth;
        return 0;
    }
    if (which != 0) {
        alog("audioshim: mapped dummy output device\n");
        *pcm = (snd_pcm_t *)&g_h_dummy;
        return 0;
    }

    /* The master.  Clear NONBLOCK: the hardware is the transport clock, and
     * a non-blocking handle returns EAGAIN instead of pacing us.  (This used
     * to clear bit 2, which is ASYNC - so NONBLOCK stayed set, and an open of
     * a momentarily busy card failed outright instead of waiting.) */
    if (!g_real_playback)
        open_real_playback(mode & ~(SND_PCM_NONBLOCK | SND_PCM_ASYNC));

    if (g_real_playback) {
        g_real_refs++;
        alog("audioshim: mapped Master -> the real device (refs=%d)\n",
             g_real_refs);
        *pcm = g_real_playback;
        return 0;
    }

    alog("audioshim: no hardware for Master - running it virtually, paced in "
         "software, with no sound\n");
    *pcm = (snd_pcm_t *)&g_h_master;
    return 0;
}

/* Configure the real device ourselves, from a clean parameter set.
 *
 * The engine's snd_pcm_hw_params_t describes the RX3's own output, and it is
 * built by calls this shim intercepts - so it arrives carrying whatever the
 * engine refined it against.  Applying it to a USB controller has now failed
 * three times over, each for a different reason, and the last one could not
 * even be named: hw_params returned 0, sw_params returned 0, prepare()
 * returned 0, and the device sat in SETUP refusing every write with EBADFD.
 *
 * So the real device stops being configured from the engine's struct.  This
 * allocates its own, fills it from the device's actual capabilities, sets
 * only what this shim needs, and applies it once.  The engine's own
 * hw_params calls are accepted and ignored, exactly as its sw_params already
 * are - they describe a device it is not writing to.
 */
static void apply_sw_params(void);

static int configure_real_device(void)
{
    snd_pcm_hw_params_t *hw = NULL;
    unsigned int rate = (unsigned)g_rate;
    unsigned int periods = (unsigned)g_min_periods;
    snd_pcm_uframes_t period = (snd_pcm_uframes_t)g_min_period;
    int err, state;

    if (!g_real_playback || !real_hw_malloc || !real_hw_free ||
        !real_snd_pcm_hw_params_any || !real_snd_pcm_hw_params) {
        alog("audioshim: cannot configure the device - alsa-lib did not give "
             "up all the symbols needed for it\n");
        return -1;
    }
    if (real_hw_malloc(&hw) < 0 || !hw)
        return -1;

    err = real_snd_pcm_hw_params_any(g_real_playback, hw);
    if (err < 0) {
        alog("audioshim: hw_params_any on the real device res=%d\n", err);
        real_hw_free(hw);
        return err;
    }

    if (real_snd_pcm_hw_params_set_access) {
        err = real_snd_pcm_hw_params_set_access(g_real_playback, hw,
                                                SND_PCM_ACCESS_RW_INTERLEAVED);
        alog("audioshim: setup access=RW_INTERLEAVED res=%d\n", err);
    }
    if (real_snd_pcm_hw_params_set_format) {
        err = real_snd_pcm_hw_params_set_format(g_real_playback, hw, g_format);
        alog("audioshim: setup format=%d res=%d\n", g_format, err);
    }
    if (real_snd_pcm_hw_params_set_channels) {
        err = real_snd_pcm_hw_params_set_channels(g_real_playback, hw,
                                                  (unsigned)g_real_ch);
        alog("audioshim: setup channels=%d res=%d\n", g_real_ch, err);
        if (err < 0 && g_real_ch != 2) {
            err = real_snd_pcm_hw_params_set_channels(g_real_playback, hw, 2);
            alog("audioshim: %d channels refused, stereo res=%d\n",
                 g_real_ch, err);
            if (err >= 0)
                g_real_ch = 2;
        }
    }
    if (real_snd_pcm_hw_params_set_rate_near) {
        err = real_snd_pcm_hw_params_set_rate_near(g_real_playback, hw,
                                                   &rate, NULL);
        alog("audioshim: setup rate=%u res=%d\n", rate, err);
    }
    if (real_snd_pcm_hw_params_set_period_size_near) {
        err = real_snd_pcm_hw_params_set_period_size_near(g_real_playback, hw,
                                                          &period, NULL);
        alog("audioshim: setup period=%lu res=%d\n",
             (unsigned long)period, err);
    }
    if (real_snd_pcm_hw_params_set_periods_near) {
        err = real_snd_pcm_hw_params_set_periods_near(g_real_playback, hw,
                                                      &periods, NULL);
        alog("audioshim: setup periods=%u res=%d\n", periods, err);
    }

    err = real_snd_pcm_hw_params(g_real_playback, hw);
    alog("audioshim: setup hw_params res=%d\n", err);
    if (err >= 0) {
        unsigned int gaccess = 0, gch = 0, grate = 0;
        int gfmt = 0;
        snd_pcm_uframes_t gperiod = 0, gbuffer = 0;
        if (real_get_access) real_get_access(hw, &gaccess);
        if (real_get_format) real_get_format(hw, &gfmt);
        if (real_get_channels) real_get_channels(hw, &gch);
        if (real_get_rate) real_get_rate(hw, &grate, NULL);
        if (real_get_period_size) real_get_period_size(hw, &gperiod, NULL);
        if (real_get_buffer_size) real_get_buffer_size(hw, &gbuffer);
        if (gperiod) g_hw_period = gperiod;
        if (gbuffer) g_hw_buffer = gbuffer;
        if (gch) g_real_ch = (int)gch;
        alog("audioshim: the device is now access=%u format=%d channels=%u "
             "rate=%u period=%lu buffer=%lu (%.1f ms)\n",
             gaccess, gfmt, gch, grate, (unsigned long)g_hw_period,
             (unsigned long)g_hw_buffer,
             grate ? (double)g_hw_buffer * 1000.0 / (double)grate : 0.0);
    }
    real_hw_free(hw);
    if (err < 0)
        return err;

    apply_sw_params();

    state = real_snd_pcm_state ? real_snd_pcm_state(g_real_playback) : -1;
    if (state == 2 || state == 3) {
        g_pcm_dead = 0;
        g_pcm_fails = 0;
        alog("audioshim: the device is ready to take audio (state %d)\n",
             state);
        return 0;
    }
    alog("audioshim: the device configured but will not leave state %d "
         "(2 = PREPARED); writes are going to fail\n", state);
    return -1;
}

/* Give the real device software parameters that match the buffer it really
 * has - because the engine's do not.
 *
 * Every hardware parameter is overridden for this device (access, format,
 * channels, period, periods), so the buffer it ends up with is nothing like
 * the 128 frames the engine believes in.  Its SOFTWARE parameters were being
 * forwarded verbatim all the same, and a stop_threshold of 128 frames on a
 * 2048-frame buffer stops the stream the moment it is prepared: avail is the
 * whole empty buffer, which is already past the threshold.  So prepare()
 * reported success, the state went straight back to SETUP, and the first
 * write returned EBADFD:
 *
 *   the device was in state 1 (not ready to be written); prepare() res=0
 *   the output device stopped accepting audio (File descriptor in bad state,
 *   errno 77, after 1 writes, state 1)
 *
 * stop_threshold is the boundary here, so an underrun never stops the
 * stream - the write loop handles EPIPE itself and can carry on.
 */
static void apply_sw_params(void)
{
    snd_pcm_sw_params_t *sw = NULL;
    snd_pcm_uframes_t boundary = 0x40000000;
    int err;

    if (!real_sw_malloc || !real_sw_free || !real_snd_pcm_sw_params_current ||
        !real_snd_pcm_sw_params || !g_real_playback)
        return;
    if (real_sw_malloc(&sw) < 0 || !sw)
        return;
    if (real_snd_pcm_sw_params_current(g_real_playback, sw) < 0) {
        real_sw_free(sw);
        return;
    }
    if (real_snd_pcm_sw_params_get_boundary)
        real_snd_pcm_sw_params_get_boundary(sw, &boundary);
    if (boundary == 0) {
        /* alsa-lib computes the boundary as the buffer size doubled until it
         * nearly fills a long.  Ask for it and it can still come back zero -
         * and a stop_threshold of ZERO stops the stream harder than the
         * engine's own value did, which is how "stop=boundary 0" left the
         * device sitting in SETUP.  Never pass a threshold of nothing. */
        boundary = g_hw_buffer ? g_hw_buffer : 2048;
        while (boundary * 2 <= (snd_pcm_uframes_t)(0x7fffffffL - (long)g_hw_buffer))
            boundary *= 2;
        alog("audioshim: the device reported a boundary of 0; using %lu "
             "(the buffer, doubled until it nearly fills a long)\n",
             (unsigned long)boundary);
    }
    if (real_snd_pcm_sw_params_set_stop_threshold)
        real_snd_pcm_sw_params_set_stop_threshold(g_real_playback, sw, boundary);
    if (real_snd_pcm_sw_params_set_start_threshold)
        real_snd_pcm_sw_params_set_start_threshold(g_real_playback, sw,
                                                   g_hw_period);
    if (real_snd_pcm_sw_params_set_silence_threshold)
        real_snd_pcm_sw_params_set_silence_threshold(g_real_playback, sw, 0);
    if (real_snd_pcm_sw_params_set_silence_size)
        real_snd_pcm_sw_params_set_silence_size(g_real_playback, sw, 0);
    if (real_sw_set_avail_min)
        real_sw_set_avail_min(g_real_playback, sw, g_hw_period);

    err = real_snd_pcm_sw_params(g_real_playback, sw);
    alog("audioshim: our own sw_params (start=%lu, stop=boundary %lu, "
         "avail_min=%lu) res=%d\n", (unsigned long)g_hw_period,
         (unsigned long)boundary, (unsigned long)g_hw_period, err);
    real_sw_free(sw);

    if (real_snd_pcm_prepare) {
        err = real_snd_pcm_prepare(g_real_playback);
        alog("audioshim: prepare after setup res=%d, state now %d "
             "(2 = PREPARED, which is what writei needs)\n", err,
             real_snd_pcm_state ? real_snd_pcm_state(g_real_playback) : -1);
    }
}

int snd_pcm_close(snd_pcm_t *pcm)
{
    init_real_alsa();
    alog("audioshim: snd_pcm_close(handle=%p)\n", pcm);
    if (whose(pcm) == H_ALSA)
        return real_snd_pcm_close ? real_snd_pcm_close(pcm) : 0;
    if (whose(pcm) != H_MASTER)
        return 0;

    /* The master.  Do NOT close the card: the engine opens and closes it
     * several times while it starts up, and a card that has just been closed
     * can still be busy when the next open arrives.  It stays configured and
     * prepared - configure_real_device() did that once, at open - so there is
     * nothing to tear down here either. */
    if (--g_real_refs > 0)
        return 0;
    g_real_refs = 0;
    alog("audioshim: the engine closed the master; the card stays open and "
         "configured\n");
    return 0;
}

int snd_pcm_hw_params_any(snd_pcm_t *pcm, snd_pcm_hw_params_t *params)
{
    snd_pcm_t *donor;

    init_real_alsa();
    if (whose(pcm) == H_NONE || !params)
        return 0;
    if (whose(pcm) != H_VIRTUAL)
        return real_snd_pcm_hw_params_any ?
               real_snd_pcm_hw_params_any(pcm, params) : 0;

    /* A virtual stream still has to leave `params` filled in.  Returning 0
     * and touching nothing leaves the caller's zeroed struct behind, and the
     * next thing that reads it aborts the player inside alsa-lib. */
    donor = params_donor();
    if (donor && real_snd_pcm_hw_params_any)
        return real_snd_pcm_hw_params_any(donor, params);
    alog("audioshim: hw_params_any on a virtual stream with no donor - "
         "returning EINVAL rather than an unreadable params struct\n");
    return -EINVAL;
}

/* ---- the engine's hardware parameters -----------------------------------
 *
 * On the MASTER these are accepted and not applied: the engine is describing
 * the RX3's own output, and configure_real_device() has already set up the
 * one that exists.  They still have to SUCCEED, because the engine checks and
 * will not open its audio otherwise.
 *
 * On anything else they go straight through - see whose().
 */
int snd_pcm_hw_params_set_access(snd_pcm_t *pcm, snd_pcm_hw_params_t *params, int access)
{
    init_real_alsa();
    if (whose(pcm) == H_ALSA)
        return real_snd_pcm_hw_params_set_access ?
               real_snd_pcm_hw_params_set_access(pcm, params, access) : 0;
    return 0;
}

int snd_pcm_hw_params_set_format(snd_pcm_t *pcm, snd_pcm_hw_params_t *params, int format)
{
    init_real_alsa();
    if (whose(pcm) == H_ALSA)
        return real_snd_pcm_hw_params_set_format ?
               real_snd_pcm_hw_params_set_format(pcm, params, format) : 0;
    return 0;
}

int snd_pcm_hw_params_set_channels(snd_pcm_t *pcm, snd_pcm_hw_params_t *params, unsigned int val)
{
    init_real_alsa();
    if (whose(pcm) == H_ALSA)
        return real_snd_pcm_hw_params_set_channels ?
               real_snd_pcm_hw_params_set_channels(pcm, params, val) : 0;
    return 0;
}

int snd_pcm_hw_params_set_rate_near(snd_pcm_t *pcm, snd_pcm_hw_params_t *params, unsigned int *val, int *dir)
{
    init_real_alsa();
    if (whose(pcm) == H_ALSA)
        return real_snd_pcm_hw_params_set_rate_near ?
               real_snd_pcm_hw_params_set_rate_near(pcm, params, val, dir) : 0;
    /* the engine reads this back and clocks itself from it */
    if (val) *val = (unsigned)g_rate;
    return 0;
}

int snd_pcm_hw_params_set_period_size_near(snd_pcm_t *pcm, snd_pcm_hw_params_t *params, snd_pcm_uframes_t *val, int *dir)
{
    init_real_alsa();
    if (whose(pcm) == H_ALSA)
        return real_snd_pcm_hw_params_set_period_size_near ?
               real_snd_pcm_hw_params_set_period_size_near(pcm, params, val, dir) : 0;
    return 0;
}

int snd_pcm_hw_params_set_periods_near(snd_pcm_t *pcm, snd_pcm_hw_params_t *params, unsigned int *val, int *dir)
{
    init_real_alsa();
    if (whose(pcm) == H_ALSA)
        return real_snd_pcm_hw_params_set_periods_near ?
               real_snd_pcm_hw_params_set_periods_near(pcm, params, val, dir) : 0;
    return 0;
}

/* The engine asks what the (RX3) device can do before it opens it; answer for
 * the stereo stream it believes in, not for the real sink.  These take no
 * pcm, so they cannot be told apart - but alsa-lib does not call them
 * internally on a slave, it reads the masks directly. */
int snd_pcm_hw_params_get_channels_min(const snd_pcm_hw_params_t *params, unsigned int *val)
{
    if (val) *val = 2;
    return 0;
}

int snd_pcm_hw_params_get_channels_max(const snd_pcm_hw_params_t *params, unsigned int *val)
{
    if (val) *val = 2;
    return 0;
}

int snd_pcm_hw_params_test_rate(snd_pcm_t *pcm, snd_pcm_hw_params_t *params, unsigned int rate)
{
    cfg_init();
    init_real_alsa();
    if (whose(pcm) == H_ALSA)
        return real_test_rate ? real_test_rate(pcm, params, rate) : 0;
    return ((int)rate == g_rate) ? 0 : -EINVAL;
}

int snd_pcm_hw_params(snd_pcm_t *pcm, snd_pcm_hw_params_t *params)
{
    init_real_alsa();
    if (whose(pcm) == H_ALSA)
        return real_snd_pcm_hw_params ? real_snd_pcm_hw_params(pcm, params) : 0;
    if (whose(pcm) == H_MASTER) {
        /* Already configured at open.  If something left it unusable, this
         * is a good moment to try again. */
        int state = real_snd_pcm_state ? real_snd_pcm_state(g_real_playback) : -1;
        if (state != 2 && state != 3) {
            alog("audioshim: the engine is configuring its output and ours is "
                 "in state %d; setting the device up again\n", state);
            configure_real_device();
        }
    }
    return 0;
}

int snd_pcm_sw_params_current(snd_pcm_t *pcm, snd_pcm_sw_params_t *params)
{
    init_real_alsa();
    if (whose(pcm) == H_ALSA)
        return real_snd_pcm_sw_params_current ?
               real_snd_pcm_sw_params_current(pcm, params) : 0;
    return 0;
}

int snd_pcm_sw_params_get_boundary(const snd_pcm_sw_params_t *params, snd_pcm_uframes_t *val)
{
    init_real_alsa();
    int err = 0;
    if (real_snd_pcm_sw_params_get_boundary)
        err = real_snd_pcm_sw_params_get_boundary(params, val);
    if (err != 0 || !val || *val == 0) {
        if (val) *val = 0x40000000;
        err = 0;
    }
    return err;
}

/* Software parameters: through for alsa-lib, ignored for ours.  The engine
 * sizes these for the 128-frame buffer it believes in; apply_sw_params() sets
 * them from the buffer the hardware actually has. */
int snd_pcm_sw_params_set_silence_threshold(snd_pcm_t *pcm, snd_pcm_sw_params_t *params, snd_pcm_uframes_t val)
{
    init_real_alsa();
    if (whose(pcm) == H_ALSA && real_snd_pcm_sw_params_set_silence_threshold)
        return real_snd_pcm_sw_params_set_silence_threshold(pcm, params, val);
    return 0;
}

int snd_pcm_sw_params_set_silence_size(snd_pcm_t *pcm, snd_pcm_sw_params_t *params, snd_pcm_uframes_t val)
{
    init_real_alsa();
    if (whose(pcm) == H_ALSA && real_snd_pcm_sw_params_set_silence_size)
        return real_snd_pcm_sw_params_set_silence_size(pcm, params, val);
    return 0;
}

int snd_pcm_sw_params_set_start_threshold(snd_pcm_t *pcm, snd_pcm_sw_params_t *params, snd_pcm_uframes_t val)
{
    init_real_alsa();
    if (whose(pcm) == H_ALSA && real_snd_pcm_sw_params_set_start_threshold)
        return real_snd_pcm_sw_params_set_start_threshold(pcm, params, val);
    return 0;
}

int snd_pcm_sw_params_set_stop_threshold(snd_pcm_t *pcm, snd_pcm_sw_params_t *params, snd_pcm_uframes_t val)
{
    init_real_alsa();
    if (whose(pcm) == H_ALSA && real_snd_pcm_sw_params_set_stop_threshold)
        return real_snd_pcm_sw_params_set_stop_threshold(pcm, params, val);
    return 0;
}

int snd_pcm_sw_params(snd_pcm_t *pcm, snd_pcm_sw_params_t *params)
{
    init_real_alsa();
    if (whose(pcm) == H_ALSA)
        return real_snd_pcm_sw_params ? real_snd_pcm_sw_params(pcm, params) : 0;
    return 0;
}

int snd_pcm_prepare(snd_pcm_t *pcm)
{
    init_real_alsa();
    /* alsa-lib prepares a plug PCM by calling THIS on its slave.  Passing it
     * through is the whole reason the device can reach PREPARED at all. */
    if (whose(pcm) == H_ALSA)
        return real_snd_pcm_prepare ? real_snd_pcm_prepare(pcm) : 0;
    if (whose(pcm) == H_MASTER && real_snd_pcm_prepare)
        return real_snd_pcm_prepare(g_real_playback);
    return 0;
}

static void pace_report(struct pace *p, snd_pcm_uframes_t frames)
{
    long long now = rb4r5_now_us();
    unsigned int rate = g_rate ? g_rate : 44100;

    (void)frames;
    if (p->reported_us == 0) p->reported_us = now;
    if (now - p->reported_us < 5000000)
        return;
    p->reported_us = now;
    if (p->started && now > p->t0_us) {
        double elapsed = (double)(now - p->t0_us) / 1000000.0;
        double audio = (double)p->frames / (double)rate;
        alog("audioshim: %s has written %.1fs of audio in %.1fs of real time "
             "(%.2fx)%s\n", p->name, audio, elapsed,
             elapsed > 0.01 ? audio / elapsed : 0.0,
             p->slept_us ? " [governed]" : "");
    }
}

static void publish_levels(int32_t left, int32_t right, int32_t phones)
{
    static long long s_last_ms = 0;
    static unsigned long s_seq = 0;
    long long now;
    int fd;
    int32_t record[5];

    /* rb4r5_now_us() goes through the raw syscall: see rate_gate.h */
    now = rb4r5_now_us() / 1000LL;
    if (now - s_last_ms < 50)
        return;
    s_last_ms = now;

    record[0] = (int32_t)(++s_seq);
    record[1] = left;
    record[2] = right;
    record[3] = phones;
    record[4] = g_full_scale;            /* full scale, so the reader scales */

    fd = open(RB_LEVELS_PATH, O_WRONLY | O_CREAT | O_TRUNC | O_NONBLOCK, 0666);
    if (fd < 0)
        return;
    if (write(fd, record, sizeof(record)) < 0) { /* nothing to be done */ }
    close(fd);
}

snd_pcm_sframes_t snd_pcm_writei(snd_pcm_t *pcm, const void *buffer, snd_pcm_uframes_t size)
{
    init_real_alsa();
    /* alsa-lib writes to a plug PCM's slave through this same symbol.  Those
     * are its own frames, already converted, and must go straight on. */
    if (whose(pcm) == H_ALSA)
        return real_snd_pcm_writei ?
               real_snd_pcm_writei(pcm, buffer, size) : (snd_pcm_sframes_t)size;
    if (!buffer || size == 0) return size;
    if (whose(pcm) == H_NONE) return size;

    if (size > MAX_FRAMES)
        size = MAX_FRAMES;

    const int32_t *src = (const int32_t *)buffer;
    static int32_t s_peak_master = 0;
    static int32_t s_peak_hp = 0;
    static int s_has_hp_audio = 0;
    /* Separately per side, because a master meter that cannot show one
     * channel clipping on its own is not a master meter. */
    static int32_t s_peak_l = 0, s_peak_r = 0;

    if (pcm == (snd_pcm_t *)&g_h_hp) {
        /* Headphone/cue stream: park it in channels 2/3 of the mix and return.
         * The master stream is what actually drives the write to the device -
         * but this one still has to keep to real time, or the engine clocks
         * itself off it and the whole deck runs fast. */
        static struct pace pace_hp = { 0, 0, 0, "the headphone stream", 0, 0 };
        for (snd_pcm_uframes_t i = 0; i < size; i++) {
            int32_t l = engine_sample(src[i * 2 + 0]);
            int32_t r = engine_sample(src[i * 2 + 1]);
            int32_t al = (l < 0) ? -l : l;
            int32_t ar = (r < 0) ? -r : r;
            if (al > s_peak_hp) s_peak_hp = al;
            if (ar > s_peak_hp) s_peak_hp = ar;
            g_mix4ch[i * 4 + 2] = l;
            g_mix4ch[i * 4 + 3] = r;
        }
        if (s_peak_hp > 100) s_has_hp_audio = 1;
        pace_stream(&pace_hp, size, g_rate);
        pace_report(&pace_hp, size);
        return size;
    }

    if (pcm == (snd_pcm_t *)&g_h_booth || pcm == (snd_pcm_t *)&g_h_dummy) {
        static struct pace pace_booth = { 0, 0, 0, "the booth stream", 0, 0 };
        pace_stream(&pace_booth, size, g_rate);  /* the FLX4 has no booth output, but
                                          * the engine still counts on it
                                          * taking real time */
        return size;
    }

    /* Master stream */
    for (snd_pcm_uframes_t i = 0; i < size; i++) {
        int32_t l = engine_sample(src[i * 2 + 0]);
        int32_t r = engine_sample(src[i * 2 + 1]);
        int32_t al = (l < 0) ? -l : l;
        int32_t ar = (r < 0) ? -r : r;
        if (al > s_peak_master) s_peak_master = al;
        if (ar > s_peak_master) s_peak_master = ar;
        if (al > s_peak_l) s_peak_l = al;
        if (ar > s_peak_r) s_peak_r = ar;
        g_mix4ch[i * 4 + 0] = l;
        g_mix4ch[i * 4 + 1] = r;
        /* Mirror master into the cue pair only while the engine has not
         * produced a cue mix yet, and only if asked: on a real controller the
         * CUE buttons are what should decide what the headphones hear. */
        if (!s_has_hp_audio && (g_cue_mirror || g_real_ch == 2)) {
            g_mix4ch[i * 4 + 2] = l;
            g_mix4ch[i * 4 + 3] = r;
        }
    }

    g_write_count++;

    /* Build the hardware buffer */
    if (g_real_ch == 4) {
        /* DDJ-FLX4: 1/2 = MASTER out, 3/4 = HEADPHONES out */
        memcpy(g_out, g_mix4ch, (size_t)size * 4 * sizeof(int32_t));
    } else {
        /* stereo sink (HDMI): master, or the cue mix when it has audio and the
         * operator asked for cue-on-stereo */
        for (snd_pcm_uframes_t i = 0; i < size; i++) {
            int use_cue = (g_cue_on_2ch && s_has_hp_audio);
            g_out[i * 2 + 0] = g_mix4ch[i * 4 + (use_cue ? 2 : 0)];
            g_out[i * 2 + 1] = g_mix4ch[i * 4 + (use_cue ? 3 : 1)];
        }
    }

    /* startup mute + fade-in over every output channel */
    startup_env_init();
    {
        unsigned long long t0   = g_startup_frames_done;
        unsigned long long mute = (unsigned long long)g_startup_mute_frames;
        unsigned long long fade = (unsigned long long)g_startup_fade_frames;
        g_startup_frames_done += size;
        if (mute > 0 && t0 < mute + fade) {
            if (!g_startup_logged_mute) {
                g_startup_logged_mute = 1;
                alog("audioshim: startup mute active: %llu+%llu frames\n", mute, fade);
            }
            for (snd_pcm_uframes_t i = 0; i < size; i++) {
                float g = startup_gain(t0 + i, mute, fade);
                if (g >= 1.0f) continue;
                for (int c = 0; c < g_real_ch; c++)
                    g_out[i * g_real_ch + c] =
                        (int32_t)((float)g_out[i * g_real_ch + c] * g);
            }
        }
        if (!g_startup_logged_open && mute > 0 && g_startup_frames_done >= mute + fade) {
            g_startup_logged_open = 1;
            alog("audioshim: startup mute released after %llu frames\n",
                 g_startup_frames_done);
        }
    }

    snd_pcm_sframes_t written = 0;
    if (g_real_playback && real_snd_pcm_writei && !g_pcm_dead) {
        /* Never write to a PCM that is not ready for it.  alsa-lib asserts
         * its way out of several of those states rather than returning an
         * error, and an assert is abort() - the whole player goes, with
         * SIGABRT and no explanation.  States: 0 OPEN, 1 SETUP, 2 PREPARED,
         * 3 RUNNING, 4 XRUN, 5 DRAINING, 6 PAUSED, 7 SUSPENDED, 8
         * DISCONNECTED.  Anything below PREPARED has to be prepared first;
         * DISCONNECTED means the device has gone. */
        if (real_snd_pcm_state) {
            int state = real_snd_pcm_state(g_real_playback);
            if (state == 8) {
                if (!g_pcm_dead) {
                    g_pcm_dead = 1;
                    alog("audioshim: the device has been disconnected; "
                         "pacing the engine in software from here\n");
                }
            }
            else if (state < 2 || state == 4) {
                int err = real_snd_pcm_prepare ?
                          real_snd_pcm_prepare(g_real_playback) : -1;
                alog("audioshim: the device was in state %d (not ready to be "
                     "written); prepare() res=%d\n", state, err);
                if (err < 0) {
                    g_pcm_dead = 1;
                    alog("audioshim: it will not prepare, so it is not going "
                         "to take audio - pacing the engine in software "
                         "instead of writing to it (which would abort)\n");
                }
            }
        }
    }

    if (g_real_playback && real_snd_pcm_writei && !g_pcm_dead) {
        snd_pcm_uframes_t done = 0;

        /* Write the whole period.  A short write is normal when the device
         * buffer is nearly full; treating one as "done" loses those frames
         * and, worse, returns to the engine early - and the engine's
         * transport is clocked by this call, so returning early is what makes
         * the music run fast. */
        while (done < size) {
            written = real_snd_pcm_writei(g_real_playback,
                                          g_out + done * g_real_ch,
                                          size - done);
            if (written > 0) {
                done += (snd_pcm_uframes_t)written;
                g_pcm_fails = 0;
                continue;
            }
            if (written == -EPIPE || written == -EINTR || written == -EBADFD) {
                /* EPIPE is an underrun: prepare() is the whole recovery.
                 * EBADFD means the stream is not in a state that takes
                 * writes at all, and if prepare() will not fix that, a full
                 * re-setup is the only thing left that can. */
                if (real_snd_pcm_prepare)
                    real_snd_pcm_prepare(g_real_playback);
                if (written == -EBADFD && real_snd_pcm_state &&
                    real_snd_pcm_state(g_real_playback) < 2 &&
                    g_pcm_fails == 8) {
                    alog("audioshim: prepare() will not bring the device out "
                         "of state %d; setting it up again from scratch\n",
                         real_snd_pcm_state(g_real_playback));
                    configure_real_device();
                }
                if (++g_pcm_fails < 32)
                    continue;
            }
            /* Anything else - or too many underruns in a row - means this
             * device is not taking audio.  Stop pretending it is. */
            if (!g_pcm_dead) {
                g_pcm_dead = 1;
                alog("audioshim: the output device stopped accepting audio "
                     "(%s, errno %d, after %lu writes, state %d); pacing the "
                     "engine in software instead, so the deck still runs at "
                     "the right speed - but there will be no sound until "
                     "this is fixed\n",
                     strerror((int)-written), (int)-written, g_write_count,
                     real_snd_pcm_state ?
                         real_snd_pcm_state(g_real_playback) : -1);
            }
            break;
        }
    }

    /* The master stream, governed too.  A working device paces it and this
     * never sleeps; a dead or missing one would otherwise free-run. */
    {
        static struct pace pace_master = { 0, 0, 0, "the master stream", 0, 0 };
        pace_stream(&pace_master, size, g_rate);
        pace_report(&pace_master, size);
    }

    /* Publish the master level ~20x a second.  The launcher draws the meter
     * from this (rb4r5/overlay.py); nothing else in the stack knows what the
     * output is actually doing, because the engine's own meters are drawn
     * into the panel link this port does not decode. */
    publish_levels(s_peak_l, s_peak_r, s_peak_hp);
    {
        /* let the peaks fall back so the meter follows the music rather than
         * holding the loudest moment since the player started */
        static unsigned long s_decay = 0;
        if ((++s_decay & 0x0f) == 0) {
            s_peak_l -= s_peak_l >> 2;
            s_peak_r -= s_peak_r >> 2;
            s_peak_hp -= s_peak_hp >> 2;
        }
    }

    if ((g_write_count % 500) == 1) {
        alog("audioshim: writei #%lu frames=%lu written=%ld ch=%d peak_m=%d "
             "peak_cue=%d of %d (%d%% of full scale)\n",
             g_write_count, size, (long)written, g_real_ch, s_peak_master,
             s_peak_hp, g_full_scale,
             (int)((long long)s_peak_master * 100 / g_full_scale));
        s_peak_master = 0;
        s_peak_hp = 0;
    }

    return size;
}

snd_pcm_sframes_t snd_pcm_readi(snd_pcm_t *pcm, void *buffer, snd_pcm_uframes_t size)
{
    /* Clean silence for capture, so the mic path can never error out */
    if (buffer && size > 0)
        memset(buffer, 0, size * 8);
    return size;
}

/* ---- control interface -------------------------------------------------
 * rbp opens snd_ctl_open("hw:cs4344audiorev8") to enumerate the device.  That
 * card does not exist; failing the call makes JUCE report rate 0 and the engine
 * aborts, so fake success for the RX3 names and pass real ones through. */
int snd_ctl_open(snd_ctl_t **ctl, const char *name, int mode)
{
    init_real_alsa();
    alog("audioshim: snd_ctl_open(name='%s')\n", name ? name : "null");
    if (real_snd_ctl_open && name && strncmp(name, "hw:cs4344", 9) != 0 &&
        strstr(name, "esaics4344") == NULL) {
        int err = real_snd_ctl_open(ctl, name, mode);
        if (err == 0)
            return 0;
    }
    if (ctl) *ctl = (snd_ctl_t *)0x12345;
    return 0;
}

int snd_ctl_close(snd_ctl_t *ctl)
{
    init_real_alsa();
    if (ctl != (snd_ctl_t *)0x12345 && real_snd_ctl_close)
        return real_snd_ctl_close(ctl);
    return 0;
}

int snd_ctl_pcm_info(snd_ctl_t *ctl, snd_pcm_info_t *info)
{
    return 0;
}

/* ---- scheduling stubs ---------------------------------------------------
 * rbp asks for SCHED_FIFO and pins threads to CPUs that the RX3 had.  Left
 * alone, its RT threads starve the UI.  Neutralise them; the Pi 5 has four
 * A76 cores and the default scheduler copes fine. */
int pthread_setaffinity_np(pthread_t thread, size_t cpusetsize, const void *cpuset)
{
    (void)thread; (void)cpusetsize; (void)cpuset;
    return 0;
}

int sched_setaffinity(pid_t pid, size_t cpusetsize, const void *cpuset)
{
    (void)pid; (void)cpusetsize; (void)cpuset;
    return 0;
}

int sched_setscheduler(pid_t pid, int policy, const void *param)
{
    (void)pid; (void)policy; (void)param;
    return 0;
}

int pthread_setschedparam(pthread_t thread, int policy, const void *param)
{
    (void)thread; (void)policy; (void)param;
    return 0;
}

int pthread_setschedprio(pthread_t thread, int prio)
{
    (void)thread; (void)prio;
    return 0;
}

int pthread_attr_setschedpolicy(void *attr, int policy)
{
    (void)attr; (void)policy;
    return 0;
}

int pthread_attr_setschedparam(void *attr, const void *param)
{
    (void)attr; (void)param;
    return 0;
}
