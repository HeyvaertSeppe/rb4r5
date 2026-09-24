/* keyshim.c - LD_PRELOAD shim: inject XDJ-RX3 button presses and control
 * values into rbp from two FIFOs, so host-side daemons (the DDJ-FLX4 MIDI
 * bridge, the touchscreen daemon, the keyboard daemon) can drive the engine.
 *
 * Mechanism (from Prime GO knobshim2.c, live-verified):
 *   IUiObjManager singleton @ 0x2685f2c; KeyManager = *(obj+100);
 *   vtable word 2 = IKeyManager::sendKey(keycode, op, ch, param, f, l).
 *   op 0 = press, 2 = release, 4 = rotate, 5 = absolute value.
 *
 * Inputs (both live in /tmp, which the chroot bind-mounts to the host /tmp):
 *   /tmp/rb-keys.fifo  12-byte records { int32 key, ch, down }  (buttons)
 *   /tmp/rb-ctrl.fifo  24-byte records { key, ch, op, param, float f, l }
 *                      (controller events: faders, EQ, jog, relative knobs)
 *
 * The control FIFO is what a real MIDI controller bridge (ddj400-bridge.c)
 * feeds.  While such a bridge is active the periodic re-application of the
 * synthetic mixer defaults is disabled, so physical faders are not fought over.
 *
 * rbp is the same binary as on the Prime GO / Chromebit ports, so every
 * hard-coded address below is unchanged (XDJ-RX3 v1.20).
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <pthread.h>
#include <poll.h>
#include <stdint.h>
#include <time.h>
#include <sys/syscall.h>

static int real_open(const char *p, int flags)
{
    return syscall(SYS_openat, AT_FDCWD, p, flags, 0);
}
static ssize_t real_read(int fd, void *buf, size_t n)
{
    return syscall(SYS_read, fd, buf, n);
}
static int real_close(int fd)
{
    return syscall(SYS_close, fd);
}

/* ---- tiny logger (raw syscalls, no printf) ----
 *
 * Capped, and quiet by default.  It used to log five lines for EVERY control
 * record - an open, a write and a close each - and the jog alone sends a
 * hundred records a second per deck.  That was thousands of syscalls a second
 * on the path between the wheel and the engine, and a log that grew without
 * end until /tmp filled.  Per-event lines now need KEYSHIM_VERBOSE=1. */
#ifndef KLOG_PATH
#define KLOG_PATH "/tmp/keyshim.log"
#endif
#define KLOG_CAP  (1024 * 1024)

static int klog_verbose = -1;

static int klog_is_verbose(void)
{
    if (klog_verbose < 0) {
        const char *v = getenv("KEYSHIM_VERBOSE");
        klog_verbose = (v && *v && *v != '0') ? 1 : 0;
    }
    return klog_verbose;
}

static void klog_str(const char *s)
{
    int fd = syscall(SYS_openat, AT_FDCWD, KLOG_PATH,
                     O_WRONLY | O_CREAT | O_APPEND, 0666);
    if (fd < 0)
        return;
    if (syscall(SYS_lseek, fd, 0, SEEK_END) > KLOG_CAP) {
        (void)syscall(SYS_close, fd);
        fd = syscall(SYS_openat, AT_FDCWD, KLOG_PATH,
                     O_WRONLY | O_CREAT | O_TRUNC, 0666);
        if (fd < 0)
            return;
    }
    (void)syscall(SYS_write, fd, s, strlen(s));
    (void)syscall(SYS_close, fd);
}
static void klog_hex(const char *label, unsigned int v)
{
    char buf[64];
    int n = 0;
    static const char hex[] = "0123456789abcdef";
    while (*label && n < 32) buf[n++] = *label++;
    for (int i = 7; i >= 0; i--) buf[n++] = hex[(v >> (i * 4)) & 0xf];
    buf[n++] = '\n';
    buf[n] = 0;
    klog_str(buf);
}
static void klog_dec(const char *label, int v)
{
    char buf[64];
    int n = 0, i, neg = 0;
    char tmp[16];
    int t = 0;
    while (*label && n < 32) buf[n++] = *label++;
    if (v < 0) { neg = 1; v = -v; }
    do { tmp[t++] = "0123456789"[v % 10]; v /= 10; } while (v);
    if (neg) tmp[t++] = '-';
    for (i = t - 1; i >= 0; i--) buf[n++] = tmp[i];
    buf[n++] = '\n';
    buf[n] = 0;
    klog_str(buf);
}

/* ---- rbp process check ---- */
static int is_rbp_process(void)
{
    char cmd[128];
    int fd, n;
    fd = real_open("/proc/self/cmdline", O_RDONLY);
    if (fd < 0) return 0;
    n = (int)real_read(fd, cmd, sizeof(cmd) - 1);
    real_close(fd);
    if (n <= 0) return 0;
    cmd[n] = '\0';
    for (int i = 0; i < n; i++)
        if (cmd[i] == '\0') cmd[i] = ' ';
    return strstr(cmd, "rbp") != NULL;
}

/* ---- KeyManager injection ---- */
#define UI_OBJ_MGR_GLOBAL 0x2685f2cUL
#define KEY_MANAGER_OFF   100
#define SENDKEY_VTABLE_WORD 2

#define OP_PRESS   0
#define OP_RELEASE 2
#define OP_ROTATE  4
#define OP_VALUE   5

static int rbp_checked = -1;

static void *get_key_manager(void)
{
    void **p;
    void *mgr;
    if (rbp_checked < 0)
        rbp_checked = is_rbp_process();
    if (!rbp_checked)
        return NULL;
    p = (void **)UI_OBJ_MGR_GLOBAL;
    if (!p) return NULL;
    mgr = *p;
    if (!mgr) return NULL;
    return *(void **)((char *)mgr + KEY_MANAGER_OFF);
}

typedef void (*sendkey_fn)(void *km, int keycode, int op, int ch,
                           long param, float f, long l);

