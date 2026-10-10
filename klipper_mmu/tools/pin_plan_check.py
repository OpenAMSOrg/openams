#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reject a klipper-mmu pin map at build time that the firmware would refuse
at startup, so a bad map is caught before anything is flashed.

    tools/pin_plan_check.py KCONFIG_CONFIG KLIPPER_SRC

mcu/Makefile runs it on every STM32 build with CONFIG_KLIPPER_MMU_PIN_CONFIG.
It checks each CONFIG_KLIPPER_MMU_PINS_* list (one pin per lane or one shared
pin, no pin used twice; the selector's TMC UART pin is required when
KLIPPER_MMU_SELECTOR_UART is on) and, on the STM32H723,
the rules the startup resource check enforces: each lane's respooler
forward/reverse pins are two channels of a pair-capable timer (TIM2, TIM3,
TIM4, TIM5 or TIM12; TIM12 channels 1-2), no two lanes share a timer, and
DiffA/DiffB are two channels of one ADC. The PWM and ADC routes are read from
KLIPPER_SRC's own tables (src/stm32/hard_pwm.c and src/stm32/stm32h7_adc.c),
first entry per pin, as the firmware resolves them. Exit status 1 with one
line per problem; 0 when the map is acceptable.

The one exception to "no pin used twice" is Happy Hare's classic ERCF 1.1
EASY-BRD wiring: the selector's TMC UART pin may equal the gear's own TMC
UART pin, but only when CONFIG_KLIPPER_MMU_SELECTOR_UART_ADDRESS differs from
the gear's own CONFIG_KLIPPER_MMU_TMC_ADDRESSES (mcu/pin_config.c and
mcu/tmc_uart_gpio.c both enforce the same rule at build/run time). Every other
pin reuse, and equal addresses on this one, stay refused.
"""
import re
import sys
from pathlib import Path

PAIR_TIMERS = {2, 3, 4, 5, 12}
ADC_BANK_SIZE = 20  # ADCIN_BANK_SIZE in src/stm32/stm32h7_adc.c


def read_config(path):
    config = {}
    for line in Path(path).read_text().splitlines():
        match = re.match(r'(CONFIG_\w+)=(.*)', line)
        if match:
            config[match.group(1)] = match.group(2).strip('"')
            continue
        # A .config's own way of saying a bool is explicitly off, distinct
        # from a symbol this file predates entirely (config.get's default).
        match = re.match(r'# (CONFIG_\w+) is not set', line)
        if match:
            config[match.group(1)] = 'n'
    return config


def enabled(config, symbol):
    return config.get(symbol) in ('y', '1')  # .config or autoconf.h form


def roles(config):
    """(symbol suffix, name, per lane) in mcu/pin_config.c's group order."""
    on = lambda name: enabled(config, 'CONFIG_KLIPPER_MMU_' + name)
    # With a selector (a stepper axis and/or a servo), the gear behind it is
    # one driver bound to every lane, so its STEP/DIR/ENABLE/UART lists hold
    # exactly one shared pin instead of one per lane (selector.h).
    gear_per_lane = not (on('SELECTOR_STEPPER') or on('SERVO'))
    groups = [('STEP', 'STEP', gear_per_lane), ('DIR', 'DIR', gear_per_lane),
              ('ENABLE', 'ENABLE', gear_per_lane)]
    # A gear with no TMC UART chip (drivers set in hardware) never claims
    # this pin. KLIPPER_MMU_GEAR_TMC_UART (mcu/Kconfig.control) is a
    # promptless, device-derived symbol -- kconfiglib's own .config writer
    # never persists a promptless symbol at all (not even "# ... is not
    # set"), so reading it back from a saved .config's text cannot work;
    # QIDI Box is the one device whose gear has none (KLIPPER_MMU_
    # GEAR_TMC_UART's own default), so checking the device name directly
    # reproduces the identical fact.
    if not on('DEVICE_QIDI_BOX'):
        groups.append(('UART', 'TMC UART', gear_per_lane))
    if on('BAY_SENSORS'):
        groups.append(('BAY', 'BAY switch', True))
    if on('LOAD_SENSORS') or on('DEVICE_EMU'):
        groups.append(('LOAD', 'LOAD switch', True))
    if not on('VIRTUAL_HUB'):
        groups.append(('HUB', 'HUB switch', False))
    if on('TOOLHEAD_SENSOR'):
        groups.append(('TOOLHEAD', 'toolhead switch', False))
    groups.append(('BUFFER_A', 'DiffA', False))
    # A tension-only buffer (KLIPPER_MMU_BUFFER_SINGLE_SWITCH, e.g. QIDI Box)
    # has no second, compression switch to claim a pin for.
    if not on('BUFFER_SINGLE_SWITCH'):
        groups.append(('BUFFER_B', 'DiffB', False))
    if on('POWERED_REWIND'):
        groups += [('REWIND_FORWARD', 'respooler forward', True),
                   ('REWIND_REVERSE', 'respooler reverse', True),
                   ('REWIND_ENABLE', 'respooler enable', True)]
    if on('ENCODER'):
        groups.append(('ENCODER_A', 'encoder A', False))
        # Happy Hare's own ERCF/Tradrack encoder is one wire (installer/
        # Kconfig.encoder:128); pin B only exists with a genuine second wire.
        if on('ENCODER_DUAL_PIN'):
            groups.append(('ENCODER_B', 'encoder B', False))
    if on('MEASURED_CURRENT'):
        groups.append(('CURRENT', 'current sense', True))
    if on('SELECTOR_STEPPER'):
        # SELECTOR_UART is the one optional wire (a plain step/dir driver
        # needs none) and is left out of this strict one-pin check.
        groups += [('SELECTOR_STEP', 'selector STEP', False),
                   ('SELECTOR_DIR', 'selector DIR', False),
                   ('SELECTOR_ENABLE', 'selector ENABLE', False)]
    if on('SELECTOR_HAS_ENDSTOP'):
        groups.append(('SELECTOR_ENDSTOP', 'selector endstop', False))
    if on('SELECTOR_KIND_INDEXED'):
        # INDEXED has no gate position table -- one index switch per lane
        # instead (klipper_mmu/selector.h), like BAY above.
        groups.append(('SELECTOR_GATE', 'selector gate index switch', True))
    if on('SERVO'):
        groups.append(('SERVO', 'servo', False))
    return groups


