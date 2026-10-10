// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KM_SIM_PLANT_H
#define KM_SIM_PLANT_H
// Test-only: a filament plant for the sim board. See plant.c and
// docs/plan/linux-mcu-integration.md, "The plant in the loop".
#include <stdint.h>
#include <stddef.h>

// KM_SIM_PLANT_LANES (plant.c): every image's own configured lane count
// (up to KM_MAX_LANES), selector and non-selector alike. Not part of this
// header's own interface -- nothing outside plant.c needs it.

// DECL_INIT hook: loads each lane's starting tip position from the file
// named by the KM_SIM_PLANT environment variable (one lane per line,
// "lane=<n> state=<empty|bay|load|hub>"; a missing variable or file leaves
// every lane empty) and, if KM_SIM_PLANT_OUT is set, registers an
// atexit/SIGUSR1 ground-truth dump to that path. Exposed here (rather than
// only linker-registered) so a native test can call it directly.
// KM_SIM_PLANT_TOOLHEAD_MM and KM_SIM_PLANT_BUFFER_MM override the sim's own
// extruder-grip distance (150 mm past the HUB) and buffer travel (15 mm)
// with other, still sim-only, geometry.
void km_sim_plant_init(void);

// Called by gpio.c's set_pin() on every output pin write, with the pin's
// new level and the current time (virtual inside a timer callback, exactly
// like timer_read_time() itself). A cheap no-op for any pin that is not one
// of the four lanes' STEP/DIR/ENABLE pins. A qualifying STEP edge (the
// lane's configured active level, mcu/Kconfig.feeder
// KLIPPER_MMU_FEEDER_STEP_HIGH) advances that lane's tip position by one
// step's length in the direction the DIR pin currently reads, but only
// while the tip is at or past the feeder's grip point -- an ungripped lane
// (nothing inserted) sees the pulse counted but not moved.
void km_sim_plant_notify_write(uint32_t pin, uint8_t level, uint32_t time);

// The same hook for the extruder's own STEP/DIR pins (PF0/PF1), which are
// not one of the four lanes: a rising STEP edge credits one microstep of
// filament pulled, signed by the DIR level (high is forward). The plant uses
// the running total to know how much of the filament pushed past the HUB the
// extruder has consumed, which is what makes the buffer's compression fall
// again once the extruder is pulling. See km_sim_plant_buffer_permille().
void km_sim_plant_notify_extruder_write(uint32_t pin, uint8_t level,
                                        uint32_t time);

// The buffer between the feeder and the extruder, in permille of its travel:
// 0 at uncompressed rest, 1000 fully compressed (past the end of its stroke,
// which the firmware's BUFFER_SIGNAL reports). From the farthest tip position
// past the HUB, the extruder's grip point and the extruder's own draw. The
// one number gpio.c's DiffA/DiffB read answers with.
int km_sim_plant_buffer_permille(void);

// Called by gpio.c's gpio_in_read() for a switch pin (BAY/LOAD per lane,
// the shared HUB) or the shared-path encoder's own input pin. Returns the
// polarity-corrected electrical level (mcu/Kconfig.switch KLIPPER_MMU_BAY_
// ACTIVE_HIGH/_LOAD_ACTIVE_HIGH/_HUB_ACTIVE_HIGH) the plant's tip position
// produces at that pin, the encoder's own toggling level (see plant.c's
// KM_SIM_PLANT_ENCODER_A_PIN comment), or -1 if the pin is not one the
// plant owns. A level injected through the control socket
// (km_sim_plant_set_switch_override) wins over either.
int km_sim_plant_switch_level(uint32_t pin);

// Test-only accessor (not used by gpio.c): the lane's current tip position
// in micrometres, for a native test that wants to assert on it directly
// instead of reading the ground-truth file.
int64_t km_sim_plant_position_um(uint8_t lane);

