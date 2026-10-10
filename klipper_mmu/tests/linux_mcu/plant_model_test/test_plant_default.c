// SPDX-License-Identifier: GPL-3.0-or-later
// Native regression check: sim/board/plant.c's original four-lane,
// no-selector mode (BoxTurtle/NightOwl/EMU) is unaffected by the selector
// mode added next to it -- KM_SIM_PLANT_LANES stays the fixed 4 and the
// original per-lane STEP/DIR/BAY/LOAD pin tables still drive lane 0
// exactly as before. See run.py and test_plant_selector.c (the new mode).
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
#define LANE0_BAY  GPIO('E', 13)
#define LANE0_LOAD GPIO('E', 8)
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
    snprintf(state_path, sizeof(state_path), "%s/plant-default-state.txt", scratch);

    FILE *f = fopen(state_path, "w");
    if (!f) { fprintf(stderr, "cannot write %s\n", state_path); return 1; }
    fprintf(f, "lane=0 state=bay\nlane=1 state=empty\nlane=2 state=empty\nlane=3 state=empty\n");
    fclose(f);
    setenv("KM_SIM_PLANT", state_path, 1);

    km_sim_plant_init();

    // Lane 0 starts at its BAY switch (state=bay): BAY is wired active-low
    // (KLIPPER_MMU_BAY_ACTIVE_HIGH=0), so "covered" reads electrical level
    // 0; LOAD and HUB are active-high and not yet covered, reading 0 too.
    CHECK(km_sim_plant_switch_level(LANE0_BAY) == 0,
         "lane0 BAY not asserted at start (active-low pin, expect level 0)");
    CHECK(km_sim_plant_switch_level(LANE0_LOAD) == 0, "lane0 LOAD active before feeding");
    CHECK(km_sim_plant_switch_level(HUB_PIN) == 0, "HUB active before feeding");

    // KLIPPER_MMU_FEEDER_FORWARD_HIGH default is 0 (see the shim autoconf's
    // comment): DIR low is forward on this wiring, same as plant.c's own
    // KM_SIM_PLANT_DIR_FORWARD_LEVEL. 8000 steps clears the sim's own LOAD
    // threshold (10909 steps at 727272 steps/m) from the BAY start (3636).
    int64_t before = km_sim_plant_position_um(0);
    for (int i = 0; i < 8000; ++i) step_forward(LANE0_STEP, LANE0_DIR, 0);
    int64_t after = km_sim_plant_position_um(0);
    CHECK(after > before, "lane0 did not advance: before=%lld after=%lld",
         (long long)before, (long long)after);
    CHECK(km_sim_plant_switch_level(LANE0_LOAD) == 1,
         "lane0 LOAD not active after feeding past it (position_um=%lld)",
         (long long)after);
    CHECK(km_sim_plant_switch_level(LANE0_BAY) == 0,
         "lane0 BAY should stay asserted once passed, not reset");
    CHECK(km_sim_plant_switch_level(HUB_PIN) == 0,
         "HUB should not be active yet (only %lld um fed)", (long long)after);

    // Other lanes are untouched by lane 0's own STEP/DIR pins.
    CHECK(km_sim_plant_position_um(1) == 0, "lane1 moved unexpectedly");
    CHECK(km_sim_plant_position_um(2) == 0, "lane2 moved unexpectedly");
    CHECK(km_sim_plant_position_um(3) == 0, "lane3 moved unexpectedly");

    // A non-selector image reports no selector state at all.
    CHECK(km_sim_plant_selector_position_um() == 0, "non-selector image reports a selector position");
    CHECK(km_sim_plant_servo_pulse_us() == 0, "non-selector image reports a servo pulse");
    CHECK(km_sim_plant_engaged_gate() == -1, "non-selector image reports an engaged gate");

    // The wire format is unchanged: no selector_* field leaks in.
    char truth[1024];
    km_sim_plant_format_truth(truth, sizeof(truth));
    CHECK(strstr(truth, "selector_position_um=") == NULL,
         "non-selector truth unexpectedly carries a selector field: %s", truth);

    remove(state_path);

    if (failures) {
        fprintf(stderr, "%d check(s) failed\n", failures);
        return 1;
    }
    printf("plant default-mode regression test passed.\n");
    return 0;
}
