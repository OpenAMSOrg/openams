// SPDX-License-Identifier: GPL-3.0-or-later
#include "config.h"
#include "autoconf.h"
#include "command.h"
#include "feeder_mechanics.h"
#include "selector_mechanics.h"
#include <stdint.h>
#include <string.h>

#if defined(__arm__) && CONFIG_MACH_STM32 && CONFIG_KLIPPER_MMU
_Static_assert(CONFIG_STACK_SIZE >= 4096,
               "klipper_mmu: regenerate config with at least 4096 stack bytes");
#endif

/* Saved headers from before FPS arrival keep toolhead-switch arrival. */
#ifndef CONFIG_KLIPPER_MMU_EJECT_FINAL_DISTANCE_MM
#define CONFIG_KLIPPER_MMU_EJECT_FINAL_DISTANCE_MM 0
#endif
#ifndef CONFIG_KLIPPER_MMU_FPS_ARRIVAL
#define CONFIG_KLIPPER_MMU_FPS_ARRIVAL 0
#define CONFIG_KLIPPER_MMU_FPS_ARRIVAL_PERMILLE 0
#define CONFIG_KLIPPER_MMU_FPS_ARRIVAL_SAMPLES 0
#define CONFIG_KLIPPER_MMU_FPS_ARRIVAL_HOLD_MS 0
#define CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MIN_MM 0
#define CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MAX_MM 0
#define CONFIG_KLIPPER_MMU_LOAD_CRUISE_SPEED_MM_S 0
#define CONFIG_KLIPPER_MMU_LOAD_CRAWL_SPEED_MM_S 0
#define CONFIG_KLIPPER_MMU_LOAD_WINDOW_MM 0
#define CONFIG_KLIPPER_MMU_BUFFER_TRAVEL_MM 0
#endif
#ifndef CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR
#define CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR 1
#endif
/* The gain entry exists only with the DiffA/DiffB buffer, and the builder
 * does not ship it otherwise. The core reads the field only when the buffer
 * is present, so an unset 0 is honest and keeps km_profile_validate's
 * proportional branch off. */
#ifndef CONFIG_KLIPPER_MMU_BUFFER_GAIN_UM_S
#define CONFIG_KLIPPER_MMU_BUFFER_GAIN_UM_S 0
#endif

/* Selector list settings are visible only under their own kind; a saved
 * header or a device with a different kind may not define them at all. */
#ifndef CONFIG_KLIPPER_MMU_SELECTOR_GATE_POSITIONS_MM
#define CONFIG_KLIPPER_MMU_SELECTOR_GATE_POSITIONS_MM ""
#endif
#ifndef CONFIG_KLIPPER_MMU_SELECTOR_GATE_DIRECTIONS
#define CONFIG_KLIPPER_MMU_SELECTOR_GATE_DIRECTIONS ""
#endif
#ifndef CONFIG_KLIPPER_MMU_SELECTOR_RELEASE_GATES
#define CONFIG_KLIPPER_MMU_SELECTOR_RELEASE_GATES ""
#endif
#ifndef CONFIG_KLIPPER_MMU_SELECTOR_GATE_ORDER
#define CONFIG_KLIPPER_MMU_SELECTOR_GATE_ORDER ""
#endif
#ifndef CONFIG_KLIPPER_MMU_SELECTOR_SERVO_GATE_ANGLES_DEG
#define CONFIG_KLIPPER_MMU_SELECTOR_SERVO_GATE_ANGLES_DEG ""
#endif

/* Strict comma list parser: exactly `count` fields, digits only, with at
 * most `decimals` digits after an optional single '.', no signs, no spaces,
 * no empty fields, no trailing text. Produces int32_t values scaled by
 * 10^decimals, so decimals=3 turns "4.2" into 4200 (micrometres) and
 * decimals=0 parses plain non-negative integers. A short, malformed or
 * empty list (including the "uncalibrated" empty-string default) returns
 * false; this package never turns that into a silent zero. Used by every
 * per-lane list below: selector gate/direction/order/angle tables (a device
 * with neither a selector stepper nor a selector servo skips those) and the
 * feeder rotation distance list every device has. */
static bool parse_scaled_list(const char *s, unsigned count, unsigned decimals, int32_t *out)
{
    int32_t scale = 1;
    for (unsigned d = 0; d < decimals; ++d) scale *= 10;
    for (unsigned i = 0; i < count; ++i) {
        int32_t whole = 0;
        unsigned digits = 0;
        while (*s >= '0' && *s <= '9') { whole = whole * 10 + (*s++ - '0'); ++digits; }
        if (!digits) return false;
        int32_t frac = 0;
        unsigned frac_digits = 0;
        if (*s == '.') {
            if (!decimals) return false;
            ++s;
            while (*s >= '0' && *s <= '9') {
                if (frac_digits < decimals) frac = frac * 10 + (*s - '0');
                ++s; ++frac_digits;
            }
            if (!frac_digits || frac_digits > decimals) return false;
        }
        for (unsigned d = frac_digits; d < decimals; ++d) frac *= 10;
        out[i] = whole * scale + frac;
        if (i + 1 < count) {
            if (*s != ',') return false;
            ++s;
        } else if (*s != '\0') {
            return false;
        }
    }
    return true;
}