static void send_rx_key(int keycode, int op, int ch)
{
    void *km = get_key_manager();
    if (!km) {
        klog_str("send_rx_key: no key manager\n");
        return;
    }
    void **vt = *(void ***)km;
    sendkey_fn fn = (sendkey_fn)vt[SENDKEY_VTABLE_WORD];
    if (!fn) {
        klog_str("send_rx_key: vtable fn null\n");
        return;
    }
    if (klog_is_verbose())
        klog_hex("sendkey key=", (unsigned)keycode);
    fn(km, keycode, op, ch, 0, 0.0f, 0);
}

static void send_rx_key_fl(int keycode, int op, int ch, long param, float f, long l)
{
    void *km = get_key_manager();
    if (!km)
        return;
    void **vt = *(void ***)km;
    sendkey_fn fn = (sendkey_fn)vt[SENDKEY_VTABLE_WORD];
    if (fn)
        fn(km, keycode, op, ch, param, f, l);
}

/* Mixer input routing (djengine::MixerRouteMngr) @ 0x01149f50/0x01149f54.
 * On the RX3 the physical DECK/LINE switches assign each mixer channel to a
 * player; with no sub-microcontroller both channels come up pointing at
 * Player 0, so the *channel-2 fader controls deck 1* (both faders move the
 * same sound).  Pin them like PrimeBox's knobshim2 (docs/05 §5):
 *   ch1 -> route object 0x01149f08 (Player 0 / deck 1)
 *   ch2 -> route object 0x01149f10 (Player 1 / deck 2)   */
#define MIX_ROUTE_CH1   0x01149f50UL
#define MIX_ROUTE_CH2   0x01149f54UL
#define MIX_ROUTE_PLAYER0 0x01149f08UL
#define MIX_ROUTE_PLAYER1 0x01149f10UL

static void mixer_route(void)
{
    *(volatile unsigned int *)MIX_ROUTE_CH1 = MIX_ROUTE_PLAYER0;
    *(volatile unsigned int *)MIX_ROUTE_CH2 = MIX_ROUTE_PLAYER1;
}

/* Without the RX3 sub-microcontroller the mixer controls have no physical
 * position, so the engine keeps its defaults — channel faders 0.0 (= silence)
 * and the crossfader hard left.  Push sane defaults once the KeyManager is up
 * (same idea as the Prime GO knobshim2 mixer init, SESSION_STATE_12).
 * keycodes: FADER 0x501e, TRIM 0x5019, EQH/M/L 0x501a/b/c, XFADER 0x6017. */
static void mixer_defaults(void)
{
    int ch;
    mixer_route();  /* re-assert Input 0->deck1, Input 1->deck2 */
    for (ch = 1; ch <= 2; ch++) {
        send_rx_key_fl(0x501e, OP_ROTATE, ch, 1023, 1.0f, 0); /* channel fader up */
        send_rx_key_fl(0x5019, OP_ROTATE, ch, 512, 0.5f, 0);  /* trim center */
        send_rx_key_fl(0x501a, OP_ROTATE, ch, 512, 0.5f, 0);  /* HI */
        send_rx_key_fl(0x501b, OP_ROTATE, ch, 512, 0.5f, 0);  /* MID */
        send_rx_key_fl(0x501c, OP_ROTATE, ch, 512, 0.5f, 0);  /* LOW */
    }
    send_rx_key_fl(0x6017, OP_ROTATE, 1, 512, 0.5f, 0);       /* crossfader center */
}

/* ---- PanelComPeer "UiMain" message-pump thread ----
 * The uif::MsgManager that IKeyManager::sendKey posts to is the MsgManager
 * sub-object of the PanelComPeerLinux singleton (mainMsgManager @ 0x268612c).
 * Its queue is drained ONLY by PanelComPeerLinux::run() (thread name "UiMain"):
 * it loops takeOutMessage() -> MsgManager::handleMessage().  That thread is
 * started by PanelComPeerLinux::openDevice(); on the phone the subucom open
 * path never runs, so no UiMain thread exists and every injected key sits in
 * the queue forever (IKeyManager::onKey is never reached).  Start it here.
 */
#define PANEL_PEER_INSTANCE_GLOBAL 0x26870b0UL  /* ui::PanelComPeerLinux::instance */
#define PANEL_PEER_NUM_DEVICES_OFF 0x94         /* +0x94 = nDevices */
#define JUCE_THREAD_OFF            4            /* juce::Thread subobject at +4 */
#define FN_THREAD_IS_RUNNING       0x3b6360UL   /* juce::Thread::isThreadRunning() const */
#define FN_THREAD_START            0x3b6674UL   /* juce::Thread::startThread(int) */
#define FN_OPEN_DEVICE             0x366804UL   /* ui::PanelComPeerLinux::openDevice(int) */

static int ui_pump_running(void)
{
    void *inst = *(void **)PANEL_PEER_INSTANCE_GLOBAL;
    if (!inst)
        return 0;
    return ((int (*)(void *))FN_THREAD_IS_RUNNING)((char *)inst + JUCE_THREAD_OFF);
}

/* PanelComPeerLinux::openDevice() is NOT idempotent: calling it again on an
 * already-initialised instance re-runs the epoll/device setup and the UiMain
 * thread then dereferences NULL+0x10 (SIGSEGV).  The startup wait loops below
 * used to call this up to 150 times in a row, which crash-looped rbp on a
 * loaded system.  Open the device at most once per "pump not running" episode
 * and re-arm only after the pump is seen running again. */
static int ui_pump_ensure(void)
{
    static int opened = 0;
    void *inst;
    int n;

    if (ui_pump_running()) {
        opened = 0;             /* re-arm for a future restart */
        return 1;
    }
    inst = *(void **)PANEL_PEER_INSTANCE_GLOBAL;
    if (!inst)
        return 0;
    n = *(int *)((char *)inst + PANEL_PEER_NUM_DEVICES_OFF);
    if (n <= 0)
        return 0;   /* run() returns immediately while nDevices <= 0 */

    if (__sync_bool_compare_and_swap(&opened, 0, 1)) {
        klog_str("keyshim: UiMain pump not running -> openDevice(0)\n");
        ((int (*)(void *, int))FN_OPEN_DEVICE)(inst, 0);

        if (!ui_pump_running()) {
            klog_str("keyshim: openDevice did not start it -> startThread\n");
            ((void (*)(void *, int))FN_THREAD_START)((char *)inst + JUCE_THREAD_OFF, 5);
        }
    }
    if (ui_pump_running()) {
        opened = 0;
        klog_str("keyshim: UiMain pump started\n");
        return 1;
    }
    return 0;
}

