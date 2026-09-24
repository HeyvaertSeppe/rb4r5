/* test_probe_guard.c - a wrong address costs a probe, not the player.
 *
 * keyshim's state probes read rbp's memory at fixed addresses.  Here none of
 * those addresses exist, so every probe faults: the process must survive,
 * switch each faulting probe off, and still publish a (empty) state record.
 *
 * Built by run-all.sh natively and, when qemu is there, as soft-float ARM -
 * which is what runs inside the player.
 */
#define KEYSHIM_NO_CONSTRUCTOR
#include "../../src/shims/keyshim.c"

#include <sys/stat.h>

static int fails;

static void expect(const char *what, int ok)
{
    printf("%s %s\n", ok ? "ok  " : "FAIL", what);
    if (!ok)
        fails++;
}

int main(void)
{
    struct stat sb;
    rbp_checked = 1;
    unlink(RB_STATE_PATH);
    state_begin();
    for (int i = 0; i < 6; i++)
        state_tick();

    expect("the process is still here after every probe faulted", 1);
    expect("the PlayEngine probe switched itself off",
           (g_probe_off & P_ENGINE) != 0);
    expect("the LedStat probe switched itself off",
           (g_probe_off & P_LEDSTAT) != 0);
    expect("the MixerEngine probe switched itself off",
           (g_probe_off & P_MIXER) != 0);
    expect("and the headphone toggle will not call into it",
           g_mixer_usable == 0);
    expect("the unproven probes were never on",
           (g_probe_off & (P_PADBANK | P_BFX)) == (P_PADBANK | P_BFX));
    expect("a state record is still published",
           stat(RB_STATE_PATH, &sb) == 0 && sb.st_size == sizeof(struct rb_state));
    expect("saying the player told it nothing", st.flags == 0);

    /* a fault outside the guard still reaches whoever handles it: the
     * previous handler is back in place */
    {
        struct sigaction now;
        sigaction(SIGSEGV, NULL, &now);
        expect("the player's own SIGSEGV handler is back after each pass",
               now.sa_handler == SIG_DFL);
    }
    unlink(RB_STATE_PATH);
    if (fails)
        printf("%d failure(s)\n", fails);
    else
        printf("a faulting probe costs a lamp, not the player\n");
    return fails != 0;
}