/* Build requests, not runtime hardware availability. Shared by the factory
 * and dictionary; neither a menu choice nor these constants enable an output. */
DECL_CONSTANT("KLIPPER_MMU_CONFIG_POWERED_REWIND", CONFIG_KLIPPER_MMU_POWERED_REWIND);
DECL_CONSTANT("KLIPPER_MMU_CONFIG_ENCODER", CONFIG_KLIPPER_MMU_ENCODER);
DECL_CONSTANT("KLIPPER_MMU_CONFIG_DIFF_BUFFER", CONFIG_KLIPPER_MMU_DIFF_BUFFER);
DECL_CONSTANT("KLIPPER_MMU_CONFIG_MEASURED_CURRENT", CONFIG_KLIPPER_MMU_MEASURED_CURRENT);
DECL_CONSTANT("KLIPPER_MMU_CONFIG_LOAD_SENSORS", CONFIG_KLIPPER_MMU_LOAD_SENSORS);
/* Whether this build's shared "hub" reading is a real switch or projected
 * from a lane's own LOAD switch instead (load_is_virtual_hub above; EMU
 * today). The host cannot tell the two apart from the hub sample alone --
 * both report configured=true (has_hub_sensor is always true; see
 * km_core_get_sensor_snapshot) -- so it needs this fact stated directly. */
DECL_CONSTANT("KLIPPER_MMU_CONFIG_VIRTUAL_HUB", CONFIG_KLIPPER_MMU_VIRTUAL_HUB);
DECL_CONSTANT("KLIPPER_MMU_HOST_LEASE_MS", CONFIG_KLIPPER_MMU_HOST_LEASE);
/* Host reports only: converts LOAD travel to millimetres. Derived from the
 * menu mechanics, so it is never a guess and never zero. */
DECL_CONSTANT("KLIPPER_MMU_FEEDER_STEPS_PER_M", KM_FEEDER_STEPS_PER_M);

/* The menu travel bounds are this MMU's outer limits: a fitment's calibrated
 * HUB-to-arrival distance must land between them. The host restores a stored
 * step estimate through a config command whose oid makes Klipper refuse it
 * after finalize_config. The menu speaks millimetres; host and core speak
 * microsteps. */
#define KM_FPS_MIN_STEPS \
    KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MIN_MM)
#define KM_FPS_MAX_STEPS \
    KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MAX_MM)
static uint32_t fps_calibrated_steps;

/* Stage timeout defaults for every device that names no timeout of its own
 * (Kconfig's 0 sentinel): 1.25x this stage's slowest configured speed and
 * furthest possible travel, plus one ramp at this device's own
 * acceleration -- the same margin tests/motion_budget/check_budget.py
 * already checks BoxTurtle's own qualified deadlines against (distance/speed
 * + speed/accel, times 5/4). mm and mm/s are the menu's own units; no step
 * conversion is needed here because it would cancel. */
#define KM_STAGE_DEADLINE_MS(distance_mm, speed_mm_s) \
    ((uint32_t)((5ULL * ((1000ULL * (uint32_t)(distance_mm)) / (uint32_t)(speed_mm_s) \
                          + (1000ULL * (uint32_t)(speed_mm_s)) \
                            / (uint32_t)CONFIG_KLIPPER_MMU_ACCELERATION_MM_S2)) \
                / 4ULL))

/* LOAD_FEED_TO_HUB: gate to HUB. A device with no KLIPPER_MMU_GATE_HOMING_MAX_MM
 * evidence keeps the old flat guess instead of inventing a distance. */
#define KM_HUB_TIMEOUT_MS \
    (CONFIG_KLIPPER_MMU_HUB_TIMEOUT ? (uint32_t)CONFIG_KLIPPER_MMU_HUB_TIMEOUT \
     : CONFIG_KLIPPER_MMU_GATE_HOMING_MAX_MM \
       ? KM_STAGE_DEADLINE_MS(CONFIG_KLIPPER_MMU_GATE_HOMING_MAX_MM, CONFIG_KLIPPER_MMU_FEED_SPEED_MM_S) \
       : 8000u)
/* LOAD_FEED_TO_BUFFER: HUB to arrival (the Bowden). A calibrated FPS-arrival
 * LOAD crawls this whole stretch (load_approach(), src/operation_core.c); a
 * switch-arrival LOAD never slows down before its toolhead sensor, so feed
 * speed is the whole run's speed instead. */