/* ---- clear the stale "USB Error / Remove Device" browse caution ----
 * TotalCnt_CautionTASK sets [0x05a191fc]=0x12d (caution 0xc021) from early USB
 * initialisation.  While non-zero the UI disables browse/touch handling.
 * On the phone no USB mount ever runs the
 * code that clears it, so clear it ourselves (same as knobshim2's usb_auto). */
#define BROWSE_CAUTION_ID 0x05a191fcUL

static void clear_browse_caution(void)
{
    volatile unsigned int *p = (volatile unsigned int *)BROWSE_CAUTION_ID;
    if (*p != 0)
        *p = 0;
}

/* ---- FIFO input ---- */
#define KEY_FIFO  "/tmp/rb-keys.fifo"
#define CTRL_FIFO "/tmp/rb-ctrl.fifo"

struct key_ev {
    int32_t key;
    int32_t ch;
    int32_t down;
};

/* controller event: op is one of OP_PRESS/OP_RELEASE/OP_ROTATE/OP_VALUE.
 *   op 4 (ROTATE): param = relative delta, f = speed, l = position/counter
 *   op 5 (VALUE) : param = 10-bit absolute, f = normalised value
 * Same payload layout as the sendKey vtable call, so it can be forwarded
 * verbatim.  Must stay 24 bytes and fixed-size (a FIFO has no framing). */
struct ctrl_ev {
    int32_t key;
    int32_t ch;
    int32_t op;
    int32_t param;
    float   f;
    int32_t l;
};

/* Set once a real control surface starts feeding /tmp/rb-ctrl.fifo: from then
 * on the synthetic mixer defaults are no longer re-applied every 10 s (they
 * would snap the physical faders/EQ back). */
static volatile int ctrl_surface_active = 0;

static void *key_thread(void *arg)
{
    struct pollfd pfd;
    int fd = -1;
    (void)arg;

    klog_str("keyshim: thread started\n");

    for (int i = 0; i < 300; i++) {
        if (get_key_manager())
            break;
        usleep(100000);
    }
    if (!get_key_manager()) {
        klog_str("keyshim: key manager never ready\n");
        return NULL;
    }
    klog_hex("keyshim: key manager ready km=", (unsigned)(uintptr_t)get_key_manager());

    /* Dismiss the stale USB-error caution early so browse/touch isn't gated. */
    clear_browse_caution();

    /* Wait for the panel devices to be registered, then make sure the UiMain
     * message-pump thread exists (it is what dispatches posted keys). */
    for (int i = 0; i < 150 && !ui_pump_running(); i++) {
        ui_pump_ensure();
        usleep(100000);
    }

    /* Unmute the mixer: faders default to 0 on a device with no subucom.  Send
     * after the pump exists so the messages are actually dispatched. */
    mixer_defaults();
    klog_str("keyshim: mixer defaults sent (ch1->deck1, ch2->deck2, faders up, "
             "trims/EQ/crossfader center)\n");

    for (int i = 0; i < 100; i++) {
        fd = real_open(KEY_FIFO, O_RDWR | O_NONBLOCK);
        if (fd >= 0)
            break;
        usleep(100000);
    }
    if (fd < 0) {
        klog_str("keyshim: fifo open failed\n");
        return NULL;
    }
    klog_str("keyshim: fifo open ok\n");

    pfd.fd = fd;
    pfd.events = POLLIN;

    /* The FIFO record is 12 bytes, but a writer may deliver it in several
     * write()s (this shim's own /tmp injectors do).  Accumulate across reads
     * instead of discarding partial data. */
    unsigned char rec[sizeof(struct key_ev)];
    size_t rec_len = 0;

    time_t last_mix = time(0);
    for (;;) {
        int pr = poll(&pfd, 1, 500);
        clear_browse_caution();   /* keep the USB-error popup dismissed */
        /* The mixer objects are created well after startup, so the first send
         * is often lost; keep re-applying them (idempotent, nothing physical
         * can change them on the phone). */
        time_t now = time(0);
        if (!ctrl_surface_active && now - last_mix >= 10) {
            mixer_defaults();
            last_mix = now;
        }
        if (pr <= 0)
            continue;
        ssize_t n = real_read(fd, rec + rec_len, sizeof(rec) - rec_len);
        if (n > 0) {
            rec_len += (size_t)n;
            if (rec_len == sizeof(rec)) {
                struct key_ev ev;
                memcpy(&ev, rec, sizeof(ev));
                rec_len = 0;

                if (ev.key == 0x7f01) {   /* host trigger: re-send mixer defaults */
                    mixer_defaults();
                    continue;
                }
                if (ev.key == 0x7f02) {   /* host trigger: rotate browse knob */
                    send_rx_key_fl(0x420c, OP_ROTATE, 1, ev.down, 0.0f, 0);
                    continue;
                }
                ui_pump_ensure();   /* restart pump if it ever exits */
                if (klog_is_verbose()) {
                    klog_dec("keyshim: got key ch=", ev.ch);
                    klog_dec("         down=", ev.down);
                }
                send_rx_key(ev.key, ev.down ? OP_PRESS : OP_RELEASE, ev.ch);
            }
        } else if (n < 0 && errno != EAGAIN && errno != EINTR) {
            break;
        }
    }
    return NULL;
}