// Test-only accessors (not used by gpio.c): the shared-path encoder's own
// ground truth -- the same numbers km_sim_plant_format_truth's
// encoder_clicks=/encoder_um_per_click= fields already carry, exposed
// directly so a native test can assert on them without parsing that string.
uint64_t km_sim_plant_encoder_click_count(void);
int64_t km_sim_plant_encoder_resolution_um(void);

// ---- Selector mode (shared-drive machines: selector.h) --------------------
// Compiled in only when this image has a selector stepper and/or servo
// (CONFIG_KLIPPER_MMU_SELECTOR_STEPPER || CONFIG_KLIPPER_MMU_SERVO); a
// non-selector image (BoxTurtle, NightOwl, EMU) is unaffected -- the
// per-lane feeder pin tables above are unchanged and these three
// accessors always report 0/-1. One gear STEP/DIR/ENABLE moves the tip of
// whichever lane the selector currently sits at (LINEAR: within
// KM_SIM_PLANT_SELECTOR_TOLERANCE_UM of that gate's configured position AND
// the servo reading its engage pulse; ROTARY: within tolerance of the gate,
// no servo; SERVO: the servo pulse matches that gate's angle, no selector
// axis at all). Gate positions and servo engage/release/move pulses are
// read from the same CONFIG_KLIPPER_MMU_SELECTOR_* Kconfig strings
// mcu/config.c resolves the firmware's own profile from -- not a second
// copy of the numbers.

// The selector axis's current position, in micrometres from its home
// endstop (0 at/below home). Always 0 on a non-selector image or one with
// no selector stepper (pure SERVO kind).
int64_t km_sim_plant_selector_position_um(void);

// The most recently completed servo pulse width, in microseconds (0 if the
// servo has never pulsed, or this image has none).
uint32_t km_sim_plant_servo_pulse_us(void);

// The gate currently engaged (gripped/angled and ready for the gear to
// drive), or -1 if none. See km_sim_plant_format_truth's added fields.
int km_sim_plant_engaged_gate(void);

// ---- Trouble injection (sim/board/control.c's control socket) ------------
// 0 on success, -1 for an argument the plant does not own (unknown lane or
// pin, unknown mode, zero bounce period). Nothing here is reachable from
// firmware: only the test-side control socket calls these.

// Refuse the lane's forward motion from `um` onward (a jam at that position),
// or clear the jam when `um` is negative. Retracting is unaffected.
int km_sim_plant_set_block(uint8_t lane, int64_t um);

// Empty the lane's spool: the strand's trailing end starts at the tip's
// current position and follows it, so a switch behind both stops reading
// made once the tail has passed it.
int km_sim_plant_set_runout(uint8_t lane);

// Force a switch pin's answer instead of letting the plant compute it.
// KM_SIM_PLANT_SW_FREE clears the override, _SW_STUCK returns `level`
// forever, and _SW_BOUNCE toggles 0/1 every `period_us`.
#define KM_SIM_PLANT_SW_FREE   0
#define KM_SIM_PLANT_SW_STUCK  1
#define KM_SIM_PLANT_SW_BOUNCE 2
int km_sim_plant_set_switch_override(uint32_t pin, uint8_t mode, uint8_t level,
                                     uint32_t period_us);

// Write the plant's ground truth into buf as one newline-terminated line:
// "ok" then the same per-lane fields the SIGUSR1 dump writes (lane=,
// position_um=, steps=, dir_changes=, bay=, load=, hub=, block_um=,
// runout=, buffer_permille=, toolhead=, extruder_consumed_um=,
// encoder_clicks=, encoder_um_per_click=), for every lane in order. On a
// selector image, every lane's fields also carry selector_position_um=,
// servo_pulse_us= and engaged_gate= (-1 if none) -- shared hardware,
// repeated per lane like buffer_permille/encoder_clicks already are.
// Returns the number of bytes written (excluding the terminator). `size`
// must be at least ~900 (a 4-lane image) to fit; a 12-lane selector image
// needs the control socket's own larger reply buffer (sim/board/control.c).
int km_sim_plant_format_truth(char *buf, size_t size);

#endif
