// SPDX-License-Identifier: GPL-3.0-or-later
#include "stm32/gpio.h"
#include "stm32/internal.h"
#include "command.h"
#include "sched.h"
#include "board/misc.h"                   // timer_read_time
#include "stm32/adc_local.h"
#include "stm32/gpio_local.h"
#include "stm32/pwm.h"
#include "tmc2209.h"                       // km_sim_tmc2209_notify_write/_chip_level
#include "diff_model.h"                    // km_sim_diff_raw
#include "plant.h"                         // km_sim_plant_notify_write/_switch_level
#include "klipper_mmu/mcu/calibration.h"   // km_diff_calibration_get

// Phase 2: DiffA/DiffB (BoxTurtle/AFC Lite preset, KLIPPER_MMU_DIFF_BUFFER)
// live on these two pins (mcu/Kconfig.pins KLIPPER_MMU_PINS_BUFFER_A/_B
// defaults). The position they report is the plant's buffer compression
// (sim/board/plant.c), so FPS arrival and the follower see the real buffer
// move; diff_model.c turns that permille into the pair's raw counts.
#define KM_SIM_DIFF_A_PIN GPIO('C', 4)
#define KM_SIM_DIFF_B_PIN GPIO('C', 5)

// H723 has no GPIOI (see lib/stm32h7/include/stm32h723xx.h), so
// src/stm32/gpio.c's #ifdef GPIOI range never fires for this chip either:
// stop at PH0 to match. GPIO() itself comes from stm32/internal.h, already
// included above, so pin numbers are identical to the real board.
DECL_ENUMERATION_RANGE("pin", "PA0", GPIO('A', 0), 16);
DECL_ENUMERATION_RANGE("pin", "PB0", GPIO('B', 0), 16);
DECL_ENUMERATION_RANGE("pin", "PC0", GPIO('C', 0), 16);
DECL_ENUMERATION_RANGE("pin", "PD0", GPIO('D', 0), 16);
DECL_ENUMERATION_RANGE("pin", "PE0", GPIO('E', 0), 16);
DECL_ENUMERATION_RANGE("pin", "PF0", GPIO('F', 0), 16);
DECL_ENUMERATION_RANGE("pin", "PG0", GPIO('G', 0), 16);
DECL_ENUMERATION_RANGE("pin", "PH0", GPIO('H', 0), 16);
DECL_ENUMERATION("pin", "ADC_TEMPERATURE", 254);


static uint8_t pin_state[9 * 16];

// Every genuine level change on a watched pin (see tmc2209.c) is reported
// with the current time, virtual inside a timer callback exactly like
// timer_read_time() itself. Klipper's shared tmcuart.c only ever mutates an
// output pin from within its own scheduled timer callbacks, so this is
// always a real, precisely timestamped edge, not a poll artifact.
static void set_pin(uint32_t bit, uint8_t val) {
    if (bit >= sizeof(pin_state)) return;
    pin_state[bit] = !!val;
    uint32_t now = timer_read_time();
    km_sim_tmc2209_notify_write(bit, pin_state[bit], now);
    km_sim_plant_notify_write(bit, pin_state[bit], now);
    km_sim_plant_notify_extruder_write(bit, pin_state[bit], now);
}

struct gpio_out gpio_out_setup(uint32_t pin, uint32_t val) {
    struct gpio_out g = {0};
    g.bit = pin;
    gpio_out_reset(g, val);
    return g;
}

void gpio_out_reset(struct gpio_out g, uint32_t val) { set_pin(g.bit, !!val); }

void gpio_out_toggle_noirq(struct gpio_out g) {
    if (g.bit < sizeof(pin_state)) set_pin(g.bit, pin_state[g.bit] ^ 1);
}

void gpio_out_toggle(struct gpio_out g) {
    if (g.bit < sizeof(pin_state)) set_pin(g.bit, pin_state[g.bit] ^ 1);
}

void gpio_out_write(struct gpio_out g, uint32_t val) { set_pin(g.bit, !!val); }

struct gpio_in gpio_in_setup(uint32_t pin, int32_t pull_up) {
    struct gpio_in g = {0};
    g.bit = pin;
    return g;
}

void gpio_in_reset(struct gpio_in g, int32_t pull_up) {}

