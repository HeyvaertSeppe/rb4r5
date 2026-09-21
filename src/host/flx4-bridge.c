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
#include <poll.h>
#include <time.h>
#include <dirent.h>
#include <stdint.h>

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
#define K_TEMPO_RANGE 0x4107
#define K_TEMPO_SLIDER 0x4109
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
#define K_OVERLAY_FX  0xf001

#define K_BFXTYPE     0x448b
#define K_BFXCH       0x448c
#define K_BFX         0x448d
#define K_DEPTH       0x448f
#define K_BEATPREV    0x4490
#define K_BEATNEXT    0x4491
#define K_TAP         0x4492

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
static float jog_tpr = 1800.0f;    /* FLX4 messages per revolution        */
static int   jog_idle_ms = 60;     /* emit speed 0 after this idle time  */
static int   jog_touch_timeout_ms = 4000;  /* 0 = never let a touch go     */
static int   jog_emit_ms = 10;     /* one speed per this many ms          */
static const char *jog_conf = "/tmp/rb-jog.conf";  /* live tuning          */
static float jog_bend_scale = 0.25f;   /* the rim, relative to the plate  */
static int   jog_reverse = 0;      /* the platter turns the other way     */
static float jog_scale = 1.0f;     /* how hard a turn pushes the engine   */

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
    revs = (float)s->pending / (jog_tpr > 0.5f ? jog_tpr : 1800.0f);
    s->pending = 0;
    s->last_emit_ms = t;
    s->moving = 1;

    s->vpos += revs * jog_ppr;
    while (s->vpos >= jog_ppr) s->vpos -= jog_ppr;
    while (s->vpos < 0.0f)     s->vpos += jog_ppr;

    speed = revs / dt * jog_scale;
    if (s->mode == JOG_BEND)
        speed *= jog_bend_scale;     /* the rim nudges, it does not scratch */
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
        s->moving = 0;
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

static void led_set(int ch, int note, int on)
{
    midi_send3(0x90 | (ch & 0x0f), note, on ? 0x7f : 0x00);
}

/* ---- what a button's light should DO ----
 * Echoing the press is right for a button that means "do this now" and wrong
 * for one that means "you are now in this state".  PLAY stays lit while the
 * deck plays; a pad mode stays lit while that mode is chosen and the other
 * three go dark; CUE lights while held.  The player's own LED state is down
 * the panel link and not decoded yet (docs/13-panel-link.md), so this is
 * modelled from what we send - which is right until something else changes
 * the deck. */
#define LED_MOMENTARY 0        /* lit while held                        */
#define LED_TOGGLE    1        /* flips on each press                   */
#define LED_RADIO     2        /* one of a group, the rest go dark      */

struct led_rule {
    int key;                   /* the engine key this button sends      */
    int kind;
    int group;                 /* for LED_RADIO: which set it belongs to */
};

static const struct led_rule led_rules[] = {
    { K_PLAY,     LED_TOGGLE, 0 },
    { K_SYNC,     LED_TOGGLE, 0 },
    { K_MASTER,   LED_TOGGLE, 0 },
    { K_BFX,      LED_TOGGLE, 0 },
    { K_RELOOP,   LED_TOGGLE, 0 },
    /* the four pad modes are one group per deck */
    { K_HOTCUE,   LED_RADIO,  1 },
    { K_ALOOP,    LED_RADIO,  1 },
    { K_SLIPLOOP, LED_RADIO,  1 },
    { K_BEATJUMP, LED_RADIO,  1 },
};

/* the state we are showing, per (channel, note) */
#define LED_MAX 128
static struct { int ch, note, key, on, sch; } led_state[LED_MAX];
static int led_n = 0;

static int led_kind_of(int key, int *group)
{
    for (size_t i = 0; i < sizeof(led_rules) / sizeof(led_rules[0]); i++)
        if (led_rules[i].key == key) {
            if (group) *group = led_rules[i].group;
            return led_rules[i].kind;
        }
    return LED_MOMENTARY;
}

static void led_remember(int ch, int note, int key, int sch, int on)
{
    for (int i = 0; i < led_n; i++)
        if (led_state[i].ch == ch && led_state[i].note == note) {
            led_state[i].on = on;
            led_state[i].key = key;
            led_state[i].sch = sch;
            return;
        }
    if (led_n < LED_MAX) {
        led_state[led_n].ch = ch;
        led_state[led_n].note = note;
        led_state[led_n].key = key;
        led_state[led_n].sch = sch;
        led_state[led_n].on = on;
        led_n++;
    }
}

