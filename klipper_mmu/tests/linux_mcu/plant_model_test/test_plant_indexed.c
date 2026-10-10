// SPDX-License-Identifier: GPL-3.0-or-later
// Native unit test for sim/board/plant.c's INDEXED selector mode
// (selector.h's shared-drive machines, docs/selectors.md "Indexed Gates"),
// shaped like the BTT ViViD sim image tests/linux_mcu/devices.py builds
// (see shim_indexed/autoconf.h and run.py): one gear, an INDEXED selector
// axis with a real physical gate order, 4 lanes, no grip servo and no gate
// position table at all -- every gate is found by its own index switch.
// Drives synthetic STEP/DIR edges the way the real firmware's driver setup
// would and checks the plant's own physical truth, not the firmware --
// there is no firmware in this test, only the board model.
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "sched.h"
#include "stm32/internal.h"
#include "board/misc.h"
#include "autoconf.h"
#include "klipper_mmu/mcu/selector_mechanics.h"
#include "plant.h"

extern uint32_t km_sim_fake_now;

#define GEAR_STEP GPIO('D', 4)
#define GEAR_DIR  GPIO('D', 3)
#define SEL_STEP  GPIO('C', 12)
#define SEL_DIR   GPIO('C', 11)
/* Per-gate index switches, lane order (klipper_mmu/selector.h): sim/board/
 * plant.c's gate_switch_pins table reuses the four-lane preset's own LOAD
 * pins for every INDEXED device, regardless of gate_order. */
#define GATE_PIN(lane) GPIO('E', 8 + (lane))

#define DIR_FORWARD 0
#define DIR_REVERSE 1

static int failures;
#define CHECK(cond, fmt, ...) do { \
    if (!(cond)) { \
        failures++; \
        fprintf(stderr, "FAIL %s:%d: " fmt "\n", __FILE__, __LINE__, ##__VA_ARGS__); \
    } \
} while (0)

/* mm<->steps<->um is not perfectly lossless (KM_SELECTOR_STEPS_PER_M is
 * itself a truncated integer, selector_mechanics.h), so a position driven
 * by a whole number of steps and read back through
 * km_sim_plant_selector_position_um() lands within a few microns of the
 * millimetre figure it was moved by, not bit-exact -- same tolerance
 * device_scenarios.py's own SELECTOR_TOLERANCE_UM uses for the whole-stack
 * sim's equivalent check. */
#define POS_TOLERANCE_UM 200
static int64_t iabs64(int64_t v) { return v < 0 ? -v : v; }

static void selector_step(uint8_t dir_level) {
    km_sim_fake_now += 5;
    km_sim_plant_notify_write(SEL_DIR, dir_level, km_sim_fake_now);
    km_sim_fake_now += 5;
    km_sim_plant_notify_write(SEL_STEP, 1, km_sim_fake_now);
    km_sim_fake_now += 5;
    km_sim_plant_notify_write(SEL_STEP, 0, km_sim_fake_now);
}

static void move_selector(int32_t steps) {
    uint8_t dir = steps >= 0 ? DIR_FORWARD : DIR_REVERSE;
    for (int32_t i = 0, n = steps >= 0 ? steps : -steps; i < n; ++i)
        selector_step(dir);
}

static void gear_step(uint8_t dir_level) {
    km_sim_fake_now += 5;
    km_sim_plant_notify_write(GEAR_DIR, dir_level, km_sim_fake_now);
    km_sim_fake_now += 5;
    km_sim_plant_notify_write(GEAR_STEP, 1, km_sim_fake_now);
    km_sim_fake_now += 5;
    km_sim_plant_notify_write(GEAR_STEP, 0, km_sim_fake_now);
}

