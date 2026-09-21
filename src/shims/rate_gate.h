/*
 * rate_gate.h - keep a stream from running faster than real time.
 *
 * The player's transport is clocked by how fast snd_pcm_writei accepts
 * frames: the deck plays, and the time display counts, at exactly the rate
 * that call returns at.  That is fine for the master stream, which a real
 * device paces - but the engine writes several streams (master, headphones,
 * booth) and the ones with no hardware behind them are mixed in memory and
 * returned immediately.  A stream that never blocks lets the engine run as
 * fast as the CPU can push frames into it, and then a three minute track goes
 * by in twenty seconds with the seconds counting up just as fast.
 *
 * So every stream gets a governor: it may run at real time, never faster.  A
 * stream the hardware already paces never trips it, because it is never
 * ahead.
 *
 * Kept free of ALSA so it can be tested on its own: tools/tests/test_rate_gate.c.
 */
#ifndef RB4R5_RATE_GATE_H
#define RB4R5_RATE_GATE_H

#include <time.h>
#include <unistd.h>

struct pace {
    int                started;     /* t0_us is meaningless until this is set;
                                     * a zero timestamp is a real time, and
                                     * using it as "not started" restarts the
                                     * clock on every call when it comes up */
    long long          t0_us;       /* when this stream's clock started    */
    unsigned long long frames;      /* frames accepted since then          */
    const char        *name;
    unsigned long long slept_us;    /* how much we have had to hold it back */
};

/* Two seconds behind means the engine (or the device) stalled somewhere else.
 * Catching up by running fast would be worse than the stall, so the clock
 * starts again from here instead. */
#define RB4R5_PACE_RESYNC_US  2000000LL
/* Do not bother sleeping for less than this; the sleep itself costs more. */
#define RB4R5_PACE_MIN_US     1000LL

#ifndef RB4R5_PACE_NOW
static long long rb4r5_now_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000000LL + ts.tv_nsec / 1000LL;
}
# define RB4R5_PACE_NOW()      rb4r5_now_us()
#endif
#ifndef RB4R5_PACE_SLEEP
# define RB4R5_PACE_SLEEP(us)  usleep((useconds_t)(us))
#endif

/* How long this call should wait, in microseconds (0 = not ahead).  Split out
 * so a test can check the arithmetic without waiting for it. */
static long long pace_due_us(struct pace *p, unsigned long frames,
                             unsigned int rate, long long now)
{
    long long due, ahead;

    if (rate == 0)
        rate = 44100;
    if (!p->started) {
        p->started = 1;
        p->t0_us = now;
        p->frames = 0;
    }
    /* What was accepted BEFORE this call decides when this one may return.
     * That leaves the stream one batch ahead, which is exactly the buffer it
     * is filling - a device-paced stream is never held up, and a stream with
     * nothing behind it still comes out at real time. */
    due = p->t0_us + (long long)(p->frames * 1000000ULL / rate);
    ahead = due - now;
    p->frames += frames;

    if (ahead < -RB4R5_PACE_RESYNC_US) {
        /* far enough behind that catching up would be worse than the stall:
         * start the clock again from here, this batch included */
        p->t0_us = now;
        p->frames = frames;
        return 0;
    }
    return (ahead > RB4R5_PACE_MIN_US) ? ahead : 0;
}

static void pace_stream(struct pace *p, unsigned long frames, unsigned int rate)
{
    long long wait = pace_due_us(p, frames, rate, RB4R5_PACE_NOW());
    if (wait > 0) {
        RB4R5_PACE_SLEEP(wait);
        p->slept_us += (unsigned long long)wait;
    }
}

#endif /* RB4R5_RATE_GATE_H */
