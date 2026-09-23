/* flx4-bridge.c - Pioneer DDJ-FLX4 (USB MIDI) -> XDJ-RX3 rekordbox engine (rbp)
 *
 * `rbp` (the standalone rekordbox player) has no MIDI input: it reads its front
 * panel from Pioneer microcontrollers and exposes an internal key manager.
 * keyshim.so (LD_PRELOADed into rbp) turns 12-byte key records from
 * /tmp/rb-keys.fifo into IKeyManager::sendKey() calls and 24-byte control
 * records from /tmp/rb-ctrl.fifo into the value/relative half of the same API.
 *
 *   DDJ-FLX4 --USB MIDI--> this bridge --24B records--> /tmp/rb-ctrl.fifo
 *                           (Raspberry Pi OS host)             |
 *                                                              v
 *                keyshim.so -> IKeyManager::sendKey(key, op, ch, param, f, l)
 *
 * Runs on the Pi's own rootfs, NOT in the soft-float chroot: it needs only
 * /dev/snd/midiC*D* and the shared /tmp.  No libasound dependency - the ALSA
 * rawmidi device is read directly and MIDI is parsed here - so it builds with
 * the Pi's own gcc in a second and has no runtime dependencies.
 *
 * MIDI map source: the Mixxx "Pioneer-DDJ-FLX4.midi.xml" mapping (which encodes
 * AlphaTheta's DDJ-FLX4 MIDI message list), cross-checked against the DDJ-400
 * bridge this file grew out of.  rbp keycodes and op semantics come from the
 * live-verified Prime GO knobshim2 / Chromebit port (docs/06-controller.md):
 *
 *   op 0 PRESS / op 2 RELEASE : param/f/l unused
 *   op 4 ROTATE: param = 10-bit absolute (faders, EQ, trim, crossfader)
 *                or relative delta (browse knob); f = normalised / jog speed
 *   op 5 VALUE : param = 10-bit, f = normalised (colour FX, Beat FX depth,
 *                tempo slider [-1..+1])
 *
 * Controls that have no XDJ-RX3 equivalent (or whose keycode we could not
 * verify) are left unbound and logged with -v; bind them without recompiling
 * via a map file (-m, see config/flx4-map.conf).
 *
 * Usage:
 *   flx4-bridge [-v] [-s] [-l] [-d /dev/snd/midiC1D0] [-f /tmp/rb-ctrl.fifo]
 *               [-m mapfile] [-J pulses_per_rev] [-F]
 *     -v  verbose (log every mapped event)
 *     -s  sniff: log every MIDI message, send nothing (use this to extend the
 *         map against real hardware)
 *     -l  print the mapping table and exit
 *     -m  load key overrides / additions from a map file
 *     -J  jog pulses per revolution (default 1800)
 *     -F  select FILTER as the Sound Color FX type on both channels at startup
 *     -N  light the lamps from the buttons only, not from the player's state
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <errno.h>
#include <math.h>
#include <poll.h>
#include <time.h>
#include <dirent.h>
#include <stdint.h>

#include "../shims/rb_state.h"

/* ------------------------------------------------------------------ */
/* rbp / XDJ-RX3 keys                                                  */
/* ------------------------------------------------------------------ */
#define OP_PRESS      0
#define OP_RELEASE    2
#define OP_ROTATE     4
#define OP_VALUE      5

#define K_SELECTOR    0x420c
#define K_SOURCE      0x0201
#define K_BROWSE      0x0202
#define K_MENU        0x0206
#define K_BACK        0x420d
#define K_LOAD        0x4311   /* + deck channel */
#define K_PLAY        0x4101
#define K_CUE         0x4102
#define K_SYNC        0x4112
#define K_JOG_TOUCH   0x4306
#define K_JOG_ROT     0x4305
/* The tempo (pitch) fader.
 *
 * The SC Live 4 port has onKey_TempoSlider at 0x4107, and on that reading
 * these two were swapped here - but swapping them BROKE the fader on the
 * RX3's own build, which had been working on 0x4109.  Hardware beats
 * inference from a different product: 0x4109 it is.
 *
 * Both builds are rbp, so one of them has the keys the other way round, or
 * the RX3 routes its fader through a different one.  Left as found, because
 * that is what works here. */
#define K_TEMPO_SLIDER 0x4109
#define K_TEMPO_RANGE  0x4107
#define K_ALOOP       0x4114
#define K_HOTCUE      0x4113
#define K_SLIPLOOP    0x4115
#define K_BEATJUMP    0x4116
#define K_PAD1        0x4117   /* .. K_PAD1+7 */
#define K_LOOPIN      0x410c
#define K_LOOPOUT     0x410d
#define K_RELOOP      0x410e
#define K_REV         0x410f
#define K_MASTER      0x4111
#define K_TRIM        0x5019
#define K_EQH         0x501a
#define K_EQM         0x501b
#define K_EQL         0x501c
#define K_FADER       0x501e
#define K_XFADER      0x6017
#define K_HPMIX       0x4405
#define K_HPLEVEL     0x4406
#define K_COLOR       0x509d
#define K_FILTER      0x50a6
/* Not an RX3 keycode: a note mapped to this opens the launcher's effect
 * picker instead of going to the player.  The FLX4 has one FX knob and no
 * way to see the list, so the picker is the missing half of that control. */
#define K_OVERLAY_FX  0xf001      /* FX SELECT: one effect down the list */
#define K_OVERLAY_FXUP 0xf002     /* SHIFT+FX SELECT: one effect up */

#define K_BFXTYPE     0x448b
#define K_MASTERCUE   0x4407
#define K_BFXCH       0x448c
#define K_BFX         0x448d
#define K_DEPTH       0x448f
#define K_EFFECTQUANT 0x0493
#define K_BEATPREV    0x4490
#define K_BEATNEXT    0x4491
#define K_TAP         0x4492
#define K_SRFWD       0x411f   /* SEARCH >, per deck */
#define K_SRREV       0x4120   /* SEARCH <, per deck */

#define CH_GLOBAL     1

/* DDJ-FLX4 MIDI channels (0-based nibble of the status byte).
 * Deck 1 = 0x9n/0xBn with n=0, deck 2 with n=1; Beat FX lives on channels 5
 * and 6; browser/mixer on channel 7; pads have four channels of their own. */
#define MC_DECK1      0
#define MC_DECK2      1
#define MC_FX1        4        /* ch 5: Beat FX, and "FX on CH1/1+2"  */
#define MC_FX2        5        /* ch 6: the same buttons when FX is on CH2 */
#define MC_MIXER      6        /* ch 7: browser, crossfader, phones, filter */
#define MC_PAD1       7        /* pads deck 1, no SHIFT   */
#define MC_PAD1_SH    8        /* pads deck 1, with SHIFT */
#define MC_PAD2       9        /* pads deck 2, no SHIFT   */
#define MC_PAD2_SH   10        /* pads deck 2, with SHIFT */

/* ------------------------------------------------------------------ */
static int   opt_verbose = 0;
static int   opt_sniff   = 0;
static int   opt_filter_init = 0;
static const char *fifo_path = "/tmp/rb-ctrl.fifo";
static const char *midi_dev  = NULL;
static const char *map_file  = NULL;
static const char *opt_overlay = "/tmp/rb-overlay.fifo";
/* Two different resolutions, and confusing them is what makes a jog wheel
 * feel dead and then skip:
 *   jog_ppr   - the units the ENGINE counts a platter revolution in, so the
 *               position it is handed must wrap at this and no higher;
 *   jog_tpr   - how many messages the FLX4 sends for one revolution of its
 *               own wheel, which is what turns those messages into a real
 *               speed.  Measure it: launch.py jogtest.
 * Dividing the FLX4's ticks by the engine's number (which is what this used
 * to do) makes every turn look far slower than it is, and letting the
 * position run to 16 bits hands the engine an angle it cannot mean. */
static float jog_ppr = 1800.0f;    /* engine units per revolution         */
static float jog_tpr = 600.0f;     /* FLX4 messages per revolution        */
/* 600 is the FLX4's, and it has to match controller.jog_ticks_per_rev in the
 * config.  It was 1800 - the RX3's own wheel - so whenever /tmp/rb-jog.conf
 * was missing the wheel came out three times too slow with nothing to say
 * why. */
static int   jog_idle_ms = 60;     /* emit speed 0 after this idle time  */
static int   jog_touch_timeout_ms = 4000;  /* 0 = never let a touch go     */
static int   jog_emit_ms = 10;     /* one speed per this many ms          */
static const char *jog_conf = "/tmp/rb-jog.conf";  /* live tuning          */
static float jog_bend_scale = 0.25f;   /* the rim, relative to the plate  */
static int   jog_reverse = 0;      /* the platter turns the other way     */
static float jog_scale = 1.0f;     /* how hard a turn pushes the engine   */
static int   jog_spindown_ms = 900; /* how long a let-go wheel keeps turning */

/* SMART FADER holds the pitch.
 *
 * With it on, the player is meant to match tempo by itself and the pitch
 * fader should stop moving the deck - otherwise nudging the fader fights
 * whatever the player just set.  There is one switch and it holds BOTH
 * decks, which is how the controller works.
 *
 * Not mapped by default: the note it sends has to come from `launch.py
 * sniff`, and a wrong guess here would silently swallow the pitch fader. */
static int g_smart_ch = -1;
static int g_smart_note = 0;
static int g_smart_on = 0;

static int fifo_fd = -1;

struct ctrl_ev {
    int32_t key;
    int32_t ch;
    int32_t op;
    int32_t param;
    float   f;
    int32_t l;
};

static void logmsg(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stdout, fmt, ap);
    va_end(ap);
    fflush(stdout);
}

/* ---------------- FIFO output ---------------- */
static void send_ctrl(int key, int op, int ch, int param, float f, int l)
{
    struct ctrl_ev ev;
    size_t off = 0;

    if (opt_sniff || key == 0)
        return;

    ev.key = key; ev.op = op; ev.ch = ch; ev.param = param; ev.f = f; ev.l = l;

    if (fifo_fd < 0)
        fifo_fd = open(fifo_path, O_RDWR | O_NONBLOCK);
    if (fifo_fd < 0)
        return;

    while (off < sizeof(ev)) {
        ssize_t n = write(fifo_fd, (char *)&ev + off, sizeof(ev) - off);
        if (n > 0) {
            off += (size_t)n;
        } else if (n < 0 && (errno == EAGAIN || errno == EINTR)) {
            static int warned = 0;
            if (!warned) {
                logmsg("flx4: no reader on %s (rbp/keyshim not running?)\n", fifo_path);
                warned = 1;
            }
            return;
        } else {
            /* the player restarted and recreated the FIFO: reopen it */
            close(fifo_fd);
            fifo_fd = open(fifo_path, O_RDWR | O_NONBLOCK);
            return;
        }
    }
    if (opt_verbose)
        logmsg("  -> key=0x%04x op=%d ch=%d param=%d f=%.3f l=%d\n",
               key, op, ch, param, (double)f, l);
}

/* How long the bridge itself took, from the MIDI byte arriving to the record
 * reaching the player's fifo.  "It responds slowly" has to be somebody's
 * microseconds, and this says whether they are ours. */
static long long handled_at_us = 0;

static void note_latency(const char *what)
{
    struct timespec ts;
    long long now;

    if (!opt_verbose || !handled_at_us)
        return;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    now = (long long)ts.tv_sec * 1000000LL + ts.tv_nsec / 1000LL;
    logmsg("  %s handled in %lldus\n", what, now - handled_at_us);
}

static void send_tap(int key, int ch)
{
    send_ctrl(key, OP_PRESS, ch, 0, 0.0f, 0);
    send_ctrl(key, OP_RELEASE, ch, 0, 0.0f, 0);
}