// The single-wire TMC UART pad is the wired-AND of the MCU's own output and
// the chip's: idle/released reads as whichever side is actually driving low,
// exactly like the real open-drain-style pull-up line.
//
// BAY/LOAD/HUB switch pins are answered by the plant (sim/board/plant.c)
// instead: those pins are never driven by set_pin() (they are firmware
// inputs, not outputs), so pin_state for them would otherwise sit at its
// zero-initialized default forever, regardless of the filament plant's
// state.
uint8_t gpio_in_read(struct gpio_in g) {
    int plant_level = km_sim_plant_switch_level(g.bit);
    if (plant_level >= 0) return (uint8_t)plant_level;
    if (g.bit < sizeof(pin_state))
        return pin_state[g.bit] && km_sim_tmc2209_chip_level(g.bit);
    return 0;
}

// GPIO_LOCAL_OK is 0 (stm32/gpio_local.h), a status code, not a boolean:
// board_startup.c's reserve_expected_mmu_pins() and diff_probe.c compare
// this return value against GPIO_LOCAL_OK/_ERR_* by name, so a stub that
// returned a bare truthy 1 here fell through every named case straight to
// "return KM_ERR_FAULTED" -- the sim board never actually reserved pins
// successfully, it only looked like it did until something checked.
int gpio_local_reserve(const uint32_t *pins, uint32_t count) { return GPIO_LOCAL_OK; }
struct gpio_in gpio_in_setup_local(uint32_t pin, int32_t pull_up) { return gpio_in_setup(pin, pull_up); }
struct gpio_out gpio_out_setup_local(uint32_t pin, uint32_t val) { return gpio_out_setup(pin, val); }

int gpio_adc_pair_validate_pins(uint32_t pin_a, uint32_t pin_b) { return 1; }
void gpio_adc_pair_setup(uint32_t pin_a, uint32_t pin_b) {}
void gpio_adc_pair_sample_prepare(void) {}
int gpio_adc_pair_sample_start(void) { return 1; }
void gpio_adc_pair_sample_read(uint16_t *a, uint16_t *b) {
    *a = 2048; *b = 2048;
}
void gpio_adc_pair_sample_cancel(void) {}

int gpio_pwm_pair_validate_pins(uint32_t pin_a, uint32_t pin_b) { return GPIO_PWM_PAIR_OK; }
// struct gpio_pwm_pair is defined only inside the excluded stm32/pwm.c (real
// STM32 timer/CCR state), so it is opaque here. But an all-zero-returning
// stub isn't just "unmodeled hardware": mcu/pwm_bridge_gpio.c's io_write_pwm
// reads back gpio_pwm_pair_get_info() after every update() and rejects the
// write unless is_ready is set and the confirmed forward/reverse counts
// match what was just requested -- exactly the acknowledgement discipline
// real hardware gives it. A stub that leaves that struct un-filled reads as
// "never ready," so every powered-rewind lane's very first safe-stop write
// (0/0 duty, done unconditionally during setup) latched a driver fault.
// This is an ideal PWM: whatever is written reads back as confirmed
// immediately, with no timer lag.
#define KM_SIM_PWM_PAIR_MAX 8
typedef struct { uint32_t confirmed_forward, confirmed_reverse; } km_sim_pwm_pair_t;
static km_sim_pwm_pair_t pwm_pairs[KM_SIM_PWM_PAIR_MAX];
int gpio_pwm_pair_setup(const struct gpio_pwm_pair_config *config, struct gpio_pwm_pair **out) {
    (void)config;
    static unsigned next;
    if (!out || next >= KM_SIM_PWM_PAIR_MAX) return GPIO_PWM_PAIR_ERR_INVALID;
    km_sim_pwm_pair_t *p = &pwm_pairs[next++];
    p->confirmed_forward = 0;
    p->confirmed_reverse = 0;
    *out = (struct gpio_pwm_pair *)p;
    return GPIO_PWM_PAIR_OK;
}
void gpio_pwm_pair_write(struct gpio_pwm_pair *pair, uint32_t val_a, uint32_t val_b) {
    km_sim_pwm_pair_t *p = (km_sim_pwm_pair_t *)pair;
    if (!p) return;
    p->confirmed_forward = val_a;
    p->confirmed_reverse = val_b;
}
int gpio_pwm_pair_update(struct gpio_pwm_pair *pair, uint32_t forward_val, uint32_t reverse_val) {
    km_sim_pwm_pair_t *p = (km_sim_pwm_pair_t *)pair;
    if (!p) return GPIO_PWM_PAIR_ERR_INVALID;
    p->confirmed_forward = forward_val;
    p->confirmed_reverse = reverse_val;
    return GPIO_PWM_PAIR_OK;
}
void gpio_pwm_pair_raw_disable(struct gpio_pwm_pair *pair) {
    km_sim_pwm_pair_t *p = (km_sim_pwm_pair_t *)pair;
    if (!p) return;
    p->confirmed_forward = 0;
    p->confirmed_reverse = 0;
}
int gpio_pwm_pair_is_ready(const struct gpio_pwm_pair *pair) { return pair != NULL; }
int gpio_pwm_pair_get_info(const struct gpio_pwm_pair *pair, struct gpio_pwm_pair_info *out_info) {
    const km_sim_pwm_pair_t *p = (const km_sim_pwm_pair_t *)pair;
    if (!p || !out_info) return GPIO_PWM_PAIR_ERR_INVALID;
    out_info->hwpwm_ticks = 1;
    out_info->prescaler = 1;
    out_info->reverse_hwpwm_ticks = 1;
    out_info->reverse_prescaler = 1;
    out_info->confirmed_forward_val = p->confirmed_forward;
    out_info->confirmed_reverse_val = p->confirmed_reverse;
    out_info->confirmed_forward_counts = p->confirmed_forward;
    out_info->confirmed_reverse_counts = p->confirmed_reverse;
    out_info->forward_channel = 0;
    out_info->reverse_channel = 1;
    out_info->same_timer = 0;
    out_info->is_ready = 1;
    return GPIO_PWM_PAIR_OK;
}
void gpio_pwm_pair_shutdown(void) {}