#define KM_ARRIVAL_TIMEOUT_MS \
    (CONFIG_KLIPPER_MMU_ARRIVAL_TIMEOUT ? (uint32_t)CONFIG_KLIPPER_MMU_ARRIVAL_TIMEOUT \
     : CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MAX_MM \
       ? KM_STAGE_DEADLINE_MS(CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MAX_MM, \
             CONFIG_KLIPPER_MMU_FPS_ARRIVAL ? CONFIG_KLIPPER_MMU_LOAD_CRAWL_SPEED_MM_S \
                                             : CONFIG_KLIPPER_MMU_FEED_SPEED_MM_S) \
       : 12000u)
/* UNLOAD_RETRACT_TO_HUB/LOAD, EJECT_TO_BAY and a LOAD retry's own retract:
 * the retry is the long one, back past HUB from the outer arrival travel by
 * the unload clear distance (src/operation_core.c's KM_STAGE_LOAD_RETRY_RETRACT). */
#define KM_RETRACT_TIMEOUT_MS \
    (CONFIG_KLIPPER_MMU_RETRACT_TIMEOUT ? (uint32_t)CONFIG_KLIPPER_MMU_RETRACT_TIMEOUT \
     : CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MAX_MM \
       ? KM_STAGE_DEADLINE_MS((uint32_t)CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MAX_MM \
                                  + (uint32_t)CONFIG_KLIPPER_MMU_UNLOAD_HUB_CLEAR_MM, \
             CONFIG_KLIPPER_MMU_RETRACT_SPEED_MM_S) \
       : 8000u)
/* UNLOAD_STOP_SETTLE (waiting for the preceding move to stop) and
 * UNLOAD_REWIND_TO_SPOOL (the clear-distance move past it) share this
 * deadline; both are bounded by the unload clear distance, not the Bowden. */
#define KM_REWIND_TIMEOUT_MS \
    (CONFIG_KLIPPER_MMU_REWIND_TIMEOUT ? (uint32_t)CONFIG_KLIPPER_MMU_REWIND_TIMEOUT \
     : CONFIG_KLIPPER_MMU_UNLOAD_HUB_CLEAR_MM \
       ? KM_STAGE_DEADLINE_MS(CONFIG_KLIPPER_MMU_UNLOAD_HUB_CLEAR_MM, CONFIG_KLIPPER_MMU_RETRACT_SPEED_MM_S) \
       : 15000u)
/* CALIBRATE_RETRACT: from wherever CALIBRATE_PROBE stopped (up to the outer
 * arrival travel) back until HUB and the toolhead both clear on sensors, so
 * no unload clear distance is added (calibrate_retract(), src/operation_core.c). */
#define KM_CAL_RETRACT_TIMEOUT_MS \
    (CONFIG_KLIPPER_MMU_CAL_RETRACT_TIMEOUT ? (uint32_t)CONFIG_KLIPPER_MMU_CAL_RETRACT_TIMEOUT \
     : CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MAX_MM \
       ? KM_STAGE_DEADLINE_MS(CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MAX_MM, CONFIG_KLIPPER_MMU_RETRACT_SPEED_MM_S) \
       : 15000u)
#if CONFIG_KLIPPER_MMU_FPS_ARRIVAL
#include "basecmd.h"
#include "sched.h"
DECL_CONSTANT("KLIPPER_MMU_FPS_MIN_STEPS", KM_FPS_MIN_STEPS);
DECL_CONSTANT("KLIPPER_MMU_FPS_MAX_STEPS", KM_FPS_MAX_STEPS);

/* A variable, not the macro: an unset (0) minimum makes "< min" always
 * false, which is correct but trips -Wtype-limits. */
static const uint32_t fps_min_steps = KM_FPS_MIN_STEPS;

void command_config_km_fps_steps(uint32_t *args)
{
    if (args[1] && (args[1] < fps_min_steps || args[1] > KM_FPS_MAX_STEPS))
        shutdown("Invalid FPS calibrated travel");
    oid_alloc((uint8_t)args[0], command_config_km_fps_steps, 0);
    fps_calibrated_steps = args[1];
}
DECL_COMMAND(command_config_km_fps_steps,
             "config_km_fps_steps oid=%c steps=%u");
#endif

#include "basecmd.h"
#include "sched.h"

/* Zero-initialized: "not set" until the host overrides it. A real rotation
 * distance is never 0 (km_fill_selector's own gate_steps has no such gap to
 * exploit, but fps_calibrated_steps above already uses the same "0 unset"
 * convention), so one array does the job of the separate value+bool pair
 * every other per-lane override here uses -- KM_MAX_LANES x 4 bytes instead
 * of x 5, worth doing on the tightest RAM targets (mcu/Kconfig.control). */