/* 7-bit CC (0..127) -> RX3 10-bit value (0..1023) */
static int cc_to_10bit(int v)
{
    if (v < 0) v = 0;
    if (v > 127) v = 127;
    return (v << 3) | (v >> 4);
}

/* ---------------- 14-bit CC pairs ----------------
 * The FLX4 sends every continuous control as MSB on cc and LSB on cc+0x20. */
#define N14 24
struct cc14 {
    int ch;
    int msb_cc;
    int key;
    int op;          /* OP_ROTATE for faders/knobs, OP_VALUE for colour/tempo */
    int send_ch;
    int signed_norm; /* tempo slider: normalise to -1..+1 instead of 0..1 */
    int msb;
    int have_msb;
    int last10;      /* dedupe */
    const char *name;
};
static struct cc14 cc14[N14];
static int cc14_n = 0;

/* Add a control, or replace the one already bound to (ch, msb_cc) - a map
 * file must be able to override a built-in binding, and handle_cc14() takes
 * the first match. */
/* What the DJ has each channel set to, 0..1, straight off the faders.
 *
 * The audio this shim sees is the MASTER mix - one stereo stream, already
 * mixed - so there is no per-deck level in it to show.  The fader is the
 * closest real signal there is: the left meter follows deck 1's fader and
 * the right follows deck 2's, so pulling one down drops its own meter.  It
 * is an approximation, and an honest one: it shows what you are sending,
 * not what the deck is playing. */
static float g_fader[2] = { 1.0f, 1.0f };

/* How the meter maps level to scale.  A DJ engine leaves headroom, so a
 * track peaking at -20 dBFS is normal and a -48 dB floor makes that look
 * half lit.  Tighten the floor, or add gain, from the map file. */
static float g_meter_floor_db = 36.0f;
static float g_meter_gain_db  = 0.0f;

/* The master level knob, if it sends anything.  Not mapped by default -
 * `launch.py sniff` says whether yours does - and published for the
 * launcher's on-screen meter as well as used here. */
static int   g_master_ch = -1;
static int   g_master_cc = 0;
static float g_master = 1.0f;

static void publish_master(void)
{
    char text[32];
    int fd = open("/tmp/rb-master.dat", O_WRONLY | O_CREAT | O_TRUNC, 0666);
    if (fd < 0)
        return;
    int len = snprintf(text, sizeof(text), "%.4f\n", (double)g_master);
    if (write(fd, text, len) < 0) { /* nothing to be done */ }
    close(fd);
}

static void add_cc14(int ch, int msb_cc, int key, int op, int send_ch,
                     int signed_norm, const char *name)
{
    struct cc14 *slot = NULL;

    for (int i = 0; i < cc14_n; i++) {
        if (cc14[i].ch == ch && cc14[i].msb_cc == msb_cc) {
            slot = &cc14[i];
            break;
        }
    }
    if (!slot) {
        if (cc14_n >= N14) {
            logmsg("flx4: cc14 table full, dropping %s\n", name);
            return;
        }
        slot = &cc14[cc14_n++];
    }
    slot->ch = ch;
    slot->msb_cc = msb_cc;
    slot->key = key;
    slot->op = op;
    slot->send_ch = send_ch;
    slot->signed_norm = signed_norm;
    slot->have_msb = 0;
    slot->last10 = -1;
    slot->name = name;
}

static int handle_cc14(int ch, int cc, int val)
{
    for (int i = 0; i < cc14_n; i++) {
        struct cc14 *s = &cc14[i];
        if (s->ch != ch)
            continue;
        if (!s->key && (cc == s->msb_cc || cc == s->msb_cc + 0x20))
            return 1;               /* explicitly unbound in the map file */
        if (cc == s->msb_cc) {
            s->msb = val;
            s->have_msb = 1;
            return 1;
        }
        if (cc == s->msb_cc + 0x20) {
            if (!s->have_msb)
                return 1;                    /* MSB not seen yet */
            int pos = (s->msb << 7) | val;   /* 0..16383 */
            float norm;
            int v10;
            if (s->signed_norm) {
                norm = ((float)pos / 16383.0f) * 2.0f - 1.0f;
                v10 = (int)((norm + 1.0f) * 511.5f + 0.5f);
            } else {
                norm = (float)pos / 16383.0f;
                v10 = (int)(norm * 1023.0f + 0.5f);
            }
            if (v10 < 0) v10 = 0;
            if (v10 > 1023) v10 = 1023;
            if (s->key == K_FADER && s->send_ch >= 1 && s->send_ch <= 2)
                g_fader[s->send_ch - 1] = norm;
            if (s->key == K_TEMPO_SLIDER && g_smart_on) {
                /* SMART FADER is on: the player is setting the tempo, so the
                 * fader must not fight it. */
                if (opt_verbose)
                    logmsg("  tempo held: SMART FADER is on\n");
                return 1;
            }
            if (v10 != s->last10) {
                s->last10 = v10;
                send_ctrl(s->key, s->op, s->send_ch, v10, norm, pos);
                if (opt_verbose)
                    logmsg("  %s ch%d pos=%d norm=%.3f v10=%d\n",
                           s->name, s->send_ch, pos, (double)norm, v10);
            }
            return 1;
        }
    }
    return 0;
}

static void build_cc14(void)
{
    /* deck strips: fader / trim / 3-band EQ (deck channel == send channel) */
    add_cc14(MC_DECK1, 0x13, K_FADER, OP_ROTATE, 1, 0, "channel fader");
    add_cc14(MC_DECK2, 0x13, K_FADER, OP_ROTATE, 2, 0, "channel fader");
    add_cc14(MC_DECK1, 0x04, K_TRIM,  OP_ROTATE, 1, 0, "trim");
    add_cc14(MC_DECK2, 0x04, K_TRIM,  OP_ROTATE, 2, 0, "trim");
    add_cc14(MC_DECK1, 0x07, K_EQH,   OP_ROTATE, 1, 0, "EQ hi");
    add_cc14(MC_DECK2, 0x07, K_EQH,   OP_ROTATE, 2, 0, "EQ hi");
    add_cc14(MC_DECK1, 0x0B, K_EQM,   OP_ROTATE, 1, 0, "EQ mid");
    add_cc14(MC_DECK2, 0x0B, K_EQM,   OP_ROTATE, 2, 0, "EQ mid");
    add_cc14(MC_DECK1, 0x0F, K_EQL,   OP_ROTATE, 1, 0, "EQ low");
    add_cc14(MC_DECK2, 0x0F, K_EQL,   OP_ROTATE, 2, 0, "EQ low");
    /* tempo slider: OP_VALUE, signed normalised [-1 = slow .. +1 = fast] */
    add_cc14(MC_DECK1, 0x00, K_TEMPO_SLIDER, OP_VALUE, 1, 1, "tempo");
    add_cc14(MC_DECK2, 0x00, K_TEMPO_SLIDER, OP_VALUE, 2, 1, "tempo");
    /* master section (ch 7) */
    add_cc14(MC_MIXER, 0x1F, K_XFADER,  OP_ROTATE, CH_GLOBAL, 0, "crossfader");
    add_cc14(MC_MIXER, 0x0C, K_HPMIX,   OP_ROTATE, CH_GLOBAL, 0, "phones mixing");
    add_cc14(MC_MIXER, 0x0D, K_HPLEVEL, OP_ROTATE, CH_GLOBAL, 0, "phones level");
    /* the two FILTER knobs drive the RX3 Sound Color FX (14-bit on the FLX4) */
    add_cc14(MC_MIXER, 0x17, K_COLOR, OP_VALUE, 1, 0, "filter/colour FX");
    add_cc14(MC_MIXER, 0x18, K_COLOR, OP_VALUE, 2, 0, "filter/colour FX");
}

/* ---------------- live tuning ----------------
 * How far a turn of the wheel moves the deck depends on a number nobody
 * documents - how many messages the FLX4 sends for one revolution - and the
 * only way to get it right is to turn the wheel and see.  Doing that through
 * a rebuild, a restart and a reload of the whole player takes minutes per
 * guess; re-reading a four line file takes none, so the wheel can be dialled
 * in while it is in your hand.
 *
 *     tpr=600        messages per revolution of the FLX4's wheel
 *     scale=1.0      multiplies how far a turn pushes the deck
 *     bend=0.25      the rim, relative to the plate
 *     reverse=0      1 if it counts the wrong way
 */
static void reload_jog_conf(int announce)
{
    static time_t last_mtime = 0;
    struct stat st;
    FILE *f;
    char line[128];

    if (stat(jog_conf, &st) != 0)
        return;
    if (st.st_mtime == last_mtime)
        return;
    last_mtime = st.st_mtime;

    f = fopen(jog_conf, "r");
    if (!f)
        return;
    while (fgets(line, sizeof(line), f)) {
        char *eq = strchr(line, '=');
        char *hash = strchr(line, '#');
        if (hash) *hash = '\0';
        if (!eq)
            continue;
        *eq = '\0';
        double value = atof(eq + 1);
        if (!strcmp(line, "tpr") && value > 0.5)        jog_tpr = (float)value;
        else if (!strcmp(line, "scale") && value > 0.0) jog_scale = (float)value;
        else if (!strcmp(line, "bend") && value >= 0.0) jog_bend_scale = (float)value;
        else if (!strcmp(line, "reverse"))              jog_reverse = value != 0.0;
        else if (!strcmp(line, "emit_ms") && value >= 1) jog_emit_ms = (int)value;
        else if (!strcmp(line, "spindown") && value >= 0)
            jog_spindown_ms = (int)value;
    }
    fclose(f);
    if (announce)
        logmsg("flx4: jog retuned from %s: wheel %g/rev, scale %g, bend %g%s\n",
               jog_conf, (double)jog_tpr, (double)jog_scale,
               (double)jog_bend_scale, jog_reverse ? ", reversed" : "");
}

/* ---------------- jog wheels ---------------- */
/* What the wheel is being asked to do.  The RX3 decides between scratching
 * and bending from whether the PLATE is held, and the FLX4 reports the plate
 * and the rim on different CCs - so the difference is knowable here, and has
 * to be, because a rim nudge that scratches is the difference between
 * "nudge it back into time" and "the track jumps". */
#define JOG_SCRATCH  0
#define JOG_BEND     1
#define JOG_SEARCH   2

struct jog {
    int midi_ch;
    int send_ch;
    float vpos;            /* platter angle in engine units, 0 .. jog_ppr */
    int moving;
    int touched;           /* the plate is held (note 0x36)               */
    int auto_touch;        /* ... and we are the ones saying so           */
    int pending;           /* ticks accumulated since the last emit       */
    int mode;              /* JOG_*                                        */
    long long last_ms;
    long long last_emit_ms;
    long long touch_ms;    /* when the touch arrived, for the stuck guard */
    float last_speed;      /* what it was doing, so it can spin down       */
};
static struct jog jogs[2];

static long long now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000LL + ts.tv_nsec / 1000000LL;
}

static struct jog *jog_for(int midi_ch)
{
    for (int i = 0; i < 2; i++)
        if (jogs[i].midi_ch == midi_ch)
            return &jogs[i];
    return NULL;
}

static void jog_touch_set(struct jog *s, int on, int automatic)
{
    if (s->touched == on)
        return;
    s->touched = on;
    s->auto_touch = on ? automatic : 0;
    s->touch_ms = now_ms();
    send_ctrl(K_JOG_TOUCH, on ? OP_PRESS : OP_RELEASE, s->send_ch, 0, 0.0f, 0);
    if (opt_verbose)
        logmsg("  jog deck%d: plate %s%s\n", s->send_ch,
               on ? "held" : "let go", automatic ? " (from the wheel)" : "");
}

/* Only accumulate here.  Working out a speed from the gap between two MIDI
 * messages makes it jump about - the messages arrive in bursts, so the gap is
 * sometimes a millisecond and sometimes twenty, and the speed swings by the
 * same factor even though the wheel is turning steadily.  That is the
 * "momentum" that comes and goes.  jog_tick() turns the accumulated ticks
 * into one speed at a fixed rate instead. */
