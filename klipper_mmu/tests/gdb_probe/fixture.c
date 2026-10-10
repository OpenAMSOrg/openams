// SPDX-License-Identifier: GPL-3.0-or-later
/* Fixture for tools/gdb_probe.py tests: a small struct updated in a loop, an
 * assertion that fails at a known iteration (argv[1] == "fail"), a nonzero exit
 * (argv[1] == "exit") and a hang (argv[1] == "spin"). */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct state {
    int step;
    long total;
};

static void finish(int code)
{
    exit(code);
}

int main(int argc, char **argv)
{
    int fail = argc > 1 && strcmp(argv[1], "fail") == 0;
    int spin = argc > 1 && strcmp(argv[1], "spin") == 0;
    struct state st = {0, 0};

    if (argc > 1 && strcmp(argv[1], "exit") == 0)
        finish(3);
    if (spin)
        for (;;)
            ;
    for (int i = 0; i < 10; i++) {
        st.step = i;
        st.total += i; /* probe-trace */
        if (fail)
            assert(st.total < 30); /* probe-assert */
    }
    printf("done total=%ld\n", st.total);
    return 0;
}