static uint32_t lane_rotation_override_um[KM_MAX_LANES];

/* Host override of KLIPPER_MMU_FEEDER_LANE_ROTATION_DISTANCES_UM, one lane at
 * a time (host/__init__.py's feeder_rotation_distances, docs/install.md);
 * read by km_fill_lane_rotation below. */
void command_config_km_feeder_rotation(uint32_t *args)
{
    unsigned lane = args[1];
    if (lane >= (unsigned)CONFIG_KLIPPER_MMU_LANE_COUNT || lane >= KM_MAX_LANES || !args[2])
        shutdown("Invalid feeder rotation distance");
    oid_alloc((uint8_t)args[0], command_config_km_feeder_rotation, 0);
    lane_rotation_override_um[lane] = args[2];
}
DECL_COMMAND(command_config_km_feeder_rotation,
             "config_km_feeder_rotation oid=%c lane=%c um=%u");

/* Every lane's real rotation distance: the Kconfig list if the device or its
 * builder set one, overridden per lane by the host above, else every lane at
 * the reference value. feeder_rotation_distance_um (and so
 * lane_rotation_distance_um) stays 0 -- the feature untouched, every lane
 * implicitly 1:1 -- unless a real per-lane customization is actually present
 * (list or host override): engage_feeder (src/operation_core.c) skips the
 * trajectory's lane-scale API entirely on that 0, so an ordinary build that
 * never touches this feature takes not one extra instruction over today. A
 * malformed list refuses the whole build, the same as every other per-lane
 * Kconfig list here (km_fill_selector below). */
static km_result_t km_fill_lane_rotation(km_device_profile_t *p, unsigned lanes)
{
    if (!lanes || lanes > KM_MAX_LANES) return KM_ERR_INVALID_PARAM;
    bool customized = CONFIG_KLIPPER_MMU_FEEDER_LANE_ROTATION_DISTANCES_UM[0] != '\0';
    for (unsigned i = 0; i < lanes && !customized; ++i)
        customized = lane_rotation_override_um[i] != 0;
    if (!customized) return KM_OK; /* p is zero-initialized here already. */

    p->feeder_rotation_distance_um = (uint32_t)CONFIG_KLIPPER_MMU_FEEDER_ROTATION_DISTANCE_UM;
    for (unsigned i = 0; i < lanes; ++i)
        p->lane_rotation_distance_um[i] = p->feeder_rotation_distance_um;
    if (CONFIG_KLIPPER_MMU_FEEDER_LANE_ROTATION_DISTANCES_UM[0]) {
        int32_t values[KM_MAX_LANES] = {0};
        if (!parse_scaled_list(CONFIG_KLIPPER_MMU_FEEDER_LANE_ROTATION_DISTANCES_UM, lanes, 0, values))
            return KM_ERR_INVALID_PARAM;
        for (unsigned i = 0; i < lanes; ++i) {
            if (values[i] <= 0) return KM_ERR_INVALID_PARAM;
            p->lane_rotation_distance_um[i] = (uint32_t)values[i];
        }
    }
    for (unsigned i = 0; i < lanes; ++i)
        if (lane_rotation_override_um[i])
            p->lane_rotation_distance_um[i] = lane_rotation_override_um[i];
    return KM_OK;
}

#if CONFIG_KLIPPER_MMU_SELECTOR_STEPPER
#include "basecmd.h"
#include "sched.h"

/* Static storage is zero-initialized: "not set" until the host overrides it. */
static int32_t selector_gate_override_steps[KM_MAX_LANES];
static bool selector_gate_override_set[KM_MAX_LANES];

void command_config_km_selector_gate(uint32_t *args)
{
    unsigned gate = args[1];
    if (gate >= (unsigned)CONFIG_KLIPPER_MMU_LANE_COUNT || gate >= KM_MAX_LANES)
        shutdown("Invalid selector gate index");
    oid_alloc((uint8_t)args[0], command_config_km_selector_gate, 0);
    selector_gate_override_steps[gate] = (int32_t)args[2];
    selector_gate_override_set[gate] = true;
}
DECL_COMMAND(command_config_km_selector_gate,
             "config_km_selector_gate oid=%c gate=%c steps=%i");

#if CONFIG_KLIPPER_MMU_SELECTOR_KIND_ROTARY
/* NOT a selector approach direction. Happy Hare's selector_gate_directions
 * is the GEAR (filament drive) stepper's direction for gate g, applied with
 * set_gear_direction() when that gate is gripped
 * (extras/mmu/unit/selectors/mmu_rotary_selector.py:61-64,246-257). That
 * calls the gear stepper's set_dir_inverted(d)
 * (extras/mmu/unit/mmu_drive.py:214-219): d=0 leaves the configured DIR
 * polarity alone (normal), d=1 inverts it (reverse). This firmware maps 0
 * to KM_DIR_FORWARD and 1 to KM_DIR_REVERSE. */
