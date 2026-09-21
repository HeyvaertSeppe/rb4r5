#!/bin/sh
# build-directfb.sh - build the patched DirectFB 1.4.16 stack the player needs.
#
# rb renders through DirectFB 1.4.  The RX3's own DirectFB core is too old for
# the patched fbdev driver, so the whole core is rebuilt soft-float against the
# RX3 userland, with three layers of changes:
#
#   1. PrimeBox's directfb-full.diff  - soft-float/fbdev groundwork for this
#      player on non-Pioneer hardware (serialized fb ioctls, forced real fb
#      format, FRONTONLY fallback, the RGB565->RGB32 rotate/scale path)
#   2. directfb-pi5.patch + rb4r5_scale.h - run the convert+scale path with
#      rotation off, publish on UpdateRegion (not just FlipRegion), always write
#      physical buffer 0, scale 1280x800 -> the real mode in the fb's own pixel
#      format (RGB565 or XRGB8888, whichever the kernel handed out - see
#      rb4r5_scale.h), NEON row expander, no versioned fcntl()
#
# Runs on the Pi itself (or any machine with the armel cross toolchain).
#
#   REPO=<repo> RX3=<chroot> PRIMEBOX=<PrimeBox checkout> \
#   OUT=<work>/dfb BUILD=<work>/dfb-build SYS=<work>/sysroot NEON=1 \
#       sh src/directfb/build-directfb.sh
#
# Produces in $OUT/lib:
#   libdirectfb-1.4.so.0.0.0  libdirect-1.4.so.0.0.0  libfusion-1.4.so.0.0.0
#   directfb-1.4-6/systems/libdirectfb_fbdev.so
#   directfb-1.4-6/wm/libdirectfbwm_default.so
#   directfb-1.4-6/inputdrivers/libdirectfb_{linux_input,keyboard}.so
set -eu

