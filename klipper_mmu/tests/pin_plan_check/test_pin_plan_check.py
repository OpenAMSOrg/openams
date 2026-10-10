# SPDX-License-Identifier: GPL-3.0-or-later
"""tools/pin_plan_check.py against small tables in Klipper's own format."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
import pin_plan_check as check  # noqa: E402

HARD_PWM = """static const struct gpio_pwm_info pwm_regs[] = {
#elif CONFIG_MACH_STM32G4
  {TIM9, GPIO('A', 0), 1},
#elif CONFIG_MACH_STM32H7
  {TIM2, GPIO('A', 0), 1},  // first route wins over TIM5 below
  {TIM2, GPIO('A', 1), 2},
  {TIM3, GPIO('A', 6), 1}, {TIM3, GPIO('A', 7), 2},
  {TIM12, GPIO('B', 14), 1}, {TIM12, GPIO('B', 15), 2},
  {TIM15, GPIO('E', 5), 1}, {TIM15, GPIO('E', 6), 2},
  {TIM5, GPIO('A', 0), 1},
#endif
};
"""
# ADC1 channels 4 and 8 on PC4/PC5, ADC2 channel 15 on PA3; the H723 branch
# is the one the firmware uses.
ADC = ("static const uint8_t adc_pins[] = {\n#if CONFIG_MACH_STM32H7\n"
       + "    ADC_INVALID_PIN,\n" * 4 + "    GPIO('C', 4),  // ADC12_INP4\n"
       + "    ADC_INVALID_PIN,\n" * 3 + "    GPIO('C', 5),\n"
       + "    ADC_INVALID_PIN,\n" * 11 + "    ADC_INVALID_PIN,\n" * 15
       + "    GPIO('A', 3),\n"
       + "  #if CONFIG_MACH_STM32H723\n    ADC_TEMPERATURE_PIN,\n"
       + "  #else\n    GPIO('F', 9),\n  #endif\n"
       + "#elif CONFIG_MACH_STM32G4\n    GPIO('B', 1),\n#endif\n};\n")


def config(**overrides):
    base = {
        'CONFIG_MACH_STM32H723': 'y', 'CONFIG_KLIPPER_MMU_PIN_CONFIG': 'y',
        'CONFIG_KLIPPER_MMU_LANE_COUNT': '2',
        'CONFIG_KLIPPER_MMU_DIFF_BUFFER': 'y',
        'CONFIG_KLIPPER_MMU_POWERED_REWIND': 'y',
        'CONFIG_KLIPPER_MMU_PINS_STEP': 'PD4,PC12',
        'CONFIG_KLIPPER_MMU_PINS_DIR': 'PD3,PC11',
        'CONFIG_KLIPPER_MMU_PINS_ENABLE': 'PD6,PD1',
        'CONFIG_KLIPPER_MMU_PINS_UART': 'PD5,PD0',
        'CONFIG_KLIPPER_MMU_BAY_SENSORS': 'y',
        'CONFIG_KLIPPER_MMU_PINS_BAY': 'PE13,PB0',
        'CONFIG_KLIPPER_MMU_PINS_HUB': 'PE12',
        'CONFIG_KLIPPER_MMU_PINS_BUFFER_A': 'PC4',
        'CONFIG_KLIPPER_MMU_PINS_BUFFER_B': 'PC5',
        'CONFIG_KLIPPER_MMU_PINS_REWIND_FORWARD': 'PA1,PA7',
        'CONFIG_KLIPPER_MMU_PINS_REWIND_REVERSE': 'PA0,PA6',
        'CONFIG_KLIPPER_MMU_PINS_REWIND_ENABLE': 'PA2,PA5',
    }
    base.update(overrides)
    return base


class PinPlanCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = Path(self.tmp.name)
        (self.src / 'stm32').mkdir()
        (self.src / 'stm32/hard_pwm.c').write_text(HARD_PWM)
        (self.src / 'stm32/stm32h7_adc.c').write_text(ADC)

    def tearDown(self):
        self.tmp.cleanup()

    def problems(self, **overrides):
        return check.check(config(**overrides), self.src)

    def test_routes_come_from_the_h7_tables_first_entry_first(self):
        self.assertEqual(check.pwm_routes(self.src)['PA0'], (2, 1))
        adc = check.adc_routes(self.src)
        self.assertEqual((adc['PC4'], adc['PC5'], adc['PA3']), ((1, 4), (1, 8), (2, 15)))
        self.assertNotIn('PF9', adc)  # the non-H723 branch is not used

    def test_valid_map_passes_and_disabled_config_is_ignored(self):
        self.assertEqual(self.problems(), [])
        self.assertEqual(self.problems(CONFIG_KLIPPER_MMU_PIN_CONFIG='n',
                                       CONFIG_KLIPPER_MMU_PINS_STEP='junk'), [])

    def test_wrong_count_and_duplicate_pins(self):
        problem, = self.problems(CONFIG_KLIPPER_MMU_PINS_STEP='PD4')
        self.assertIn('CONFIG_KLIPPER_MMU_PINS_STEP needs 2 pins', problem)
        problem, = self.problems(CONFIG_KLIPPER_MMU_PINS_HUB='PD4')
        self.assertEqual(problem, 'PD4 is used twice: STEP lane 0 and HUB switch')

    def test_encoder_pin_b_optional_unless_dual_pin(self):
        # Off entirely: neither pin role exists, nothing for this checker to see.
        self.assertEqual(self.problems(CONFIG_KLIPPER_MMU_ENCODER='n'), [])
        # One wire -- Happy Hare's own ERCF/Tradrack shape: A alone is enough.
        self.assertEqual(self.problems(CONFIG_KLIPPER_MMU_ENCODER='y',
                                       CONFIG_KLIPPER_MMU_PINS_ENCODER_A='PF2'), [])
        # A is still required even with the second wire off.
        problem, = self.problems(CONFIG_KLIPPER_MMU_ENCODER='y')
        self.assertIn('CONFIG_KLIPPER_MMU_PINS_ENCODER_A needs 1 pin', problem)
        # Dual pin on: B becomes required too.
        problem, = self.problems(CONFIG_KLIPPER_MMU_ENCODER='y',
                                 CONFIG_KLIPPER_MMU_PINS_ENCODER_A='PF2',
                                 CONFIG_KLIPPER_MMU_ENCODER_DUAL_PIN='y')
        self.assertIn('CONFIG_KLIPPER_MMU_PINS_ENCODER_B needs 1 pin', problem)
        self.assertEqual(self.problems(CONFIG_KLIPPER_MMU_ENCODER='y',
                                       CONFIG_KLIPPER_MMU_PINS_ENCODER_A='PF2',
                                       CONFIG_KLIPPER_MMU_ENCODER_DUAL_PIN='y',
                                       CONFIG_KLIPPER_MMU_PINS_ENCODER_B='PF3'), [])

    def test_bay_optional_and_virtual_hub_drop_their_pin_roles(self):
        # No-BAY devices (Angry Beaver, 3MS, QuattroBox) skip the BAY role
        # entirely, so a stray duplicate against the (unused) BAY pin string
        # is no longer reported.
        self.assertEqual(self.problems(CONFIG_KLIPPER_MMU_BAY_SENSORS='n',
                                       CONFIG_KLIPPER_MMU_PINS_BAY='PD4,PD1'), [])
        # Virtual-hub devices (EMU, KMS) drop the shared HUB role, so a stray
        # duplicate against the (unused) HUB pin string is no longer reported.
        self.assertEqual(self.problems(CONFIG_KLIPPER_MMU_VIRTUAL_HUB='y',
                                       CONFIG_KLIPPER_MMU_PINS_HUB='PD4'), [])

    def test_gear_tmc_uart_off_and_single_switch_buffer_drop_their_pin_roles(self):
        # A gear with no TMC UART chip (QIDI Box) skips the UART role
        # entirely, so a stray duplicate against the (unused) UART pin
        # string is no longer reported. GEAR_TMC_UART itself is a
        # promptless, device-derived symbol (mcu/Kconfig.control) that
        # kconfiglib never writes to a saved .config, so roles() keys this
        # off the device name directly, the same fact GEAR_TMC_UART's own
        # Kconfig default derives it from.
        self.assertEqual(self.problems(CONFIG_KLIPPER_MMU_DEVICE_QIDI_BOX='y',
                                       CONFIG_KLIPPER_MMU_PINS_UART='PD4,PD1'), [])
        # A tension-only buffer (single switch) skips the second,
        # compression-switch role.
        self.assertEqual(self.problems(CONFIG_KLIPPER_MMU_DIFF_BUFFER='n',
                                       CONFIG_KLIPPER_MMU_BUFFER_SINGLE_SWITCH='y',
                                       CONFIG_KLIPPER_MMU_PINS_BUFFER_B='PD4'), [])
        # No device named at all (every device but QIDI Box) still means
        # "has TMC UART", the ordinary case.
        problem, = self.problems(CONFIG_KLIPPER_MMU_PINS_UART='PD4')
        self.assertIn('CONFIG_KLIPPER_MMU_PINS_UART needs 2 pins', problem)

    def test_respooler_pairs_follow_the_pair_timer_rules(self):
        problem, = self.problems(CONFIG_KLIPPER_MMU_PINS_REWIND_FORWARD='PA1,PE5',
                                 CONFIG_KLIPPER_MMU_PINS_REWIND_REVERSE='PA0,PE6')
        self.assertIn('lane 1: forward PE5 and reverse PE6', problem)
        self.assertIn('TIM15 ch1, TIM15 ch2', problem)
        problem, = self.problems(CONFIG_KLIPPER_MMU_PINS_REWIND_FORWARD='PF0,PA7')
        self.assertIn('no PWM', problem)
        # One lane may mix timers (TIM12 and TIM3) as long as no other lane uses them.
        self.assertEqual(self.problems(CONFIG_KLIPPER_MMU_PINS_REWIND_FORWARD='PA1,PB15',
                                       CONFIG_KLIPPER_MMU_PINS_REWIND_REVERSE='PA0,PA6'), [])
        self.assertEqual(self.problems(
            CONFIG_KLIPPER_MMU_PINS_REWIND_FORWARD='PA1,PA0',
            CONFIG_KLIPPER_MMU_PINS_REWIND_REVERSE='PA6,PA7'),
            ['respooler lanes 0 and 1 both use TIM2; each lane needs its own timer',
             'respooler lanes 0 and 1 both use TIM3; each lane needs its own timer'])

    def test_diff_pair_needs_two_channels_of_one_adc(self):
        problem, = self.problems(CONFIG_KLIPPER_MMU_PINS_BUFFER_B='PA3')
        self.assertIn('DiffA PC4 and DiffB PA3', problem)
        self.assertIn('ADC1 ch4, ADC2 ch15', problem)
        self.assertEqual(self.problems(CONFIG_KLIPPER_MMU_DIFF_BUFFER='n',
                                       CONFIG_KLIPPER_MMU_PINS_BUFFER_B='PA3'), [])

    def test_other_mcus_get_only_the_generic_checks(self):
        self.assertEqual(self.problems(CONFIG_MACH_STM32H723='n',
                                       CONFIG_KLIPPER_MMU_PINS_REWIND_FORWARD='PF0,PF1'), [])

    def selector_config(self, **overrides):
        # A selector's own pins and its single-pin gear, without the base
        # fixture's per-lane BAY/rewind roles (moot for a selector device).
        base = dict(CONFIG_KLIPPER_MMU_BAY_SENSORS='n',
                    CONFIG_KLIPPER_MMU_POWERED_REWIND='n',
                    CONFIG_KLIPPER_MMU_DIFF_BUFFER='n',
                    CONFIG_KLIPPER_MMU_PINS_STEP='PD4', CONFIG_KLIPPER_MMU_PINS_DIR='PD3',
                    CONFIG_KLIPPER_MMU_PINS_ENABLE='PD6', CONFIG_KLIPPER_MMU_PINS_UART='PD5')
        base.update(overrides)
        return self.problems(**base)

    def test_selector_gear_is_one_shared_pin_not_one_per_lane(self):
        # A selector build's gear pins collapse to a single shared pin
        # regardless of LANE_COUNT (selector.h), so the base 2-lane STEP list
        # is rejected...
        problems = self.selector_config(CONFIG_KLIPPER_MMU_SELECTOR_STEPPER='y',
                                        CONFIG_KLIPPER_MMU_PINS_STEP='PD4,PC12',
                                        CONFIG_KLIPPER_MMU_PINS_SELECTOR_STEP='PB0',
                                        CONFIG_KLIPPER_MMU_PINS_SELECTOR_DIR='PB1',
                                        CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENABLE='PB2',
                                        CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENDSTOP='PB3')
        self.assertEqual(len(problems), 1)
        self.assertIn('CONFIG_KLIPPER_MMU_PINS_STEP needs 1 pin', problems[0])
        # ...and a single shared gear pin plus the selector's own pins passes.
        self.assertEqual(self.selector_config(
            CONFIG_KLIPPER_MMU_SELECTOR_STEPPER='y',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_STEP='PB0',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_DIR='PB1',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENABLE='PB2',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENDSTOP='PB3'), [])

    def test_selector_gate_switches_one_per_lane_indexed_only(self):
        # INDEXED's per-gate index switch (klipper_mmu/selector.h): one pin
        # per lane, like BAY, but only checked under SELECTOR_KIND_INDEXED.
        problems = self.selector_config(
            CONFIG_KLIPPER_MMU_SELECTOR_STEPPER='y',
            CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED='y',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_STEP='PB0',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_DIR='PB1',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENABLE='PB2',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENDSTOP='PB3',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_GATE='PB4')  # Only 1 of 2 lanes.
        self.assertEqual(len(problems), 1)
        self.assertIn('CONFIG_KLIPPER_MMU_PINS_SELECTOR_GATE needs 2 pins', problems[0])
        self.assertEqual(self.selector_config(
            CONFIG_KLIPPER_MMU_SELECTOR_STEPPER='y',
            CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED='y',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_STEP='PB0',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_DIR='PB1',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENABLE='PB2',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENDSTOP='PB3',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_GATE='PB4,PB5'), [])
        # Not INDEXED (LINEAR, say): the role is never checked, even if set.
        self.assertEqual(self.selector_config(
            CONFIG_KLIPPER_MMU_SELECTOR_STEPPER='y',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_STEP='PB0',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_DIR='PB1',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENABLE='PB2',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENDSTOP='PB3',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_GATE='bogus'), [])

    def test_selector_pins_catch_duplicates_against_the_gear(self):
        problems = self.selector_config(
            CONFIG_KLIPPER_MMU_SELECTOR_STEPPER='y',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_STEP='PD4',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_DIR='PB1',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENABLE='PB2',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENDSTOP='PB3')
        self.assertEqual(problems, ['PD4 is used twice: STEP and selector STEP'])

    def test_selector_uart_pin_required_when_enabled(self):
        # KLIPPER_MMU_SELECTOR_UART off (a plain step/dir selector): no pin, no problem.
        self.assertEqual(self.selector_config(
            CONFIG_KLIPPER_MMU_SELECTOR_STEPPER='y',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_STEP='PB0',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_DIR='PB1',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENABLE='PB2',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENDSTOP='PB3'), [])
        # On without a pin: refused, never a silent plain step/dir axis.
        problem, = self.selector_config(
            CONFIG_KLIPPER_MMU_SELECTOR_STEPPER='y', CONFIG_KLIPPER_MMU_SELECTOR_UART='y',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_STEP='PB0',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_DIR='PB1',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENABLE='PB2',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENDSTOP='PB3')
        self.assertIn('CONFIG_KLIPPER_MMU_PINS_SELECTOR_UART needs one pin', problem)
        # On with a valid, unclaimed pin: no problem.
        self.assertEqual(self.selector_config(
            CONFIG_KLIPPER_MMU_SELECTOR_STEPPER='y',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_STEP='PB0',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_DIR='PB1',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENABLE='PB2',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENDSTOP='PB3',
            CONFIG_KLIPPER_MMU_SELECTOR_UART='y', CONFIG_KLIPPER_MMU_PINS_SELECTOR_UART='PB4'), [])
        # Malformed value.
        problem, = self.selector_config(
            CONFIG_KLIPPER_MMU_SELECTOR_STEPPER='y',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_STEP='PB0',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_DIR='PB1',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENABLE='PB2',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENDSTOP='PB3',
            CONFIG_KLIPPER_MMU_SELECTOR_UART='y', CONFIG_KLIPPER_MMU_PINS_SELECTOR_UART='junk')
        self.assertIn('CONFIG_KLIPPER_MMU_PINS_SELECTOR_UART needs one pin', problem)
        # Reused against the selector's own STEP pin.
        problems = self.selector_config(
            CONFIG_KLIPPER_MMU_SELECTOR_STEPPER='y',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_STEP='PB0',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_DIR='PB1',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENABLE='PB2',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENDSTOP='PB3',
            CONFIG_KLIPPER_MMU_SELECTOR_UART='y', CONFIG_KLIPPER_MMU_PINS_SELECTOR_UART='PB0')
        self.assertEqual(problems, ['PB0 is used twice: selector STEP and selector TMC UART'])
        # No selector stepper at all (independent per-lane gear): never
        # checked, however malformed -- KLIPPER_MMU_SELECTOR_UART depends on
        # KLIPPER_MMU_SELECTOR_STEPPER, so it is never on here.
        self.assertEqual(self.selector_config(
            CONFIG_KLIPPER_MMU_PINS_STEP='PD4,PC12', CONFIG_KLIPPER_MMU_PINS_DIR='PD3,PC11',
            CONFIG_KLIPPER_MMU_PINS_ENABLE='PD6,PD1', CONFIG_KLIPPER_MMU_PINS_UART='PD5,PD0',
            CONFIG_KLIPPER_MMU_PINS_SELECTOR_UART='not a pin'), [])

    def test_selector_uart_may_share_the_gear_pin_at_a_distinct_address(self):
        # Happy Hare's classic ERCF 1.1 EASY-BRD wiring: gear and selector
        # TMC2209 on one shared UART pin, told apart only by address.
        shared = dict(CONFIG_KLIPPER_MMU_SELECTOR_STEPPER='y',
                     CONFIG_KLIPPER_MMU_PINS_SELECTOR_STEP='PB0',
                     CONFIG_KLIPPER_MMU_PINS_SELECTOR_DIR='PB1',
                     CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENABLE='PB2',
                     CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENDSTOP='PB3',
                     CONFIG_KLIPPER_MMU_SELECTOR_UART='y',
                     CONFIG_KLIPPER_MMU_PINS_SELECTOR_UART='PD5')  # == the gear's own UART pin.
        # Distinct addresses: the one legitimate reuse, no problem reported.
        self.assertEqual(self.selector_config(
            CONFIG_KLIPPER_MMU_TMC_ADDRESSES='0',
            CONFIG_KLIPPER_MMU_SELECTOR_UART_ADDRESS='1', **shared), [])
        self.assertEqual(self.selector_config(
            CONFIG_KLIPPER_MMU_TMC_ADDRESSES='3',
            CONFIG_KLIPPER_MMU_SELECTOR_UART_ADDRESS='0', **shared), [])
        # Equal addresses on the one shared wire cannot be told apart.
        problem, = self.selector_config(
            CONFIG_KLIPPER_MMU_TMC_ADDRESSES='2',
            CONFIG_KLIPPER_MMU_SELECTOR_UART_ADDRESS='2', **shared)
        self.assertIn('PD5: gear and selector TMC2209 share this UART pin but '
                      'both use address 2', problem)
        # The selector address's own implicit default (0, unset here) equals
        # an explicit gear address of 0 too.
        problem, = self.selector_config(CONFIG_KLIPPER_MMU_TMC_ADDRESSES='0', **shared)
        self.assertIn('both use address 0', problem)
        # An unset/malformed gear address can't be proven distinct either.
        problem, = self.selector_config(
            CONFIG_KLIPPER_MMU_SELECTOR_UART_ADDRESS='1', **shared)
        self.assertEqual(problem, 'PD5 shares the gear TMC UART pin but '
                                  'CONFIG_KLIPPER_MMU_TMC_ADDRESSES is not set')
        # Any *other* pin reuse (not the gear's own UART pin) is still a
        # plain conflict regardless of addresses.
        problems = self.selector_config(
            CONFIG_KLIPPER_MMU_TMC_ADDRESSES='0',
            CONFIG_KLIPPER_MMU_SELECTOR_UART_ADDRESS='1',
            **{**shared, 'CONFIG_KLIPPER_MMU_PINS_SELECTOR_UART': 'PB0'})
        self.assertEqual(problems, ['PB0 is used twice: selector STEP and selector TMC UART'])

    def test_servo_only_selector_also_collapses_the_gear_to_one_pin(self):
        # MMX (KM_SELECTOR_SERVO): no selector stepper, so no SELECTOR_* roles,
        # but the gear behind the servo is still one shared driver.
        problems = self.selector_config(CONFIG_KLIPPER_MMU_SERVO='y',
                                        CONFIG_KLIPPER_MMU_PINS_STEP='PD4,PC12',
                                        CONFIG_KLIPPER_MMU_PINS_SERVO='PB4')
        self.assertEqual(len(problems), 1)
        self.assertIn('CONFIG_KLIPPER_MMU_PINS_STEP needs 1 pin', problems[0])
        self.assertEqual(self.selector_config(CONFIG_KLIPPER_MMU_SERVO='y',
                                              CONFIG_KLIPPER_MMU_PINS_SERVO='PB4'), [])

    def test_command_line_exit_status(self):
        cfg = self.src / 'config'
        cfg.write_text('\n'.join('%s="%s"' % item for item in
                                 config(CONFIG_KLIPPER_MMU_PINS_HUB='PD4').items()))
        run = subprocess.run([sys.executable, str(ROOT / 'tools/pin_plan_check.py'),
                              str(cfg), str(self.src)], capture_output=True, text=True,
                             timeout=30)
        self.assertEqual(run.returncode, 1)
        self.assertIn('klipper-mmu pin map: PD4 is used twice', run.stderr)
        run = subprocess.run([sys.executable, str(ROOT / 'tools/pin_plan_check.py'),
                              '--help'], capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0)
        self.assertIn('Reject a klipper-mmu pin map', run.stdout)


if __name__ == '__main__':
    unittest.main()