static void jog_delta(int midi_ch, int delta, int mode)
{
    struct jog *s = jog_for(midi_ch);
    if (!s || delta == 0)
        return;
    if (jog_reverse)
        delta = -delta;

    s->pending += delta;
    s->mode = mode;
    s->last_ms = now_ms();

    /* The engine tells scratching from bending by whether the plate is held.
     * If the plate's own note never arrives - and on some units it does not -
     * moving the plate says so on its behalf, and moving the rim takes it
     * back. */
    if (mode == JOG_SCRATCH || mode == JOG_SEARCH) {
        if (!s->touched)
            jog_touch_set(s, 1, 1);
    }
    else if (s->auto_touch) {
        jog_touch_set(s, 0, 1);
    }
}

static void jog_emit(struct jog *s, long long t)
{
    float dt = (float)(t - s->last_emit_ms) / 1000.0f;
    float revs, speed;

    if (dt < 0.001f)
        dt = 0.001f;
    revs = (float)s->pending / (jog_tpr > 0.5f ? jog_tpr : 600.0f);
    s->pending = 0;
    s->last_emit_ms = t;
    s->moving = 1;

    s->vpos += revs * jog_ppr;
    while (s->vpos >= jog_ppr) s->vpos -= jog_ppr;
    while (s->vpos < 0.0f)     s->vpos += jog_ppr;

    speed = revs / dt * jog_scale;
    /* What makes a turn a scratch is the plate being HELD, not which CC
     * carried it.  Deciding on the CC alone meant that touching the top and
     * turning still counted as the rim - a quarter-speed nudge - whenever the
     * FLX4 sent the rim's CC, which is what "captive touch feels like the
     * side" was. */
    if (s->mode == JOG_BEND && !s->touched)
        speed *= jog_bend_scale;     /* the rim nudges, it does not scratch */
    s->last_speed = speed;
    if (speed > 8.0f) speed = 8.0f;
    if (speed < -8.0f) speed = -8.0f;

    send_ctrl(K_JOG_ROT, OP_ROTATE, s->send_ch, 0, speed, (int)s->vpos);
    if (opt_verbose)
        logmsg("  jog deck%d %s speed=%.3f rev/s pos=%d%s\n", s->send_ch,
               s->mode == JOG_BEND ? "bend " :
               s->mode == JOG_SEARCH ? "search" : "scratch",
               (double)speed, (int)s->vpos, s->touched ? " (held)" : "");
}

/* A jog that stops sending must be told to stop, or the engine keeps nudging. */
static void jog_tick(void)
{
    long long t = now_ms();
    static long long last_conf = 0;

    if (t - last_conf > 500) {          /* twice a second is plenty */
        last_conf = t;
        reload_jog_conf(1);
    }
    for (int i = 0; i < 2; i++) {
        struct jog *s = &jogs[i];

        /* one speed per interval, however the messages arrived */
        if (s->pending != 0 && t - s->last_emit_ms >= jog_emit_ms)
            jog_emit(s, t);

        if (!s->moving)
            continue;
        /* A plate reported as touched but not moving for a long time is
         * almost always a note-off that went missing (the FLX4 sends them on
         * a different note under SHIFT).  Let it go rather than leave the
         * deck in scratch. */
        if (s->touched && jog_touch_timeout_ms > 0 &&
            t - s->touch_ms > jog_touch_timeout_ms &&
            t - s->last_ms > jog_touch_timeout_ms) {
            if (opt_verbose)
                logmsg("  jog deck%d: releasing a touch held %lldms with no "
                       "movement\n", s->send_ch, t - s->touch_ms);
            jog_touch_set(s, 0, 0);
        }
        if (t - s->last_emit_ms < jog_idle_ms)
            continue;

        /* A real platter does not stop the instant you let go.  Let the last
         * speed run down instead of cutting it to zero, so a backspin carries
         * on turning and the wheel feels heavier than it is. */
        if (jog_spindown_ms > 0 && !s->touched &&
            (s->last_speed > 0.15f || s->last_speed < -0.15f)) {
            float per = (float)jog_emit_ms / (float)jog_spindown_ms;
            s->last_speed -= s->last_speed * per;
            s->vpos += s->last_speed * jog_ppr * (float)jog_emit_ms / 1000.0f;
            while (s->vpos >= jog_ppr) s->vpos -= jog_ppr;
            while (s->vpos < 0.0f)     s->vpos += jog_ppr;
            s->last_emit_ms = t;
            send_ctrl(K_JOG_ROT, OP_ROTATE, s->send_ch, 0, s->last_speed,
                      (int)s->vpos);
            continue;
        }
        s->moving = 0;
        s->last_speed = 0.0f;
        send_ctrl(K_JOG_ROT, OP_ROTATE, s->send_ch, 0, 0.0f, (int)s->vpos);
    }
}

/* ---------------- LEDs ----------------
 * Pioneer controllers light a button by being sent the note that button
 * sends, with velocity 0x7f for on and 0x00 for off.  So lighting is mostly
 * echoing: press CUE, CUE lights.  That is not the same as mirroring the
 * player - the player's own LED state goes down the panel link, which this
 * port does not decode yet (docs/13-panel-link.md) - but it is the difference
 * between a controller that responds and one that looks dead. */
static int  midi_fd = -1;
static int  opt_leds = 1;

static void midi_send3(int status, int d1, int d2)
{
    unsigned char msg[3];
    if (midi_fd < 0 || !opt_leds)
        return;
    msg[0] = (unsigned char)status;
    msg[1] = (unsigned char)(d1 & 0x7f);
    msg[2] = (unsigned char)(d2 & 0x7f);
    if (write(midi_fd, msg, sizeof(msg)) < 0 && opt_verbose)
        logmsg("  (led write failed: %s)\n", strerror(errno));
}


/* What every lamp was last told, so a lamp is only written when it changes.
 * The lamps are redrawn from state every pass of the loop; without this that
 * would be thousands of messages a second down a USB MIDI link. */
static signed char lamp_last[16][128];
static int lamp_cache_ready = 0;

static void lamps_forget(void)
{
    memset(lamp_last, -1, sizeof(lamp_last));
    lamp_cache_ready = 1;
}

static void led_set(int ch, int note, int on)
{
    signed char *last;
    on = on ? 1 : 0;
    if (!lamp_cache_ready)
        lamps_forget();
    last = &lamp_last[ch & 0x0f][note & 0x7f];
    if (*last == on)
        return;
    *last = (signed char)on;
    midi_send3(0x90 | (ch & 0x0f), note, on ? 0x7f : 0x00);
}

/* ---- the BEAT FX SELECT lamp ----
 *
 * There is no box to be in any more: the effect list is always on screen, so
 * the button lights for a moment each time it moves the selection, the way a
 * momentary button does on the RX3.  It never lit at all before, because the
 * branch that handles it returns before led_for_press() is reached.
 */
#define FX_SELECT_NOTE  0x63
#define FX_FLASH_MS     120

static int       g_fx_lit    = 0;
static long long g_fx_at     = 0;

static void fx_lamp_flash(void)
{
    g_fx_lit = 1;
    g_fx_at  = now_ms();
    led_set(MC_FX1, FX_SELECT_NOTE, 1);
}

static void fx_lamp_tick(void)
{
    if (!g_fx_lit)
        return;
    if (now_ms() - g_fx_at < FX_FLASH_MS)
        return;
    g_fx_lit = 0;
    led_set(MC_FX1, FX_SELECT_NOTE, 0);
}

/* ====================================================================
 * The lamps
 * ====================================================================
 *
 * The RX3 decides every lamp on its panel itself - PLAY blinking while
 * paused, SYNC, the hot cue pads that hold a cue, BEAT FX - and keyshim.so
 * now reads that decision from inside the player and publishes it
 * (src/shims/rb_state.h).  So the lamps below follow the PLAYER, not the
 * buttons: a track loaded from the touchscreen, a loop that ends by itself or
 * a hot cue stored last week all show, which no amount of echoing presses
 * could do.
 *
 * When the player's state is not there (an old keyshim, RB_ENGINE_STATE=0, or
 * the player still starting) the same lamps are worked out from a model of
 * the deck kept from the buttons - the fallback, not the plan.
 */
static int opt_engine_state = 1;       /* -N turns it off                   */
static const char *state_path = RB_STATE_PATH;   /* -P, for the tests      */

static struct rb_state g_rs;
static int       g_rs_ok = 0;          /* fresh within the last 750 ms      */
static uint32_t  g_rs_seq = 0;
static long long g_rs_fresh_at = 0;
static long long g_rs_read_at = 0;

static void player_state_up(void);

static void state_tick(long long t)
{
    struct rb_state st;
    int ok, fd;

    if (!opt_engine_state || t - g_rs_read_at < 25)
        return;
    g_rs_read_at = t;
    fd = open(state_path, O_RDONLY);
    if (fd >= 0) {
        ssize_t n = read(fd, &st, sizeof(st));
        close(fd);
        if (n == (ssize_t)sizeof(st) && st.magic == RB_STATE_MAGIC &&
            st.version == RB_STATE_VERSION && st.seq == st.seq_end) {
            if (st.seq != g_rs_seq) {
                g_rs_seq = st.seq;
                g_rs_fresh_at = t;
            }
            g_rs = st;
        }
    }
    ok = g_rs_fresh_at && t - g_rs_fresh_at < 750;
    if (ok != g_rs_ok) {
        g_rs_ok = ok;
        logmsg("flx4: %s\n", ok
               ? "the lamps now follow the player's own state"
               : "the player's state went quiet - lamps follow the buttons");
        if (ok) {
            logmsg("flx4: player reports%s%s%s%s\n",
                   (g_rs.flags & RBS_ENGINE)  ? " transport" : "",
                   (g_rs.flags & RBS_LEDSTAT) ? " lamps" : "",
                   (g_rs.flags & RBS_METERS)  ? " meters" : "",
                   (g_rs.flags & RBS_MIXER)   ? " headphone-cue" : "");
            player_state_up();
        }
    }
}

static int rs_has(uint32_t what)
{
    return g_rs_ok && (g_rs.flags & what) == what;
}

static int blink_on(void)
{
    return (int)((now_ms() / 400) & 1);    /* the RX3's ~1.25 Hz blink */
}

/* rbp's lamp state -> on/off right now */
static int lamp_level(uint8_t st, int fallback)
{
    if (st == RBL_UNKNOWN)
        return fallback;
    if (st == RBL_BLINK)
        return blink_on();
    return st != RBL_OFF;
}

/* ---- the fallback: a deck as the buttons describe it ---- */
struct deck_model {
    int loaded, playing;
    int cue_held, cue_preview, at_cue;
    long long cue_released_at;
    int looping, armed, has_loop, loop_pad, prev_looping;
    int sync, pfl;
};
static struct deck_model g_deck[2] = {
    { .loop_pad = -1 }, { .loop_pad = -1 },
};
static int g_bfx_on = 0;
static int g_hotcue[2][8];             /* model only: which slots hold a cue */
static int g_pad_held[2][8];

static int deck_loaded(int d)
{
    return rs_has(RBS_ENGINE) ? g_rs.deck[d].loaded : g_deck[d].loaded;
}

static int deck_playing(int d)
{
    if (rs_has(RBS_ENGINE))
        return g_rs.deck[d].playing;
    return g_deck[d].playing || g_deck[d].cue_preview;
}

static int deck_looping(int d)
{
    return rs_has(RBS_ENGINE) ? g_rs.deck[d].looping : g_deck[d].looping;
}

/* What a button did to the deck, CDJ rules:
 *   PLAY toggles; PLAY while CUE is held keeps it playing when CUE lets go.
 *   CUE while playing goes back to the cue point and stops.  CUE while paused
 *   on the cue point plays for as long as it is held; paused anywhere else
 *   it sets the cue point there. */
