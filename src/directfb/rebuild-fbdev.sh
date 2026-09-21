#!/bin/sh
# rebuild-fbdev.sh - rebuild ONLY the DirectFB fbdev driver from an existing
# build tree, fix the sonames, check the ABI and stage it.
#
# Use this while iterating on the display path; build-directfb.sh does the
# first (full) build.
#
# It re-applies the patches before compiling.  It has to: recompiling a tree
# that still holds the previous version of directfb-pi5.patch produces a
# byte-identical module, the install step copies it over the good one, and the
# player goes on using the old display path with nothing anywhere saying so.
#
#   REPO=<repo> PRIMEBOX=<checkout> BUILD=<work>/dfb-build OUT=<work>/dfb \
#       NEON=1 sh src/directfb/rebuild-fbdev.sh
set -eu

BUILD=${BUILD:-/opt/rb4r5/work/dfb-build}
OUT=${OUT:-/opt/rb4r5/work/dfb}
CROSS=${CROSS:-arm-linux-gnueabi-}
NEON=${NEON:-1}
PRIMEBOX=${PRIMEBOX:-/opt/rb4r5/PrimeBox}
DFBDIFF=${DFBDIFF:-$PRIMEBOX/tools/build-directfb/directfb-full.diff}

die() { echo "rebuild-fbdev: $*" >&2; exit 1; }

# the files directfb-pi5.patch owns
PATCHED="systems/fbdev/fbdev.c systems/fbdev/fbdev.h \
         systems/fbdev/fbdev_surface_pool.c \
         inputdrivers/linux_input/linux_input.c"

[ -d "$BUILD/systems/fbdev" ] || {
    echo "no DirectFB build tree at $BUILD - run build-directfb.sh first" >&2
    exit 1
}

cd "$BUILD"

# ---------------------------------------------------------------------------
# Re-apply the patch stack to the files we own, so what gets compiled is what
# is in the repository right now.  The tree is a git clone, so the pristine
# upstream copies are one checkout away; PrimeBox's diff then goes back on
# (-N skips the hunks that are still applied in the files we did not touch),
# and ours on top of that.
# ---------------------------------------------------------------------------
if [ -n "${REPO:-}" ] && [ -f "$REPO/src/directfb/directfb-pi5.patch" ] && \
   [ -d .git ] && command -v git >/dev/null 2>&1; then
    echo "== re-applying the patches (so a stale tree cannot be rebuilt as-is)"
    # shellcheck disable=SC2086
    git checkout -- $PATCHED || die "cannot restore $PATCHED from git"
    if [ -f "$DFBDIFF" ]; then
        # already-applied hunks elsewhere in the tree are skipped, which patch
        # reports with a non-zero exit; the files we restored do get theirs
        patch -p1 -N -F3 --quiet < "$DFBDIFF" >/dev/null 2>&1 || true
    else
        die "PrimeBox diff not found: $DFBDIFF (set PRIMEBOX=)"
    fi
    cp -f "$REPO/src/directfb/rb4r5_scale.h" systems/fbdev/rb4r5_scale.h
    patch -p1 -F3 --quiet < "$REPO/src/directfb/directfb-pi5.patch" || \
        die "directfb-pi5.patch did not apply (see *.rej under $BUILD)"
    sed -i 's@^.*fopen("/tmp/dfbdig9.log".*$@/* rb4r5: debug log removed */@' \
        systems/fbdev/fbdev_surface_pool.c 2>/dev/null || true
    grep -q rb4r5_scale systems/fbdev/fbdev.c || \
        die "fbdev.c does not include the scaler after patching - stop"
else
    echo "== not a git tree (or no REPO): compiling it as it stands"
    [ -n "${REPO:-}" ] && [ -f "$REPO/src/directfb/rb4r5_scale.h" ] && \
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

# DirectFB 1.4's dependency tracking misses edits to fbdev.c, and the patch
# above touches more than one file: delete the objects first.
rm -f systems/fbdev/*.lo systems/fbdev/.libs/*.o \
      systems/fbdev/.libs/libdirectfb_fbdev.so
make -C systems/fbdev CFLAGS="$BASECFLAGS $FBDEV_CFLAGS" libdirectfb_fbdev.la

SO=systems/fbdev/.libs/libdirectfb_fbdev.so
echo "== checking the module really carries the current publish path"
for marker in "FBDev/rb4r5:" "bilinear"; do
    strings "$SO" | grep -qF "$marker" || die "the module just built does not
    contain \"$marker\" - it was compiled from an older source tree.  Delete
    $BUILD and run a full build: sudo python3 launch.py build"
done
echo "   both markers present"
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