static int led_is_on(int ch, int note)
{
    for (int i = 0; i < led_n; i++)
        if (led_state[i].ch == ch && led_state[i].note == note)
            return led_state[i].on;
    return 0;
}

/* Light a button the way its kind says, and darken the rest of its group. */
static void led_for_press(int ch, int note, int key, int sch, int on)
{
    int group = 0;
    int kind = led_kind_of(key, &group);

    if (kind == LED_MOMENTARY) {
        led_set(ch, note, on);
        led_remember(ch, note, key, sch, on);
        return;
    }
    if (!on)
        return;                        /* state buttons act on the press */

    if (kind == LED_TOGGLE) {
        int next = !led_is_on(ch, note);
        led_set(ch, note, next);
        led_remember(ch, note, key, sch, next);
        return;
    }

    /* LED_RADIO: this one on, the others in the group on the same deck off */
    for (int i = 0; i < led_n; i++) {
        int other_group = 0;
        if (led_state[i].ch == ch && led_state[i].note == note)
            continue;
        if (led_kind_of(led_state[i].key, &other_group) != LED_RADIO ||
            other_group != group || led_state[i].sch != sch)
            continue;
        if (led_state[i].on) {
            led_set(led_state[i].ch, led_state[i].note, 0);
            led_state[i].on = 0;
        }
    }
    led_set(ch, note, 1);
    led_remember(ch, note, key, sch, 1);
}

/* ---------------- performance pads ----------------
 * On the FLX4 the pad note is (mode base + pad index), with a different base
 * per pad mode, and the four pad MIDI channels separate deck and SHIFT:
 *
 *   base 0x00 HOT CUE      -> RX3 HOT CUE bank
 *   base 0x20 BEAT JUMP    -> RX3 BEAT JUMP bank
 *   base 0x30 SAMPLER      -> RX3 SLIP LOOP bank (no sampler in this engine)
 *   base 0x60 BEAT LOOP    -> RX3 AUTO LOOP bank
 *   base 0x40 KEYBOARD, 0x70 KEY SHIFT, PAD FX  -> no RX3 equivalent
 */
static int pad_bank[2] = { -1, -1 };

static int pad_bank_key(int base)
{
    switch (base) {
    case 0x0: return K_HOTCUE;
    case 0x2: return K_BEATJUMP;
    case 0x3: return K_SLIPLOOP;
    case 0x6: return K_ALOOP;
    default:  return 0;
    }
}

static void pad_select_bank(int deck, int base)
{
    int key = pad_bank_key(base);
    if (!key || pad_bank[deck] == base)
        return;
    pad_bank[deck] = base;
    send_tap(key, deck + 1);
}

static void handle_pad(int ch, int deck, int note, int on)
{
    int base = (note >> 4) & 0x0F;
    int idx  = note & 0x0F;

    if (opt_verbose)
        logmsg("  pad deck%d note 0x%02x (base 0x%x pad %d) %s%s\n",
               deck + 1, note, base, idx + 1, on ? "press" : "release",
               pad_bank_key(base) ? "" : "  [no RX3 bank: ignored]");
    if (idx > 7 || !pad_bank_key(base))
        return;

    /* Pads take their own path rather than the note table - the note is
     * computed from the mode and the pad number - so they were missed by the
     * lighting, which is why every button lit except these. */
    led_set(ch, note, on);

    pad_select_bank(deck, base);
    send_ctrl(K_PAD1 + idx, on ? OP_PRESS : OP_RELEASE, deck + 1, 0, 0.0f, 0);
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

    /* ---- pad mode buttons (the pads themselves carry their mode) ---- */
    { MC_DECK1, 0x1B, K_HOTCUE,   0, "PAD MODE hot cue" },
    { MC_DECK2, 0x1B, K_HOTCUE,   0, "PAD MODE hot cue" },
    { MC_DECK1, 0x6D, K_ALOOP,    0, "PAD MODE beat loop" },
    { MC_DECK2, 0x6D, K_ALOOP,    0, "PAD MODE beat loop" },
    { MC_DECK1, 0x20, K_BEATJUMP, 0, "PAD MODE beat jump" },
    { MC_DECK2, 0x20, K_BEATJUMP, 0, "PAD MODE beat jump" },
    { MC_DECK1, 0x22, K_SLIPLOOP, 0, "PAD MODE sampler -> slip loop" },
    { MC_DECK2, 0x22, K_SLIPLOOP, 0, "PAD MODE sampler -> slip loop" },

    /* ---- BEAT FX (ch 5, and ch 6 when the FX is assigned to CH2) ---- */
    /* Pressing FX SELECT opens the picker, because that is what pressing it
     * is FOR: the FLX4 has no screen, so cycling the effect blind is the
     * thing the picker exists to replace.  SHIFT+FX SELECT still cycles it
     * the old way for anyone who wants that. */
    { MC_FX1, 0x63, K_OVERLAY_FX, CH_GLOBAL, "BEAT FX select (picker)" },
    { MC_FX1, 0x64, K_BFXTYPE,  CH_GLOBAL, "SHIFT+BEAT FX select (cycle)" },
    { MC_FX1, 0x4A, K_BEATPREV, CH_GLOBAL, "BEAT <" },
    { MC_FX1, 0x4B, K_BEATNEXT, CH_GLOBAL, "BEAT >" },
    { MC_FX1, 0x47, K_BFX,      CH_GLOBAL, "BEAT FX on/off" },
    { MC_FX2, 0x47, K_BFX,      CH_GLOBAL, "BEAT FX on/off (CH2)" },
    { MC_FX1, 0x43, K_BFX,      CH_GLOBAL, "SHIFT+BEAT FX (all off)" },
    { MC_FX2, 0x43, K_BFX,      CH_GLOBAL, "SHIFT+BEAT FX (all off, CH2)" },
};
static int nmap_n = 44;   /* keep in sync with the initialiser above */

