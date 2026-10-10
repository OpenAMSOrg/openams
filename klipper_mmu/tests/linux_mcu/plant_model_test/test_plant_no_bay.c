// SPDX-License-Identifier: GPL-3.0-or-later
// Native regression check for a device with no per-lane BAY switch at all
// (CONFIG_KLIPPER_MMU_BAY_SENSORS unset -- Angry Beaver, 3MS, QuattroBox
// 1.0/1.1/2, tests/linux_mcu/devices.py): such a device's lane starts with
// filament already at the spool, gripped by an always-engaged gear
// (its own "filament_always_gripped": true device profile), never at a BAY
// switch it has none of -- so its plant lane starts at "empty" (position 0),
// not "bay". Before this fix, plant.c's grip point sat unconditionally at
// the ordinary BAY switch's position regardless of whether the image even
// has one, so a lane starting at position 0 could never move: every STEP
// pulse was silently swallowed forever (`position_steps < grip_threshold_
// steps()` stayed true no matter how many pulses arrived), and LOAD could
// never feed a single micrometre. See run.py and test_plant_default.c (the
// ordinary BAY-sensored mode, unaffected by this fix).
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "sched.h"
#include "stm32/internal.h"
#include "board/misc.h"
#include "plant.h"

extern uint32_t km_sim_fake_now;

#define LANE0_STEP GPIO('D', 4)
#define LANE0_DIR  GPIO('D', 3)
#define HUB_PIN    GPIO('E', 12)

static int failures;
#define CHECK(cond, fmt, ...) do { \
    if (!(cond)) { \
        failures++; \
        fprintf(stderr, "FAIL %s:%d: " fmt "\n", __FILE__, __LINE__, ##__VA_ARGS__); \
    } \
} while (0)

static void step_forward(uint32_t step_pin, uint32_t dir_pin, uint8_t forward_level) {
    km_sim_fake_now += 3;
    km_sim_plant_notify_write(dir_pin, forward_level, km_sim_fake_now);
    km_sim_fake_now += 3;
    km_sim_plant_notify_write(step_pin, 1, km_sim_fake_now);
    km_sim_fake_now += 3;
    km_sim_plant_notify_write(step_pin, 0, km_sim_fake_now);
}

int main(int argc, char **argv) {
    const char *scratch = argc > 1 ? argv[1] : ".";
    char state_path[512];
    snprintf(state_path, sizeof(state_path), "%s/plant-no-bay-state.txt", scratch);

    FILE *f = fopen(state_path, "w");
    if (!f) { fprintf(stderr, "cannot write %s\n", state_path); return 1; }
    // Every lane starts "empty": no BAY switch to have been inserted up to
    // (build/b2/passivesim.txt: "lanes start with filament at the spool,
    // not at a BAY switch").
    fprintf(f, "lane=0 state=empty\nlane=1 state=empty\nlane=2 state=empty\nlane=3 state=empty\n");
    fclose(f);
    setenv("KM_SIM_PLANT", state_path, 1);

    km_sim_plant_init();

    int64_t before = km_sim_plant_position_um(0);
    CHECK(before == 0, "lane0 should start at position 0 (empty, no BAY switch): got %lld",
         (long long)before);

    // KLIPPER_MMU_FEEDER_FORWARD_HIGH default is 0: DIR low is forward on
    // this wiring, same as plant.c's own KM_SIM_PLANT_DIR_FORWARD_LEVEL.
    // Before the fix this loop would leave position_um stuck at 0 forever
    // (every step refused by the ordinary BAY-relative grip point, which a
    // no-BAY image has no switch to justify); one step is already enough to
    // prove the fix, but several make the failure message legible.
    for (int i = 0; i < 10; ++i) step_forward(LANE0_STEP, LANE0_DIR, 0);
    int64_t after = km_sim_plant_position_um(0);
    CHECK(after > before,
         "lane0 with no BAY sensor never advanced from position 0 -- the "
         "grip point is refusing motion a filament_always_gripped feeder "
         "should never have to earn: before=%lld after=%lld",
         (long long)before, (long long)after);

    // Feed the rest of the way to the shared HUB, proving a full LOAD-shaped
    // feed (not just the first step) actually reaches it from a cold start.
    // KM_SIM_PLANT_HUB_UM (80 mm) needs 58182 steps at this shim's 727272
    // steps/m (test_plant_default.c's own reference mechanics); 60000 more
    // clears it with margin.
    for (int i = 0; i < 60000; ++i) step_forward(LANE0_STEP, LANE0_DIR, 0);
    CHECK(km_sim_plant_switch_level(HUB_PIN) == 1,
         "lane0 never reached the shared HUB switch from a no-BAY empty "
         "start (position_um=%lld)", (long long)km_sim_plant_position_um(0));

    // Other lanes, also starting "empty" with no BAY switch, are untouched.
    CHECK(km_sim_plant_position_um(1) == 0, "lane1 moved unexpectedly");
    CHECK(km_sim_plant_position_um(2) == 0, "lane2 moved unexpectedly");
    CHECK(km_sim_plant_position_um(3) == 0, "lane3 moved unexpectedly");

    remove(state_path);

    if (failures) {
        fprintf(stderr, "%d check(s) failed\n", failures);
        return 1;
    }
    printf("plant no-BAY-sensor regression test passed.\n");
    return 0;
}