/* ====================================================================
 * The player's own state, for the controller's lamps
 * ====================================================================
 *
 * rbp works out every lamp on the RX3's panel - PLAY blinking while paused,
 * SYNC, the hot cue pads, BEAT FX, the level meters - and sends the result
 * down the panel link to microcontrollers that are not here.  flx4-bridge
 * used to GUESS all of it from the buttons it had seen pressed, which is why
 * the lamps were wrong as soon as anything happened that it had not caused:
 * a track loaded from the touchscreen, a loop ending, a hot cue that was
 * already stored.
 *
 * This reads the same state rbp reads, from inside rbp, and publishes it to
 * RB_STATE_PATH for the bridge.  Every address below comes from the SC Live 4
 * port (rblive4, knobshim2.c), where it is live-verified against this same
 * XDJ-RX3 v1.20 rbp - the build whose UI_OBJ_MGR_GLOBAL this file already
 * depends on.  Set RB_ENGINE_STATE=0 to turn all of it off.
 */
#include <sys/mman.h>
#include "rb_state.h"
#include <stdio.h>

/* djengine PlayEngine: the object DjEngineIF::isPlaying() etc. delegate to */
#define PLAYENGINE_GLOBAL  0x011497d0UL
#define PE_ISPLAYING       0x5d880UL
#define PE_ISLOADED        0x5d3b0UL
#define PE_ISSYNCON        0x5ff28UL
#define PE_ISMASTERTEMPO   0x5dfa8UL
#define PE_ISVINYLMODE     0x5e260UL
#define PE_ISSLIPMODEON    0x5fc60UL
#define PE_ISLOOPING       0x5eadcUL
#define PE_ISCANRELOOP     0x5eab0UL
#define PE_ISAUTOBEATLOOP  0x5ef74UL

/* uif::LedStat: IUiObjManager::getLedManager() is *(*(0x026867c0) + 104),
 * and the table sits at LedManager+0x30:
 *   +4 u16 count, +8 Led* array; each Led is 0x2c bytes,
 *   { +0 u32 id, +4 u32 channel, +16 u32 state, +40..42 rgb } */
#define LEDMGR_HOLDER_GLOBAL 0x026867c0UL
#define LEDMGR_OFF           104
#define LEDSTAT_OFF          0x30
#define LED_ENTRY_SIZE       0x2c
#define LED_TABLE_MAX        256

#define LEDSTAT_PLAY   49      /* per deck: 1 playing, 2 paused (blink)    */
#define LEDSTAT_SYNC   4       /* per deck: 1 locked, 2 synced but nudged  */
#define LEDSTAT_PAD0   18      /* per deck: the eight pads, 18..25         */
#define LEDSTAT_BFX    48      /* channel 0: BEAT FX ON/OFF                */

/* djengine MixerEngine: headphone cue per channel, and master cue */
#define ME_SINGLETON      0x011493c0UL
#define ME_SET_CUE        0x000575a0UL
#define ME_GET_CUE        0x000575e8UL
#define ME_GET_MASTER_CUE 0x0005767cUL

/* ui::Mixer::MonoLvMeter::getLedValue(unsigned char): rbp computes every
 * level meter's lit segments in this one function, called from
 * ui::Mixer::checkLedStat once each for master / ch1 / ch2 - the return
 * address says which. */
#define ADDR_GETLEDVALUE   0x2d07a8UL
#define RET_MASTER         0x2d0940UL
#define RET_CH1            0x2d0a44UL
#define RET_CH2            0x2d0a8cUL
#define PROLOGUE_GETLED    0xe92d4070u   /* push {r4, r5, r6, lr} */

static int engine_state_enabled(void)
{
    static int enabled = -1;
    if (enabled < 0) {
        const char *v = getenv("RB_ENGINE_STATE");
        enabled = !(v && *v == '0');
    }
    return enabled && rbp_checked > 0;
}

/* ---- headphone cue ---- */
static void *mixer_engine(void)
{
    return *(void **)ME_SINGLETON;
}

/* MixerEngine keeps its channels in a vector at +12/+16; each channel holds
 * its djengine::EnMixerInput at +20, which is what the cue calls take. */
static int me_channel_input(int idx)
{
    void *engine = mixer_engine();
    void **begin, **end;
    int i = 0;
    if (!engine)
        return -1;
    begin = *(void ***)((char *)engine + 12);
    end   = *(void ***)((char *)engine + 16);
    if (!begin || !end || begin > end || end - begin > 16)
        return -1;
    for (void **p = begin; p < end; p++) {
        if (!*p)
            continue;
        if (i == idx)
            return *(int *)((char *)*p + 20);
        i++;
    }
    return -1;
}

static int me_get_cue(int idx)
{
    int input = me_channel_input(idx);
    if (input < 0)
        return -1;
    return ((int (*)(void *, int))ME_GET_CUE)(mixer_engine(), input) != 0;
}

static void me_set_cue(int idx, int on)
{
    int input = me_channel_input(idx);
    if (input < 0)
        return;
    ((void (*)(void *, int, int))ME_SET_CUE)(mixer_engine(), input, on);
}

static int me_get_master_cue(void)
{
    void *engine = mixer_engine();
    if (!engine)
        return -1;
    return ((int (*)(void *))ME_GET_MASTER_CUE)(engine) != 0;
}

/* Commands that are not panel keys.  The RX3 has no keycode for a channel's
 * headphone CUE - its PFL buttons go straight to the mixer - so the bridge
 * asks for one here.  Returns 1 if the record was one of these. */
/* set by the state thread once reading the mixer has worked, and never while
 * that probe is switched off - the toggle calls into the same object */
static volatile int g_mixer_usable = 0;

static int engine_command(const struct ctrl_ev *ev)
{
    if (ev->key != RB_CMD_PFL_TOGGLE)
        return 0;
    if (ev->op == OP_PRESS && ev->ch >= 1 && ev->ch <= 2 &&
        engine_state_enabled() && g_mixer_usable) {
        int cur = me_get_cue(ev->ch - 1);
        if (cur >= 0)
            me_set_cue(ev->ch - 1, !cur);
        klog_dec("keyshim: headphone cue toggled on channel ", ev->ch);
    }
    return 1;
}

