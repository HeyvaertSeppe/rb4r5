#!/bin/sh
# rebuild-fbdev.sh - rebuild ONLY the DirectFB fbdev driver from an existing
# build tree, fix the sonames, check the ABI and stage it.
#
# Use this while iterating on the display path; build-directfb.sh does the
# first (full) build.  The tree must already have all patches applied.
#
#   REPO=<repo> BUILD=<work>/dfb-build OUT=<work>/dfb NEON=1 \
#       sh src/directfb/rebuild-fbdev.sh
set -eu

BUILD=${BUILD:-/opt/rb4r5/work/dfb-build}
OUT=${OUT:-/opt/rb4r5/work/dfb}
CROSS=${CROSS:-arm-linux-gnueabi-}
NEON=${NEON:-1}

[ -d "$BUILD/systems/fbdev" ] || {
    echo "no DirectFB build tree at $BUILD - run build-directfb.sh first" >&2
    exit 1
}

cd "$BUILD"
# pick up edits to the scaler header, which lives in the repo, not the tree
if [ -n "${REPO:-}" ] && [ -f "$REPO/src/directfb/rb4r5_scale.h" ]; then
    cp -f "$REPO/src/directfb/rb4r5_scale.h" systems/fbdev/rb4r5_scale.h
fi
FBDEV_CFLAGS=""
if [ "$NEON" = 1 ]; then
    # softfp keeps the soft-float calling convention (no Tag_ABI_VFP_args), so
    # the module stays link-compatible with the soft-float core and player.
    FBDEV_CFLAGS="-march=armv7-a -mfpu=neon -mfloat-abi=softfp"
    echo "== rebuild systems/fbdev (NEON: $FBDEV_CFLAGS)"
else
    echo "== rebuild systems/fbdev (scalar)"
fi
BASECFLAGS=$(sed -n 's/^CFLAGS = //p' systems/fbdev/Makefile)

# DirectFB 1.4's dependency tracking misses edits to fbdev.c: delete first.
rm -f systems/fbdev/fbdev.lo systems/fbdev/.libs/fbdev.o \
      systems/fbdev/.libs/libdirectfb_fbdev.so
make -C systems/fbdev CFLAGS="$BASECFLAGS $FBDEV_CFLAGS" libdirectfb_fbdev.la

SO=systems/fbdev/.libs/libdirectfb_fbdev.so
echo "== fix NEEDED sonames (.so.6 -> .so.0, matching the RX3 libs)"
for s in libdirect-1.4.so.6 libfusion-1.4.so.6 libdirectfb-1.4.so.6; do
    patchelf --replace-needed "$s" "${s%.6}.0" "$SO" 2>/dev/null || true
done
${CROSS}objdump -p "$SO" | grep NEEDED

echo "== GLIBC symbol versions (must be <= 2.13)"
${CROSS}objdump -T "$SO" | grep -o 'GLIBC_[0-9.]*' | sort -u

mkdir -p "$OUT/lib/directfb-1.4-6/systems"
cp "$SO" "$OUT/lib/directfb-1.4-6/systems/libdirectfb_fbdev.so"
echo "staged: $OUT/lib/directfb-1.4-6/systems/libdirectfb_fbdev.so"
md5sum "$OUT/lib/directfb-1.4-6/systems/libdirectfb_fbdev.so"
