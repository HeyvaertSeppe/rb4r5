# rb4r5 host daemons - built with the Pi's own gcc (no cross, no dependencies).
#
#   flx4-bridge   DDJ-FLX4 USB MIDI -> /tmp/rb-ctrl.fifo
#   rbkeyd        USB keyboard      -> /tmp/rb-keys.fifo + /tmp/rb-ctrl.fifo
#   fakekbd       uinput test keyboard (for testing without hardware)
#   fbpublish     a 1280x800 frame -> the panel, through the driver's own
#                 publish path (src/directfb/rb4r5_scale.h)
#
# Usage:  make OUT=/opt/rb4r5/work/host

CC      ?= cc
CFLAGS  ?= -O2 -Wall -Wextra -Wno-unused-parameter
OUT     ?= .

TOOLS := $(OUT)/flx4-bridge $(OUT)/rbkeyd $(OUT)/fakekbd $(OUT)/fbpublish

.PHONY: all clean
all: $(TOOLS)

$(OUT):
	mkdir -p $(OUT)

$(OUT)/flx4-bridge: flx4-bridge.c | $(OUT)
	$(CC) $(CFLAGS) -o $@ $<

$(OUT)/rbkeyd: rbkeyd.c | $(OUT)
	$(CC) $(CFLAGS) -o $@ $<

$(OUT)/fakekbd: fakekbd.c | $(OUT)
	$(CC) $(CFLAGS) -o $@ $<

# NEON where the compiler knows the target has it (always on aarch64, which is
# what Raspberry Pi OS is); harmless everywhere else.
$(OUT)/fbpublish: fbpublish.c ../directfb/rb4r5_scale.h | $(OUT)
	$(CC) $(CFLAGS) -o $@ $<

clean:
	rm -f $(TOOLS)