static void model_key(int d, int key, int on)
{
    struct deck_model *m = &g_deck[d];

    switch (key) {
    case K_PLAY:
        if (!on)
            break;
        m->loaded = 1;
        if (m->cue_held) {
            m->cue_preview = 0;
            m->playing = 1;
        } else {
            m->playing = !deck_playing(d);
            if (!m->playing)
                m->at_cue = 0;
        }
        break;
    case K_CUE:
        if (on) {
            m->loaded = 1;
            m->cue_held = 1;
            if (deck_playing(d)) {
                m->playing = 0;
                m->at_cue = 1;
            } else if (m->at_cue) {
                m->cue_preview = 1;
            } else {
                m->at_cue = 1;
            }
        } else {
            m->cue_held = 0;
            m->cue_released_at = now_ms();
            if (m->cue_preview) {
                m->cue_preview = 0;
                if (!m->playing)
                    m->at_cue = 1;
            }
        }
        break;
    case K_LOAD:
        if (!on)
            break;
        m->loaded = 1;
        m->playing = m->cue_preview = 0;
        m->at_cue = 1;
        m->looping = m->armed = m->has_loop = 0;
        m->loop_pad = -1;
        memset(g_hotcue[d], 0, sizeof(g_hotcue[d]));
        break;
    case K_LOOPIN:
        if (on && !deck_looping(d))
            m->armed = 1;
        break;
    case K_LOOPOUT:
        if (on && (m->armed || deck_looping(d))) {
            m->looping = m->has_loop = 1;
            m->armed = 0;
            m->loop_pad = -1;
        }
        break;
    case K_RELOOP:
        if (!on)
            break;
        m->armed = 0;
        if (deck_looping(d))
            m->looping = 0;
        else if (m->has_loop)
            m->looping = 1;
        break;
    case K_SYNC:
        if (on)
            m->sync = !m->sync;
        break;
    }
}

/* Keep the model in step with the player whenever the player can say, so
 * that the fallback starts from the truth if the state goes away. */
static void model_follow_player(int d)
{
    struct deck_model *m = &g_deck[d];
    const struct rb_state_deck *k = &g_rs.deck[d];
    int looping;

    if (!rs_has(RBS_ENGINE))
        return;
    m->loaded = k->loaded;
    if (!m->cue_held)
        m->playing = k->playing;
    m->sync = k->sync_on;
    looping = k->looping;
    if (looping || (m->prev_looping && !looping))
        m->armed = 0;                  /* a loop started, or one ended */
    if (looping)
        m->has_loop = 1;
    m->looping = looping;
    m->prev_looping = looping;
    /* playing moves the deck off its cue point - but not in the moment
     * after CUE lets go, while the player is still on its way back to it */
    if (k->playing && !m->cue_held && now_ms() - m->cue_released_at > 250)
        m->at_cue = 0;
}

/* ---- pad modes ----
 *
 * The FLX4's pad-mode buttons, and the RX3 bank each one puts the deck in.
 * The RX3 has four banks (keys 0x4113..0x4116) and a second press of a bank
 * key flips it to that bank's SECOND function - which is how RELEASE FX and
 * SLIP LOOP share 0x4115.  So PAD FX1 and SAMPLER are the same RX3 bank, told
 * apart by that second function:
 *
 *   HOT CUE  (0x1B, pads 0x00..) -> HOT CUE
 *   BEAT LOOP(0x6D, pads 0x60..) -> BEAT LOOP  (SHIFT + BEAT JUMP)
 *   BEAT JUMP(0x20, pads 0x20..) -> BEAT JUMP
 *   PAD FX1  (0x1E, pads 0x10..) -> RELEASE FX (0x4115, second function)
 *   SAMPLER  (0x22, pads 0x30..) -> SLIP LOOP  (0x4115, first function)
 *
 * The bank key is only ever sent when the deck is not already where it
 * should be: pressing it again is not "select", it is "flip".  Where the
 * player publishes its bank (rb_state pad_mode / pad_sub) that is what is
 * compared; otherwise the bank we last sent is.
 *
 * KEYBOARD, PAD FX2 and KEY SHIFT have no RX3 bank; their pads do nothing.
 */
enum { PM_HOTCUE, PM_BEATLOOP, PM_BEATJUMP, PM_RELEASEFX, PM_SLIPLOOP,
       PM_COUNT };

static const struct pad_mode {
    int note;          /* the mode button, on the deck channel      */
    int base;          /* high nibble of the pads' notes in this mode */
    int key;           /* the RX3 bank key                          */
    int sub;           /* 0 = the bank's first function, 1 = second */
    const char *name;
} pad_modes[PM_COUNT] = {
    { 0x1B, 0x0, K_HOTCUE,   0, "HOT CUE" },
    { 0x6D, 0x6, K_ALOOP,    0, "BEAT LOOP" },
    { 0x20, 0x2, K_BEATJUMP, 0, "BEAT JUMP" },
    { 0x1E, 0x1, K_SLIPLOOP, 1, "PAD FX1 = RELEASE FX" },
    { 0x22, 0x3, K_SLIPLOOP, 0, "SAMPLER = SLIP LOOP" },
};

/* The FLX4 wakes up in HOT CUE, and so does the player. */
static int g_padmode[2] = { PM_HOTCUE, PM_HOTCUE };
static int g_bank[2]    = { K_HOTCUE, K_HOTCUE };   /* the bank we last sent */
static int g_sub[2]     = { 0, 0 };                 /* ... and its function  */
static int g_sub_want[2] = { -1, -1 };  /* a function to check once settled */
static long long g_bank_settle[2];      /* rbp's report is stale until then */

static int pad_mode_by_base(int base)
{
    for (int m = 0; m < PM_COUNT; m++)
        if (pad_modes[m].base == base)
            return m;
    return -1;
}

/* Banks that have a second function this bridge cares about.  Only these
 * are ever pressed twice: flipping HOT CUE or BEAT JUMP to something unknown
 * would be worse than leaving them. */
static int bank_has_sub(int key)
{
    return key == K_SLIPLOOP || key == K_ALOOP;
}

static int rbp_bank(int d)
{
    if (now_ms() >= g_bank_settle[d] && rs_has(RBS_ENGINE) &&
        g_rs.deck[d].pad_mode <= 3)
        return K_HOTCUE + g_rs.deck[d].pad_mode;
    return g_bank[d];
}

static int live_sub(int d)
{
    return g_rs_ok && g_rs.deck[d].pad_sub <= 1;
}

static int rbp_sub(int d)
{
    if (now_ms() >= g_bank_settle[d] && live_sub(d))
        return g_rs.deck[d].pad_sub;
    return g_sub[d];
}

static void pad_mode_enter(int d, int m)
{
    const struct pad_mode *pm = &pad_modes[m];
    long long t = now_ms();

    g_padmode[d] = m;
    if (rbp_bank(d) != pm->key) {
        send_tap(pm->key, d + 1);
        g_bank[d] = pm->key;
        g_sub[d] = 0;                  /* a bank opens on its first function */
        g_bank_settle[d] = t + 300;
        if (bank_has_sub(pm->key)) {
            if (live_sub(d)) {
                g_sub_want[d] = pm->sub;       /* checked in pad_mode_tick */
            } else if (pm->sub) {
                send_tap(pm->key, d + 1);
                g_sub[d] = 1;
            }
        }
        logmsg("flx4: deck%d pads -> %s\n", d + 1, pm->name);
        return;
    }
    if (bank_has_sub(pm->key) && rbp_sub(d) != pm->sub) {
        send_tap(pm->key, d + 1);
        g_sub[d] = pm->sub;
        g_bank_settle[d] = t + 300;
        g_sub_want[d] = -1;
        logmsg("flx4: deck%d pads -> %s (second press)\n", d + 1, pm->name);
        return;
    }
    if (opt_verbose)
        logmsg("  deck%d already in %s; not pressing the bank key again\n",
               d + 1, pm->name);
}

/* After entering a bank, check which function it opened on - the player
 * says - and flip it once if that is not the one asked for. */
static void pad_mode_tick(long long t)
{
    for (int d = 0; d < 2; d++) {
        int want = g_sub_want[d];
        if (want < 0 || t < g_bank_settle[d])
            continue;
        g_sub_want[d] = -1;
        if (!live_sub(d))
            continue;
        if (g_rs.deck[d].pad_sub != want) {
            send_tap(g_bank[d], d + 1);
            g_bank_settle[d] = t + 300;
            logmsg("flx4: deck%d bank opened on its other function; "
                   "pressed it once more\n", d + 1);
        }
        g_sub[d] = want;
    }
}

static int handle_padmode(int ch, int deck, int note, int on)
{
    (void)ch;
    for (int m = 0; m < PM_COUNT; m++)
        if (pad_modes[m].note == note) {
            if (on)                     /* a mode is chosen on the press */
                pad_mode_enter(deck, m);
            return 1;
        }
    return 0;
}

/* ---- the pads ---- */
static int pad_lamp(int d, int m, int p)
{
    int held = g_pad_held[d][p];
    const struct rb_state_deck *k = &g_rs.deck[d];

    /* rbp's own pad lamps, while it has the deck in the bank these pads
     * belong to (its lamps show ITS bank, which the touchscreen can change) */
    if (rs_has(RBS_LEDSTAT) && rbp_bank(d) == pad_modes[m].key &&
        k->pad_led[p] != RBL_UNKNOWN)
        return lamp_level(k->pad_led[p], 0) || held;

    switch (m) {
    case PM_HOTCUE:
        return g_hotcue[d][p] || held;
    case PM_BEATLOOP:
        return (deck_looping(d) && g_deck[d].loop_pad == p) || held;
    default:
        return held;
    }
}

static void pads_refresh(int d)
{
    int plain = d == 0 ? MC_PAD1 : MC_PAD2;
    int shift = d == 0 ? MC_PAD1_SH : MC_PAD2_SH;

    /* Every mode's pad notes, so leaving a mode darkens what it lit.  The
     * SHIFT channel gets the same, or the pads go dark under SHIFT. */
    for (int m = 0; m < PM_COUNT; m++)
        for (int p = 0; p < 8; p++) {
            int note = (pad_modes[m].base << 4) | p;
            int v = (m == g_padmode[d]) ? pad_lamp(d, m, p) : 0;
            led_set(plain, note, v);
            led_set(shift, note, v);
        }
}

static void handle_pad(int ch, int deck, int note, int on)
{
    int base = (note >> 4) & 0x0F;
    int idx  = note & 0x0F;
    int shifted = (ch == MC_PAD1_SH || ch == MC_PAD2_SH);
    int m = pad_mode_by_base(base);

    if (opt_verbose)
        logmsg("  pad deck%d note 0x%02x (base 0x%x pad %d) %s%s\n",
               deck + 1, note, base, idx + 1, on ? "press" : "release",
               m >= 0 ? "" : "  [no RX3 bank: ignored]");
    if (idx > 7 || m < 0)
        return;

    /* The FLX4 changes its pads' notes by itself when a mode button is
     * pressed, so a pad says which mode the controller is really in. */
    if (on)
        pad_mode_enter(deck, m);
    g_pad_held[deck][idx] = on;

    /* The RX3 has no SHIFT key for the pads, so SHIFT + a hot cue pad
     * would CALL the cue the DJ meant to delete.  Better that it does
     * nothing. */
    if (shifted && m == PM_HOTCUE) {
        if (on)
            logmsg("flx4: SHIFT + hot cue pad has no RX3 equivalent "
                   "(delete hot cues on the touchscreen)\n");
        return;
    }

    if (on && m == PM_HOTCUE)
        g_hotcue[deck][idx] = 1;           /* an empty pad stores one */
    if (on && m == PM_BEATLOOP) {
        struct deck_model *dm = &g_deck[deck];
        if (deck_looping(deck) && dm->loop_pad == idx) {
            dm->looping = 0;               /* the lit pad again: exit */
        } else {
            dm->looping = dm->has_loop = 1;
            dm->armed = 0;
            dm->loop_pad = idx;
        }
    }
    send_ctrl(K_PAD1 + idx, on ? OP_PRESS : OP_RELEASE, deck + 1, 0, 0.0f, 0);
    pads_refresh(deck);                    /* no waiting for the next tick */
}

