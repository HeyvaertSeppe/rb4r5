/*
 * test_rate_gate.c - the thing that decides how fast the music plays.
 *
 * The player's transport is clocked by snd_pcm_writei: the deck plays, and
 * the seconds count, at exactly the rate that call returns at.  So this
 * governor is the difference between a three minute track taking three
 * minutes and taking twenty seconds.  It is tested with a fake clock, so the
 * test is exact and instant rather than approximate and slow.
 */
#include <stdio.h>
#include <string.h>

/* a clock we control, instead of the real one */
static long long fake_now = 0;
static long long fake_slept = 0;
#define RB4R5_PACE_NOW()      (fake_now)
#define RB4R5_PACE_SLEEP(us)  do { fake_slept += (us); fake_now += (us); } while (0)

#include "../../src/shims/rate_gate.h"

static int failures;

#define CHECK(cond, ...) do {                                            \
     if (!(cond)) {                                                      \
          printf("  FAIL %s:%d: ", __FILE__, __LINE__);                  \
          printf(__VA_ARGS__);                                           \
          printf("\n");                                                  \
          failures++;                                                    \
     }                                                                   \
} while (0)

#define RATE   44100u
#define PERIOD 1024u                 /* ~23.2ms of audio                  */

int
main( void )
{
     printf("== a stream with nothing behind it is held to real time\n");
     {
          /* the engine pushing periods as fast as it can: without the
           * governor this loop takes no time at all, and the track races */
          struct pace p = { 0, 0, 0, "test", 0 };
          int periods = 500;                     /* 500 * 1024 frames      */
          double audio = (double)periods * PERIOD / RATE;

          fake_now = 1000000;
          fake_slept = 0;
          for (int i = 0; i < periods; i++)
               pace_stream( &p, PERIOD, RATE );

          double real = (double)fake_slept / 1000000.0;
          printf("   %.2fs of audio took %.2fs of (fake) real time\n",
                 audio, real);
          CHECK( real > audio * 0.98 && real < audio * 1.02,
                 "%.3fs of audio was let through in %.3fs", audio, real );
     }

     printf("== a stream the hardware already paces is never held up\n");
     {
          struct pace p = { 0, 0, 0, "test", 0 };
          /* the device takes exactly one period's worth of time each call */
          fake_now = 5000000;
          fake_slept = 0;
          for (int i = 0; i < 200; i++) {
               pace_stream( &p, PERIOD, RATE );
               fake_now += (long long)PERIOD * 1000000LL / RATE;
          }
          CHECK( fake_slept == 0, "it slept %lldus for a stream already at "
                 "real time", fake_slept );
     }

     printf("== a slow device is not 'caught up' by running fast\n");
     {
          struct pace p = { 0, 0, 0, "test", 0 };
          fake_now = 0;
          fake_slept = 0;
          /* the device takes twice as long as real time for 50 periods */
          for (int i = 0; i < 50; i++) {
               pace_stream( &p, PERIOD, RATE );
               fake_now += 2 * ((long long)PERIOD * 1000000LL / RATE);
          }
          CHECK( fake_slept == 0, "it slept %lldus while already behind",
                 fake_slept );
          /* It is allowed to use up the deficit the slow device built - that
           * is the engine catching up, which is right - and then it governs
           * again.  50 slow periods owed 1.16s; 100 more periods are 2.32s,
           * so about 1.16s of that should be held back. */
          fake_slept = 0;
          for (int i = 0; i < 100; i++)
               pace_stream( &p, PERIOD, RATE );
          double held = (double)fake_slept / 1000000.0;
          CHECK( held > 0.9 && held < 1.5,
                 "after the deficit it held back %.2fs (expected about 1.16)",
                 held );
     }

     printf("== a long stall restarts the clock instead of sprinting\n");
     {
          struct pace p = { 0, 0, 0, "test", 0 };
          fake_now = 0;
          pace_stream( &p, PERIOD, RATE );
          fake_now += 10000000;                  /* stalled ten seconds    */
          fake_slept = 0;
          pace_stream( &p, PERIOD, RATE );
          CHECK( fake_slept == 0, "it slept after a stall" );
          CHECK( p.frames == PERIOD, "the clock restarted (frames=%llu)",
                 (unsigned long long)p.frames );
          /* and from there it is back to real time */
          fake_slept = 0;
          for (int i = 0; i < 100; i++)
               pace_stream( &p, PERIOD, RATE );
          double audio = 100.0 * PERIOD / RATE;
          CHECK( fake_slept > (long long)(audio * 1000000 * 0.9),
                 "it did not resume governing (%lldus for %.2fs)",
                 fake_slept, audio );
     }

     printf("== a clock that reads zero is a time, not 'never started'\n");
     {
          struct pace p = { 0, 0, 0, "test", 0 };
          fake_now = 0;
          pace_due_us( &p, PERIOD, RATE, 0 );
          CHECK( p.started && p.frames == PERIOD,
                 "the clock started at zero (started=%d frames=%llu)",
                 p.started, (unsigned long long)p.frames );
          CHECK( pace_due_us( &p, 0, RATE, 0 ) > 20000,
                 "and a second call at the same instant is owed a wait" );
     }

     printf("== the rate is what decides, and a missing one is 44100\n");
     {
          struct pace a = { 0, 0, 0, "48k", 0 }, b = { 0, 0, 0, "default", 0 };
          long long due_a, due_b;

          fake_now = 0;
          /* the first call has nothing behind it, so it is the second that
           * says when a second's worth of audio was due */
          pace_due_us( &a, 48000, 48000, 0 );
          pace_due_us( &b, 44100, 0, 0 );
          due_a = pace_due_us( &a, 0, 48000, 0 );
          due_b = pace_due_us( &b, 0, 0, 0 );
          CHECK( due_a > 990000 && due_a < 1010000,
                 "48000 frames at 48kHz is a second (%lldus)", due_a );
          CHECK( due_b > 990000 && due_b < 1010000,
                 "44100 frames with no rate given is a second (%lldus)",
                 due_b );
     }

     printf("== tiny waits are not worth a syscall\n");
     {
          struct pace p = { 0, 0, 0, "test", 0 };
          fake_now = 0;
          CHECK( pace_due_us( &p, 10, RATE, 0 ) == 0,
                 "a 0.2ms lead should not sleep" );
     }

     printf(failures ? "\n%d FAILURES\n" : "\nall rate gate tests passed\n",
            failures);
     return failures ? 1 : 0;
}
