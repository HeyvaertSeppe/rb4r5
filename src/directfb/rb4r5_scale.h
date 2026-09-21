/*
 * rb4r5_scale.h - publish the player's RGB565 frame into the physical
 *                 framebuffer, whatever pixel format that framebuffer is in.
 *
 * rbp draws a 1280x800 RGB565 logical frame (the fb shim tells it that is the
 * panel).  The real /dev/fb0 on a Pi 5 is a different size AND, crucially, not
 * necessarily a different depth from what we assume: the vc4 DRM driver's fbdev
 * emulation hands out RGB565 on some kernels and XRGB8888 on others, and a
 * kernel cmdline like "video=HDMI-A-1:1920x1080-32" changes it.  Writing 32-bit
 * pixels into a 16-bit framebuffer covers two destination pixels per write:
 * the picture comes out double width (so only its left half is on screen), the
 * rows overrun into each other, and every colour is wrong in a characteristic
 * way - dark blue backgrounds turn olive, blue-grey panels turn lavender, while
 * white stays white.  That is the bug this file exists to make impossible.
 *
 * So: no format is assumed.  The destination layout is classified once from the
 * fb_var_screeninfo bitfields, the writer is chosen from that, and the write is
 * clamped to the reported line length so a misdetected mode can never scribble
 * past the end of a row.
 *
 * Kept free of DirectFB types so it can be unit tested on the build host:
 * see tools/tests/test_fbscale.c.
 */
#ifndef RB4R5_SCALE_H
#define RB4R5_SCALE_H

#include <stddef.h>
#include <string.h>

#if defined(__ARM_NEON) || defined(__ARM_NEON__)
# include <arm_neon.h>
# define RB4R5_NEON 1
#endif

/* widest source row we expand in one go (the RX3 frame is 1280) */
#define RB4R5_ROW_MAX 4096
/* widest destination row we precompute the sample map for (4K is 3840) */
#define RB4R5_DST_MAX 4096

enum {
     RB_DST_UNKNOWN = 0,
     RB_DST_RGB565,      /* 16bpp  r@11 g@5 b@0                        */
     RB_DST_BGR565,      /* 16bpp  b@11 g@5 r@0                        */
     RB_DST_RGB555,      /* 16bpp  r@10 g@5 b@0 (5/5/5)                */
     RB_DST_XRGB8888,    /* 32bpp  r@16 g@8 b@0  (memory: b,g,r,x)     */
     RB_DST_XBGR8888,    /* 32bpp  b@16 g@8 r@0  (memory: r,g,b,x)     */
     RB_DST_RGB888,      /* 24bpp  r@16 g@8 b@0  (memory: b,g,r)       */
     RB_DST_BGR888       /* 24bpp  b@16 g@8 r@0  (memory: r,g,b)       */
};

typedef struct {
     int fmt;            /* RB_DST_*                                    */
     int bpp;            /* bytes per physical pixel: 2, 3 or 4         */
     int fw, fh;         /* usable physical size (clamped to the pitch) */
     int pitch;          /* physical line length in bytes               */
     int x, y, w, h;     /* destination rectangle the frame is drawn in */
     int filter;         /* 0 = nearest neighbour, 1 = bilinear         */
     int guessed;        /* the bitfields were not recognised           */
} RB4R5Dst;

static const char *
rb4r5_fmt_name( int fmt )
{
     switch (fmt) {
     case RB_DST_RGB565:   return "RGB565";
     case RB_DST_BGR565:   return "BGR565";
     case RB_DST_RGB555:   return "RGB555";
     case RB_DST_XRGB8888: return "XRGB8888";
     case RB_DST_XBGR8888: return "XBGR8888";
     case RB_DST_RGB888:   return "RGB888";
     case RB_DST_BGR888:   return "BGR888";
     default:              return "unknown";
     }
}

