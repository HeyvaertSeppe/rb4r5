# rb4r5 host daemons - built with the Pi's own gcc (no cross, no dependencies).
#
#   flx4-bridge   DDJ-FLX4 USB MIDI -> /tmp/rb-ctrl.fifo
#   rbkeyd        USB keyboard      -> /tmp/rb-keys.fifo + /tmp/rb-ctrl.fifo
#   fakekbd       uinput test keyboard (for testing without hardware)
#
# Usage:  make OUT=/opt/rb4r5/work/host

CC      ?= cc
CFLAGS  ?= -O2 -Wall -Wextra -Wno-unused-parameter
OUT     ?= .

TOOLS := $(OUT)/flx4-bridge $(OUT)/rbkeyd $(OUT)/fakekbd

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

clean:
	rm -f $(TOOLS)