/* ---- the LED table ---- */
__attribute__((noinline))
static unsigned char *ledstat_table(unsigned int *count)
{
    void *holder = *(void **)LEDMGR_HOLDER_GLOBAL;
    unsigned char *ls, *arr;
    void *mgr;
    if (!holder)
        return NULL;
    mgr = *(void **)((char *)holder + LEDMGR_OFF);
    if (!mgr)
        return NULL;
    ls = (unsigned char *)mgr + LEDSTAT_OFF;
    *count = *(unsigned short *)(ls + 4);
    arr = *(unsigned char **)(ls + 8);
    if (!arr || *count == 0 || *count > LED_TABLE_MAX)
        return NULL;
    return arr;
}

static unsigned char *ledstat_entry(unsigned char *arr, unsigned int count,
                                    unsigned int id, unsigned int ch)
{
    for (unsigned int i = 0; i < count; i++) {
        unsigned char *e = arr + LED_ENTRY_SIZE * i;
        if (*(unsigned int *)e == id && *(unsigned int *)(e + 4) == ch)
            return e;
    }
    return NULL;
}

static uint8_t ledstat_state(unsigned char *arr, unsigned int count,
                             unsigned int id, unsigned int ch)
{
    unsigned char *e = arr ? ledstat_entry(arr, count, id, ch) : NULL;
    unsigned int st;
    if (!e)
        return RBL_UNKNOWN;
    st = *(unsigned int *)(e + 16);
    return st > 3 ? RBL_ON : (uint8_t)st;
}

/* ---- rbp's own level meters ---- */
static volatile unsigned int g_meter_bits[3];      /* master, ch1, ch2 */
static volatile unsigned int g_meter_calls;
static unsigned int (*g_orig_getled)(void *, unsigned char);

unsigned int keyshim_getled_hook(void *self, unsigned char level);

unsigned int keyshim_getled_hook(void *self, unsigned char level)
{
    void *lr = __builtin_return_address(0);
    unsigned int v = g_orig_getled ? g_orig_getled(self, level) : 0;
    if (lr == (void *)RET_MASTER)
        g_meter_bits[0] = v;
    else if (lr == (void *)RET_CH1)
        g_meter_bits[1] = v;
    else if (lr == (void *)RET_CH2)
        g_meter_bits[2] = v;
    g_meter_calls++;
    return v;
}

static void flush_icache(void *start, void *end)
{
#ifdef __arm__
    /* __ARM_NR_cacheflush, without dragging libgcc's __clear_cache in */
    (void)syscall(0x0f0002, start, end, 0);
#else
    (void)start; (void)end;
#endif
}

/* Patch getLedValue's first two instructions with a jump to the hook, and
 * run them from a trampoline on the way back.  Refuses unless the prologue is
 * exactly the one this was verified against. */