/* Classify a framebuffer from its fb_var_screeninfo bitfields. */
static int
rb4r5_classify( int bits, int ro, int rl, int go, int gl, int bo, int bl )
{
     if (bits == 16) {
          if (rl == 5 && gl == 6 && bl == 5) {
               if (ro == 11 && go == 5 && bo == 0) return RB_DST_RGB565;
               if (bo == 11 && go == 5 && ro == 0) return RB_DST_BGR565;
          }
          if (rl == 5 && gl == 5 && bl == 5 && ro == 10 && bo == 0)
               return RB_DST_RGB555;
     }
     else if (bits == 32 || bits == 24) {
          if (rl == 8 && gl == 8 && bl == 8) {
               if (ro == 16 && go == 8 && bo == 0)
                    return (bits == 32) ? RB_DST_XRGB8888 : RB_DST_RGB888;
               if (bo == 16 && go == 8 && ro == 0)
                    return (bits == 32) ? RB_DST_XBGR8888 : RB_DST_BGR888;
          }
     }
     return RB_DST_UNKNOWN;
}

/*
 * Work out where in the framebuffer a sw x sh frame goes.
 *   aspect == 0: stretch to fill the whole panel (what the RX3 does on its
 *                own 16:10 screen, and what "full screen" means here)
 *   aspect != 0: keep 16:10, centre it, leave black bars
 */
static void
rb4r5_dst_init( RB4R5Dst *d, int bits,
                int ro, int rl, int go, int gl, int bo, int bl,
                int fw, int fh, int pitch, int sw, int sh,
                int aspect, int filter )
{
     memset( d, 0, sizeof(*d) );
     d->filter = filter ? 1 : 0;

     d->fmt = rb4r5_classify( bits, ro, rl, go, gl, bo, bl );
     if (!d->fmt) {
          /* Unrecognised bitfields: fall back to the usual layout for that
           * depth rather than refusing to draw, and say so. */
          d->fmt     = (bits == 16) ? RB_DST_RGB565
                     : (bits == 24) ? RB_DST_RGB888 : RB_DST_XRGB8888;
          d->guessed = 1;
     }
     d->bpp = (d->fmt == RB_DST_RGB565 || d->fmt == RB_DST_BGR565 ||
               d->fmt == RB_DST_RGB555) ? 2
            : (d->fmt == RB_DST_RGB888 || d->fmt == RB_DST_BGR888) ? 3 : 4;

     d->fw    = (fw > 0) ? fw : 0;
     d->fh    = (fh > 0) ? fh : 0;
     d->pitch = (pitch > 0) ? pitch : d->fw * d->bpp;

     /* A row can never be wider than the line length, whatever the mode says. */
     if (d->fw * d->bpp > d->pitch)
          d->fw = d->pitch / d->bpp;

     if (aspect && sw > 0 && sh > 0 && d->fw > 0 && d->fh > 0) {
          long by_h = (long)d->fw * sh;     /* fw/sw vs fh/sh, cross multiplied */
          long by_w = (long)d->fh * sw;
          if (by_h > by_w) {                /* height limited: full height */
               d->h = d->fh;
               d->w = (int)(by_w / sh);
          }
          else {                            /* width limited: full width */
               d->w = d->fw;
               d->h = (int)(by_h / sw);
          }
          if (d->w > d->fw) d->w = d->fw;
          if (d->h > d->fh) d->h = d->fh;
          d->x = (d->fw - d->w) / 2;
          d->y = (d->fh - d->h) / 2;
     }
     else {
          d->w = d->fw;
          d->h = d->fh;
     }
}

/* Black is all-zero bits in every format above, so the bars are a memset. */
static inline void
rb4r5_clear( unsigned char *dst, const RB4R5Dst *d )
{
     int y;
     if (!dst || d->pitch <= 0)
          return;
     for (y = 0; y < d->fh; y++)
          memset( dst + (size_t)y * d->pitch, 0, (size_t)d->fw * d->bpp );
}

/* ---- row expanders ---------------------------------------------------- */