static km_direction_t km_selector_gear_direction(int32_t raw)
{
    return raw ? KM_DIR_REVERSE : KM_DIR_FORWARD;
}
#endif

/* Fills the selector portion of the profile for a build with a selector
 * stepper and/or a selector servo. `lanes` must already be known nonzero
 * and within KM_MAX_LANES: the gate/direction/angle arrays below are fixed
 * KM_MAX_LANES C arrays, and writing lanes past that bound would overrun
 * them, not just fail validation afterwards. */
static km_result_t km_fill_selector(km_selector_profile_t *sel, unsigned lanes)
{
    if (!lanes || lanes > KM_MAX_LANES)
        return KM_ERR_INVALID_PARAM;

    sel->velocity_steps_s = (uint32_t)KM_SELECTOR_MM_TO_STEPS(CONFIG_KLIPPER_MMU_SELECTOR_MOVE_SPEED_MM_S);
    sel->acceleration_steps_s2 = (uint32_t)KM_SELECTOR_MM_TO_STEPS(CONFIG_KLIPPER_MMU_SELECTOR_ACCEL_MM_S2);
#if CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED
    /* Happy Hare's IndexedSelector has one speed/accel for every selector
     * move, homing included: _move_selector/_homing_move_rel both default
     * speed/accel to selector_move_speed/selector_accel
     * (mmu_indexed_selector.py's _find_gate and _home_selector never pass
     * their own). Unlike LINEAR/ROTARY there is no separate homing speed to
     * source, so the home config takes the same converted values. */
    int32_t homing_speed_mm = CONFIG_KLIPPER_MMU_SELECTOR_MOVE_SPEED_MM_S;
    sel->home.speed_steps_s = sel->velocity_steps_s;
#else
    int32_t homing_speed_mm = CONFIG_KLIPPER_MMU_SELECTOR_HOMING_SPEED_MM_S;
    sel->home.speed_steps_s = (uint32_t)KM_SELECTOR_MM_TO_STEPS(homing_speed_mm);
#endif
    /* Happy Hare has one selector_accel for every selector move including
     * homing; there is no separate deceleration to source, so the core's
     * symmetric ramp takes the same converted value for both. */
    sel->home.acceleration_steps_s2 = sel->acceleration_steps_s2;
    sel->home.deceleration_steps_s2 = sel->acceleration_steps_s2;

#if CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED
    /* Happy Hare's _get_max_selector_movement for IndexedSelector: num_gates
     * * cad_gate_width * cad_max_rotations (mmu_indexed_selector.py:184-186),
     * a whole-drum sweep bound rather than LINEAR/ROTARY's CAD gate-position
     * formula -- device-supplied in millimetres (Kconfig.selector), never
     * derived from lane count alone. */
    int64_t max_travel_um = (int64_t)CONFIG_KLIPPER_MMU_SELECTOR_INDEXED_MAX_TRAVEL_MM * 1000;
#else
    int64_t max_travel_um = KM_SELECTOR_MAX_TRAVEL_UM(lanes);
#endif
    if (max_travel_um <= 0 || max_travel_um > INT32_MAX)
        return KM_ERR_INVALID_PARAM;
    sel->home.max_travel_steps = (uint32_t)KM_SELECTOR_UM_TO_STEPS(max_travel_um);

    /* Firmware-owned safety bounds; Happy Hare has no equivalent setting.
     * The switch sample must be at least as fresh as the shared sensor-age
     * setting already used for the bay/load/hub switches, and the deadline
     * is 3x the time a full homing traverse takes at the configured homing
     * speed -- derived from the cited Happy Hare quantities above, not a
     * separate invented number. */
    sel->home.max_switch_age_us = (uint32_t)(CONFIG_KLIPPER_MMU_SENSOR_AGE * 1000);
    int64_t deadline_us = 3 * max_travel_um * 1000LL / homing_speed_mm;
    if (deadline_us <= 0 || deadline_us >= 0x80000000LL)
        return KM_ERR_INVALID_PARAM;
    sel->home.deadline_us = (uint32_t)deadline_us;
    /* INDEXED never actually reads this: every seek's direction is chosen
     * dynamically from gate_order (operation_core.c's indexed_direction),
     * never this fixed "toward the low end" convention. Still validated
     * (device_profile.c needs a real KM_DIR_* for every stepper-axis kind),
     * so this line applies uniformly and harmlessly. */
    sel->home.home_direction =
        CONFIG_KLIPPER_MMU_SELECTOR_HOME_TOWARD_MIN ? KM_DIR_REVERSE : KM_DIR_FORWARD;

#if CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED
    /* No gate position table (klipper_mmu/selector.h): gate_order and the
     * centering nudge replace gate_steps entirely. An empty order string is
     * AFC's own rule -- always search forward, no gate_order and no
     * calibrated switch width to center with (device_profile.c's own
     * sentinel; extras/AFC_vivid.py:159-199 select_lane) -- so gate_order
     * stays all-zero and the nudge stays 0, both left exactly as this
     * struct was zero-initialized. */
    if (CONFIG_KLIPPER_MMU_SELECTOR_GATE_ORDER[0]) {
        int32_t order[KM_MAX_LANES] = {0};
        if (!parse_scaled_list(CONFIG_KLIPPER_MMU_SELECTOR_GATE_ORDER, lanes, 0, order))
            return KM_ERR_INVALID_PARAM;
        bool seen[KM_MAX_LANES] = {0};
        for (unsigned i = 0; i < lanes; ++i) {
            if (order[i] < 0 || order[i] >= (int32_t)lanes || seen[order[i]])
                return KM_ERR_INVALID_PARAM;
            seen[order[i]] = true;
            sel->gate_order[i] = (uint8_t)order[i];
        }
        int32_t width_steps = KM_SELECTOR_MM_TO_STEPS(CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_WIDTH_MM);
        if (width_steps < 2)
            return KM_ERR_INVALID_PARAM;
        sel->center_nudge_steps = (uint32_t)width_steps / 2u;
    } else {
        int32_t unselect_steps = KM_SELECTOR_MM_TO_STEPS(CONFIG_KLIPPER_MMU_SELECTOR_INDEXED_UNSELECT_MM);
        if (unselect_steps <= 0)
            return KM_ERR_INVALID_PARAM;
        sel->unselect_steps = (uint32_t)unselect_steps;
    }
#else
    int32_t positions_um[KM_MAX_LANES] = {0};
    if (!parse_scaled_list(CONFIG_KLIPPER_MMU_SELECTOR_GATE_POSITIONS_MM, lanes, 3, positions_um))
        return KM_ERR_INVALID_PARAM;
    for (unsigned i = 0; i < lanes; ++i) {
        int32_t steps = KM_SELECTOR_UM_TO_STEPS(positions_um[i]);
        if (selector_gate_override_set[i])
            steps = selector_gate_override_steps[i];
        sel->gate_steps[i] = steps;
    }
#endif

#if CONFIG_KLIPPER_MMU_SELECTOR_KIND_ROTARY
    int32_t directions[KM_MAX_LANES] = {0};
    if (!parse_scaled_list(CONFIG_KLIPPER_MMU_SELECTOR_GATE_DIRECTIONS, lanes, 0, directions))
        return KM_ERR_INVALID_PARAM;
    int32_t release[KM_MAX_LANES] = {0};
    if (!parse_scaled_list(CONFIG_KLIPPER_MMU_SELECTOR_RELEASE_GATES, lanes, 0, release))
        return KM_ERR_INVALID_PARAM;
    for (unsigned i = 0; i < lanes; ++i) {
        if (directions[i] != 0 && directions[i] != 1)
            return KM_ERR_INVALID_PARAM;
        if (release[i] < 0 || release[i] >= (int32_t)lanes)
            return KM_ERR_INVALID_PARAM;
        sel->gate_direction[i] = km_selector_gear_direction(directions[i]);
        sel->release_gate[i] = (uint8_t)release[i];
    }
#endif

#if CONFIG_KLIPPER_MMU_SERVO
    sel->has_servo = true;
    sel->servo_dwell_ms = CONFIG_KLIPPER_MMU_SELECTOR_SERVO_DWELL_MS;
    sel->servo_hold = CONFIG_KLIPPER_MMU_SELECTOR_SERVO_ALWAYS_ACTIVE;
#if CONFIG_KLIPPER_MMU_SELECTOR_KIND_LINEAR
    sel->servo_engage_us = KM_SELECTOR_SERVO_US(CONFIG_KLIPPER_MMU_SELECTOR_SERVO_ENGAGE_ANGLE_DEG);
    sel->servo_release_us = KM_SELECTOR_SERVO_US(CONFIG_KLIPPER_MMU_SELECTOR_SERVO_RELEASE_ANGLE_DEG);
    sel->servo_move_us = KM_SELECTOR_SERVO_US(CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MOVE_ANGLE_DEG);
#endif
#endif
    return KM_OK;
}
#endif /* CONFIG_KLIPPER_MMU_SELECTOR_STEPPER */

