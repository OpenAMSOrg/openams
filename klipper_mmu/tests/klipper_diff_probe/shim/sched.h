// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef PROBE_FIXTURE_SCHED_H
#define PROBE_FIXTURE_SCHED_H
int sched_is_shutdown(void);
#define DECL_SHUTDOWN(fn) \
    static void fixture_shutdown_reg_##fn(void) __attribute__((constructor, used)); \
    static void fixture_shutdown_reg_##fn(void) { }
#endif