/* ---- CUE/LOOP CALL < and >: a running loop, halved or doubled ----
 *
 * The RX3's own CUE/LOOP CALL keycodes are not among the verified ones, and
 * BEAT < / > (which these used to send) are the Beat FX's beat, not the
 * loop's.  What is verified is the beat-loop pads, and rbp's beat-loop size
 * table runs 4, 2, 1, 1/2 .. 1/32 beats from pad 1 to pad 8 (rblive4,
 * PlayerInnards::execAutoBeatLoop).  So with a beat loop running in the BEAT
 * LOOP bank, "longer" is the pad to the left of the lit one and "shorter" the
 * pad to its right.  `loopcall reverse` in the map file flips that.
 */
static int g_loopcall_reverse = 0;

static void loop_call(int d, int longer)
{
    int cur = -1, next;

    if (!deck_looping(d)) {
        logmsg("flx4: deck%d LOOP CALL: no loop running\n", d + 1);
        return;
    }
    if (rbp_bank(d) != K_ALOOP) {
        logmsg("flx4: deck%d LOOP CALL: only a beat loop (BEAT LOOP pads) "
               "can be halved or doubled\n", d + 1);
        return;
    }
    if (rs_has(RBS_LEDSTAT))
        for (int p = 0; p < 8 && cur < 0; p++)
            if (g_rs.deck[d].pad_led[p] == RBL_ON ||
                g_rs.deck[d].pad_led[p] == RBL_BLINK)
                cur = p;
    if (cur < 0)
        cur = g_deck[d].loop_pad;
    if (cur < 0) {
        logmsg("flx4: deck%d LOOP CALL: cannot tell which beat loop is "
               "running\n", d + 1);
        return;
    }
    next = cur + ((longer != g_loopcall_reverse) ? -1 : 1);
    if (next < 0 || next > 7)
        return;                           /* already the longest/shortest */
    send_tap(K_PAD1 + next, d + 1);
    g_deck[d].loop_pad = next;
    if (opt_verbose)
        logmsg("  deck%d loop %s: pad %d -> pad %d\n", d + 1,
               longer ? "doubled" : "halved", cur + 1, next + 1);
}

/* ---- every lamp that shows state, from the player or the model ---- */
static void lamps_refresh(void)
{
    int blink = blink_on();

    if (midi_fd < 0 || !opt_leds)
        return;
    for (int d = 0; d < 2; d++) {
        int ch = d == 0 ? MC_DECK1 : MC_DECK2;
        struct deck_model *m = &g_deck[d];
        const struct rb_state_deck *k = &g_rs.deck[d];
        int ledstat = rs_has(RBS_LEDSTAT);
        int loaded, playing, looping, v;

        model_follow_player(d);
        loaded  = deck_loaded(d);
        playing = deck_playing(d);
        looping = deck_looping(d);

        /* PLAY: lit while playing, blinking while paused on a track */
        v = playing ? 1 : (loaded ? blink : 0);
        if (ledstat)
            v = lamp_level(k->play_led, v);
        led_set(ch, 0x0B, v);
        led_set(ch, 0x47, v);                /* the SHIFT layer's PLAY */

        /* CUE: lit on the cue point, blinking when paused anywhere else
         * (press it to set the cue there), lit while held */
        v = m->cue_held ? 1
          : (loaded && !playing) ? (m->at_cue ? 1 : blink) : 0;
        led_set(ch, 0x0C, v);
        led_set(ch, 0x48, v);

        /* SYNC: rbp's own lamp keeps its third state - blinking when synced
         * but nudged off the beat */
        v = rs_has(RBS_ENGINE) ? k->sync_on : m->sync;
        if (ledstat)
            v = lamp_level(k->sync_led, v);
        led_set(ch, 0x58, v);

        /* LOOP IN and OUT flash while a loop plays; IN alone flashes once its
         * point is set and OUT is awaited; RELOOP/EXIT is lit while there is
         * a loop to exit.  Nothing stays lit once the loop is gone. */
        led_set(ch, 0x10, (looping || (m->armed && !looping)) ? blink : 0);
        led_set(ch, 0x11, looping ? blink : 0);
        led_set(ch, 0x4D, looping);

        /* headphone CUE */
        v = (rs_has(RBS_MIXER) && k->pfl <= 1) ? k->pfl : m->pfl;
        led_set(ch, 0x54, v);

        /* the pad-mode buttons live on the DECK channel (0x90/0x91) */
        for (int pm = 0; pm < PM_COUNT; pm++)
            led_set(ch, pad_modes[pm].note, pm == g_padmode[d]);

        pads_refresh(d);
    }

    /* BEAT FX ON/OFF: rbp blinks it while an effect is on */
    {
        int v = g_bfx_on;
        if (rs_has(RBS_LEDSTAT))
            v = lamp_level(g_rs.bfx_led, v);
        led_set(MC_FX1, 0x47, v);
        led_set(MC_FX2, 0x47, v);
    }
}

/* Keys whose lamp shows STATE, and is drawn by lamps_refresh() - never
 * echoed from the press, which is what made them wrong. */
static int lamp_owned(int key)
{
    switch (key) {
    case K_PLAY: case K_CUE: case K_SYNC: case K_LOOPIN: case K_LOOPOUT:
    case K_RELOOP: case K_BFX: case K_MASTERCUE:
        return 1;
    }
    return 0;
}

/* ---- the controls' real positions ----
 *
 * A fader that is up when the player starts reads as down until it is
 * moved, because nothing has said where it is.  Pioneer's controllers
 * answer this sysex with the position of every fader and knob (it is what
 * the DDJ-400/FLX4 Mixxx scripts send at startup; a controller that does not
 * know it ignores it).  It is sent when the controller appears and again,
 * every few seconds for half a minute, once the player is up - a position
 * sent before the player's mixer exists is dropped.
 */
static const unsigned char query_positions[] = {
    0xF0, 0x00, 0x40, 0x05, 0x00, 0x00, 0x02, 0x06, 0x00, 0x03, 0x01, 0xF7
};
static long long g_query_until = 0, g_query_next = 0;

static void positions_window(long long ms)
{
    long long t = now_ms();
    if (t + ms > g_query_until)
        g_query_until = t + ms;
    g_query_next = t;
}

static void positions_tick(long long t)
{
    if (midi_fd < 0 || t >= g_query_until || t < g_query_next)
        return;
    g_query_next = t + 3000;
    /* forget what was sent, or the answer - the same positions - would be
     * dropped as "no change" */
    for (int i = 0; i < cc14_n; i++) {
        cc14[i].last10 = -1;
        cc14[i].have_msb = 0;
    }
    if (write(midi_fd, query_positions, sizeof(query_positions)) < 0 &&
        opt_verbose)
        logmsg("  (position query failed: %s)\n", strerror(errno));
    else if (opt_verbose)
        logmsg("  asked the controller where its faders are\n");
}

static void player_state_up(void)
{
    positions_window(30000);
    /* the player may have been restarted under us: start from its banks */
    for (int d = 0; d < 2; d++)
        g_bank_settle[d] = 0;
}

/* ---------------- note mapping table ---------------- */
struct notemap {
    int ch;
    int note;
    int key;
    int send_ch;   /* 0 = derive from the MIDI channel (deck 1/2) */
    const char *name;
};

#define NMAP_MAX 96
static struct notemap notemap[NMAP_MAX] = {
    /* ---- BROWSER / LOAD (ch 7) ---- */
    { MC_MIXER, 0x41, K_SELECTOR, CH_GLOBAL, "BROWSE push (select)" },
    { MC_MIXER, 0x42, K_BACK,     CH_GLOBAL, "SHIFT+BROWSE push (back)" },
    { MC_MIXER, 0x46, K_LOAD,     1,         "LOAD deck 1" },
    { MC_MIXER, 0x47, K_LOAD,     2,         "LOAD deck 2" },
    /* The FLX4 has no SOURCE/MENU/BROWSE buttons: SHIFT+LOAD reaches them.
     * (SHIFT+LOAD is not in the Mixxx table; these notes come from the DDJ-400
     * and are logged as unmapped if the FLX4 does not send them.) */
    { MC_MIXER, 0x68, K_SOURCE,   CH_GLOBAL, "SHIFT+LOAD 1 (source)" },
    { MC_MIXER, 0x7A, K_MENU,     CH_GLOBAL, "SHIFT+LOAD 2 (menu)" },

    /* ---- deck transport (ch 1/2) ---- */
    { MC_DECK1, 0x0B, K_PLAY, 0, "PLAY/PAUSE" },
    { MC_DECK2, 0x0B, K_PLAY, 0, "PLAY/PAUSE" },
    { MC_DECK1, 0x0E, K_REV,  0, "SHIFT+PLAY (censor/reverse)" },
    { MC_DECK2, 0x0E, K_REV,  0, "SHIFT+PLAY (censor/reverse)" },
    { MC_DECK1, 0x0C, K_CUE,  0, "CUE" },
    { MC_DECK2, 0x0C, K_CUE,  0, "CUE" },
    /* The headphone CUE buttons - note 0x54, per deck, lit on the same note.
     * They were not bound at all, which is why they did nothing and never
     * lit.  The RX3's per-channel PFL keycode is not among the ones verified
     * so far, so this goes to MASTER CUE: the headphones follow, which is
     * most of what the button is for.  Bind it properly from the map file
     * once the right keycode is known. */
    /* CUE/LOOP CALL < and > halve and double a running beat loop - see
     * loop_call(); they are handled before this table.  With SHIFT they are
     * the RX3's SEARCH < / >, which scan while held. */
    { MC_DECK1, 0x3E, K_SRREV, 0, "SHIFT+LOOP CALL < (search back)" },
    { MC_DECK2, 0x3E, K_SRREV, 0, "SHIFT+LOOP CALL < (search back)" },
    { MC_DECK1, 0x3D, K_SRFWD, 0, "SHIFT+LOOP CALL > (search forward)" },
    { MC_DECK2, 0x3D, K_SRFWD, 0, "SHIFT+LOOP CALL > (search forward)" },
    /* SHIFT + RELOOP/EXIT toggles quantize */
    { MC_DECK1, 0x50, K_EFFECTQUANT, CH_GLOBAL, "SHIFT+RELOOP (quantize)" },
    { MC_DECK2, 0x50, K_EFFECTQUANT, CH_GLOBAL, "SHIFT+RELOOP (quantize)" },
    /* headphone CUE, per channel: toggled in the player's mixer directly
     * (handle_pfl), because the RX3 has no keycode for it.  MASTER CUE is
     * only the fallback for a player that cannot take the direct toggle. */
    { MC_DECK1, 0x54, K_MASTERCUE, CH_GLOBAL, "headphone CUE (deck 1)" },
    { MC_DECK2, 0x54, K_MASTERCUE, CH_GLOBAL, "headphone CUE (deck 2)" },
    { MC_DECK1, 0x58, K_SYNC, 0, "BEAT SYNC" },
    { MC_DECK2, 0x58, K_SYNC, 0, "BEAT SYNC" },
    { MC_DECK1, 0x5C, K_MASTER, 0, "BEAT SYNC long (master)" },
    { MC_DECK2, 0x5C, K_MASTER, 0, "BEAT SYNC long (master)" },
    { MC_DECK1, 0x60, K_TEMPO_RANGE, 0, "SHIFT+SYNC (tempo range)" },
    { MC_DECK2, 0x60, K_TEMPO_RANGE, 0, "SHIFT+SYNC (tempo range)" },