REPO=${REPO:-$(cd "$(dirname "$0")/../.." && pwd)}
RX3=${RX3:-/opt/rb4r5/chroot}
PRIMEBOX=${PRIMEBOX:-/opt/rb4r5/PrimeBox}
DFBDIFF=${DFBDIFF:-$PRIMEBOX/tools/build-directfb/directfb-full.diff}
OURPATCH=$REPO/src/directfb/directfb-pi5.patch
SCALEHDR=$REPO/src/directfb/rb4r5_scale.h
SYS=${SYS:-/opt/rb4r5/work/sysroot}
BUILD=${BUILD:-/opt/rb4r5/work/dfb-build}
OUT=${OUT:-/opt/rb4r5/work/dfb}
NEON=${NEON:-1}
JOBS=${JOBS:-$(nproc 2>/dev/null || echo 4)}
CROSS=${CROSS:-arm-linux-gnueabi-}
DFBURL=${DFBURL:-https://github.com/deniskropp/DirectFB.git}
FLUXSRC=${FLUXSRC:-$(dirname "$BUILD")/flux}
FLUXURL=${FLUXURL:-https://github.com/deniskropp/flux.git}
BUILDTRIPLET=${BUILDTRIPLET:-$(cc -dumpmachine)}

die() { echo "build-directfb: $*" >&2; exit 1; }

[ -d "$RX3/lib" ]  || die "no RX3 userland at $RX3/lib (run: launch.py payload)"
[ -f "$DFBDIFF" ]  || die "PrimeBox DirectFB diff not found: $DFBDIFF"
[ -f "$OURPATCH" ] || die "our patch not found: $OURPATCH"
[ -f "$SCALEHDR" ] || die "scaler header not found: $SCALEHDR"
command -v ${CROSS}gcc >/dev/null || die "missing ${CROSS}gcc (apt install gcc-arm-linux-gnueabi)"

echo "== 1. soft-float sysroot ($SYS)"
rm -rf "$SYS"
mkdir -p "$SYS/lib" "$SYS/usr/lib" "$SYS/usr/include"
cp -a "$RX3/lib/."     "$SYS/lib/"
cp -a "$RX3/usr/lib/." "$SYS/usr/lib/"
# headers come from the cross toolchain; the RX3 rootfs has no /usr/include
CROSSINC=$(dirname "$(${CROSS}gcc -print-file-name=libc.so 2>/dev/null)")/../include
for inc in /usr/arm-linux-gnueabi/include "$CROSSINC"; do
    [ -d "$inc" ] && { cp -a "$inc/." "$SYS/usr/include/"; break; }
done
[ -f "$SYS/usr/include/stdio.h" ] || die "no cross headers found (apt install libc6-dev-armel-cross)"

# glibc 2.13's runtime exports neither fstat nor __fdelt_chk, and modern glibc
# moved atexit into libc_nonshared.a - append our replacements.
cp -f /usr/arm-linux-gnueabi/lib/libc_nonshared.a "$SYS/usr/lib/libc_nonshared.a" 2>/dev/null || \
    ${CROSS}ar rcs "$SYS/usr/lib/libc_nonshared.a"
${CROSS}gcc -O2 -march=armv5t -mfloat-abi=soft -fPIC \
    -c "$REPO/src/shims/compat_nonshared.c" -o "$SYS/compat_nonshared.o"
${CROSS}ar r "$SYS/usr/lib/libc_nonshared.a" "$SYS/compat_nonshared.o"
${CROSS}ar rcs "$SYS/usr/lib/libpthread_nonshared.a"

echo "== 2. DirectFB 1.4.16 + patches ($BUILD)"
rm -rf "$BUILD"
git clone -q "$DFBURL" "$BUILD" || die "cannot clone DirectFB from $DFBURL"
cd "$BUILD"
git checkout -q origin/directfb-1.4        # == 1.4.16
patch -p1 --quiet -F3 < "$DFBDIFF"  || die "PrimeBox diff did not apply"
cp -f "$SCALEHDR" systems/fbdev/rb4r5_scale.h
patch -p1 --quiet -F3 < "$OURPATCH" || die "directfb-pi5.patch did not apply (see *.rej)"
# PrimeBox's diff leaves a per-allocation debug write in the surface pool;
# it is pure overhead here.  Harmless if the line is already gone.
sed -i 's@^.*fopen("/tmp/dfbdig9.log".*$@/* rb4r5: debug log removed */@' \
    systems/fbdev/fbdev_surface_pool.c 2>/dev/null || true
printf 'int dfb_fbdev_compat_shim(void){return 0;}\n' > systems/fbdev/compat_shim.c

# ---------------------------------------------------------------------------
# The git tree ships *.flux, not the C and H files fluxcomp generates from
# them - the 1.4.16 release tarball shipped those pre-generated, git does not.
# Without this step the build dies with:
#     CoreSlave_real.c:31:10: fatal error: core/CoreSlave.h: No such file
# and configure never warns, because it does not check for fluxcomp at all.
# ---------------------------------------------------------------------------
echo "== 2b. fluxcomp (generates CoreSlave.h and friends from *.flux)"
if command -v fluxcomp >/dev/null 2>&1; then
    echo "    using the fluxcomp already installed"
elif [ -x "$FLUXSRC/src/fluxcomp" ]; then
    echo "    using $FLUXSRC/src/fluxcomp"
    PATH="$FLUXSRC/src:$PATH"; export PATH
else
    echo "    building it from $FLUXURL"
    rm -rf "$FLUXSRC"
    git clone -q --depth 1 "$FLUXURL" "$FLUXSRC" || \
        die "cannot clone $FLUXURL - fluxcomp is needed to generate the
    DirectFB core sources.  Clone it on a machine with network, copy it to
    $FLUXSRC, and re-run."
    ( cd "$FLUXSRC" && \
      { [ -x ./configure ] || ./autogen.sh >autogen.log 2>&1 || true; } && \
      ./configure >configure.log 2>&1 && \
      make -j"$JOBS" >make.log 2>&1 ) || \
        die "fluxcomp did not build (see $FLUXSRC/make.log).  It needs a C++
    compiler and autotools: apt install build-essential autoconf automake libtool"
    [ -x "$FLUXSRC/src/fluxcomp" ] || die "no fluxcomp binary at $FLUXSRC/src"
    PATH="$FLUXSRC/src:$PATH"; export PATH
fi

echo "== 2c. generating the core sources"
for dir_args in "src/core:-c -i --include-prefix=core" "src/media:-c -i --no-direct"; do
    dir=${dir_args%%:*}
    args=${dir_args#*:}
    [ -d "$BUILD/$dir" ] || continue
    ( cd "$BUILD/$dir" || exit 0
      for f in *.flux; do
          [ -e "$f" ] || continue          # no flux sources here
          # shellcheck disable=SC2086
          fluxcomp $args "$f" || exit 1
      done ) || die "fluxcomp failed in $dir"
done
[ -f "$BUILD/src/core/CoreSlave.h" ] || \
    die "the core sources were not generated (no src/core/CoreSlave.h).
    fluxcomp ran but produced nothing - check $FLUXSRC/make.log"
echo "    generated $(ls "$BUILD"/src/core/Core*.h 2>/dev/null | wc -l) headers"

echo "== 3. configure"
export CC=${CROSS}gcc CXX=${CROSS}g++
export CFLAGS="-march=armv5t -mfloat-abi=soft --sysroot=$SYS"
export CPPFLAGS="--sysroot=$SYS"
export LDFLAGS="--sysroot=$SYS -L$SYS/usr/lib -L$SYS/lib -Wl,-rpath-link,$SYS/lib:$SYS/usr/lib"
./autogen.sh --host=arm-linux-gnueabi --build="$BUILDTRIPLET" --prefix=/usr \
    --disable-x11 --disable-sdl --disable-vnc --disable-avifile \
    --with-gfxdrivers=none --disable-osx --disable-devmem \
    --disable-freetype --disable-png --disable-jpeg --disable-gif \
    --disable-tiff --disable-libmpeg3 --disable-imlib2 \
    --disable-video4linux --disable-video4linux2 --disable-dvb --disable-alsa \
    > "$BUILD/configure.log" 2>&1 || { tail -40 "$BUILD/configure.log"; die "configure failed"; }

echo "== 4. build (-j$JOBS)"
make -j"$JOBS" LDFLAGS="$LDFLAGS" > "$BUILD/make.log" 2>&1 || {
    grep -iE "error:|undefined reference" "$BUILD/make.log" | head -20
    die "make failed (full log: $BUILD/make.log)"; }

# The fbdev module gets the NEON flags; softfp (not hard) keeps the base
# calling convention, so it stays link-compatible with the soft-float core.
# That is what makes __ARM_NEON true in rb4r5_scale.h - without it the module
# still works, just with the scalar row expander.
if [ "$NEON" = 1 ]; then
    echo "== 4b. rebuild systems/fbdev with NEON"
    BASECFLAGS=$(sed -n 's/^CFLAGS = //p' systems/fbdev/Makefile)
    rm -f systems/fbdev/fbdev.lo systems/fbdev/.libs/fbdev.o \
          systems/fbdev/.libs/libdirectfb_fbdev.so
    make -C systems/fbdev \
        CFLAGS="$BASECFLAGS -march=armv7-a -mfpu=neon -mfloat-abi=softfp" \
        libdirectfb_fbdev.la >> "$BUILD/make.log" 2>&1 || die "NEON rebuild failed"
fi

echo "== 5. stage + soname fixups -> $OUT"
rm -rf "$OUT"
mkdir -p "$OUT/lib/directfb-1.4-6/systems" \
         "$OUT/lib/directfb-1.4-6/wm" \
         "$OUT/lib/directfb-1.4-6/inputdrivers"
cp src/.libs/libdirectfb-1.4.so.6.0.10        "$OUT/lib/libdirectfb-1.4.so.0.0.0"
cp lib/direct/.libs/libdirect-1.4.so.6.0.10   "$OUT/lib/libdirect-1.4.so.0.0.0"
cp lib/fusion/.libs/libfusion-1.4.so.6.0.10   "$OUT/lib/libfusion-1.4.so.0.0.0"
cp systems/fbdev/.libs/libdirectfb_fbdev.so   "$OUT/lib/directfb-1.4-6/systems/"
cp wm/default/.libs/libdirectfbwm_default.so  "$OUT/lib/directfb-1.4-6/wm/"
cp inputdrivers/linux_input/.libs/libdirectfb_linux_input.so \
   inputdrivers/keyboard/.libs/libdirectfb_keyboard.so \
   "$OUT/lib/directfb-1.4-6/inputdrivers/"

cd "$OUT/lib"
command -v patchelf >/dev/null || die "missing patchelf (apt install patchelf)"
# The RX3 userland has the .so.0 names; ours build as .so.6.  Rewrite both the
# sonames AND the NEEDED entries - libfusion is dlopen'd by libdirectfb and
# would otherwise look for libdirect-1.4.so.6 at runtime.
patchelf --set-soname libdirectfb-1.4.so.0 libdirectfb-1.4.so.0.0.0
patchelf --set-soname libdirect-1.4.so.0   libdirect-1.4.so.0.0.0
patchelf --set-soname libfusion-1.4.so.0   libfusion-1.4.so.0.0.0
for f in libdirectfb-1.4.so.0.0.0 libdirect-1.4.so.0.0.0 libfusion-1.4.so.0.0.0 \
         directfb-1.4-6/systems/libdirectfb_fbdev.so \
         directfb-1.4-6/wm/libdirectfbwm_default.so \
         directfb-1.4-6/inputdrivers/*.so; do
    for s in libdirect-1.4.so.6 libfusion-1.4.so.6 libdirectfb-1.4.so.6; do
        patchelf --replace-needed "$s" "${s%.6}.0" "$f" 2>/dev/null || true
    done
done
ln -sf libdirectfb-1.4.so.0.0.0 libdirectfb-1.4.so.0
ln -sf libdirect-1.4.so.0.0.0   libdirect-1.4.so.0
ln -sf libfusion-1.4.so.0.0.0   libfusion-1.4.so.0

echo "== done.  GLIBC symbol versions (must be <= 2.13):"
for f in libdirectfb-1.4.so.0.0.0 libdirect-1.4.so.0.0.0 libfusion-1.4.so.0.0.0 \
         directfb-1.4-6/systems/libdirectfb_fbdev.so; do
    printf '%-48s ' "$f"
    ${CROSS}objdump -T "$f" | grep -o 'GLIBC_[0-9.]*' | sort -u | tr '\n' ' '
    echo
done
md5sum directfb-1.4-6/systems/libdirectfb_fbdev.so
