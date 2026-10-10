// SPDX-License-Identifier: GPL-3.0-or-later
// Native regression check: a non-selector image with more than four lanes
// (EMU, 5 -- see sim/board/plant.c's header comment and tests/linux_mcu/
// devices.py's 'emu' row) drives its real fifth lane on its own extended
// pins (PD2/PD7/PD9/PD11/PD12) without disturbing lanes 0-3's original
// four-lane pins, and vice versa. See run.py and test_plant_default.c
// (the original, still-unchanged four-lane shape this build coexists
// with).
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
#define LANE4_STEP GPIO('D', 2)
#define LANE4_DIR  GPIO('D', 7)
#define LANE4_BAY  GPIO('D', 11)
#define LANE4_LOAD GPIO('D', 12)
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
    snprintf(state_path, sizeof(state_path), "%s/plant-five-lane-state.txt", scratch);

    FILE *f = fopen(state_path, "w");
    if (!f) { fprintf(stderr, "cannot write %s\n", state_path); return 1; }
    fprintf(f, "lane=0 state=bay\nlane=1 state=empty\nlane=2 state=empty\n"
               "lane=3 state=empty\nlane=4 state=bay\n");
    fclose(f);
    setenv("KM_SIM_PLANT", state_path, 1);

    km_sim_plant_init();

    // Both lane 0 and lane 4 start at their own BAY switch.
    CHECK(km_sim_plant_switch_level(LANE0_BAY) == 0, "lane0 BAY not asserted at start");
    CHECK(km_sim_plant_switch_level(LANE4_BAY) == 0, "lane4 BAY not asserted at start");
    CHECK(km_sim_plant_switch_level(LANE4_LOAD) == 0, "lane4 LOAD active before feeding");
    CHECK(km_sim_plant_switch_level(HUB_PIN) == 0, "HUB active before feeding");
    int64_t lane0_at_bay = km_sim_plant_position_um(0);
    CHECK(km_sim_plant_position_um(1) == 0, "lane1 not empty at start");
    CHECK(km_sim_plant_position_um(2) == 0, "lane2 not empty at start");
    CHECK(km_sim_plant_position_um(3) == 0, "lane3 not empty at start");

    // Feed lane 4 forward on its own STEP/DIR pins, same threshold math as
    // test_plant_default.c's lane 0 (8000 steps clears LOAD from the BAY
    // start).
    int64_t before4 = km_sim_plant_position_um(4);
    for (int i = 0; i < 8000; ++i) step_forward(LANE4_STEP, LANE4_DIR, 0);
    int64_t after4 = km_sim_plant_position_um(4);
    CHECK(after4 > before4, "lane4 did not advance: before=%lld after=%lld",
         (long long)before4, (long long)after4);
    CHECK(km_sim_plant_switch_level(LANE4_LOAD) == 1,
         "lane4 LOAD not active after feeding past it (position_um=%lld)",
         (long long)after4);
    CHECK(km_sim_plant_switch_level(LANE4_BAY) == 0,
         "lane4 BAY should stay asserted once passed, not reset");

    // Lane 4's own pins never moved lane 0-3 (lane 0 stays exactly where it
    // started, at BAY; lanes 1-3 stay empty), and lane 0's own switches
    // still read its own start state (bay), untouched by lane 4.
    CHECK(km_sim_plant_position_um(0) == lane0_at_bay,
         "lane0 moved from lane4's own pins: was=%lld now=%lld",
         (long long)lane0_at_bay, (long long)km_sim_plant_position_um(0));
    CHECK(km_sim_plant_position_um(1) == 0, "lane1 moved unexpectedly");
    CHECK(km_sim_plant_position_um(2) == 0, "lane2 moved unexpectedly");
    CHECK(km_sim_plant_position_um(3) == 0, "lane3 moved unexpectedly");
    CHECK(km_sim_plant_switch_level(LANE0_BAY) == 0,
         "lane0 BAY should still read its own start state (bay)");
    CHECK(km_sim_plant_switch_level(LANE0_LOAD) == 0,
         "lane0 LOAD should not be active (never fed)");

    // Now drive lane 0 on its original four-lane pins: unaffected by lane
    // 4 existing, exactly test_plant_default.c's own check.
    int64_t before0 = km_sim_plant_position_um(0);
    for (int i = 0; i < 8000; ++i) step_forward(LANE0_STEP, LANE0_DIR, 0);
    int64_t after0 = km_sim_plant_position_um(0);
    CHECK(after0 > before0, "lane0 did not advance: before=%lld after=%lld",
         (long long)before0, (long long)after0);
    CHECK(km_sim_plant_switch_level(LANE0_LOAD) == 1,
         "lane0 LOAD not active after feeding past it (position_um=%lld)",
         (long long)after0);
    CHECK(km_sim_plant_position_um(4) == after4,
         "lane4 moved from lane0's own pins: was=%lld now=%lld",
         (long long)after4, (long long)km_sim_plant_position_um(4));

    // A non-selector image reports no selector state at all, even with a
    // fifth lane.
    CHECK(km_sim_plant_selector_position_um() == 0, "non-selector image reports a selector position");
    CHECK(km_sim_plant_servo_pulse_us() == 0, "non-selector image reports a servo pulse");
    CHECK(km_sim_plant_engaged_gate() == -1, "non-selector image reports an engaged gate");

    // The wire format carries exactly 5 lane lines and no selector_* field.
    char truth[2048];
    km_sim_plant_format_truth(truth, sizeof(truth));
    CHECK(strstr(truth, "selector_position_um=") == NULL,
         "non-selector truth unexpectedly carries a selector field: %s", truth);
    CHECK(strstr(truth, "lane=4 ") != NULL, "truth is missing lane 4: %s", truth);
    CHECK(strstr(truth, "lane=5 ") == NULL,
         "truth carries a lane 5 this 5-lane image does not have: %s", truth);

    remove(state_path);

    if (failures) {
        fprintf(stderr, "%d check(s) failed\n", failures);
        return 1;
    }
    printf("plant five-lane regression test passed.\n");
    return 0;
}
