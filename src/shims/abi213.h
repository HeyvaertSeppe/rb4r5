/* abi213.h - keep a shim's C library calls to what glibc 2.13 has.
 *
 * The shims run inside the RX3's player, against ITS glibc, 2.13 - but they
 * are compiled with whatever headers the build machine has.  From glibc 2.38
 * those headers quietly rename strtol, strtoul, sscanf (and atoi/atol, which
 * are inline strtol) to __isoc23_* functions that glibc 2.13 has never heard
 * of: the shim still builds, and then cannot resolve them inside the player.
 * Include this LAST, after every system header; it points the calls back at
 * the plain symbols.  (The 64-bit time/offset defaults of the same newer
 * toolchains are undone in the Makefile: -U_TIME_BITS -U_FILE_OFFSET_BITS.)
 */
#ifndef RB4R5_ABI213_H
#define RB4R5_ABI213_H

#if defined(__GLIBC__) && (__GLIBC__ > 2 || __GLIBC_MINOR__ >= 38)
extern long rb213_strtol(const char *, char **, int) __asm__("strtol");
extern unsigned long rb213_strtoul(const char *, char **, int) __asm__("strtoul");
extern int rb213_sscanf(const char *, const char *, ...) __asm__("sscanf");
#undef strtol
#undef strtoul
#undef sscanf
#undef atoi
#undef atol
#define strtol  rb213_strtol
#define strtoul rb213_strtoul
#define sscanf  rb213_sscanf
#define atoi(s) ((int)rb213_strtol((s), (char **)0, 10))
#define atol(s) (rb213_strtol((s), (char **)0, 10))
#endif

#endif