    /* ---- loops ---- */
    { MC_DECK1, 0x10, K_LOOPIN,  0, "LOOP IN / 4 BEAT" },
    { MC_DECK2, 0x10, K_LOOPIN,  0, "LOOP IN / 4 BEAT" },
    { MC_DECK1, 0x11, K_LOOPOUT, 0, "LOOP OUT" },
    { MC_DECK2, 0x11, K_LOOPOUT, 0, "LOOP OUT" },
    { MC_DECK1, 0x4D, K_RELOOP,  0, "RELOOP/EXIT" },
    { MC_DECK2, 0x4D, K_RELOOP,  0, "RELOOP/EXIT" },

    /* ---- jog plate touch ---- */
    { MC_DECK1, 0x36, K_JOG_TOUCH, 0, "jog plate touch" },
    { MC_DECK2, 0x36, K_JOG_TOUCH, 0, "jog plate touch" },
    { MC_DECK1, 0x67, K_JOG_TOUCH, 0, "SHIFT+jog plate touch" },
    { MC_DECK2, 0x67, K_JOG_TOUCH, 0, "SHIFT+jog plate touch" },

    /* ---- pad mode buttons: see pad_modes[], they are handled first ---- */

    /* ---- BEAT FX (ch 5, and ch 6 when the FX is assigned to CH2) ---- */
    /* Pressing FX SELECT opens the picker, because that is what pressing it
     * is FOR: the FLX4 has no screen, so cycling the effect blind is the
     * thing the picker exists to replace.  SHIFT+FX SELECT still cycles it
     * the old way for anyone who wants that. */
    { MC_FX1, 0x63, K_OVERLAY_FX,   CH_GLOBAL, "BEAT FX select (next effect)" },
    { MC_FX1, 0x64, K_OVERLAY_FXUP, CH_GLOBAL, "SHIFT+BEAT FX select (previous)" },
    { MC_FX1, 0x4A, K_BEATPREV, CH_GLOBAL, "BEAT <" },
    { MC_FX1, 0x4B, K_BEATNEXT, CH_GLOBAL, "BEAT >" },
    { MC_FX1, 0x47, K_BFX,      CH_GLOBAL, "BEAT FX on/off" },
    { MC_FX2, 0x47, K_BFX,      CH_GLOBAL, "BEAT FX on/off (CH2)" },
    { MC_FX1, 0x43, K_BFX,      CH_GLOBAL, "SHIFT+BEAT FX (all off)" },
    { MC_FX2, 0x43, K_BFX,      CH_GLOBAL, "SHIFT+BEAT FX (all off, CH2)" },
};
/* Counted at startup: a hand-kept count went stale every time the table
 * changed, and an entry past it was silently never matched. */
static int nmap_n = 0;

static void notemap_count(void)
{
    nmap_n = 0;
    while (nmap_n < NMAP_MAX && notemap[nmap_n].name)
        nmap_n++;
}

static const char *note_name(int ch, int note)
{
    for (int i = 0; i < nmap_n; i++)
        if (notemap[i].ch == ch && notemap[i].note == note)
            return notemap[i].name;
    return "";
}

/* ---------------- the level meter lamps ----------------
 *
 * A DDJ-FLX4's channel meter is lit by ONE message whose VALUE is the level -
 * ch1 CC 0x02 lights the whole left column - not by one message per segment.
 * (Found with `launch.py ledsweep`; see docs/06-controller.md.)
 *
 * Where each column comes from is a map file line, so a controller that
 * numbers them differently needs no rebuild:
 *
 *     meter <left|right> ch<n> <cc|note> <number>
 *
 * The levels come from audioshim, which publishes the master peak it is
 * actually sending to the card (docs/05-audio.md).
 */
#define LEVELS_PATH "/tmp/rb-levels.dat"

struct meter_out {
    int ch;                 /* MIDI channel, -1 = not mapped */
    int note;               /* send a note instead of a control change */
    int number;             /* the CC or note number */
    int last;               /* what was sent last, so it is only sent on change */
};

/* Confirmed on a DDJ-FLX4: the left column is ch1 CC 0x02.  The right is the
 * same control one channel up, which is how the pair is laid out on every
 * Pioneer controller this could be checked against - change it here or in
 * the map file if yours differs. */
static struct meter_out g_meter[2] = {
    { 0, 0, 0x02, -1 },     /* left  */
    { 1, 0, 0x02, -1 },     /* right */
};
static long long g_meter_at = 0;


static void meter_send(int column, int level)
{
    struct meter_out *out = &g_meter[column];

    if (out->ch < 0 || out->last == level)
        return;
    out->last = level;
    midi_send3((out->note ? 0x90 : 0xb0) | (out->ch & 0x0f),
               out->number, level & 0x7f);
}

static void meter_tick(void)
{
    int32_t record[5];
    long long t;
    int fd;
    ssize_t n;

    if (g_meter[0].ch < 0 && g_meter[1].ch < 0)
        return;
    t = now_ms();
    if (t - g_meter_at < 40)              /* twenty-five times a second */
        return;
    g_meter_at = t;

    /* The player's own channel meters, when it publishes them: each column
     * is its own deck's level, measured before the fader - exactly what the
     * RX3's channel meters show.  rbp lights 0..11 segments. */
    if (rs_has(RBS_METERS)) {
        for (int column = 0; column < 2; column++) {
            int seg = g_rs.deck[column].meter;
            if (seg > RB_METER_SEGMENTS)
                seg = RB_METER_SEGMENTS;
            meter_send(column, (seg * 127 + RB_METER_SEGMENTS / 2) /
                               RB_METER_SEGMENTS);
        }
        return;
    }

    /* Otherwise the master mix, which is one stream: each column is scaled
     * by its own fader and darkened while its deck is stopped. */
    fd = open(LEVELS_PATH, O_RDONLY);
    if (fd < 0)
        return;
    n = read(fd, record, sizeof(record));
    close(fd);
    if (n < (ssize_t)sizeof(record))
        return;

    {
        int32_t full = record[4] ? record[4] : 8388607;
        for (int column = 0; column < 2; column++) {
            int32_t peak = record[1 + column];
            int level = 0;
            /* the master mix is one stream: scale each column by its own
             * channel's fader so the two meters move apart */
            peak = (int32_t)((float)peak * g_fader[column] * g_master
                             * (deck_playing(column) ? 1.0f : 0.0f));
            if (peak > 0 && full > 0) {
                double db = 20.0 * log10((double)peak / (double)full)
                            + g_meter_gain_db;
                double share = (db + g_meter_floor_db) / g_meter_floor_db;
                if (share < 0.0) share = 0.0;
                if (share > 1.0) share = 1.0;
                level = (int)(share * 127.0 + 0.5);
            }
            meter_send(column, level);
        }
    }
}

/* ---------------- optional map file ----------------
 * Lines (# starts a comment):
 *    note <midi_ch> <note> <keycode> <send_ch|deck> [name...]
 *    cc14 <midi_ch> <msb_cc> <keycode> <rotate|value> <send_ch|deck> [signed]
 * `deck` means "derive the channel from the MIDI channel" (deck 1 / deck 2).
 * A note line replaces an existing binding for the same (ch, note); keycode 0
 * removes it.  This is how you bind a control whose RX3 keycode we could not
 * verify - see config/flx4-map.conf. */
static int parse_ch(const char *s)
{
    if (!strcmp(s, "deck"))   return 0;
    if (!strcmp(s, "global")) return CH_GLOBAL;
    return (int)strtol(s, NULL, 0);
}

static void notemap_set(int ch, int note, int key, int send_ch, const char *name)
{
    for (int i = 0; i < nmap_n; i++) {
        if (notemap[i].ch == ch && notemap[i].note == note) {
            notemap[i].key = key;
            notemap[i].send_ch = send_ch;
            notemap[i].name = name;
            return;
        }
    }
    if (nmap_n >= NMAP_MAX) {
        logmsg("flx4: note map full, dropping ch%d note 0x%02x\n", ch + 1, note);
        return;
    }
    notemap[nmap_n].ch = ch;
    notemap[nmap_n].note = note;
    notemap[nmap_n].key = key;
    notemap[nmap_n].send_ch = send_ch;
    notemap[nmap_n].name = name;
    nmap_n++;
}

static void load_map_file(const char *path)
{
    FILE *f = fopen(path, "r");
    char line[256];
    int n = 0;

    if (!f) {
        logmsg("flx4: cannot read map file %s: %s\n", path, strerror(errno));
        return;
    }
    while (fgets(line, sizeof(line), f)) {
        char kind[16], chs[16], sends[16], ops[16], name[96];
        int midich, num, key, signed_norm = 0;
        char *hash = strchr(line, '#');
        if (hash) *hash = '\0';

        if (sscanf(line, "%15s", kind) != 1)
            continue;
        if (!strcmp(kind, "note")) {
            int got = sscanf(line, "%15s %15s %i %i %15s %95[^\n]",
                             kind, chs, &num, &key, sends, name);
            if (got < 5) { logmsg("flx4: bad map line: %s", line); continue; }
            midich = parse_ch(chs) - 1;
            if (midich < 0) midich = 0;
            notemap_set(midich, num, key, parse_ch(sends),
                        got >= 6 ? strdup(name) : "(map file)");
            n++;
        } else if (!strcmp(kind, "cc14")) {
            int got = sscanf(line, "%15s %15s %i %i %15s %15s %i",
                             kind, chs, &num, &key, ops, sends, &signed_norm);
            if (got < 6) { logmsg("flx4: bad map line: %s", line); continue; }
            midich = parse_ch(chs) - 1;
            if (midich < 0) midich = 0;
            int schan = parse_ch(sends);
            if (schan == 0)                      /* "deck": derive it here */
                schan = (midich == MC_DECK2) ? 2 : 1;
            add_cc14(midich, num, key,
                     !strcmp(ops, "value") ? OP_VALUE : OP_ROTATE,
                     schan, got >= 7 ? signed_norm : 0, "(map file)");
            n++;
        } else if (!strcmp(kind, "smartfader")) {
            char how[16];
            int number = 0;
            int got = sscanf(line, "%15s %15s %15s %i", kind, chs, how, &number);
            if (got < 4) { logmsg("flx4: bad map line: %s", line); continue; }
            g_smart_ch = parse_ch(chs) - 1;
            if (g_smart_ch < 0) g_smart_ch = 0;
            g_smart_note = number;
            logmsg("flx4: SMART FADER on ch%d %s %#x - it will hold both "
                   "pitch faders\n", g_smart_ch + 1, how, number);
            n++;
        } else if (!strcmp(kind, "meterscale")) {
            float floor_db = 0.0f, gain_db = 0.0f;
            int got = sscanf(line, "%15s %f %f", kind, &floor_db, &gain_db);
            if (got < 2) { logmsg("flx4: bad map line: %s", line); continue; }
            if (floor_db > 6.0f && floor_db < 96.0f)
                g_meter_floor_db = floor_db;
            if (got >= 3)
                g_meter_gain_db = gain_db;
            logmsg("flx4: meter floor -%.0f dB, gain %+.1f dB\n",
                   (double)g_meter_floor_db, (double)g_meter_gain_db);
            n++;
        } else if (!strcmp(kind, "masterlevel")) {
            char how[16];
            int number = 0;
            int got = sscanf(line, "%15s %15s %15s %i", kind, chs, how, &number);
            if (got < 4) { logmsg("flx4: bad map line: %s", line); continue; }
            g_master_ch = parse_ch(chs) - 1;
            if (g_master_ch < 0) g_master_ch = 0;
            g_master_cc = number;
            logmsg("flx4: master level knob on ch%d CC %#x\n",
                   g_master_ch + 1, number);
            n++;
        } else if (!strcmp(kind, "loopcall")) {
            char how[16] = "";
            if (sscanf(line, "%15s %15s", kind, how) == 2 &&
                !strcmp(how, "reverse"))
                g_loopcall_reverse = 1;
            logmsg("flx4: LOOP CALL arrows %s\n", g_loopcall_reverse
                   ? "reversed (> is the pad to the left)"
                   : "normal (< halves, > doubles)");
            n++;
        } else if (!strcmp(kind, "meter")) {
            char which[16], how[16];
            int number = 0;
            int got = sscanf(line, "%15s %15s %15s %15s %i",
                             kind, which, chs, how, &number);
            if (got < 5) { logmsg("flx4: bad map line: %s", line); continue; }
            int column = !strcmp(which, "right") ? 1 : 0;
            if (!strcmp(chs, "off") || !strcmp(how, "off")) {
                g_meter[column].ch = -1;
                logmsg("flx4: %s level meter off\n", which);
            } else {
                g_meter[column].ch = parse_ch(chs) - 1;
                if (g_meter[column].ch < 0) g_meter[column].ch = 0;
                g_meter[column].note = !strcmp(how, "note");
                g_meter[column].number = number;
                g_meter[column].last = -1;
                logmsg("flx4: %s level meter on ch%d %s %#x\n", which,
                       g_meter[column].ch + 1,
                       g_meter[column].note ? "note" : "CC", number);
            }
            n++;
        } else {
            logmsg("flx4: unknown map directive '%s'\n", kind);
        }
    }
    fclose(f);
    logmsg("flx4: loaded %d entries from %s\n", n, path);
}