static int install_meter_hook(void)
{
#ifdef __arm__
    unsigned char *p = (unsigned char *)ADDR_GETLEDVALUE;
    unsigned char *tramp;
    unsigned long page;
    const unsigned long pgsz = 4096;
    const char *v = getenv("RB_METER_HOOK");

    if (v && *v == '0')
        return 0;
    if (*(volatile uint32_t *)p != PROLOGUE_GETLED) {
        klog_hex("keyshim: meter hook: unexpected prologue ",
                 *(volatile uint32_t *)p);
        return 0;
    }
    tramp = mmap(NULL, pgsz, PROT_READ | PROT_WRITE | PROT_EXEC,
                 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (tramp == MAP_FAILED)
        return 0;
    memcpy(tramp, p, 8);                          /* the displaced two */
    *(uint32_t *)(tramp + 8)  = 0xE51FF004u;       /* ldr pc, [pc, #-4] */
    *(uint32_t *)(tramp + 12) = (uint32_t)(uintptr_t)(p + 8);
    flush_icache(tramp, tramp + 16);
    g_orig_getled = (unsigned int (*)(void *, unsigned char))(void *)tramp;

    page = (unsigned long)p & ~(pgsz - 1);
    if (mprotect((void *)page, pgsz, PROT_READ | PROT_WRITE | PROT_EXEC) != 0) {
        g_orig_getled = NULL;
        return 0;
    }
    *(uint32_t *)(p + 4) = (uint32_t)(uintptr_t)&keyshim_getled_hook;
    *(uint32_t *)(p + 0) = 0xE51FF004u;
    mprotect((void *)page, pgsz, PROT_READ | PROT_EXEC);
    flush_icache(p, p + 8);
    klog_str("keyshim: meter hook installed (RB_METER_HOOK=0 turns it off)\n");
    return 1;
#else
    return 0;
#endif
}

static int popcount32(unsigned int v)
{
    int n = 0;
    while (v) { n += (int)(v & 1u); v >>= 1; }
    return n;
}

static uint8_t meter_segments(unsigned int bits)
{
    int n = popcount32(bits);
    return (uint8_t)(n > RB_METER_SEGMENTS ? RB_METER_SEGMENTS : n);
}

/* ---- the deck's pad bank ----
 * ui::PlayerInnards holds the pad mode rbp itself reads for the pads: +0x74
 * is the bank (0..3), +0x7a the bank's second function (the byte a second
 * press of the same bank key flips - triplet loops, release FX).  There is no
 * getter for the object, so it is found the way the SC Live 4 port finds it:
 * by its vptr (vtable for ui::PlayerInnards + 8) in rbp's writable memory,
 * checked against its channel byte (2/3) and its engine pointer. */
#define PVTABLE_PLAYERINNARDS 0x004d1960UL
#define PLINN_CHAN_OFF        0x26
#define PLINN_MODE_OFF        0x74
#define PLINN_SUB_OFF         0x7a

static void *g_plinn[2];

static int plinn_valid(void *p, int deck)
{
    unsigned char chan, mode;
    unsigned int eng;
    if (!p || *(volatile unsigned int *)p != PVTABLE_PLAYERINNARDS)
        return 0;
    chan = *(volatile unsigned char *)((char *)p + PLINN_CHAN_OFF);
    mode = *(volatile unsigned char *)((char *)p + PLINN_MODE_OFF);
    eng  = *(volatile unsigned int *)((char *)p + 0x30);
    return chan == 2 + deck && mode <= 3 && eng >= 0x80000000u;
}

static void plinn_scan(void)
{
    char line[256];
    FILE *f = fopen("/proc/self/maps", "r");
    if (!f)
        return;
    while (fgets(line, sizeof(line), f) && !(g_plinn[0] && g_plinn[1])) {
        unsigned long s = 0, e = 0;
        char perms[8];
        if (sscanf(line, "%lx-%lx %7s", &s, &e, perms) != 3)
            continue;
        if (perms[0] != 'r' || perms[1] != 'w' || e <= s ||
            e - s > (512UL << 20))
            continue;
        for (unsigned long a = (s + 3) & ~3UL; a + 0x90 <= e; a += 4) {
            if (*(volatile unsigned int *)a != PVTABLE_PLAYERINNARDS)
                continue;
            for (int d = 0; d < 2; d++)
                if (!g_plinn[d] && plinn_valid((void *)a, d)) {
                    g_plinn[d] = (void *)a;
                    klog_hex("keyshim: PlayerInnards found at ",
                             (unsigned)a);
                }
        }
    }
    fclose(f);
}

/* ---- the Beat FX the player is on ----
 * A static getter returns the engine's internal effect type; the selector
 * the overlay drives is a 14-position switch whose positions map to types
 * (docs/06-controller.md), so it is published as the position. */
#define ADDR_GET_BFX_TYPE 0x4d514UL
static const signed char bfx_type_to_pos[15] = {
    -1, 5, 9, 10, 6, 1, 0, 3, 12, 7, 8, 13, 11, 2, 4
};

static uint8_t bfx_position(void)
{
    int t = ((int (*)(void *))ADDR_GET_BFX_TYPE)(NULL);
    if (t < 0 || t > 14 || bfx_type_to_pos[t] < 0)
        return 0xff;
    return (uint8_t)bfx_type_to_pos[t];
}

/* ---- the publisher ---- */
typedef int (*pe_query)(void *, int);

static int pe_ask(void *pe, unsigned long fn, int deck)
{
    return ((pe_query)fn)(pe, deck) != 0;
}

static void publish_state(const struct rb_state *st)
{
    int fd = syscall(SYS_openat, AT_FDCWD, RB_STATE_PATH ".tmp",
                     O_WRONLY | O_CREAT | O_TRUNC, 0666);
    if (fd < 0)
        return;
    (void)syscall(SYS_fchmod, fd, 0666);
    (void)syscall(SYS_write, fd, st, sizeof(*st));
    real_close(fd);
    /* renameat: plain rename(2) has no syscall number on 64-bit ARM, and
     * the syntax check runs there */
    (void)syscall(SYS_renameat, AT_FDCWD, RB_STATE_PATH ".tmp",
                  AT_FDCWD, RB_STATE_PATH);
}

/* ---- a fault in a probe turns the probe off, not the player ----
 *
 * Every probe below reads rbp's memory or calls into rbp at an address that
 * is only right for the build it was found in.  If one is wrong, that must
 * cost a lamp, not the player: each tick runs with a SIGSEGV/SIGBUS handler
 * that, for a fault in THIS thread, jumps back to the top of the tick and
 * switches the probe that faulted off for good.  A fault in any other thread
 * goes to whatever handler rbp had, exactly as before. */
#include <setjmp.h>
#include <signal.h>

enum {
    P_ENGINE  = 1 << 0,     /* PlayEngine transport flags     */
    P_LEDSTAT = 1 << 1,     /* uif::LedStat                   */
    P_MIXER   = 1 << 2,     /* headphone cue                  */
    P_PADBANK = 1 << 3,     /* ui::PlayerInnards memory scan  */
    P_BFX     = 1 << 4,     /* getBeatEffectType()            */
};
static const char *probe_name(int probe)
{
    switch (probe) {
    case P_ENGINE:  return "PlayEngine";
    case P_LEDSTAT: return "LedStat";
    case P_MIXER:   return "MixerEngine";
    case P_PADBANK: return "PlayerInnards";
    case P_BFX:     return "getBeatEffectType";
    }
    return "?";
}

static volatile int g_probe_off;        /* probes switched off */
static volatile int g_probe_ok;         /* probes that have worked once */
static volatile int g_probe_step;       /* the probe running now */
static sigjmp_buf g_probe_jmp;
static volatile int g_probe_armed;
static pthread_t g_probe_thread;
static struct sigaction g_prev_segv, g_prev_bus;

static void probe_fault(int sig, siginfo_t *si, void *uc)
{
    if (g_probe_armed && pthread_equal(pthread_self(), g_probe_thread)) {
        g_probe_armed = 0;
        siglongjmp(g_probe_jmp, sig);
    }
    /* not ours: hand it back and let the instruction fault again there */
    (void)si; (void)uc;
    sigaction(sig, sig == SIGBUS ? &g_prev_bus : &g_prev_segv, NULL);
}

static void probe_guard_on(void)
{
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_sigaction = probe_fault;
    sa.sa_flags = SA_SIGINFO | SA_NODEFER;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGSEGV, &sa, &g_prev_segv);
    sigaction(SIGBUS, &sa, &g_prev_bus);
    g_probe_armed = 1;
}

static void probe_guard_off(void)
{
    g_probe_armed = 0;
    sigaction(SIGSEGV, &g_prev_segv, NULL);
    sigaction(SIGBUS, &g_prev_bus, NULL);
}

/* run this probe? (and remember which one is running) */
static int probe(int which)
{
    if (g_probe_off & which)
        return 0;
    g_probe_step = which;
    /* and no read of the probe may be moved above that store: the compiler
     * is free to hoist a plain load over a volatile one, and did - a fault
     * was then blamed on no probe at all, and retried forever */
    __asm__ __volatile__("" ::: "memory");
    return 1;
}