static inline unsigned int
rb4r5_px_8888( unsigned int v, int swap )
{
     unsigned int r = (v >> 11) & 0x1f;
     unsigned int g = (v >>  5) & 0x3f;
     unsigned int b =  v        & 0x1f;
     r = (r << 3) | (r >> 2);
     g = (g << 2) | (g >> 4);
     b = (b << 3) | (b >> 2);
     return swap ? (0xff000000u | (b << 16) | (g << 8) | r)
                 : (0xff000000u | (r << 16) | (g << 8) | b);
}

/*
 * Blend two 8-8-8 pixels.  w runs 0..256, and because the weights sum to 256
 * the widest field only reaches 255*256 = 0xff00, so the two channels packed
 * into each half never carry into one another.
 */
static inline unsigned int
rb4r5_lerp888( unsigned int p, unsigned int q, unsigned int w )
{
     unsigned int iw = 256 - w;
     unsigned int rb = ((((p & 0x00ff00ffu) * iw) +
                         ((q & 0x00ff00ffu) * w)) >> 8) & 0x00ff00ffu;
     unsigned int g  = ((((p & 0x0000ff00u) * iw) +
                         ((q & 0x0000ff00u) * w)) >> 8) & 0x0000ff00u;
     return 0xff000000u | rb | g;
}

/*
 * RGB565 -> 8-bit-per-channel, one row.  The NEON version does 8 pixels per
 * iteration; measured on a Pi 5 at 1280 px per row it is ~2.5x the scalar
 * loop (18.8 -> 7.1 ms per 1280x800 -> 1920x1080 frame), which is the
 * difference between ~16 and ~19 presented fps in the live driver.
 */
static void
rb4r5_row_8888( const unsigned short *s, unsigned int *t, int n, int swap )
{
     int i = 0;

#ifdef RB4R5_NEON
     {
          const uint16x8_t m5    = vdupq_n_u16( 0x1f );
          const uint16x8_t m6    = vdupq_n_u16( 0x3f );
          const uint32x4_t alpha = vdupq_n_u32( 0xff000000u );

          for (; i + 8 <= n; i += 8) {
               uint16x8_t v = vld1q_u16( s + i );
               uint16x8_t r = vandq_u16( vshrq_n_u16( v, 11 ), m5 );
               uint16x8_t g = vandq_u16( vshrq_n_u16( v,  5 ), m6 );
               uint16x8_t b = vandq_u16( v, m5 );
               uint16x8_t hi, lo;
               uint32x4_t o0, o1;

               r = vorrq_u16( vshlq_n_u16( r, 3 ), vshrq_n_u16( r, 2 ) );
               g = vorrq_u16( vshlq_n_u16( g, 2 ), vshrq_n_u16( g, 4 ) );
               b = vorrq_u16( vshlq_n_u16( b, 3 ), vshrq_n_u16( b, 2 ) );

               hi = swap ? b : r;          /* channel that lands at bits 16..23 */
               lo = swap ? r : b;          /* channel that lands at bits  0..7  */

               o0 = vorrq_u32( vorrq_u32( vshll_n_u16( vget_low_u16( hi ), 16 ),
                                          vshll_n_u16( vget_low_u16( g ),   8 ) ),
                               vmovl_u16( vget_low_u16( lo ) ) );
               o1 = vorrq_u32( vorrq_u32( vshll_n_u16( vget_high_u16( hi ), 16 ),
                                          vshll_n_u16( vget_high_u16( g ),   8 ) ),
                               vmovl_u16( vget_high_u16( lo ) ) );

               vst1q_u32( t + i,     vorrq_u32( o0, alpha ) );
               vst1q_u32( t + i + 4, vorrq_u32( o1, alpha ) );
          }
     }
#endif
     for (; i < n; i++)
          t[i] = rb4r5_px_8888( s[i], swap );
}

