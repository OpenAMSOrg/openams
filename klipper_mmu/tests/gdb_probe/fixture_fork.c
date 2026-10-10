// SPDX-License-Identifier: GPL-3.0-or-later
/* Fixture for tools/gdb_probe.py --follow-child tests: the parent forks, the
 * child calls child_work() where "marker" holds a known value, and the parent
 * waits for the child. Without --follow-child gdb follows the parent and
 * detaches from the child, so the breakpoint in child_work is never hit. */
#include <stdio.h>
#include <sys/wait.h>
#include <unistd.h>

static int child_work(int seed)
{
    int marker = seed * 7;
    marker += 0; /* probe-fork-trace */
    return marker;
}

int main(void)
{
    pid_t pid = fork();
    if (pid == 0) {
        int marker = child_work(6);
        printf("child marker=%d\n", marker);
        return 0;
    }
    int status = 0;
    waitpid(pid, &status, 0);
    printf("parent child=%ld\n", (long)pid);
    return 0;
}
