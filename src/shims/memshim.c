/* memshim.c - LD_PRELOAD: Raspberry Pi 5 fixups that must run before rbp's
 * own code, plus the touchscreen bridge.
 *
 * 1) /dev/mem denial + broken mmap redirect
 *    rbp's user_space_rtc_init() does:
 *        fd = open("/dev/mem", O_RDONLY);
 *        p  = mmap(NULL, 64, PROT_READ, MAP_SHARED, fd, 0x2098000);   // i.MX6 SRC
 *    On the Pi 5 (BCM2712) that physical address is not the i.MX6 SRC, so the
 *    read is garbage; rbp turns it into a bogus pointer and SIGSEGVs in a
 *    loop.  chmod 000 /dev/mem is useless (root has CAP_DAC_OVERRIDE: open()
 *    succeeded).  We deny the open here; rbp then falls back to
 *    mmap(..., MAP_SHARED, fd=-1, addr), which we redirect to private
 *    anonymous memory so sched_clock/v2_get_cycles read zero.
 *
 * 2) /dev/gpiodrv read + poll shim (formerly gpioshim.so)
 *    On the RX3 /dev/gpiodrv is a GPIO driver; reads return pin state.  Our
 *    stub is a regular file, so read() hits EOF and rbp's GpioManager busy-
 *    spins.  Return a constant 1 zero byte like gpioshim did.  Folded in here
 *    so there is a single open()/read() layer (two LD_PRELOADs both defining
 *    open() would shadow each other).
 *
 *    GpioManager also does poll(fd, POLLIN, -1) on /dev/gpiodrv.  A regular
 *    file is *always* poll-ready, so that poll returns instantly and two
 *    GpioManager threads spin at ~11k polls/s each.  We park them: never
 *    report the gpio fd ready, sleep for the requested (or 50 ms) time and
 *    return 0 (timeout), so the threads idle at ~20 wakeups/s total.
 *
 * 3) touchscreen bridge (/dev/tsc2007_2-0048)
 *    The XDJ-RX3 panel is a tsc2007 resistive touchscreen and rbp's
 *    TouchPanelComm thread read()s 6-byte records from that device.  With no
 *    touchscreen we answer "no touch" (all zeroes) at ~50 Hz, which is the
 *    default and what the Chromebit port did.
 *
 *    On the Pi 5 there *is* a touchscreen (a USB HID panel on the 22" monitor),
 *    so rbtouchd publishes the current contact in UI coordinates to
 *    RB_TOUCH_STATE (default /tmp/rb-touch.dat, 16 bytes LE:
 *    u32 seq, u32 down, u32 x, u32 y) and this shim converts it into the
 *    6-byte record rbp expects.  Enable with RB_TOUCH_NATIVE=1.
 *
 *    !! The byte layout of the RX3 record is NOT confirmed (we have no RX3 to
 *    sniff), so the layout is selectable with RB_TOUCH_FMT:
 *        fxy  u16 down, u16 x, u16 y          (default)
 *        xyf  u16 x, u16 y, u16 down
 *        xyp  u16 x, u16 y, u16 pressure      (pressure 0 or 4095)
 *        bxy  u8  down, u8 pad, u16 x, u16 y
 *    RB_TOUCH_SWAP=1 swaps x/y, RB_TOUCH_MAXX / RB_TOUCH_MAXY rescale the
 *    coordinates (default 1280x800, the RX3 panel size).  See docs/07-touch.md:
 *    the supported, always-working path is rbtouchd's zone mapping, which needs
 *    none of this.
 */
#define _GNU_SOURCE
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <unistd.h>
#include <stddef.h>
#include <stdarg.h>
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <time.h>
#include <poll.h>
#include <stdlib.h>
#include <stdint.h>
#include "abi213.h"   /* last: see the header */

#ifndef SYS_poll
#define SYS_poll __NR_poll
#endif

#ifndef SYS_mmap2
#define SYS_mmap2 __NR_mmap2
#endif
#ifndef AT_FDCWD
#define AT_FDCWD -100
#endif
#ifndef O_TMPFILE
#define O_TMPFILE 020000000
#endif

#define MAX_FDS 4096
static char is_gpio[MAX_FDS];
static char is_tsc[MAX_FDS];

static int is_dev_mem(const char *path)
{
    return path && strcmp(path, "/dev/mem") == 0;
}

static int is_gpiodrv(const char *path)
{
    size_t n;
    if (!path)
        return 0;
    n = strlen(path);
    if (n < 7)
        return 0;
    return strcmp(path + n - 7, "gpiodrv") == 0;
}

/* XDJ-RX3 custom tsc2007 touch device (we run with no touchscreen) */
static int is_tscdev(const char *path)
{
    return path && strstr(path, "tsc2007") != NULL;
}

/* ---- touchscreen state published by rbtouchd (see docs/07-touch.md) ---- */
struct rb_touch_state {
    uint32_t seq;
    uint32_t down;
    uint32_t x;
    uint32_t y;
};

enum { TFMT_FXY = 0, TFMT_XYF, TFMT_XYP, TFMT_BXY, TFMT_RX3 };