/* BEAT FX channel-select switch: ch5 note 0x10 = CH1, ch6 note 0x11 = CH2.
 *
 * The value is an index into the engine's own enum, and it starts at zero:
 *
 *   EnBeatEffectSelectChannel:
 *     0 = PLAYER_0   1 = PLAYER_1   2 = MIC_0
 *     3 = ASSIGN_A   4 = ASSIGN_B   5 = MASTER   6 = AUX
 *
 * This sent 1 for channel 1 and 2 for channel 2, which is PLAYER_1 and
 * MIC_0 - so selecting channel 1 showed channel 2, and channel 2 showed the
 * microphone.  Off by one, all the way along. */
static int handle_fxch(int ch, int note)
{
    int v = 0;
    if (ch == MC_FX1 && note == 0x10)      v = 0;   /* CH1 -> PLAYER_0 */
    else if (ch == MC_FX2 && note == 0x11) v = 1;   /* CH2 -> PLAYER_1 */
    else if (ch == MC_FX1 && note == 0x14) v = 5;   /* MASTER, if sent */
    else return 0;
    if (opt_verbose)
        logmsg("  beat fx channel select -> %d\n", v);
    send_ctrl(K_BFXCH, OP_VALUE, CH_GLOBAL, v, 0.0f, 0);
    return 1;
}

/* ---------------- MIDI dispatch ---------------- */
/* A short sweep at startup: every note we know how to light, on and then off.
 * It says "a host is here" to the controller, and it tells the operator at a
 * glance whether the output path works at all. */
static void led_hello(void)
{
    int shown = 0;
    if (midi_fd < 0 || !opt_leds)
        return;
    lamps_forget();                     /* a new device knows nothing */
    for (int d = 0; d < 2; d++)
        for (int m = 0; m < PM_COUNT; m++) {
            led_set(d == 0 ? MC_DECK1 : MC_DECK2, pad_modes[m].note, 1);
            shown++;
        }
    for (int i = 0; i < nmap_n; i++) {
        if (!notemap[i].key || notemap[i].key == K_OVERLAY_FX)
            continue;
        led_set(notemap[i].ch, notemap[i].note, 1);
        shown++;
    }
    usleep(250000);
    for (int i = 0; i < nmap_n; i++) {
        if (!notemap[i].key || notemap[i].key == K_OVERLAY_FX)
            continue;
        led_set(notemap[i].ch, notemap[i].note, 0);
    }
    for (int d = 0; d < 2; d++)
        for (int m = 0; m < PM_COUNT; m++)
            led_set(d == 0 ? MC_DECK1 : MC_DECK2, pad_modes[m].note, 0);
    /* from here lamps_refresh() draws everything from state */
    logmsg("flx4-bridge: lamp test over %d button(s)%s\n", shown,
           midi_fd < 0 ? " (no output device)" : "");
}

/* One line into the overlay daemon's command fifo.  Never blocks and never
 * matters if nothing is listening: the controller must not stall because a
 * daemon is not running. */
static void overlay_command(const char *word)
{
    static int complained = 0;
    int fd = open(opt_overlay, O_WRONLY | O_NONBLOCK);
    if (fd < 0) {
        /* Say this once even without -v: a button that appears to do nothing
         * is exactly the case where the log has to explain itself. */
        if (!complained++ || opt_verbose)
            logmsg("flx4: cannot reach the overlay daemon on %s (%s) - the "
                   "effect picker will not open.  Is rboverlay running?\n",
                   opt_overlay, strerror(errno));
        return;
    }
    if (write(fd, word, strlen(word)) < 0 || write(fd, "\n", 1) < 0) {
        if (opt_verbose)
            logmsg("  (overlay fifo write failed: %s)\n", strerror(errno));
    }
    close(fd);
}

static void handle_note(int ch, int note, int on)
{
    if (opt_sniff) {
        logmsg("MIDI ch%-2d NOTE %3d (0x%02x) %-3s %s\n",
               ch + 1, note, note, on ? "on" : "off", note_name(ch, note));
        return;
    }
    if ((ch == MC_DECK1 || ch == MC_DECK2) &&
        handle_padmode(ch, ch == MC_DECK1 ? 0 : 1, note, on))
        return;
    if ((ch == MC_FX1 || ch == MC_FX2) && handle_fxch(ch, note))
        return;
    if (ch == MC_PAD1 || ch == MC_PAD1_SH) {
        handle_pad(ch, 0, note, on);
        note_latency("pad");
        return;
    }
    if (ch == MC_PAD2 || ch == MC_PAD2_SH) {
        handle_pad(ch, 1, note, on);
        note_latency("pad");
        return;
    }

    /* SHIFT itself has no engine key: the FLX4 already sends shifted controls
     * as their own note numbers. */
    if ((ch == MC_DECK1 || ch == MC_DECK2) && note == 0x3F)
        return;

    if (ch == MC_DECK1 || ch == MC_DECK2) {
        int d = ch == MC_DECK1 ? 0 : 1;

        /* CUE/LOOP CALL < / >: halve or double a running beat loop */
        if (note == 0x51 || note == 0x53) {
            led_set(ch, note, on);
            if (on)
                loop_call(d, note == 0x53);
            return;
        }
        /* headphone CUE: straight into the player's mixer, per channel */
        if (note == 0x54) {
            if (on) {
                g_deck[d].pfl = !g_deck[d].pfl;
                if (rs_has(RBS_MIXER))
                    send_ctrl(RB_CMD_PFL_TOGGLE, OP_PRESS, d + 1, 0, 0.0f, 0);
                else
                    send_tap(K_MASTERCUE, CH_GLOBAL);
                if (opt_verbose)
                    logmsg("  headphone CUE deck%d%s\n", d + 1,
                           rs_has(RBS_MIXER) ? "" : " (as MASTER CUE)");
            }
            return;
        }
    }

    /* SHIFT + BEAT FX ON/OFF turns the effect OFF - not a second toggle,
     * which would turn an effect that is already off back on. */
    if ((ch == MC_FX1 || ch == MC_FX2) && note == 0x43) {
        int is_on = rs_has(RBS_LEDSTAT) && g_rs.bfx_led != RBL_UNKNOWN
                  ? g_rs.bfx_led != RBL_OFF : g_bfx_on;
        if (on && is_on) {
            send_tap(K_BFX, CH_GLOBAL);
            g_bfx_on = 0;
        }
        return;
    }

    for (int i = 0; i < nmap_n; i++) {
        int sch;
        if (notemap[i].ch != ch || notemap[i].note != note || !notemap[i].key)
            continue;
        if (notemap[i].key == K_JOG_TOUCH) {
            /* The plate's own note, which beats anything the wheel movement
             * inferred.  A touch that never gets its note-off would leave the
             * deck scratching for good, so jog_tick() gives up on one that
             * has been held without moving. */
            struct jog *s = jog_for(ch);
            if (s) {
                jog_touch_set(s, on, 0);
                return;
            }
        }
        if (notemap[i].key == K_OVERLAY_FX ||
            notemap[i].key == K_OVERLAY_FXUP) {
            int up = (notemap[i].key == K_OVERLAY_FXUP);
            if (on) {
                /* The launcher owns the effect list - it is the only thing
                 * that knows which one is lit - so this says which way to
                 * move and lets it do the stepping. */
                overlay_command(up ? "fx-" : "fx+");
                fx_lamp_flash();
            }
            if (opt_verbose)
                logmsg("  %s -> effect %s\n", notemap[i].name,
                       up ? "up" : "down");
            return;
        }
        sch = notemap[i].send_ch;
        if (sch == 0)
            sch = (ch == MC_DECK1) ? 1 : 2;

        /* the deck model only knows deck keys, so a global one on channel 1
         * falls through it untouched */
        if (sch >= 1 && sch <= 2)
            model_key(sch - 1, notemap[i].key, on);
        if (on && notemap[i].key == K_BFX)
            g_bfx_on = !g_bfx_on;

        send_ctrl(notemap[i].key, on ? OP_PRESS : OP_RELEASE, sch, 0, 0.0f, 0);
        /* A lamp that shows state is drawn from that state; any other button
         * lights while it is held. */
        if (lamp_owned(notemap[i].key))
            lamps_refresh();
        else
            led_set(ch, note, on);
        note_latency(notemap[i].name);
        if (opt_verbose)
            logmsg("  %s -> 0x%04x %s ch%d\n", notemap[i].name,
                   notemap[i].key, on ? "press" : "release", sch);
        return;
    }
    if (opt_verbose)
        logmsg("  unmapped ch%d note 0x%02x %s\n", ch + 1, note, on ? "on" : "off");
}

static void handle_cc(int ch, int cc, int val)
{
    if (opt_sniff) {
        logmsg("MIDI ch%-2d CC  %3d (0x%02x) val=%3d\n", ch + 1, cc, cc, val);
        return;
    }

    if (g_smart_ch >= 0 && ch == g_smart_ch && cc == g_smart_note) {
        g_smart_on = val >= 64;
        logmsg("flx4: SMART FADER %s - the pitch faders are %s\n",
               g_smart_on ? "on" : "off",
               g_smart_on ? "held" : "live again");
        return;
    }

    if (g_master_ch >= 0 && ch == g_master_ch && cc == g_master_cc) {
        g_master = (float)val / 127.0f;
        publish_master();
        if (opt_verbose)
            logmsg("  master level %.2f\n", (double)g_master);
        return;
    }

    /* browse encoder: relative, two's complement */
    if (ch == MC_MIXER && cc == 0x40) {
        int delta = (val >= 64) ? val - 128 : val;
        if (delta > 16) delta = 16;
        if (delta < -16) delta = -16;
        for (int i = 0; i < (delta < 0 ? -delta : delta); i++)
            send_ctrl(K_SELECTOR, OP_ROTATE, CH_GLOBAL, (delta < 0) ? -1 : 1, 0.0f, 0);
        if (opt_verbose)
            logmsg("  browse delta=%d\n", delta);
        return;
    }

    /* The jog wheel reports where it was touched, and that decides what the
     * turn means:
     *   0x21 the rim   -> bend: a nudge, the deck keeps playing
     *   0x22 the plate -> scratch, when the deck is in vinyl mode
     *   0x23 the plate in non-vinyl mode -> also a bend
     *   0x29 SHIFT+plate -> search through the track
     * Treating them all the same is why the wheel behaved identically
     * whether or not the plate was held. */
    if ((ch == MC_DECK1 || ch == MC_DECK2) &&
        (cc == 0x21 || cc == 0x22 || cc == 0x23 || cc == 0x29)) {
        int delta = (val >= 64) ? val - 128 : val;
        /* From the FLX4's own MIDI map:
         *   CC 0x22  PLATTER, vinyl mode ON   - the top
         *   CC 0x23  PLATTER, vinyl mode OFF  - still the top
         *   CC 0x21  SIDE                     - the rim
         *   CC 0x29  PLATTER + SHIFT          - search
         * 0x23 was being treated as the rim, so with vinyl mode off,
         * touching the top and turning was a quarter-speed nudge.  That is
         * "captive touch feels like the side". */
        int mode = (cc == 0x22 || cc == 0x23) ? JOG_SCRATCH
                 : (cc == 0x29) ? JOG_SEARCH : JOG_BEND;
        jog_delta(ch, delta, mode);
        return;
    }

    if (handle_cc14(ch, cc, val))
        return;

    /* BEAT FX level/depth is a 7-bit MSB-only control on the FLX4 */
    if ((ch == MC_FX1 || ch == MC_FX2) && cc == 0x02) {
        send_ctrl(K_DEPTH, OP_VALUE, CH_GLOBAL, cc_to_10bit(val),
                  (float)val / 127.0f, val);
        if (opt_verbose)
            logmsg("  beat fx depth val=%d\n", val);
        return;
    }

    if (opt_verbose)
        logmsg("  unmapped ch%d cc 0x%02x val=%d\n", ch + 1, cc, val);
}

