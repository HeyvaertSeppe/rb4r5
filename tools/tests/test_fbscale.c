/*
 * test_fbscale.c - unit tests for src/directfb/rb4r5_scale.h.
 *
 * The header is the publish path of the patched DirectFB fbdev driver: it
 * takes rbp's 1280x800 RGB565 frame and writes it into whatever pixel format
 * /dev/fb0 actually is.  Getting that wrong is not subtle - writing 32-bit
 * pixels into a 16-bit framebuffer (the bug this replaces) put the left half
 * of the UI across the whole panel with olive-yellow backgrounds and lavender
 * panels - but it is invisible on the build host, so it gets tested here.
 *
 * Build/run:  cc -O2 -o test_fbscale test_fbscale.c && ./test_fbscale
 * On ARM the same source exercises the NEON row expander:
 *   arm-linux-gnueabi-gcc -O2 -march=armv7-a -mfpu=neon -mfloat-abi=softfp
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../../src/directfb/rb4r5_scale.h"

static int failures;

#define CHECK(cond, ...) do {                                            \
     if (!(cond)) {                                                      \
          printf("  FAIL %s:%d: ", __FILE__, __LINE__);                  \
          printf(__VA_ARGS__);                                           \
          printf("\n");                                                  \
          failures++;                                                    \
     }                                                                   \
} while (0)

/* ---- the reference conversion, written out the slow obvious way ------- */
/* The expansion every RGB565 stack uses: repeat the high bits into the low
 * ones, so 0 stays 0 and the maximum (31 or 63) reaches a full 255.  Written
 * out as arithmetic here rather than as shifts, so that a wrong shift in the
 * header - or a wrong NEON lane - does not cancel out against the same
 * mistake in the reference. */
static unsigned int
ref_px( unsigned int v, int swap )
{
     unsigned int r5 = (v >> 11) & 0x1f;
     unsigned int g6 = (v >>  5) & 0x3f;
     unsigned int b5 =  v        & 0x1f;
     unsigned int r  = r5 * 8 + r5 / 4;
     unsigned int g  = g6 * 4 + g6 / 16;
     unsigned int b  = b5 * 8 + b5 / 4;
     return swap ? (0xff000000u | (b << 16) | (g << 8) | r)
                 : (0xff000000u | (r << 16) | (g << 8) | b);
}

/* Every one of the 65536 RGB565 values, both channel orders.  On ARM this is
 * the NEON expander; on the host it is the scalar loop.  Both must agree with
 * the reference exactly - a rounding difference here is a visible colour
 * cast across the whole UI. */
static void
test_row_conversion( void )
{
     static unsigned short src[RB4R5_ROW_MAX];
     static unsigned int   out[RB4R5_ROW_MAX];
     int swap, base, i, bad = 0;

     printf("row conversion (65536 values x 2 channel orders)\n");
     for (swap = 0; swap <= 1; swap++) {
          for (base = 0; base < 65536; base += 1024) {
               for (i = 0; i < 1024; i++)
                    src[i] = (unsigned short)(base + i);
               memset( out, 0, sizeof(out) );
               rb4r5_row_8888( src, out, 1024, swap );
               for (i = 0; i < 1024; i++) {
                    unsigned int want = ref_px( src[i], swap );
                    if (out[i] != want && bad++ < 4)
                         CHECK( 0, "swap=%d 0x%04x -> 0x%08x, want 0x%08x",
                                swap, src[i], out[i], want );
               }
          }
          /* a tail that is not a multiple of 8 must still convert */
          for (i = 0; i < 13; i++)
               src[i] = (unsigned short)(0xf800 >> i);
          memset( out, 0, sizeof(out) );
          rb4r5_row_8888( src, out, 13, swap );
          for (i = 0; i < 13; i++)
               CHECK( out[i] == ref_px( src[i], swap ),
                      "tail swap=%d i=%d 0x%08x != 0x%08x",
                      swap, i, out[i], ref_px( src[i], swap ) );
     }
     CHECK( bad == 0, "%d mismatched pixels in total", bad );
}