static int      touch_native = -1;      /* -1 = not resolved yet */
static int      touch_fmt    = TFMT_RX3;
static int      touch_invx   = 1;
static int      touch_swap   = 0;
static unsigned touch_maxx   = 1280;
static unsigned touch_maxy   = 800;
static const char *touch_path = "/tmp/rb-touch.dat";

static void touch_env_init(void)
{
    const char *s;

    if (touch_native >= 0)
        return;
    touch_native = 0;

    s = getenv("RB_TOUCH_NATIVE");
    if (s && *s && *s != '0')
        touch_native = 1;

    s = getenv("RB_TOUCH_STATE");
    if (s && *s)
        touch_path = s;

    s = getenv("RB_TOUCH_FMT");
    if (s && *s) {
        if (!strcmp(s, "xyf"))      touch_fmt = TFMT_XYF;
        else if (!strcmp(s, "xyp")) touch_fmt = TFMT_XYP;
        else if (!strcmp(s, "bxy")) touch_fmt = TFMT_BXY;
        else if (!strcmp(s, "fxy")) touch_fmt = TFMT_FXY;
        else                        touch_fmt = TFMT_RX3;
    }

    s = getenv("RB_TOUCH_INVX");
    if (s && *s)
        touch_invx = *s != '0';
    s = getenv("RB_TOUCH_SWAP");
    if (s && *s && *s != '0')
        touch_swap = 1;

    s = getenv("RB_TOUCH_MAXX");
    if (s && *s)
        touch_maxx = (unsigned)atoi(s);
    s = getenv("RB_TOUCH_MAXY");
    if (s && *s)
        touch_maxy = (unsigned)atoi(s);
    if (!touch_maxx) touch_maxx = 1280;
    if (!touch_maxy) touch_maxy = 800;
}

/* Read the current contact.  Returns 1 when the state file was readable. */
static int touch_read_state(struct rb_touch_state *out)
{
    int fd, n;

    fd = (int)syscall(SYS_openat, AT_FDCWD, touch_path, O_RDONLY, 0);
    if (fd < 0)
        return 0;
    n = (int)syscall(SYS_read, fd, out, sizeof(*out));
    (void)syscall(SYS_close, fd);
    return n == (int)sizeof(*out);
}

/* Build the 6-byte record rbp's TouchPanelComm thread expects. */
static void touch_pack(unsigned char *buf, const struct rb_touch_state *st)
{
    unsigned x = st->x, y = st->y, down = st->down ? 1 : 0;
    unsigned short a, b, c;

    if (touch_swap) { unsigned t = x; x = y; y = t; }
    if (x >= touch_maxx) x = touch_maxx - 1;
    if (y >= touch_maxy) y = touch_maxy - 1;

    memset(buf, 0, 6);
    switch (touch_fmt) {
    case TFMT_RX3:
        /* Verified on this rbp by the Prime GO / SC Live 4 ports:
         * { u8 flag, u8 0, u16 x, u16 y }, little-endian, in the 1280x800
         * UI - with X mirrored, because the firmware's commRxDataProc
         * computes calX = 1280 - rawX (its panel is wired invertX = 1).
         * That needs the identity calibration files the launcher installs
         * (TouchCalib_User.dat / TouchCalib_Factory.dat). */
        if (touch_invx)
            x = touch_maxx - 1 - x;
        buf[0] = (unsigned char)down;
        buf[2] = (unsigned char)(x & 0xff);
        buf[3] = (unsigned char)(x >> 8);
        buf[4] = (unsigned char)(y & 0xff);
        buf[5] = (unsigned char)(y >> 8);
        return;
    case TFMT_XYF:
        a = (unsigned short)x; b = (unsigned short)y; c = (unsigned short)down;
        break;
    case TFMT_XYP:
        a = (unsigned short)x; b = (unsigned short)y;
        c = (unsigned short)(down ? 4095 : 0);
        break;
    case TFMT_BXY:
        buf[0] = (unsigned char)down;
        buf[1] = 0;
        buf[2] = (unsigned char)(x & 0xff);
        buf[3] = (unsigned char)(x >> 8);
        buf[4] = (unsigned char)(y & 0xff);
        buf[5] = (unsigned char)(y >> 8);
        return;
    case TFMT_FXY:
    default:
        a = (unsigned short)down; b = (unsigned short)x; c = (unsigned short)y;
        break;
    }
    buf[0] = (unsigned char)(a & 0xff); buf[1] = (unsigned char)(a >> 8);
    buf[2] = (unsigned char)(b & 0xff); buf[3] = (unsigned char)(b >> 8);
    buf[4] = (unsigned char)(c & 0xff); buf[5] = (unsigned char)(c >> 8);
}

static int deny_mem(void)
{
    errno = EACCES;
    return -1;
}

static mode_t take_mode(int flags, va_list ap)
{
    mode_t mode = 0;
    if (flags & (O_CREAT | O_TMPFILE))
        mode = va_arg(ap, mode_t);
    return mode;
}

