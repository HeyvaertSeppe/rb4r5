/*
 * fbpublish - put a 1280x800 RGB565 frame on the panel through exactly the
 * code the patched DirectFB driver uses.
 *
 * The point is to separate two questions that otherwise look identical on a
 * photograph of the screen:
 *
 *   * is the publish path (format, placement, scaling) right?
 *   * is the player actually loading the module that contains it?
 *
 * This binary is built from src/directfb/rb4r5_scale.h - the same header that
 * is compiled into libdirectfb_fbdev.so - so if the picture it puts up is
 * correct while the player's is not, the player is running an older module
 * and needs `launch.py build --fast-directfb`.
 *
 *   fbpublish [-d /dev/fb0] [-f frame.raw] [-a|-s] [-n] [-t seconds]
 *
 *     -f  a raw 1280x800 RGB565 frame (as the player renders); without it a
 *         built-in frame is drawn: a fine grid, 1px lines, small text-sized
 *         detail and colour ramps, which is what shows up scaling artefacts
 *     -a  keep the aspect ratio with black bars (default)
 *     -s  stretch to fill the panel
 *     -n  nearest neighbour instead of bilinear
 *     -t  hold it for this many seconds (default 30), then clear
 */
#include <errno.h>
#include <fcntl.h>
#include <linux/fb.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

#include "../directfb/rb4r5_scale.h"

#define UI_W 1280
#define UI_H 800

static unsigned short frame[UI_H * UI_W];

static void
put( int x, int y, unsigned short px )
{
     if (x >= 0 && x < UI_W && y >= 0 && y < UI_H)
          frame[y * UI_W + x] = px;
}

static unsigned short
rgb( int r, int g, int b )
{
     return (unsigned short)(((r & 0xf8) << 8) | ((g & 0xfc) << 3) | (b >> 3));
}

/* A frame with the things that actually show scaling faults: single pixel
 * lines, a fine checkerboard, text-sized strokes and smooth ramps. */
static void
draw_builtin( void )
{
     int x, y;

     for (y = 0; y < UI_H; y++)
          for (x = 0; x < UI_W; x++)
               frame[y * UI_W + x] = rgb( 12, 14, 18 );      /* the RX3's dark grey */

     /* smooth ramps across the top: banding here means the blend is wrong */
     for (y = 20; y < 90; y++)
          for (x = 0; x < UI_W; x++) {
               int v = x * 255 / (UI_W - 1);
               frame[y * UI_W + x] = (y < 43) ? rgb( v, v, v )
                                   : (y < 66) ? rgb( v, 0, 0 )
                                              : rgb( 0, 0, v );
          }

     /* one pixel horizontal and vertical lines, 8px apart, then 4, then 2:
      * at some spacing a nearest neighbour scale starts dropping them */
     for (x = 0; x < UI_W; x += 8)
          for (y = 110; y < 190; y++)
               put( x, y, rgb( 255, 255, 255 ) );
     for (x = 0; x < UI_W; x += 4)
          for (y = 200; y < 280; y++)
               put( x, y, rgb( 255, 255, 255 ) );
     for (x = 0; x < UI_W; x += 2)
          for (y = 290; y < 370; y++)
               put( x, y, rgb( 255, 255, 255 ) );

     /* a 1px checkerboard: it should look like flat grey, not like a moire */
     for (y = 390; y < 470; y++)
          for (x = 0; x < UI_W; x++)
               if (((x ^ y) & 1) == 0)
                    put( x, y, rgb( 255, 255, 255 ) );

     /* text-sized strokes: 2px wide bars with 2px gaps, the size of the
      * player's own labels */
     for (y = 490; y < 560; y++)
          for (x = 0; x < UI_W; x++)
               if ((x % 4) < 2)
                    put( x, y, rgb( 220, 220, 230 ) );

     /* the frame edges and the centre, to check placement */
     for (x = 0; x < UI_W; x++) {
          put( x, 0, rgb( 255, 255, 0 ) );
          put( x, UI_H - 1, rgb( 255, 255, 0 ) );
     }
     for (y = 0; y < UI_H; y++) {
          put( 0, y, rgb( 255, 255, 0 ) );
          put( UI_W - 1, y, rgb( 255, 255, 0 ) );
     }
     for (x = UI_W / 2 - 80; x <= UI_W / 2 + 80; x++)
          for (y = UI_H / 2 - 2; y <= UI_H / 2 + 2; y++)
               put( x, y, rgb( 0, 255, 0 ) );
     for (y = UI_H / 2 - 80; y <= UI_H / 2 + 80; y++)
          for (x = UI_W / 2 - 2; x <= UI_W / 2 + 2; x++)
               put( x, y, rgb( 0, 255, 0 ) );

     /* corner blocks, so a cropped or shifted frame is unmistakable */
     for (y = 0; y < 40; y++)
          for (x = 0; x < 40; x++) {
               put( x, y, rgb( 255, 0, 0 ) );
               put( UI_W - 1 - x, y, rgb( 0, 255, 0 ) );
               put( x, UI_H - 1 - y, rgb( 0, 0, 255 ) );
               put( UI_W - 1 - x, UI_H - 1 - y, rgb( 255, 255, 255 ) );
          }
}