def gear_uart_address(config):
    """The gear's own (lane 0) TMC UART address digit, or None if unset/malformed.

    A selector's gear is one shared driver (KM_GEAR_DRIVERS == 1), so lane 0's
    entry of CONFIG_KLIPPER_MMU_TMC_ADDRESSES is the only one that matters
    when deciding whether sharing its UART pin with the selector is safe.
    """
    first = config.get('CONFIG_KLIPPER_MMU_TMC_ADDRESSES', '').split(',')[0]
    return first if first in ('0', '1', '2', '3') else None


def h7_section(text, start):
    """The CONFIG_MACH_STM32H7 branch of a table, with its H723 sub-branches."""
    body = text.split(start, 1)[1]
    body = re.split(r'#elif|#endif\s*\n\s*}', body, 1)[0]
    body = re.sub(r'#if CONFIG_MACH_STM32H723(.*?)#else.*?#endif', r'\1', body, flags=re.S)
    return re.sub(r'//[^\n]*', '', body)


def pwm_routes(src):
    """Pin name -> (timer, channel), first entry per pin as gpio_pwm_find."""
    text = (Path(src) / 'stm32/hard_pwm.c').read_text()
    routes = {}
    for timer, port, pin, channel in re.findall(
            r"\{TIM(\d+),\s*GPIO\('([A-K])',\s*(\d+)\),\s*(\d+)",
            h7_section(text, '#elif CONFIG_MACH_STM32H7')):
        routes.setdefault('P%s%s' % (port, pin), (int(timer), int(channel)))
    return routes