static void
rb4r5_row_bgr565( const unsigned short *s, unsigned short *t, int n )
{
     int i;
     for (i = 0; i < n; i++) {
          unsigned int v = s[i];
          t[i] = (unsigned short)(((v & 0x1f) << 11) | (v & 0x07e0) | (v >> 11));
     }
}

static void
rb4r5_row_rgb555( const unsigned short *s, unsigned short *t, int n )
{
     int i;
     for (i = 0; i < n; i++) {
          unsigned int v = s[i];
          t[i] = (unsigned short)(((v >> 1) & 0x7fe0) | (v & 0x1f));
     }
}

/* Store one already-converted pixel.  Used by the rotation path, which walks
 * the destination out of order and so cannot work a row at a time. */
static inline void
rb4r5_store( unsigned char *p, int fmt, unsigned int src565 )
{
     switch (fmt) {
     case RB_DST_RGB565:
          *(unsigned short *)p = (unsigned short)src565;
          break;
     case RB_DST_BGR565:
          *(unsigned short *)p = (unsigned short)
               (((src565 & 0x1f) << 11) | (src565 & 0x07e0) | (src565 >> 11));
          break;
     case RB_DST_RGB555:
          *(unsigned short *)p = (unsigned short)
               (((src565 >> 1) & 0x7fe0) | (src565 & 0x1f));
          break;
     case RB_DST_RGB888:
     case RB_DST_BGR888: {
          unsigned int v = rb4r5_px_8888( src565, fmt == RB_DST_BGR888 );
          p[0] = (unsigned char)(v & 0xff);
          p[1] = (unsigned char)((v >> 8) & 0xff);
          p[2] = (unsigned char)((v >> 16) & 0xff);
          break;
     }
     default:
          *(unsigned int *)p = rb4r5_px_8888( src565, fmt == RB_DST_XBGR8888 );
          break;
     }
}

/* Store one 8-8-8 pixel (the layer format when the shim reports 32bpp, and
 * what the rotation path carries) into the destination format. */
static inline void
rb4r5_store_argb( unsigned char *p, int fmt, unsigned int argb )
{
     unsigned int r = (argb >> 16) & 0xff;
     unsigned int g = (argb >>  8) & 0xff;
     unsigned int b =  argb        & 0xff;

     switch (fmt) {
     case RB_DST_RGB565:
          *(unsigned short *)p = (unsigned short)
               (((r & 0xf8) << 8) | ((g & 0xfc) << 3) | (b >> 3));
          break;
     case RB_DST_BGR565:
          *(unsigned short *)p = (unsigned short)
               (((b & 0xf8) << 8) | ((g & 0xfc) << 3) | (r >> 3));
          break;
     case RB_DST_RGB555:
          *(unsigned short *)p = (unsigned short)
               (((r & 0xf8) << 7) | ((g & 0xf8) << 2) | (b >> 3));
          break;
     case RB_DST_RGB888:
          p[0] = (unsigned char)b; p[1] = (unsigned char)g; p[2] = (unsigned char)r;
          break;
     case RB_DST_BGR888:
          p[0] = (unsigned char)r; p[1] = (unsigned char)g; p[2] = (unsigned char)b;
          break;
     case RB_DST_XBGR8888:
          *(unsigned int *)p = 0xff000000u | (b << 16) | (g << 8) | r;
          break;
     default:
          *(unsigned int *)p = 0xff000000u | (r << 16) | (g << 8) | b;
          break;
     }
}

/* ---- the publish path ------------------------------------------------- */

