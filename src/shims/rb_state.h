/* rb_state.h - what the player itself says its lamps and meters are doing.
 *
 * keyshim.so (inside rbp) fills this from the engine's own state - the
 * PlayEngine transport flags, uif::LedStat (the table every LED write in rbp
 * goes through), the mixer's headphone-cue flags and rbp's own per-channel
 * level meters - and writes it to RB_STATE_PATH about forty times a second.
 * flx4-bridge reads it and lights the FLX4 the way the RX3 lights its panel.
 *
 * Shared by a 32-bit soft-float ARM build (the shim) and a 64-bit host build
 * (the bridge), so it is only fixed-width fields at their natural alignment
 * and its size is pinned below.
 *
 * A record is whole when seq == seq_end; the reader drops any other.
 */
#ifndef RB_STATE_H
#define RB_STATE_H

#include <stdint.h>

#ifndef RB_STATE_PATH
#define RB_STATE_PATH     "/tmp/rb-state.dat"
#endif
#define RB_STATE_MAGIC    0x54534252u        /* "RBST" little-endian */
#define RB_STATE_VERSION  1u

/* flags: which parts of the record were actually read from the engine */
#define RBS_ENGINE   0x01u    /* PlayEngine transport flags               */
#define RBS_LEDSTAT  0x02u    /* uif::LedStat (play/sync/pads/fx lamps)   */
#define RBS_METERS   0x04u    /* rbp's own level meters (hooked)          */
#define RBS_MIXER    0x08u    /* headphone cue per channel                */

/* a lamp as rbp holds it */
#define RBL_OFF      0
#define RBL_ON       1
#define RBL_BLINK    2
#define RBL_DIM      3
#define RBL_UNKNOWN  0xff

#define RB_METER_SEGMENTS 11  /* rbp's meters light 0..11 segments */

/* Records on /tmp/rb-ctrl.fifo whose key is one of these are commands for
 * keyshim itself, not panel keys.  The RX3 has no keycode for a channel's
 * headphone CUE (its PFL buttons go straight to the mixer), so:
 *   key RB_CMD_PFL_TOGGLE, op 0 (press), ch 1|2  -> toggle that channel's cue */
#define RB_CMD_PFL_TOGGLE 0x7e54

struct rb_state_deck {
    uint8_t loaded, playing, sync_on, looping;       /* engine */
    uint8_t can_reloop, auto_loop, slip, vinyl;      /* engine */
    uint8_t master_tempo, pfl, play_led, sync_led;   /* pfl: 0/1, 0xff unknown */
    uint8_t pad_led[8];                              /* RBL_* per pad          */
    uint8_t pad_rgb[8][3];
    uint8_t meter;                                   /* 0..11, pre-fader      */
    /* ui::PlayerInnards: which pad bank rbp has the deck in (0 HOT CUE,
     * 1 BEAT LOOP, 2 SLIP LOOP, 3 BEAT JUMP - the order of the bank keys
     * 0x4113..0x4116) and the byte that holds a bank's second function.
     * 0xff when the object has not been found. */
    uint8_t pad_mode, pad_sub;
    /* the track is about to end: rbp is blinking its end warning (on the
     * RX3 the jog display flashes).  0/1, 0xff unknown. */
    uint8_t end_warn;
};

struct rb_state {
    uint32_t magic;
    uint32_t version;
    uint32_t seq;
    uint32_t flags;
    struct rb_state_deck deck[2];
    uint8_t bfx_led;          /* BEAT FX ON/OFF lamp, RBL_*                  */
    uint8_t master_cue;       /* 0/1, 0xff unknown                          */
    uint8_t master_meter;     /* 0..11                                      */
    uint8_t bfx_pos;          /* BEAT FX selector position 0..13, 0xff unknown */
    uint32_t seq_end;
};

typedef char rb_state_size_is_pinned[(sizeof(struct rb_state) == 120) ? 1 : -1];

#endif