def adc_routes(src):
    """Pin name -> (unit, channel), first entry per pin as adc_route_from_pin."""
    text = (Path(src) / 'stm32/stm32h7_adc.c').read_text()
    table = text.split('adc_pins[] = {', 1)[1]
    entries = re.findall(r"GPIO\('([A-K])',\s*(\d+)\)|ADC_\w+_PIN",
                         h7_section(table, '#if CONFIG_MACH_STM32H7'))
    routes = {}
    for index, (port, pin) in enumerate(entries):
        if port:
            routes.setdefault('P%s%s' % (port, pin),
                              (index // ADC_BANK_SIZE + 1, index % ADC_BANK_SIZE))
    return routes


def check(config, src):
    """Return a list of problems; empty when the map is acceptable."""
    if not enabled(config, 'CONFIG_KLIPPER_MMU_PIN_CONFIG'):
        return []
    lanes = int(config.get('CONFIG_KLIPPER_MMU_LANE_COUNT', '0'))
    problems, pins, owner = [], {}, {}
    for suffix, name, per_lane in roles(config):
        symbol = 'CONFIG_KLIPPER_MMU_PINS_' + suffix
        values = config.get(symbol, '').split(',')
        want = lanes if per_lane else 1
        if len(values) != want or not all(re.fullmatch(r'P[A-K]\d{1,2}', v) for v in values):
            problems.append('%s needs %d pin%s like PA1, one %s, comma separated; got "%s"'
                            % (symbol, want, 's' if want > 1 else '',
                               'per lane' if per_lane else 'shared', config.get(symbol, '')))
            continue
        pins[suffix] = values
        for lane, pin in enumerate(values):
            label = '%s lane %d' % (name, lane) if per_lane else name
            if pin in owner:
                problems.append('%s is used twice: %s and %s' % (pin, owner[pin], label))
            owner.setdefault(pin, label)
    # SELECTOR_UART on requires the selector's own TMC UART pin; off, the
    # plain step/dir selector has none (docs/selectors.md).
    if enabled(config, 'CONFIG_KLIPPER_MMU_SELECTOR_UART'):
        selector_uart = config.get('CONFIG_KLIPPER_MMU_PINS_SELECTOR_UART', '')
        if not re.fullmatch(r'P[A-K]\d{1,2}', selector_uart):
            problems.append('CONFIG_KLIPPER_MMU_PINS_SELECTOR_UART needs one pin like '
                            'PA1 (or turn KLIPPER_MMU_SELECTOR_UART off); got "%s"'
                            % selector_uart)
        elif selector_uart in owner and owner[selector_uart] == 'TMC UART':
            # Happy Hare's classic ERCF 1.1 EASY-BRD ties the gear's and the
            # selector's TMC2209 to one shared single-wire UART pin at
            # distinct addresses (installer/boards/Kconfig.easy_brd_rp2040:
            # 24-25; docs/selectors.md) -- the one legitimate reuse of the
            # gear's own UART pin, and only when the two chips' addresses
            # actually differ (mcu/pin_config.c, mcu/tmc_uart_gpio.c).
            gear_address = gear_uart_address(config)
            selector_address = config.get('CONFIG_KLIPPER_MMU_SELECTOR_UART_ADDRESS', '0')
            if gear_address is None:
                problems.append('%s shares the gear TMC UART pin but '
                                'CONFIG_KLIPPER_MMU_TMC_ADDRESSES is not set'
                                % selector_uart)
            elif gear_address == selector_address:
                problems.append(
                    '%s: gear and selector TMC2209 share this UART pin but both use '
                    'address %s; give the selector a different '
                    'CONFIG_KLIPPER_MMU_SELECTOR_UART_ADDRESS (0..3)'
                    % (selector_uart, selector_address))
        elif selector_uart in owner:
            problems.append('%s is used twice: %s and selector TMC UART'
                            % (selector_uart, owner[selector_uart]))
        else:
            owner[selector_uart] = 'selector TMC UART'
    if not enabled(config, 'CONFIG_MACH_STM32H723'):
        return problems
    if 'REWIND_FORWARD' in pins and 'REWIND_REVERSE' in pins:
        routes, timer_lane = pwm_routes(src), {}
        for lane, pair in enumerate(zip(pins['REWIND_FORWARD'], pins['REWIND_REVERSE'])):
            found = [routes.get(pin) for pin in pair]
            if (None in found or any(t not in PAIR_TIMERS or (t == 12 and c > 2)
                                     for t, c in found) or found[0] == found[1]):
                problems.append(
                    'respooler lane %d: forward %s and reverse %s must be two channels '
                    'of TIM2, TIM3, TIM4, TIM5 or TIM12 (got %s)'
                    % (lane, pair[0], pair[1], ', '.join(
                        'TIM%d ch%d' % r if r else 'no PWM' for r in found)))
                continue
            for timer, _ in found:
                other = timer_lane.setdefault(timer, lane)
                if other != lane:
                    problems.append('respooler lanes %d and %d both use TIM%d; each lane '
                                    'needs its own timer' % (other, lane, timer))
    if 'BUFFER_A' in pins and enabled(config, 'CONFIG_KLIPPER_MMU_DIFF_BUFFER'):
        routes = adc_routes(src)
        a, b = pins['BUFFER_A'][0], pins['BUFFER_B'][0]
        ra, rb = routes.get(a), routes.get(b)
        if not ra or not rb or ra[0] != rb[0] or ra[1] == rb[1]:
            problems.append('DiffA %s and DiffB %s must be two different channels of one '
                            'ADC (got %s)' % (a, b, ', '.join(
                                'ADC%d ch%d' % r if r else 'no ADC' for r in (ra, rb))))
    return problems


def main(argv):
    if len(argv) != 3 or argv[1] in ('-h', '--help'):
        print(__doc__)
        return 0 if len(argv) == 2 else 2
    problems = check(read_config(argv[1]), argv[2])
    for problem in problems:
        print('klipper-mmu pin map: ' + problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