/* ---- format classification ------------------------------------------- */
static void
test_classify( void )
{
     RB4R5Dst d;

     printf("format classification\n");
     /* Pi 5, vc4 KMS fbdev emulation as it comes up by default */
     CHECK( rb4r5_classify( 16, 11,5, 5,6, 0,5 ) == RB_DST_RGB565, "RGB565" );
     /* ... and with video=HDMI-A-1:1920x1080-32 on the kernel cmdline */
     CHECK( rb4r5_classify( 32, 16,8, 8,8, 0,8 ) == RB_DST_XRGB8888, "XRGB8888" );
     CHECK( rb4r5_classify( 32, 0,8, 8,8, 16,8 ) == RB_DST_XBGR8888, "XBGR8888" );
     CHECK( rb4r5_classify( 16, 0,5, 5,6, 11,5 ) == RB_DST_BGR565, "BGR565" );
     CHECK( rb4r5_classify( 16, 10,5, 5,5, 0,5 ) == RB_DST_RGB555, "RGB555" );
     CHECK( rb4r5_classify( 24, 16,8, 8,8, 0,8 ) == RB_DST_RGB888, "RGB888" );
     CHECK( rb4r5_classify( 24, 0,8, 8,8, 16,8 ) == RB_DST_BGR888, "BGR888" );
     CHECK( rb4r5_classify( 8, 0,3, 3,3, 6,2 ) == RB_DST_UNKNOWN, "8bpp" );

     /* an unrecognised layout still draws, at the usual depth, and says so */
     rb4r5_dst_init( &d, 32, 8,8, 16,8, 24,8, 64, 32, 256, 32, 16, 0 );
     CHECK( d.guessed == 1, "odd 32bpp layout should be flagged" );
     CHECK( d.fmt == RB_DST_XRGB8888 && d.bpp == 4, "fallback fmt/bpp" );

     /* bytes per pixel follows the format, not the reported depth */
     rb4r5_dst_init( &d, 16, 11,5, 5,6, 0,5, 1920, 1080, 3840, 1280, 800, 0 );
     CHECK( d.bpp == 2 && d.fmt == RB_DST_RGB565, "16bpp -> 2 bytes" );
     CHECK( d.guessed == 0, "RGB565 is not a guess" );
}

/* ---- destination rectangle ------------------------------------------- */
static void
test_fit( void )
{
     RB4R5Dst d;

     printf("destination rectangle\n");
     /* fill: the whole panel, which is what "full screen" means here */
     rb4r5_dst_init( &d, 16, 11,5, 5,6, 0,5, 1920, 1080, 3840, 1280, 800, 0 );
     CHECK( d.x == 0 && d.y == 0 && d.w == 1920 && d.h == 1080,
            "fill 1920x1080: %d,%d %dx%d", d.x, d.y, d.w, d.h );

     /* aspect: 16:10 centred on a 16:9 panel, bars left and right */
     rb4r5_dst_init( &d, 16, 11,5, 5,6, 0,5, 1920, 1080, 3840, 1280, 800, 1 );
     CHECK( d.w == 1728 && d.h == 1080 && d.x == 96 && d.y == 0,
            "aspect 1920x1080: %d,%d %dx%d", d.x, d.y, d.w, d.h );

     /* aspect: 16:10 on a 4:3 panel, bars top and bottom */
     rb4r5_dst_init( &d, 32, 16,8, 8,8, 0,8, 1024, 768, 4096, 1280, 800, 1 );
     CHECK( d.w == 1024 && d.h == 640 && d.x == 0 && d.y == 64,
            "aspect 1024x768: %d,%d %dx%d", d.x, d.y, d.w, d.h );

     /* a line length too short for the mode clamps the width - a row must
      * never be allowed to run into the next one */
     rb4r5_dst_init( &d, 32, 16,8, 8,8, 0,8, 1920, 1080, 3840, 1280, 800, 0 );
     CHECK( d.fw == 960 && d.w == 960, "pitch clamp: fw=%d w=%d", d.fw, d.w );
}

/* ---- the scaler ------------------------------------------------------ */

#define SW 1280
#define SH 800

static unsigned short *
make_source( int spitch )
{
     unsigned short *src = calloc( (size_t)SH, spitch );
     int x, y;
     for (y = 0; y < SH; y++) {
          unsigned short *row = (unsigned short *)((char *)src + (size_t)y * spitch);
          for (x = 0; x < SW; x++)
               row[x] = (unsigned short)((x * 31 / (SW - 1)) << 11 |
                                         (y * 63 / (SH - 1)) <<  5 |
                                         ((x ^ y) & 0x1f));
     }
     return src;
}

/* Pre-fill the framebuffer with a sentinel, scale into it, then check that
 * (a) every pixel of the destination rectangle was written, (b) nothing
 * outside the rectangle or past the line length was touched, and (c) each
 * destination pixel carries the nearest-neighbour source pixel. */