#if CONFIG_KLIPPER_MMU_SERVO && !CONFIG_KLIPPER_MMU_SELECTOR_STEPPER
/* SERVO kind (MMX): no selector stepper, so km_fill_selector above does not
 * exist in this build; the servo fields are filled directly here. */
static km_result_t km_fill_selector(km_selector_profile_t *sel, unsigned lanes)
{
    if (!lanes || lanes > KM_MAX_LANES)
        return KM_ERR_INVALID_PARAM;
    sel->has_servo = true;
    sel->servo_dwell_ms = CONFIG_KLIPPER_MMU_SELECTOR_SERVO_DWELL_MS;
    sel->servo_hold = CONFIG_KLIPPER_MMU_SELECTOR_SERVO_ALWAYS_ACTIVE;
#if CONFIG_KLIPPER_MMU_SELECTOR_KIND_SERVO
    /* An empty list (Pico MMU's own default: Happy Hare calibrates its cam
     * instead of shipping angles) leaves every servo_gate_us at 0 --
     * "uncalibrated" (klipper_mmu/selector.h). Startup then succeeds; a
     * SELECT of an uncalibrated gate is refused at admission until the host
     * measures and saves a real angle (km_core_set_selector_gate). */
    if (CONFIG_KLIPPER_MMU_SELECTOR_SERVO_GATE_ANGLES_DEG[0]) {
        int32_t angles[KM_MAX_LANES] = {0};
        if (!parse_scaled_list(CONFIG_KLIPPER_MMU_SELECTOR_SERVO_GATE_ANGLES_DEG, lanes, 0, angles))
            return KM_ERR_INVALID_PARAM;
        for (unsigned i = 0; i < lanes; ++i) {
            if (angles[i] < 0 || angles[i] > CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MAX_ANGLE_DEG)
                return KM_ERR_INVALID_PARAM;
            sel->servo_gate_us[i] = KM_SELECTOR_SERVO_US(angles[i]);
        }
    }
#if CONFIG_KLIPPER_MMU_SELECTOR_SERVO_RELEASE_ANGLE_DEG >= 0
    sel->servo_release_us = KM_SELECTOR_SERVO_US(CONFIG_KLIPPER_MMU_SELECTOR_SERVO_RELEASE_ANGLE_DEG);
#endif
#endif
    return KM_OK;
}
#endif