/*
 * Bilinear resample of an RGB565 frame into the destination rectangle.
 *
 * Nearest neighbour is wrong for this job.  The RX3 renders 1280x800 and a
 * 22" panel is 1920x1080, so the scale is 1.35x: nearest duplicates about a
 * third of the columns and rows and leaves the rest alone, which on text and
 * on the waveform's one-pixel lines reads as a coarse, uneven, low-resolution
 * picture - exactly the complaint.  Interpolating costs more per pixel and
 * looks like what it is: a 1280x800 image shown larger.
 *
 * Pixel centres are mapped properly (dst centre -> src centre, the -0.5
 * offset), so a 1:1 scale is a bit-exact copy rather than a half-pixel blur,
 * and the edges clamp instead of sampling past the frame.
 *
 * The step is carried in 32.32 rather than 16.16: at 16.16 the truncated step
 * (1280<<16)/1920 loses 0.667 per pixel, which by the right hand side of a
 * 1920 wide panel has drifted far enough to pick visibly different weights -
 * measured against a floating point reference it was out by up to 6.6/255 on
 * a high contrast edge.  A 64 bit accumulator costs an add-with-carry per
 * pixel and takes that to under 1/255.
 *
 * Everything is blended in canonical 8-8-8 (r<<16 | g<<8 | b) and packed into
 * the framebuffer's own format on the way out, so 16bpp destinations get the
 * full precision of the blend before being rounded down to 5-6-5.
 */
/* One destination column: which source column it starts at, and how much of
 * the next one to mix in.  Constant for a given geometry, so it is worked out
 * once and reused - that keeps all the 64 bit arithmetic out of the per pixel
 * loop, which matters on a 32 bit ARM where a 64x64 multiply is several
 * instructions. */
typedef struct {
     int          sc;
     unsigned int w;
} RB4R5Tap;

/* Where destination index i samples from, mapping pixel centre to pixel
 * centre (the -0.5), clamped at both ends so nothing reads past the frame. */
static inline void
rb4r5_tap( RB4R5Tap *tap, int i, int n, unsigned long long step )
{
     const unsigned long long HALF  = 1ULL << 31;   /* half a source pixel   */
     const unsigned long long ROUND = 1ULL << 23;   /* half of 1/256 of one  */
     long long at = (long long)i * (long long)step
                  + (long long)(step / 2) - (long long)HALF;

     if (at < 0)
          at = 0;
     tap->sc = (int)(at >> 32);
     if (tap->sc >= n - 1) {
          tap->sc = n - 1;
          tap->w  = 0;                   /* the last one: nothing beside it */
     }
     else
          tap->w = (unsigned int)
               (((unsigned long long)(at & 0xffffffffULL) + ROUND) >> 24);
}

static void
rb4r5_taps( RB4R5Tap *map, int count, int n, unsigned long long step )
{
     int i;
     for (i = 0; i < count; i++)
          rb4r5_tap( &map[i], i, n, step );
}

/* above*(256-w) + below*w, per channel, for a whole row.  The alpha byte
 * blends with itself and stays 0xff. */
static void
rb4r5_blend_row( const unsigned int *above, const unsigned int *below,
                 unsigned int *out, int n, unsigned int w )
{
     int i = 0;

#ifdef RB4R5_NEON
     {
          const uint8x8_t vw  = vdup_n_u8( (unsigned char)w );
          const uint8x8_t viw = vdup_n_u8( (unsigned char)(256 - w) );

          for (; i + 4 <= n; i += 4) {
               uint8x16_t a = vld1q_u8( (const unsigned char *)(above + i) );
               uint8x16_t b = vld1q_u8( (const unsigned char *)(below + i) );
               uint16x8_t lo = vmlal_u8( vmull_u8( vget_low_u8( a ), viw ),
                                         vget_low_u8( b ), vw );
               uint16x8_t hi = vmlal_u8( vmull_u8( vget_high_u8( a ), viw ),
                                         vget_high_u8( b ), vw );
               vst1q_u8( (unsigned char *)(out + i),
                         vcombine_u8( vshrn_n_u16( lo, 8 ),
                                      vshrn_n_u16( hi, 8 ) ) );
          }
     }
#endif
     for (; i < n; i++)
          out[i] = rb4r5_lerp888( above[i], below[i], w );
}