int
main( int argc, char **argv )
{
     const char *dev = "/dev/fb0", *path = NULL;
     int aspect = 1, filter = 1, seconds = 30, keep = 0, reserve = 0;
     struct fb_var_screeninfo var;
     struct fb_fix_screeninfo fix;
     RB4R5Dst d;
     unsigned char *map;
     size_t maplen;
     int fd, opt;

     while ((opt = getopt( argc, argv, "d:f:asnt:r:kh" )) != -1) {
          switch (opt) {
          case 'd': dev = optarg; break;
          case 'f': path = optarg; break;
          case 'a': aspect = 1; break;
          case 's': aspect = 0; break;
          case 'n': filter = 0; break;
          case 't': seconds = atoi( optarg ); break;
          case 'r': reserve = atoi( optarg ); break;
          case 'k': keep = 1; break;
          default:
               fprintf( stderr, "usage: %s [-d fbdev] [-f frame.raw] "
                                "[-a|-s] [-n] [-t seconds] [-r rows] [-k]\n",
                                argv[0] );
               return 2;
          }
     }

     if (path) {
          FILE *handle = fopen( path, "rb" );
          size_t got;
          if (!handle) {
               fprintf( stderr, "cannot open %s: %s\n", path, strerror( errno ) );
               return 1;
          }
          got = fread( frame, 1, sizeof(frame), handle );
          fclose( handle );
          if (got != sizeof(frame)) {
               fprintf( stderr, "%s holds %zu bytes, a 1280x800 RGB565 frame "
                                "is %zu\n", path, got, sizeof(frame) );
               return 1;
          }
     }
     else
          draw_builtin();

     fd = open( dev, O_RDWR );
     if (fd < 0) {
          fprintf( stderr, "cannot open %s: %s\n", dev, strerror( errno ) );
          return 1;
     }
     if (ioctl( fd, FBIOGET_VSCREENINFO, &var ) < 0 ||
         ioctl( fd, FBIOGET_FSCREENINFO, &fix ) < 0) {
          fprintf( stderr, "cannot read the screen info: %s\n", strerror( errno ) );
          close( fd );
          return 1;
     }

     rb4r5_dst_init( &d, (int)var.bits_per_pixel,
                     (int)var.red.offset,   (int)var.red.length,
                     (int)var.green.offset, (int)var.green.length,
                     (int)var.blue.offset,  (int)var.blue.length,
                     (int)var.xres, (int)var.yres, (int)fix.line_length,
                     UI_W, UI_H, reserve, aspect, filter );

     printf( "%s: %ux%u %s (%u bpp, pitch %u)%s\n", dev, var.xres, var.yres,
             rb4r5_fmt_name( d.fmt ), var.bits_per_pixel, fix.line_length,
             d.guessed ? "  [layout not recognised, assumed]" : "" );
     printf( "frame %dx%d -> %d,%d %dx%d   %s, %s\n", UI_W, UI_H,
             d.x, d.y, d.w, d.h, aspect ? "aspect (black bars)" : "stretched",
             filter ? "bilinear" : "nearest neighbour" );
     if (d.x || d.y)
          printf( "bars: %d px at each side, %d px top and bottom\n",
                  d.x, d.y );

     maplen = (size_t)d.pitch * (size_t)d.fh;
     map = mmap( NULL, maplen, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0 );
     if (map == MAP_FAILED) {
          fprintf( stderr, "cannot map %s: %s\n", dev, strerror( errno ) );
          close( fd );
          return 1;
     }

     rb4r5_clear( map, &d );
     rb4r5_scale565( (const unsigned char *)frame, UI_W, UI_H, UI_W * 2, map, &d );

     printf( "published; holding for %ds\n", seconds );
     fflush( stdout );
     if (seconds > 0)
          sleep( (unsigned)seconds );
     if (!keep)
          rb4r5_clear( map, &d );

     munmap( map, maplen );
     close( fd );
     return 0;
}
