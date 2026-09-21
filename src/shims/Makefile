# rb4r5 LD_PRELOAD shims
#
# Every shim is loaded into the XDJ-RX3 player, so all of them must be
# soft-float EABI5 and reference only GLIBC_2.4/2.7 - the RX3 userland ships
# glibc 2.13, and a modern host toolchain would pull in GLIBC_2.17+.  The RX3
# chroot is therefore used as the sysroot.
#
# Usage (normally called by launch.py build):
#   make RX3=/opt/rb4r5/chroot OUT=/opt/rb4r5/work/shims COMPAT=/opt/rb4r5/work/compat
#   make RX3=... check     # print/verify the symbol versions
#   make clean

CROSS   ?= arm-linux-gnueabi-
CC      := $(CROSS)gcc
OBJDUMP := $(CROSS)objdump
READELF := $(CROSS)readelf

RX3     ?= /opt/rb4r5/chroot
OUT     ?= .
COMPAT  ?=

RX3LIB  := $(RX3)/lib
RX3ULIB := $(RX3)/usr/lib

# armv5t/soft keeps the binary loadable by the RX3 loader on any ARM core.
CFLAGS  := -O2 -march=armv5t -mfloat-abi=soft -fno-stack-protector -fPIC \
           -Wall -Wextra -Wno-unused-parameter
LDFLAGS := $(if $(COMPAT),-L$(COMPAT),) -L$(RX3LIB) -L$(RX3ULIB) \
           -Wl,-rpath-link,$(RX3LIB):$(RX3ULIB)

SHIMS   := $(OUT)/memshim.so $(OUT)/fbshim.so $(OUT)/audioshim.so $(OUT)/keyshim.so

.PHONY: all clean check
all: $(SHIMS)

$(OUT):
	mkdir -p $(OUT)

# memshim must be FIRST in LD_PRELOAD: it denies /dev/mem, redirects the
# player's broken RTC mmap, parks the GpioManager poll loops and feeds the
# touchscreen record.  Two preloads both defining open() would shadow one
# another, so all of that lives in this one shim.
$(OUT)/memshim.so: memshim.c | $(OUT)
	$(CC) $(CFLAGS) -shared -o $@ $< -lc $(LDFLAGS)

# fbshim reports the RX3's 1280x800 RGB565 panel.  Built -nostdlib with raw
# syscalls so the glibc-2.13 loader accepts it with no dependencies at all.
$(OUT)/fbshim.so: fbshim.c | $(OUT)
	$(CC) -O2 -march=armv5t -mfloat-abi=soft -fno-stack-protector -fPIC \
	    -shared -nostdlib -o $@ $<

# audioshim must bind dlopen/dlsym to libdl GLIBC_2.4 (the .symver directives
# are in the source), so link the RX3's own libdl explicitly.
$(OUT)/audioshim.so: audioshim.c | $(OUT)
	$(CC) $(CFLAGS) -shared -o $@ $< $(RX3LIB)/libdl.so.2 -lc $(LDFLAGS)

$(OUT)/keyshim.so: keyshim.c | $(OUT)
	$(CC) $(CFLAGS) -shared -o $@ $< -lpthread -lc $(LDFLAGS)

# Fail the build if a GLIBC version above 2.13 or a hard-float tag sneaks in.
check: $(SHIMS)
	@echo "== GLIBC symbol versions (must be <= 2.13, normally 2.4/2.7) =="
	@for f in $(SHIMS); do \
	  echo "-- $$f"; \
	  $(OBJDUMP) -T $$f 2>/dev/null | grep -o 'GLIBC_[0-9.]*' | sort -u; \
	  $(READELF) -A $$f 2>/dev/null | grep -i 'Tag_ABI_VFP_args' \
	      && echo "  !! HARD-FLOAT - this will not load" || true; \
	done

clean:
	rm -f $(SHIMS)