static void
rb4r5_scale565_bilinear( const unsigned char *src, int sw, int sh, int spitch,
                         unsigned char *dst, const RB4R5Dst *d )
{
     static unsigned int rowbuf[2][RB4R5_ROW_MAX];
     static unsigned int vblend[RB4R5_ROW_MAX];
     static RB4R5Tap     xmap[RB4R5_DST_MAX];
     static int          xmap_n = -1, xmap_w = -1;
     unsigned int *above = rowbuf[0], *below = rowbuf[1];
     int above_row = -1, below_row = -1;
     int n = (sw > RB4R5_ROW_MAX) ? RB4R5_ROW_MAX : sw;
     /* 32.32 fixed point: at 16.16 the truncated step loses 0.667 per pixel,
      * which by the right hand edge of a 1920 wide panel has drifted far
      * enough to pick visibly wrong weights (measured against a floating
      * point reference: out by up to 6.6/255 on a high contrast edge). */
     unsigned long long ystep = ((unsigned long long)sh << 32) / (unsigned)d->h;
     RB4R5Tap ytap;
     int fast565  = (d->fmt == RB_DST_RGB565);
     int fast8888 = (d->fmt == RB_DST_XRGB8888);
     int x, y;

     if (d->w > RB4R5_DST_MAX)
          return;                           /* wider than any panel we handle */

     if (xmap_n != n || xmap_w != d->w) {
          rb4r5_taps( xmap, d->w, n,
                      ((unsigned long long)n << 32) / (unsigned)d->w );
          xmap_n = n;
          xmap_w = d->w;
     }

     for (y = 0; y < d->h; y++) {
          const unsigned int *vrow;
          unsigned char *drow = dst + (size_t)(d->y + y) * d->pitch
                                    + (size_t)d->x * d->bpp;
          int sr, next;

          rb4r5_tap( &ytap, y, sh, ystep );
          sr   = ytap.sc;
          next = (sr + 1 < sh) ? sr + 1 : sr;

          /* keep the two source rows expanded, reusing them as y advances,
           * so each source row is expanded exactly once per frame */
          if (above_row != sr && below_row == sr) {
               unsigned int *swapbuf = above;
               int           swaprow = above_row;
               above = below;   above_row = below_row;
               below = swapbuf; below_row = swaprow;
          }
          if (above_row != sr) {
               rb4r5_row_8888( (const unsigned short *)
                               (src + (size_t)sr * spitch), above, n, 0 );
               above_row = sr;
          }
          if (ytap.w && below_row != next) {
               rb4r5_row_8888( (const unsigned short *)
                               (src + (size_t)next * spitch), below, n, 0 );
               below_row = next;
          }

          if (!ytap.w)
               vrow = above;                 /* the row lands exactly */
          else if (ytap.w >= 256)
               vrow = below;
          else {
               rb4r5_blend_row( above, below, vblend, n, ytap.w );
               vrow = vblend;
          }

          if (fast565) {
               unsigned short *o = (unsigned short *)drow;
               for (x = 0; x < d->w; x++) {
                    unsigned int v = xmap[x].w
                         ? rb4r5_lerp888( vrow[xmap[x].sc],
                                          vrow[xmap[x].sc + 1], xmap[x].w )
                         : vrow[xmap[x].sc];
                    o[x] = (unsigned short)((((v >> 16) & 0xf8) << 8) |
                                            (((v >> 8) & 0xfc) << 3) |
                                            ((v & 0xff) >> 3));
               }
          }
          else if (fast8888) {
               unsigned int *o = (unsigned int *)drow;
               for (x = 0; x < d->w; x++)
                    o[x] = xmap[x].w
                         ? rb4r5_lerp888( vrow[xmap[x].sc],
                                          vrow[xmap[x].sc + 1], xmap[x].w )
                         : vrow[xmap[x].sc];
          }
          else {
               for (x = 0; x < d->w; x++) {
                    unsigned int v = xmap[x].w
                         ? rb4r5_lerp888( vrow[xmap[x].sc],
                                          vrow[xmap[x].sc + 1], xmap[x].w )
                         : vrow[xmap[x].sc];
                    rb4r5_store_argb( drow + (size_t)x * d->bpp, d->fmt, v );
               }
          }
     }
}