static void probe_worked(int which)
{
    if (g_probe_ok & which)
        return;
    g_probe_ok |= which;
    klog_str("keyshim: probe ok: ");
    klog_str(probe_name(which));
    klog_str("\n");
}

static int env_on(const char *name, int dflt)
{
    const char *v = getenv(name);
    if (!v || !*v)
        return dflt;
    return *v != '0';
}

/* Installed from the constructor, before rbp has started a single thread:
 * the patch is two words, and a thread running the function while they
 * change would execute half of each. */
static int g_hooked = 0;

/* One pass over the probes: fills and publishes the record.  Everything it
 * keeps between passes is here, at file scope, where the jump back from a
 * faulting probe cannot clobber it. */
static struct rb_state st, sent;
static unsigned int last_calls = 0;
static int quiet = 0, scans = 0, scan_wait = 60, idle = 0, said_meter = 0;

static void state_tick(void)
{
    /* volatile: their values are not relied on after a fault's jump back,
     * but that way the compiler knows it too */
    void *volatile pe = NULL;
    unsigned int count = 0;
    unsigned char *volatile leds = NULL;
    volatile uint32_t flags = 0;

    probe_guard_on();
    if (sigsetjmp(g_probe_jmp, 1)) {
        probe_guard_off();
        g_probe_off |= g_probe_step;
        if (g_probe_step == P_MIXER)
            g_mixer_usable = 0;
        klog_str("keyshim: probe FAULTED and is now off: ");
        klog_str(probe_name(g_probe_step));
        klog_str("\n");
        return;
    }

    if (probe(P_ENGINE))
        pe = *(void **)PLAYENGINE_GLOBAL;
    if (probe(P_LEDSTAT))
        leds = ledstat_table(&count);

    for (int d = 0; d < 2; d++) {
        struct rb_state_deck *k = &st.deck[d];
        if (pe && probe(P_ENGINE)) {
            k->loaded       = (uint8_t)pe_ask(pe, PE_ISLOADED, d);
            k->playing      = (uint8_t)pe_ask(pe, PE_ISPLAYING, d);
            k->sync_on      = (uint8_t)pe_ask(pe, PE_ISSYNCON, d);
            k->looping      = (uint8_t)pe_ask(pe, PE_ISLOOPING, d);
            k->can_reloop   = (uint8_t)pe_ask(pe, PE_ISCANRELOOP, d);
            k->auto_loop    = (uint8_t)pe_ask(pe, PE_ISAUTOBEATLOOP, d);
            k->slip         = (uint8_t)pe_ask(pe, PE_ISSLIPMODEON, d);
            k->vinyl        = (uint8_t)pe_ask(pe, PE_ISVINYLMODE, d);
            k->master_tempo = (uint8_t)pe_ask(pe, PE_ISMASTERTEMPO, d);
            probe_worked(P_ENGINE);
        }
        k->play_led = k->sync_led = RBL_UNKNOWN;
        memset(k->pad_led, RBL_UNKNOWN, sizeof(k->pad_led));
        if (leds && probe(P_LEDSTAT)) {
            k->play_led = ledstat_state(leds, count, LEDSTAT_PLAY,
                                        (unsigned)d + 1);
            k->sync_led = ledstat_state(leds, count, LEDSTAT_SYNC,
                                        (unsigned)d + 1);
            for (int p = 0; p < 8; p++) {
                unsigned char *e = ledstat_entry(leds, count,
                                                 LEDSTAT_PAD0 + (unsigned)p,
                                                 (unsigned)d + 1);
                k->pad_led[p] = ledstat_state(leds, count,
                                              LEDSTAT_PAD0 + (unsigned)p,
                                              (unsigned)d + 1);
                if (e) {
                    k->pad_rgb[p][0] = e[40];
                    k->pad_rgb[p][1] = e[41];
                    k->pad_rgb[p][2] = e[42];
                }
            }
            probe_worked(P_LEDSTAT);
        }
        k->pfl = 0xff;
        if (probe(P_MIXER)) {
            int cue = me_get_cue(d);
            if (cue >= 0) {
                k->pfl = (uint8_t)cue;
                flags |= RBS_MIXER;
                probe_worked(P_MIXER);
                g_mixer_usable = 1;
            }
        }
        k->meter = meter_segments(g_meter_bits[1 + d]);
        k->pad_mode = k->pad_sub = 0xff;
        if (probe(P_PADBANK)) {
            if (g_plinn[d] && !plinn_valid(g_plinn[d], d))
                g_plinn[d] = NULL;               /* freed or moved */
            if (g_plinn[d]) {
                k->pad_mode = *(volatile unsigned char *)
                              ((char *)g_plinn[d] + PLINN_MODE_OFF);
                k->pad_sub  = *(volatile unsigned char *)
                              ((char *)g_plinn[d] + PLINN_SUB_OFF);
                probe_worked(P_PADBANK);
            }
        }
    }
    /* The scan walks rbp's writable memory, so it is not done on every
     * tick: every two seconds until both decks are found, a dozen tries at
     * most - after that the bridge models the bank. */
    if (pe && (!g_plinn[0] || !g_plinn[1]) && scans < 12 &&
        ++scan_wait >= 80 && probe(P_PADBANK)) {
        scan_wait = 0;
        scans++;
        plinn_scan();
    }
    if (pe)
        flags |= RBS_ENGINE;
    st.bfx_led = RBL_UNKNOWN;
    if (leds && probe(P_LEDSTAT)) {
        flags |= RBS_LEDSTAT;
        st.bfx_led = ledstat_state(leds, count, LEDSTAT_BFX, 0);
    }
    st.master_cue = 0xff;
    if (probe(P_MIXER)) {
        int mc = me_get_master_cue();
        st.master_cue = mc < 0 ? 0xff : (uint8_t)mc;
    }
    st.bfx_pos = 0xff;
    if (pe && probe(P_BFX)) {
        st.bfx_pos = bfx_position();
        probe_worked(P_BFX);
    }
    probe_guard_off();

    st.master_meter = meter_segments(g_meter_bits[0]);
    /* The meters are only real while rbp keeps calling getLedValue.  If it
     * stops, say so rather than publish a frozen level. */
    if (g_hooked) {
        if (g_meter_calls != last_calls) {
            last_calls = g_meter_calls;
            quiet = 0;
            if (!said_meter) {
                said_meter = 1;
                klog_str("keyshim: meter hook: rbp is calling it\n");
            }
        } else if (++quiet > 20) {
            st.deck[0].meter = st.deck[1].meter = 0;
            st.master_meter = 0;
        }
        if (quiet <= 20)
            flags |= RBS_METERS;
    }

    st.flags = flags;
    /* Written when something changed, and five times a second anyway so the
     * bridge can tell a quiet player from a dead one.  /tmp is on the SD card
     * on most Pis; there is no point rewriting the same hundred bytes forty
     * times a second. */
    st.seq = sent.seq;
    st.seq_end = sent.seq_end;
    if (memcmp(&st, &sent, sizeof(st)) != 0 || ++idle >= 8) {
        idle = 0;
        st.seq++;
        st.seq_end = st.seq;
        publish_state(&st);
        sent = st;
    }
}