static int track(int fd, const char *path)
{
    if (fd >= 0 && fd < MAX_FDS) {
        is_gpio[fd] = is_gpiodrv(path);
        is_tsc[fd]  = is_tscdev(path);
    }
    return fd;
}

/* --- deny /dev/mem; track /dev/gpiodrv --- */
int open(const char *path, int flags, ...)
{
    va_list ap;
    mode_t mode;
    int fd;

    if (is_dev_mem(path))
        return deny_mem();

    va_start(ap, flags);
    mode = take_mode(flags, ap);
    va_end(ap);

    fd = (int)syscall(SYS_openat, AT_FDCWD, path, flags, mode);
    return track(fd, path);
}

int open64(const char *path, int flags, ...)
{
    va_list ap;
    mode_t mode;
    int fd;

    if (is_dev_mem(path))
        return deny_mem();

    va_start(ap, flags);
    mode = take_mode(flags, ap);
    va_end(ap);

    fd = (int)syscall(SYS_openat, AT_FDCWD, path, flags, mode);
    return track(fd, path);
}

int openat(int dirfd, const char *path, int flags, ...)
{
    va_list ap;
    mode_t mode;
    int fd;

    if (is_dev_mem(path))
        return deny_mem();

    va_start(ap, flags);
    mode = take_mode(flags, ap);
    va_end(ap);

    fd = (int)syscall(SYS_openat, dirfd, path, flags, mode);
    return track(fd, path);
}

int openat64(int dirfd, const char *path, int flags, ...)
{
    va_list ap;
    mode_t mode;
    int fd;

    if (is_dev_mem(path))
        return deny_mem();

    va_start(ap, flags);
    mode = take_mode(flags, ap);
    va_end(ap);

    fd = (int)syscall(SYS_openat, dirfd, path, flags, mode);
    return track(fd, path);
}

int close(int fd)
{
    if (fd >= 0 && fd < MAX_FDS) {
        is_gpio[fd] = 0;
        is_tsc[fd]  = 0;
    }
    return (int)syscall(SYS_close, fd);
}

/* --- /dev/gpiodrv: constant GPIO input (never changes) --- */
ssize_t read(int fd, void *buf, size_t count)
{
    if (fd >= 0 && fd < MAX_FDS && is_gpio[fd] && buf && count >= 1) {
        memset(buf, 0, 1);   /* simulate a pin that never changes */
        return 1;
    }
    /* XDJ-RX3 touch device.  Without RB_TOUCH_NATIVE this reports "no touch"
     * (all zeroes) at ~50 Hz so rbp's TouchPanelComm thread polls without
     * busy-spinning; with it, the contact rbtouchd published is handed over in
     * the 6-byte record layout selected by RB_TOUCH_FMT. */
    if (fd >= 0 && fd < MAX_FDS && is_tsc[fd] && buf && count >= 6) {
        struct timespec ts = { 0, 16000000 };   /* ~60 Hz */
        struct rb_touch_state st;

        touch_env_init();
        if (touch_native && touch_read_state(&st))
            touch_pack(buf, &st);
        else
            memset(buf, 0, 6);
        nanosleep(&ts, NULL);
        return 6;
    }
    return (ssize_t)syscall(SYS_read, fd, buf, count);
}

/* --- /dev/gpiodrv: park GpioManager instead of spinning on a regular file ---
 * A regular file is always poll-ready, so rbp's GpioManager threads return from
 * poll() instantly and re-poll (~11k/s each).  Report "no event": clear revents
 * for the gpio fds, sleep, and return 0 (timeout).  GpioManager treats that as
 * "nothing happened" and just polls again slowly. */
int poll(struct pollfd *fds, nfds_t nfds, int timeout)
{
    if (fds && nfds >= 1) {
        nfds_t i;
        int all_gpio = 1;

        for (i = 0; i < nfds; i++) {
            if (fds[i].fd >= 0 && fds[i].fd < MAX_FDS && is_gpio[fds[i].fd])
                fds[i].revents = 0;          /* never ready */
            else
                all_gpio = 0;
        }

        if (all_gpio) {
            if (timeout < 0) {
                struct timespec ts = { 0, 50000000 };       /* 50 ms */
                nanosleep(&ts, NULL);
            } else if (timeout > 0) {
                struct timespec ts = { timeout / 1000,
                                       (long)(timeout % 1000) * 1000000L };
                nanosleep(&ts, NULL);
            }
            return 0;
        }
    }
    return (int)syscall(SYS_poll, fds, nfds, timeout);
}

/* --- redirect the broken mmap(fd=-1, MAP_SHARED) --- */
void *mmap(void *addr, size_t length, int prot, int flags, int fd, off_t offset)
{
    if (fd < 0 && (flags & MAP_SHARED) && !(flags & MAP_ANONYMOUS)) {
        flags = (flags & ~MAP_SHARED) | MAP_PRIVATE | MAP_ANONYMOUS;
        offset = 0;
    }
    return (void *)syscall(SYS_mmap2, addr, length, prot, flags, fd,
                           (unsigned long)offset >> 12);
}