/*
 * Scale an RGB565 frame into the destination rectangle, nearest neighbour,
 * 16.16 fixed point.  Every pixel of the rectangle is written, so stale
 * content can never show through.
 */
static void
rb4r5_scale565( const unsigned char *src, int sw, int sh, int spitch,
                unsigned char *dst, const RB4R5Dst *d )
{
     static unsigned int   t32[RB4R5_ROW_MAX];
     static unsigned short t16[RB4R5_ROW_MAX];
     unsigned int xstep, ystep;
     int x, y, n, last_sr = -1;

     if (!src || !dst || sw <= 0 || sh <= 0 || spitch <= 0)
          return;
     if (d->w <= 0 || d->h <= 0 || d->bpp < 2 || d->pitch <= 0)
          return;
     /* never read or expand more than the row cache holds */
     n = (sw > RB4R5_ROW_MAX) ? RB4R5_ROW_MAX : sw;
     /* the rectangle must be inside the framebuffer */
     if (d->x < 0 || d->y < 0 ||
         d->x + d->w > d->fw || d->y + d->h > d->fh ||
         d->fw * d->bpp > d->pitch)
          return;

     if (d->filter) {
          rb4r5_scale565_bilinear( src, sw, sh, spitch, dst, d );
          return;
     }

     xstep = (unsigned int)(((unsigned long long)n  << 16) / (unsigned)d->w);
     ystep = (unsigned int)(((unsigned long long)sh << 16) / (unsigned)d->h);

     for (y = 0; y < d->h; y++) {
          const unsigned short *srow;
          const unsigned short *g16;
          unsigned char *drow = dst + (size_t)(d->y + y) * d->pitch
                                    + (size_t)d->x * d->bpp;
          unsigned int xf = 0;
          int sr = (int)(((unsigned int)y * ystep) >> 16);

          if (sr >= sh)
               sr = sh - 1;
          srow = (const unsigned short *)(src + (size_t)sr * spitch);

          /* expand the source row once, reuse it for every destination row
           * that samples it (1 or 2 rows when scaling 800 -> 1080) */
          if (sr != last_sr) {
               switch (d->fmt) {
               case RB_DST_RGB565:
                    break;                                  /* no conversion */
               case RB_DST_BGR565:
                    rb4r5_row_bgr565( srow, t16, n );
                    break;
               case RB_DST_RGB555:
                    rb4r5_row_rgb555( srow, t16, n );
                    break;
               default:
                    rb4r5_row_8888( srow, t32, n,
                                    d->fmt == RB_DST_XBGR8888 ||
                                    d->fmt == RB_DST_BGR888 );
                    break;
               }
               last_sr = sr;
          }

          switch (d->bpp) {
          case 2: {
               unsigned short *o = (unsigned short *)drow;
               g16 = (d->fmt == RB_DST_RGB565) ? srow : t16;
               if (d->w == n)
                    memcpy( o, g16, (size_t)n * 2 );
               else
                    for (x = 0; x < d->w; x++, xf += xstep)
                         o[x] = g16[xf >> 16];
               break;
          }
          case 4: {
               unsigned int *o = (unsigned int *)drow;
               if (d->w == n)
                    memcpy( o, t32, (size_t)n * 4 );
               else
                    for (x = 0; x < d->w; x++, xf += xstep)
                         o[x] = t32[xf >> 16];
               break;
          }
          default: {                                        /* 24bpp packed */
               unsigned char *o = drow;
               for (x = 0; x < d->w; x++, xf += xstep, o += 3) {
                    unsigned int v = t32[xf >> 16];
                    o[0] = (unsigned char)(v & 0xff);
                    o[1] = (unsigned char)((v >> 8) & 0xff);
                    o[2] = (unsigned char)((v >> 16) & 0xff);
               }
               break;
          }
          }
     }
}

#endif /* RB4R5_SCALE_H */