static void
test_scale( int bits, int ro, int rl, int go, int gl, int bo, int bl,
            int fw, int fh, int aspect, const char *what )
{
     int spitch = SW * 2;
     unsigned short *src = make_source( spitch );
     RB4R5Dst d;
     unsigned char *fb;
     size_t fbsize;
     int x, y, wrong = 0, spill = 0;

     printf("scale %s -> %dx%d %s\n", rb4r5_fmt_name(
                rb4r5_classify( bits, ro,rl, go,gl, bo,bl ) ),
            fw, fh, aspect ? "(aspect)" : "(fill)");

     rb4r5_dst_init( &d, bits, ro,rl, go,gl, bo,bl,
                     fw, fh, fw * ((bits + 7) / 8), SW, SH, aspect );
     fbsize = (size_t)d.pitch * d.fh;
     fb = malloc( fbsize );
     memset( fb, 0xa5, fbsize );
     if (aspect)
          rb4r5_clear( fb, &d );          /* the launcher clears the bars once */

     rb4r5_scale565( (const unsigned char *)src, SW, SH, spitch, fb, &d );

     for (y = 0; y < d.fh; y++) {
          const unsigned char *row = fb + (size_t)y * d.pitch;
          for (x = 0; x < d.fw; x++) {
               const unsigned char *p = row + (size_t)x * d.bpp;
               int inside = (x >= d.x && x < d.x + d.w &&
                             y >= d.y && y < d.y + d.h);
               int i;
               unsigned int got = 0, want;
               unsigned short sp;

               for (i = 0; i < d.bpp; i++)
                    got |= (unsigned int)p[i] << (8 * i);

               if (!inside) {
                    /* Outside the picture: in aspect mode the bars were
                     * cleared to black and must have stayed black; in fill
                     * mode there is nothing outside at all. */
                    if (got != 0)
                         spill++;
                    continue;
               }

               sp = ((const unsigned short *)((const char *)src +
                     (size_t)(((y - d.y) * (((unsigned long long)SH << 16)
                               / (unsigned)d.h)) >> 16) * spitch))
                    [((x - d.x) * (unsigned int)(((unsigned long long)SW << 16)
                               / (unsigned)d.w)) >> 16];

               switch (d.fmt) {
               case RB_DST_RGB565: want = sp; break;
               case RB_DST_BGR565: want = ((sp & 0x1f) << 11) | (sp & 0x7e0) | (sp >> 11); break;
               case RB_DST_RGB555: want = ((sp >> 1) & 0x7fe0) | (sp & 0x1f); break;
               case RB_DST_XRGB8888: want = ref_px( sp, 0 ); break;
               case RB_DST_XBGR8888: want = ref_px( sp, 1 ); break;
               case RB_DST_RGB888: want = ref_px( sp, 0 ) & 0xffffff; break;
               default:            want = ref_px( sp, 1 ) & 0xffffff; break;
               }
               if (got != want && wrong++ < 4)
                    CHECK( 0, "%s (%d,%d): got 0x%08x want 0x%08x",
                           what, x, y, got, want );
          }
          /* the bytes between the last pixel and the end of the line are
           * nobody's business but the kernel's */
          for (x = d.fw * d.bpp; x < d.pitch; x++)
               if (row[x] != 0xa5 && row[x] != 0)
                    spill++;
     }

     CHECK( wrong == 0, "%s: %d wrong or unwritten pixels", what, wrong );
     CHECK( spill == 0, "%s: %d bytes written outside the picture",
            what, spill );
     free( fb );
     free( src );
}

/* Known colours through the whole path, so a channel swap cannot hide in a
 * gradient.  This is the check that would have caught the 16bpp bug: pure
 * white must stay white and black must stay black in every format. */
static void
test_known_colours( void )
{
     static const struct { unsigned short in; unsigned int rgb565, xrgb; } t[] = {
          { 0x0000, 0x0000, 0xff000000 },   /* black */
          { 0xffff, 0xffff, 0xffffffff },   /* white */
          { 0xf800, 0xf800, 0xffff0000 },   /* red   */
          { 0x07e0, 0x07e0, 0xff00ff00 },   /* green */
          { 0x001f, 0x001f, 0xff0000ff },   /* blue  */
     };
     RB4R5Dst d;
     unsigned char fb[64 * 4];
     unsigned short src[4];
     size_t i;

     printf("known colours\n");
     for (i = 0; i < sizeof(t) / sizeof(t[0]); i++) {
          src[0] = src[1] = src[2] = src[3] = t[i].in;

          rb4r5_dst_init( &d, 16, 11,5, 5,6, 0,5, 4, 2, 8, 2, 2, 0 );
          memset( fb, 0, sizeof(fb) );
          rb4r5_scale565( (unsigned char *)src, 2, 2, 4, fb, &d );
          CHECK( *(unsigned short *)fb == t[i].rgb565,
                 "RGB565 0x%04x -> 0x%04x", t[i].in, *(unsigned short *)fb );

          rb4r5_dst_init( &d, 32, 16,8, 8,8, 0,8, 4, 2, 16, 2, 2, 0 );
          memset( fb, 0, sizeof(fb) );
          rb4r5_scale565( (unsigned char *)src, 2, 2, 4, fb, &d );
          CHECK( *(unsigned int *)fb == t[i].xrgb,
                 "XRGB8888 0x%04x -> 0x%08x", t[i].in, *(unsigned int *)fb );

          /* rb4r5_store_argb is the same pixel arriving as 8-8-8 (the
           * rotation path, and a 32bpp logical layer) */
          rb4r5_store_argb( fb, RB_DST_RGB565, t[i].xrgb );
          CHECK( *(unsigned short *)fb == t[i].rgb565,
                 "store_argb RGB565 0x%08x -> 0x%04x",
                 t[i].xrgb, *(unsigned short *)fb );
          rb4r5_store_argb( fb, RB_DST_XRGB8888, t[i].xrgb );
          CHECK( *(unsigned int *)fb == t[i].xrgb, "store_argb XRGB8888" );
          rb4r5_store_argb( fb, RB_DST_XBGR8888, t[i].xrgb );
          CHECK( *(unsigned int *)fb ==
                 (0xff000000u | ((t[i].xrgb & 0xff) << 16) |
                  (t[i].xrgb & 0xff00) | ((t[i].xrgb >> 16) & 0xff)),
                 "store_argb XBGR8888" );

          /* rb4r5_store takes the same pixels down the rotation path */
          rb4r5_store( fb, RB_DST_RGB565, t[i].in );
          CHECK( *(unsigned short *)fb == t[i].rgb565, "store RGB565" );
          rb4r5_store( fb, RB_DST_XRGB8888, t[i].in );
          CHECK( *(unsigned int *)fb == t[i].xrgb, "store XRGB8888" );
     }
}