km_result_t km_config_profile(km_device_profile_t *out,
                               const km_diff_adc_config_t *calibration)
{
    if (!out || (!CONFIG_KLIPPER_MMU_DIFF_BUFFER && calibration))
        return KM_ERR_INVALID_PARAM;
    if (!CONFIG_KLIPPER_MMU_PROFILE_IMPLEMENTED)
        return KM_ERR_NOT_SUPPORTED;
    /* Only FPS arrival may replace the toolhead switch. */
    if (!CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR && !CONFIG_KLIPPER_MMU_FPS_ARRIVAL)
        return KM_ERR_INVALID_PARAM;
    if (CONFIG_KLIPPER_MMU_DIFF_BUFFER && !calibration)
        return KM_ERR_UNAVAILABLE;
    km_device_profile_t p = {
        .name = CONFIG_KLIPPER_MMU_DEVICE_NAME,
        .lane_count = CONFIG_KLIPPER_MMU_LANE_COUNT,
        .has_powered_rewind = CONFIG_KLIPPER_MMU_POWERED_REWIND,
        .has_encoder = CONFIG_KLIPPER_MMU_ENCODER,
        .encoder_detection_length_steps =
            KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_ENCODER_DETECTION_LENGTH_MM),
        .has_load_sensors = CONFIG_KLIPPER_MMU_LOAD_SENSORS,
        .has_proportional_buffer = CONFIG_KLIPPER_MMU_DIFF_BUFFER,
        .has_current_sensing = CONFIG_KLIPPER_MMU_MEASURED_CURRENT,
        .has_bay_sensors = CONFIG_KLIPPER_MMU_BAY_SENSORS, .has_hub_sensor = true,
        .has_toolhead_sensor = CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR,
        .default_feed_velocity_steps_s =
            KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_FEED_SPEED_MM_S),
        .default_rewind_velocity_steps_s =
            KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_RETRACT_SPEED_MM_S),
        .default_acceleration_steps_s2 =
            KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_ACCELERATION_MM_S2),
        .default_deceleration_steps_s2 =
            KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_DECELERATION_MM_S2),
        .default_jerk_steps_s3 =
            KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_JERK_MM_S3),
        .default_run_current_ma = CONFIG_KLIPPER_MMU_RUN_CURRENT,
        .default_hold_current_ma = CONFIG_KLIPPER_MMU_HOLD_CURRENT,
        .rewind_pwm_duty_permille = CONFIG_KLIPPER_MMU_REWIND_DUTY,
        .rewind_current_limit_ma = CONFIG_KLIPPER_MMU_CURRENT_LIMIT,
        .rewind_current_target_ma = CONFIG_KLIPPER_MMU_CURRENT_TARGET,
        /* Micrometres/s per permille: the micrometre and the core's x1000
         * both cancel, so the same scale factor applies. */
        .proportional_gain_milli =
            KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_BUFFER_GAIN_UM_S),
        .buffer_deadband_permille = CONFIG_KLIPPER_MMU_BUFFER_DEADBAND,
        /* Unset (0) values are rejected by km_profile_validate, never defaulted. */
        .fps_arrival = CONFIG_KLIPPER_MMU_FPS_ARRIVAL,
        .fps_arrival_permille = CONFIG_KLIPPER_MMU_FPS_ARRIVAL_PERMILLE,
        .fps_arrival_samples = CONFIG_KLIPPER_MMU_FPS_ARRIVAL_SAMPLES,
        .fps_arrival_hold_ms = CONFIG_KLIPPER_MMU_FPS_ARRIVAL_HOLD_MS,
        .fps_arrival_min_steps = KM_FPS_MIN_STEPS,
        /* KLIPPER_MMU_FPS_ARRIVAL_MAX_MM now also feeds every device's
         * KM_ARRIVAL_TIMEOUT_MS/KM_RETRACT_TIMEOUT_MS/KM_CAL_RETRACT_TIMEOUT_MS
         * below, so it defaults nonzero even without FPS arrival: zero this
         * field out here instead, exactly as it was 0 before that move
         * (km_profile_validate rejects a nonzero one without fps_arrival). */
        .fps_arrival_max_steps = CONFIG_KLIPPER_MMU_FPS_ARRIVAL ? KM_FPS_MAX_STEPS : 0,
        .fps_calibrated_steps = fps_calibrated_steps,
        .load_cruise_velocity_steps_s =
            KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_LOAD_CRUISE_SPEED_MM_S),
        .load_crawl_velocity_steps_s =
            KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_LOAD_CRAWL_SPEED_MM_S),
        .load_window_steps =
            KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_LOAD_WINDOW_MM),
        .buffer_travel_steps =
            KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_BUFFER_TRAVEL_MM),
        .max_sensor_age_ms = CONFIG_KLIPPER_MMU_SENSOR_AGE,
        .host_watchdog_ms = CONFIG_KLIPPER_MMU_HOST_LEASE,
        .progress_timeout_ms = CONFIG_KLIPPER_MMU_PROGRESS_TIMEOUT,
        .hub_feed_timeout_ms = KM_HUB_TIMEOUT_MS,
        .buffer_feed_timeout_ms = KM_ARRIVAL_TIMEOUT_MS,
        .unload_retract_timeout_ms = KM_RETRACT_TIMEOUT_MS,
        .unload_rewind_timeout_ms = KM_REWIND_TIMEOUT_MS,
        .unload_hub_clear_steps =
            KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_UNLOAD_HUB_CLEAR_MM),
        .eject_final_steps =
            KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_EJECT_FINAL_DISTANCE_MM),
        .eject_timeout_ms = CONFIG_KLIPPER_MMU_EJECT_FINAL_DISTANCE_MM
            ? KM_STAGE_DEADLINE_MS(CONFIG_KLIPPER_MMU_EJECT_FINAL_DISTANCE_MM,
                                   CONFIG_KLIPPER_MMU_RETRACT_SPEED_MM_S) : 0,
        .ptfe_cal_probe_timeout_ms = CONFIG_KLIPPER_MMU_CAL_PROBE_TIMEOUT,
        .ptfe_cal_retract_timeout_ms = KM_CAL_RETRACT_TIMEOUT_MS,
    };
    if (CONFIG_KLIPPER_MMU_VIRTUAL_HUB)
        p.load_is_virtual_hub = true;
    if (calibration) { p.diff_calibration = *calibration; p.has_diff_calibration = true; }