static const char *note_name(int ch, int note)
{
    for (int i = 0; i < nmap_n; i++)
        if (notemap[i].ch == ch && notemap[i].note == note)
            return notemap[i].name;
    return "";
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
        } else {
            logmsg("flx4: unknown map directive '%s'\n", kind);
        }
    }
    fclose(f);
    logmsg("flx4: loaded %d entries from %s\n", n, path);
}

/* BEAT FX channel-select switch: ch5 note 0x10 = CH1, ch6 note 0x11 = CH2 */
static int handle_fxch(int ch, int note)
{
    int v = 0;
    if (ch == MC_FX1 && note == 0x10)      v = 1;
    else if (ch == MC_FX2 && note == 0x11) v = 2;
    else if (ch == MC_FX1 && note == 0x14) v = 5;   /* master, if sent */
    else return 0;
    if (opt_verbose)
        logmsg("  beat fx channel select -> %d\n", v);
    send_ctrl(K_BFXCH, OP_VALUE, CH_GLOBAL, v, 0.0f, 0);
    return 1;
}

/* ---------------- MIDI dispatch ---------------- */
/* One line into the overlay daemon's command fifo.  Never blocks and never
 * matters if nothing is listening: the picker is a convenience, and the
 * controller must not stall because a daemon is not running. */
/* A short sweep at startup: every note we know how to light, on and then off.
 * It says "a host is here" to the controller, and it tells the operator at a
 * glance whether the output path works at all. */
static void led_hello(void)
{
    int shown = 0;
    if (midi_fd < 0 || !opt_leds)
        return;
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
    led_n = 0;                          /* everything is dark again */
    logmsg("flx4-bridge: lamp test over %d button(s)%s\n", shown,
           midi_fd < 0 ? " (no output device)" : "");
}

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
        if (notemap[i].key == K_OVERLAY_FX) {
            if (on)
                overlay_command("fx");
            if (opt_verbose)
                logmsg("  %s -> effect picker %s\n", notemap[i].name,
                       on ? "toggle" : "(release)");
            return;
        }
        sch = notemap[i].send_ch;
        if (sch == 0)
            sch = (ch == MC_DECK1) ? 1 : 2;
        send_ctrl(notemap[i].key, on ? OP_PRESS : OP_RELEASE, sch, 0, 0.0f, 0);
        led_for_press(ch, note, notemap[i].key, sch, on);
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
        int mode = (cc == 0x22) ? JOG_SCRATCH
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
    while ((opt = getopt(argc, argv, "vsld:f:m:J:O:S:T:H:B:E:c:RLF")) != -1) {
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
        case 'l': opt_list = 1; break;
        default:
            fprintf(stderr, "usage: %s [-v] [-s] [-l] [-d dev] [-f fifo] "
                            "[-m mapfile] [-J engine_ppr] [-T flx4_ticks_per_rev] "
                            "[-S jog_scale] [-B bend_scale] [-E emit_ms] "
                            "[-R] [-L] [-H touch_timeout_ms] "
                            "[-O overlayfifo] [-c jogconf] [-F]\n",
                            argv[0]);
            return 2;
        }
    }

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