/* A rectangle that does not fit, a zero size, a null pointer: draw nothing
 * rather than something else's memory. */
static void
test_refuses_bad_geometry( void )
{
     RB4R5Dst d;
     unsigned char fb[4096];
     unsigned short src[64];

     printf("bad geometry\n");
     memset( src, 0xff, sizeof(src) );

     rb4r5_dst_init( &d, 16, 11,5, 5,6, 0,5, 32, 8, 64, 8, 8, 0 );
     d.w = 64;                            /* wider than the framebuffer */
     memset( fb, 0, sizeof(fb) );
     rb4r5_scale565( (unsigned char *)src, 8, 8, 16, fb, &d );
     CHECK( fb[0] == 0 && fb[63] == 0, "oversized rectangle must draw nothing" );

     rb4r5_dst_init( &d, 16, 11,5, 5,6, 0,5, 32, 8, 64, 8, 8, 0 );
     d.y = 8;                             /* starts past the last row */
     memset( fb, 0, sizeof(fb) );
     rb4r5_scale565( (unsigned char *)src, 8, 8, 16, fb, &d );
     CHECK( fb[0] == 0, "off-screen rectangle must draw nothing" );

     rb4r5_dst_init( &d, 16, 11,5, 5,6, 0,5, 32, 8, 64, 8, 8, 0 );
     memset( fb, 0, sizeof(fb) );
     rb4r5_scale565( NULL, 8, 8, 16, fb, &d );
     rb4r5_scale565( (unsigned char *)src, 0, 8, 16, fb, &d );
     rb4r5_scale565( (unsigned char *)src, 8, 8, 0, fb, &d );
     CHECK( fb[0] == 0, "null/empty source must draw nothing" );
}

int
main( void )
{
     printf("== fbscale (%s)\n",
#ifdef RB4R5_NEON
            "NEON"
#else
            "scalar"
#endif
            );
     test_row_conversion();
     test_classify();
     test_fit();
     test_known_colours();
     test_refuses_bad_geometry();

     /* the two ways a Pi 5 actually comes up */
     test_scale( 16, 11,5, 5,6, 0,5, 1920, 1080, 0, "rgb565-fill" );
     test_scale( 32, 16,8, 8,8, 0,8, 1920, 1080, 0, "xrgb8888-fill" );
     /* and the rest of the formats, at sizes that exercise up- and downscaling */
     test_scale( 16, 11,5, 5,6, 0,5, 1920, 1080, 1, "rgb565-aspect" );
     test_scale( 32, 16,8, 8,8, 0,8, 1024,  768, 1, "xrgb8888-aspect" );
     test_scale( 16, 0,5, 5,6, 11,5, 1280,  800, 0, "bgr565-1:1" );
     test_scale( 32, 0,8, 8,8, 16,8, 1280,  800, 0, "xbgr8888-1:1" );
     test_scale( 24, 16,8, 8,8, 0,8,  800,  480, 0, "rgb888-down" );
     test_scale( 24, 0,8, 8,8, 16,8, 3840, 2160, 0, "bgr888-4k" );
     test_scale( 16, 10,5, 5,5, 0,5, 1600,  900, 0, "rgb555" );

     printf(failures ? "\n%d FAILURES\n" : "\nall fbscale tests passed\n",
            failures);
     return failures ? 1 : 0;
}