#if CONFIG_KLIPPER_MMU_SELECTOR_KIND_LINEAR
    p.selector.kind = KM_SELECTOR_LINEAR;
#elif CONFIG_KLIPPER_MMU_SELECTOR_KIND_ROTARY
    p.selector.kind = KM_SELECTOR_ROTARY;
#elif CONFIG_KLIPPER_MMU_SELECTOR_KIND_SERVO
    p.selector.kind = KM_SELECTOR_SERVO;
#elif CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED
    p.selector.kind = KM_SELECTOR_INDEXED;
#else
    p.selector.kind = KM_SELECTOR_NONE;
#endif
    p.has_selector = p.selector.kind != KM_SELECTOR_NONE;
#if CONFIG_KLIPPER_MMU_SELECTOR_STEPPER || CONFIG_KLIPPER_MMU_SERVO
    if (p.has_selector) {
        km_result_t selector_result = km_fill_selector(&p.selector, p.lane_count);
        if (selector_result != KM_OK)
            return selector_result;
    }
#endif
    km_result_t rotation_result = km_fill_lane_rotation(&p, p.lane_count);
    if (rotation_result != KM_OK)
        return rotation_result;
    km_result_t result = km_profile_validate(&p);
    if (result == KM_OK) *out = p;
    return result;
}