int gpio_pin_is_local(uint32_t pin) { return 1; }
int gpio_adc_setup_local_pair_timeout(uint32_t pin_a, uint32_t pin_b, uint32_t timeout_us, struct gpio_adc *out_a, struct gpio_adc *out_b) {
    (void)timeout_us;
    if (!out_a || !out_b) return GPIO_ADC_LOCAL_ERR_INVALID;
    *out_a = gpio_adc_setup(pin_a);
    *out_b = gpio_adc_setup(pin_b);
    return GPIO_ADC_LOCAL_OK;
}

int gpio_adc_get_route(uint32_t pin, struct gpio_adc_route *out) {
    if (out) { out->unit = 1; out->channel = pin & 0xf; }
    return 1;
}

int gpio_pwm_get_route(uint32_t pin, struct gpio_pwm_route *out) {
    if (out) { out->timer_id = 1; out->channel = 1; }
    return 1;
}

struct gpio_adc gpio_adc_setup(uint32_t pin) {
    struct gpio_adc g = {0}; g.chan = pin; return g;
}
// 0 means "ready now" (see mcu/adc.c reader_hal_poll); the sim conversion
// is instant, so every channel is always ready on the first poll.
uint32_t gpio_adc_sample(struct gpio_adc g) { (void)g; return 0; }
uint16_t gpio_adc_read(struct gpio_adc g) {
#if defined(CONFIG_WANT_ADC) && CONFIG_WANT_ADC && defined(CONFIG_WANT_ADC_LOCAL) \
    && CONFIG_WANT_ADC_LOCAL && defined(CONFIG_KLIPPER_MMU_DIFF_BUFFER) \
    && CONFIG_KLIPPER_MMU_DIFF_BUFFER
    if (g.chan == KM_SIM_DIFF_A_PIN || g.chan == KM_SIM_DIFF_B_PIN) {
        const km_diff_adc_config_t *cfg = km_diff_calibration_get();
        uint16_t raw_a = 2048, raw_b = 2048;
        if (cfg && km_sim_diff_raw(cfg, km_sim_plant_buffer_permille(),
                                   &raw_a, &raw_b) == 0)
            return g.chan == KM_SIM_DIFF_A_PIN ? raw_a : raw_b;
    }
#else
    (void)g;
#endif
    return 2048;
}
void gpio_adc_cancel_sample(struct gpio_adc g) { (void)g; }

struct gpio_pwm gpio_pwm_setup(uint8_t pin, uint32_t cycle_time, uint32_t val) {
    struct gpio_pwm g = {0}; return g;
}
void gpio_pwm_write(struct gpio_pwm g, uint32_t val) {}

void gpio_peripheral(uint32_t pin, uint32_t val, int pull_up) {}