int main(int argc, char **argv) {
    const char *scratch = argc > 1 ? argv[1] : ".";
    char state_path[512];
    snprintf(state_path, sizeof(state_path), "%s/plant-indexed-state.txt", scratch);

    /* Lane 0 (gate_order position 0) and lane 3 (gate_order position 1)
     * both parked at their own gate's BAY switch, every other lane empty --
     * the same shape device_scenarios.py's write_two_gates() sets up for
     * BTT ViViD's own PRELOAD gate_1/gate_2 pair. */
    FILE *f = fopen(state_path, "w");
    if (!f) { fprintf(stderr, "cannot write %s\n", state_path); return 1; }
    fprintf(f, "lane=0 state=bay\nlane=3 state=bay\nlane=1 state=empty\nlane=2 state=empty\n");
    fclose(f);
    setenv("KM_SIM_PLANT", state_path, 1);

    km_sim_plant_init();

    /* This shim's drum: gate_order "0,3,1,2", 720 mm max travel, 4 lanes ->
     * a 360 mm circumference and a 90 mm pitch (sim/board/plant.c's own
     * selector_load_profile() derivation, not re-copied here). */
    const int32_t pitch_steps = KM_SELECTOR_MM_TO_STEPS(90);

    /* INDEXED's home reference IS gate_order[0]'s own window centre -- the
     * one fact that has no LINEAR/ROTARY counterpart (their gate 0 sits
     * some CAD offset away from the endstop, never right at it). */
    CHECK(km_sim_plant_selector_position_um() == 0,
         "selector should start at 0, got %lld",
         (long long)km_sim_plant_selector_position_um());
    CHECK(km_sim_plant_engaged_gate() == 0,
         "gate 0 (gate_order position 0) should already be engaged at home, got %d",
         km_sim_plant_engaged_gate());
    CHECK(km_sim_plant_switch_level(GATE_PIN(0)) == 1,
         "gate 0's own index switch should read asserted at home (active-high)");
    CHECK(km_sim_plant_switch_level(GATE_PIN(3)) == 0,
         "gate 3's index switch should not read asserted yet");

    /* Reverse one full pitch from home: the drum wraps, landing on whichever
     * gate sits one physical slot *before* gate_order[0] going around --
     * gate_order "0,3,1,2"'s last entry, lane 2 -- and the raw axis counter
     * itself goes negative to get there (INDEXED has no endstop clamp,
     * unlike LINEAR/ROTARY's selector_move()). */
    move_selector(-pitch_steps);
    CHECK(iabs64(km_sim_plant_selector_position_um() - (-90000)) <= POS_TOLERANCE_UM,
         "one reverse pitch should land near -90000 um, got %lld",
         (long long)km_sim_plant_selector_position_um());
    CHECK(km_sim_plant_engaged_gate() == 2,
         "one reverse pitch from home should wrap onto gate 2, got %d",
         km_sim_plant_engaged_gate());
    CHECK(km_sim_plant_switch_level(GATE_PIN(2)) == 1,
         "gate 2's own index switch should read asserted one reverse pitch out");

    /* Forward two pitches from there lands on gate_order position 1 (lane
     * 3), 90 mm forward of home. */
    move_selector(2 * pitch_steps);
    CHECK(iabs64(km_sim_plant_selector_position_um() - 90000) <= POS_TOLERANCE_UM,
         "one forward pitch from home should land near 90000 um, got %lld",
         (long long)km_sim_plant_selector_position_um());
    CHECK(km_sim_plant_engaged_gate() == 3,
         "gate_order position 1 (lane 3) should be engaged, got %d",
         km_sim_plant_engaged_gate());
    CHECK(km_sim_plant_switch_level(GATE_PIN(3)) == 1,
         "gate 3's own index switch should read asserted there");
    CHECK(km_sim_plant_switch_level(GATE_PIN(0)) == 0,
         "gate 0's index switch should no longer read asserted");

    /* The gear only ever moves the currently engaged gate's own filament --
     * lane 3 here, not lane 0, which is inserted but not engaged. */
    int64_t lane0_before = km_sim_plant_position_um(0);
    int64_t lane3_before = km_sim_plant_position_um(3);
    for (int i = 0; i < 3000; ++i) gear_step(DIR_FORWARD);
    CHECK(km_sim_plant_position_um(3) > lane3_before,
         "lane 3's filament did not advance while its gate was engaged");
    CHECK(km_sim_plant_position_um(0) == lane0_before,
         "lane 0's filament moved while a different gate was engaged");
    int64_t lane3_after = km_sim_plant_position_um(3);

    /* Three more forward pitches (a full circumference from home) wraps
     * back onto gate 0 -- the forward half of the same "drum that wraps"
     * proof the reverse move above gave. */
    move_selector(3 * pitch_steps);
    CHECK(iabs64(km_sim_plant_selector_position_um() - 360000) <= POS_TOLERANCE_UM,
         "a full circumference forward should read near 360000 um, got %lld",
         (long long)km_sim_plant_selector_position_um());
    CHECK(km_sim_plant_engaged_gate() == 0,
         "a full circumference forward should wrap back onto gate 0, got %d",
         km_sim_plant_engaged_gate());

    lane0_before = km_sim_plant_position_um(0);
    for (int i = 0; i < 3000; ++i) gear_step(DIR_FORWARD);
    CHECK(km_sim_plant_position_um(0) > lane0_before,
         "lane 0's filament did not advance once its gate was engaged again");
    CHECK(km_sim_plant_position_um(3) == lane3_after,
         "lane 3's filament moved after its gate was left");

    /* The wire format (control.c's "truth" reply and the SIGUSR1 dump)
     * carries the same three selector fields every selector kind adds. */
    char truth[4096];
    int n = km_sim_plant_format_truth(truth, sizeof(truth));
    CHECK(n > 0, "format_truth returned nothing");
    CHECK(strstr(truth, "selector_position_um=") != NULL, "truth missing selector_position_um=: %s", truth);
    CHECK(strstr(truth, "engaged_gate=0") != NULL, "truth missing engaged_gate=0: %s", truth);

    remove(state_path);

    if (failures) {
        fprintf(stderr, "%d check(s) failed\n", failures);
        return 1;
    }
    printf("plant indexed-mode test passed.\n");
    return 0;
}