/* ---------------- device discovery ---------------- */
/* Match the controller by ALSA card name, never by card index: a USB device can
 * enumerate before or after the Pi's own HDMI cards. */
static int find_controller_card(void)
{
    FILE *f = fopen("/proc/asound/cards", "r");
    char line[256];
    int card = -1, flx = -1, ddj = -1;

    if (!f)
        return -1;
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
        else if (strstr(line, "DDJ") && ddj < 0)
            ddj = card;
    }
    fclose(f);
    return (flx >= 0) ? flx : ddj;
}

static char *find_midi_node(int card)
{
    static char path[128];
    DIR *d = opendir("/dev/snd");
    struct dirent *e;
    if (!d)
        return NULL;
    while ((e = readdir(d))) {
        int c, dev;
        if (sscanf(e->d_name, "midiC%dD%d", &c, &dev) == 2 && c == card) {
            snprintf(path, sizeof(path), "/dev/snd/%.32s", e->d_name);
            closedir(d);
            return path;
        }
    }
    closedir(d);
    return NULL;
}

static int open_midi(void)
{
    if (midi_dev)
        return open(midi_dev, O_RDWR | O_NONBLOCK);

    int card = find_controller_card();
    if (card < 0)
        return -1;
    char *node = find_midi_node(card);
    if (!node) {
        logmsg("flx4: card %d has no rawmidi node in /dev/snd\n", card);
        return -1;
    }
    logmsg("flx4: using %s (ALSA card %d)\n", node, card);
    /* O_RDWR, not O_RDONLY: the same node carries the LEDs back to the
     * controller, and without writing to it every light stays dark and the
     * unit goes on blinking as though no host had claimed it. */
    {
        int fd = open(node, O_RDWR | O_NONBLOCK);
        if (fd < 0 && errno == EACCES)
            fd = open(node, O_RDONLY | O_NONBLOCK);   /* read-only is better
                                                         than nothing */
        return fd;
    }
}

static void list_map(void)
{
    printf("14-bit continuous controls (MSB cc, LSB cc+0x20):\n");
    for (int i = 0; i < cc14_n; i++)
        printf("  ch%-2d CC 0x%02x -> key 0x%04x op %s send-ch %d%s  %s\n",
               cc14[i].ch + 1, cc14[i].msb_cc, cc14[i].key,
               cc14[i].op == OP_VALUE ? "VALUE " : "ROTATE",
               cc14[i].send_ch, cc14[i].signed_norm ? " (signed)" : "",
               cc14[i].name);
    printf("notes:\n");
    for (int i = 0; i < nmap_n; i++)
        printf("  ch%-2d note 0x%02x -> key 0x%04x send-ch %-6s %s\n",
               notemap[i].ch + 1, notemap[i].note, notemap[i].key,
               notemap[i].send_ch ? "fixed" : "deck", notemap[i].name);
    printf("pads:   ch 8/9 = deck 1 (plain/SHIFT), ch 10/11 = deck 2;\n"
           "        note = mode base + (pad-1); bases 0x00 hot cue, 0x20 beat "
           "jump,\n        0x30 sampler, 0x60 beat loop -> keys 0x%04x..0x%04x\n",
           K_PAD1, K_PAD1 + 7);
    printf("browse: ch 7 CC 0x40 relative -> 0x%04x; jog: ch 1/2 CC 0x21/22/23/29\n",
           K_SELECTOR);
}

static void run_device(int fd);

int main(int argc, char **argv)
{
    int opt, fd, opt_list = 0;

    /* -l is handled after the whole option list, so `-l -m map.conf` and
     * `-m map.conf -l` behave the same. */
    while ((opt = getopt(argc, argv, "vsld:f:m:J:O:S:T:H:B:E:c:P:RLFN")) != -1) {
        switch (opt) {
        case 'v': opt_verbose = 1; break;
        case 's': opt_sniff = 1; opt_verbose = 1; break;
        case 'd': midi_dev = optarg; break;
        case 'f': fifo_path = optarg; break;
        case 'm': map_file = optarg; break;
        case 'J': jog_ppr = (float)atof(optarg); break;
        case 'R': jog_reverse = 1; break;
        case 'S': jog_scale = (float)atof(optarg); break;
        case 'T': jog_tpr = (float)atof(optarg); break;
        case 'H': jog_touch_timeout_ms = atoi(optarg); break;
        case 'B': jog_bend_scale = (float)atof(optarg); break;
        case 'E': jog_emit_ms = atoi(optarg); break;
        case 'c': jog_conf = optarg; break;
        case 'O': opt_overlay = optarg; break;
        case 'L': opt_leds = 0; break;
        case 'F': opt_filter_init = 1; break;
        case 'N': opt_engine_state = 0; break;
        case 'P': state_path = optarg; break;
        case 'l': opt_list = 1; break;
        default:
            fprintf(stderr, "usage: %s [-v] [-s] [-l] [-d dev] [-f fifo] "
                            "[-m mapfile] [-J engine_ppr] [-T flx4_ticks_per_rev] "
                            "[-S jog_scale] [-B bend_scale] [-E emit_ms] "
                            "[-R] [-L] [-H touch_timeout_ms] "
                            "[-O overlayfifo] [-c jogconf] [-F] [-N]\n",
                            argv[0]);
            return 2;
        }
    }

    notemap_count();
    build_cc14();
    if (map_file)
        load_map_file(map_file);
    if (opt_list) {
        list_map();
        return 0;
    }
    reload_jog_conf(0);                 /* before the first log line */
    jogs[0].midi_ch = MC_DECK1; jogs[0].send_ch = 1;
    jogs[1].midi_ch = MC_DECK2; jogs[1].send_ch = 2;

    logmsg("flx4-bridge: %s -> %s (jog: engine %g/rev, wheel %g/rev, "
           "scale %g%s)\n",
           opt_sniff ? "SNIFF (no output)" : "bridge", fifo_path,
           (double)jog_ppr, (double)jog_tpr, (double)jog_scale,
           jog_reverse ? ", reversed" : "");

    if (!opt_sniff) {
        fifo_fd = open(fifo_path, O_RDWR | O_NONBLOCK);
        if (fifo_fd < 0)
            logmsg("flx4: cannot open %s: %s (will retry per event)\n",
                   fifo_path, strerror(errno));
        if (opt_filter_init)
            for (int ch = 1; ch <= 2; ch++)
                send_tap(K_FILTER, ch);
    }

    /* Hot-plug friendly: wait for the controller instead of dying, so the
     * bridge can start before the FLX4 is plugged in and survive a replug. */
    for (;;) {
        fd = open_midi();
        if (fd < 0) {
            if (midi_dev) {
                logmsg("flx4: cannot open %s: %s\n", midi_dev, strerror(errno));
                return 1;              /* explicit -d device: fail fast */
            }
            logmsg("flx4: waiting for a DDJ-FLX4 to be plugged in...\n");
            sleep(2);
            continue;
        }
        run_device(fd);
        close(fd);
        logmsg("flx4: device closed, waiting for reconnect...\n");
        sleep(2);
    }
}

static void run_device(int fd)
{
    unsigned char buf[512];
    int status = 0, d1 = 0, need = 0, in_sysex = 0;

    /* Only ever light a real MIDI node.  A rawmidi device is a character
     * device and writes go out to the controller; anything else - a fifo in
     * a test, a regular file - sends them straight back at us as input, where
     * they parse as button presses nobody made. */
    {
        struct stat st;
        if (fstat(fd, &st) == 0 && S_ISCHR(st.st_mode)) {
            midi_fd = fd;
        }
        else {
            midi_fd = -1;
            if (opt_leds)
                logmsg("flx4: %s is not a MIDI character device, so the LEDs "
                       "are off (input still works)\n",
                       midi_dev ? midi_dev : "the device");
        }
    }
    led_hello();
    positions_window(10000);            /* where are the faders? */

    for (;;) {
        struct pollfd pfd;
        int pr;
        ssize_t n;
        pfd.fd = fd;
        pfd.events = POLLIN;
        pr = poll(&pfd, 1, 20);
        if (pr < 0) {
            if (errno == EINTR)
                continue;
            logmsg("flx4: poll: %s\n", strerror(errno));
            break;
        }
        {
            long long t = now_ms();
            state_tick(t);
            pad_mode_tick(t);
            positions_tick(t);
        }
        fx_lamp_tick();
        meter_tick();
        lamps_refresh();
        if (pr == 0) {
            jog_tick();
            continue;
        }
        n = read(fd, buf, sizeof(buf));
        if (n < 0) {
            if (errno == EAGAIN || errno == EINTR)
                continue;
            logmsg("flx4: read: %s (unplugged?)\n", strerror(errno));
            break;
        }
        if (n == 0) {
            logmsg("flx4: device EOF (unplugged?)\n");
            break;
        }
        for (ssize_t i = 0; i < n; i++) {
            unsigned char b = buf[i];

            if (b >= 0xF8)                    /* real-time: ignore */
                continue;
            if (b == 0xF0) { in_sysex = 1; status = 0; continue; }
            if (b == 0xF7) { in_sysex = 0; continue; }
            if (in_sysex)
                continue;
            if (b >= 0xF0) { status = 0; continue; }   /* system common */

            if (b & 0x80) {                   /* status byte */
                status = b;
                need = ((b & 0xF0) == 0xC0 || (b & 0xF0) == 0xD0) ? 1 : 2;
                d1 = 0;
                continue;
            }
            if (!status)
                continue;
            if (need == 2) {                  /* first data byte */
                d1 = b;
                need = 1;
                continue;
            }
            need = 2;                         /* running status: expect d1 again */
            int type = status & 0xF0;
            int ch = status & 0x0F;
            if (type == 0x90 || type == 0x80) {
                struct timespec ts;
                clock_gettime(CLOCK_MONOTONIC, &ts);
                handled_at_us = (long long)ts.tv_sec * 1000000LL +
                                ts.tv_nsec / 1000LL;
                handle_note(ch, d1, (type == 0x90 && b > 0));
                handled_at_us = 0;
            }
            else if (type == 0xB0)
                handle_cc(ch, d1, b);
            else if (opt_sniff)
                logmsg("MIDI ch%d status=0x%02x d1=%d d2=%d\n", ch + 1, status, d1, b);
        }
        jog_tick();
    }
}
