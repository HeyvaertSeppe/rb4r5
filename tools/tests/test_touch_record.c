/* test_touch_record.c - the bytes the player's touch device hands over.
 *
 * The record is the one the Prime GO / SC Live 4 ports verified on this rbp:
 * { u8 flag, u8 0, u16 x, u16 y } little-endian in the 1280x800 UI, with X
 * mirrored because the firmware computes calX = 1280 - rawX.
 */
#include "../../src/shims/memshim.c"

#include <stdio.h>

static int fails;

static void expect(const char *what, int ok)
{
    printf("%s %s\n", ok ? "ok  " : "FAIL", what);
    if (!ok)
        fails++;
}

int main(void)
{
    unsigned char b[6];
    struct rb_touch_state st = { 1, 1, 100, 200 };

    unsetenv("RB_TOUCH_FMT");
    unsetenv("RB_TOUCH_INVX");
    touch_native = -1;
    touch_env_init();
    touch_pack(b, &st);
    expect("the default record is the verified RX3 one", touch_fmt == TFMT_RX3);
    expect("byte 0 is the flag, byte 1 is zero", b[0] == 1 && b[1] == 0);
    expect("X is mirrored: 1279 - 100 = 1179",
           (b[2] | (b[3] << 8)) == 1179);
    expect("Y is as it is", (b[4] | (b[5] << 8)) == 200);

    st.down = 0;
    touch_pack(b, &st);
    expect("lifted: the flag is 0", b[0] == 0);

    st.down = 1; st.x = 5000; st.y = 5000;
    touch_pack(b, &st);
    expect("off the edge is clamped to the panel",
           (b[2] | (b[3] << 8)) == 0 && (b[4] | (b[5] << 8)) == 799);

    if (fails)
        printf("%d failure(s)\n", fails);
    else
        printf("the player gets the RX3's own touch record\n");
    return fails != 0;
}