static void state_begin(void)
{
    /* The memory scan and the Beat FX getter are the least proven of the
     * probes (the SC Live 4 port only ran them on demand): opt in. */
    if (!env_on("RB_STATE_PADBANK", 0))
        g_probe_off |= P_PADBANK;
    if (!env_on("RB_STATE_BFX", 0))
        g_probe_off |= P_BFX;
    g_probe_thread = pthread_self();
    memset(&sent, 0, sizeof(sent));
    memset(&st, 0, sizeof(st));
    st.magic = RB_STATE_MAGIC;
    st.version = RB_STATE_VERSION;
}

static void *state_thread(void *arg)
{
    (void)arg;
    for (int i = 0; i < 300 && !get_key_manager(); i++)
        usleep(100000);
    if (!engine_state_enabled()) {
        klog_str("keyshim: engine state publisher off\n");
        return NULL;
    }
    /* let the player finish building its engine and UI before reading them */
    for (int i = 0; i < 150 && !ui_pump_running(); i++)
        usleep(100000);
    sleep(3);
    state_begin();
    klog_str("keyshim: engine state publisher up\n");
    for (;;) {
        state_tick();
        usleep(25000);                          /* 40 Hz */
    }
    return NULL;
}

/* ---- controller FIFO (/tmp/rb-ctrl.fifo) ----
 * 24-byte records, forwarded straight to sendKey(key, op, ch, param, f, l).
 * This is what the DDJ-FLX4 MIDI bridge writes, so a real controller can drive
 * the engine's faders, EQ, jog wheels and relative knobs. */
static void *ctrl_thread(void *arg)
{
    struct pollfd pfd;
    int fd = -1;
    (void)arg;

    klog_str("keyshim: ctrl thread started\n");

    for (int i = 0; i < 300; i++) {
        if (get_key_manager())
            break;
        usleep(100000);
    }
    if (!get_key_manager()) {
        klog_str("keyshim: ctrl thread: key manager never ready\n");
        return NULL;
    }

    for (int i = 0; i < 150 && !ui_pump_running(); i++) {
        ui_pump_ensure();
        usleep(100000);
    }

    for (int i = 0; i < 100; i++) {
        fd = real_open(CTRL_FIFO, O_RDWR | O_NONBLOCK);
        if (fd >= 0)
            break;
        usleep(100000);
    }
    if (fd < 0) {
        klog_str("keyshim: ctrl fifo open failed\n");
        return NULL;
    }
    klog_str("keyshim: ctrl fifo open ok\n");

    pfd.fd = fd;
    pfd.events = POLLIN;

    /* Read everything that is waiting, not one record per wakeup: a jog turn
     * and a fader move arrive together, and taking them one poll() at a time
     * is what lets a burst queue up behind itself. */
    unsigned char buf[sizeof(struct ctrl_ev) * 64];
    size_t have = 0;

    for (;;) {
        int pr = poll(&pfd, 1, 1000);
        clear_browse_caution();
        if (pr <= 0)
            continue;
        ssize_t n = real_read(fd, buf + have, sizeof(buf) - have);
        if (n > 0) {
            size_t off = 0;
            have += (size_t)n;
            ctrl_surface_active = 1;
            ui_pump_ensure();
            while (have - off >= sizeof(struct ctrl_ev)) {
                struct ctrl_ev ev;
                memcpy(&ev, buf + off, sizeof(ev));
                off += sizeof(ev);
                if (klog_is_verbose()) {
                    klog_hex("keyshim: ctrl key=", (unsigned)ev.key);
                    klog_dec("         op=", ev.op);
                    klog_dec("         ch=", ev.ch);
                    klog_dec("         param=", ev.param);
                }
                if (engine_command(&ev))
                    continue;
                send_rx_key_fl(ev.key, ev.op, ev.ch, ev.param, ev.f, ev.l);
            }
            /* keep a partial record for the next read */
            memmove(buf, buf + off, have - off);
            have -= off;
        } else if (n < 0 && errno != EAGAIN && errno != EINTR) {
            break;
        }
    }
    return NULL;
}

#ifndef KEYSHIM_NO_CONSTRUCTOR
__attribute__((constructor))
#endif
static void keyshim_init(void)
{
    pthread_t tid;
    if (!is_rbp_process())
        return;                 /* the loader and helpers get nothing */
    rbp_checked = 1;
    klog_dec("keyshim: loaded into rbp, pid ", (int)getpid());
    if (engine_state_enabled())
        g_hooked = install_meter_hook();
    if (pthread_create(&tid, NULL, key_thread, NULL) == 0)
        pthread_detach(tid);
    if (pthread_create(&tid, NULL, ctrl_thread, NULL) == 0)
        pthread_detach(tid);
    if (pthread_create(&tid, NULL, state_thread, NULL) == 0)
        pthread_detach(tid);
}
