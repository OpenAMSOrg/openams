# SPDX-License-Identifier: GPL-3.0-or-later
"""Saved upstream Kconfig -> canonical C pin claims. No hardware access."""
import importlib.util
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
# Capture arrays are sized by the firmware's lane bound, not the configured lanes.
MAX_LANES = int(re.search(r'#define KM_MAX_LANES\s+(\d+)u',
                          (ROOT / 'include/klipper_mmu/types.h').read_text()).group(1))


class PinConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='km-pins-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.work = Path(cls.temp.name)
        upstream = Path(os.environ['KLIPPER_SOURCE'])
        shutil.copytree(upstream / 'src', cls.work / 'src', symlinks=True,
                        ignore=shutil.ignore_patterns('klipper_mmu'))
        cls.product = cls.work / 'src/klipper_mmu'
        for name in ('mcu', 'include', 'src', 'tests/pin_config'):
            shutil.copytree(ROOT / name, cls.product / name,
                            ignore=shutil.ignore_patterns('__pycache__'))
        # Compile the real shared PWM lookup against inert register identities.
        # This fixture aborts any attempt to initialize peripheral hardware.
        cls.pwm = cls.work / 'pwm'
        shutil.copytree(ROOT / 'tests/klipper_pwm_pair/shim', cls.pwm)
        (cls.pwm / 'autoconf.h').unlink()
        (cls.pwm / 'stm32').mkdir()
        shutil.copy2(cls.pwm / 'internal.h', cls.pwm / 'stm32/internal.h')
        for name in ('hard_pwm.c', 'pwm.h'):
            source = upstream / 'src/stm32' / name
            if source.exists():
                shutil.copy2(source, cls.pwm / name)
                if name == 'pwm.h':
                    shutil.copy2(source, cls.pwm / 'stm32/pwm.h')
        # Actual ADC consumer header, with its same-directory GPIO declaration.
        # The PWM shim's gpio.h stays the paired mock.
        cls.adc_hdr = cls.work / 'adc_hdr'
        (cls.adc_hdr / 'stm32').mkdir(parents=True)
        shutil.copy2(upstream / 'src/stm32/adc_local.h', cls.adc_hdr / 'stm32/adc_local.h')
        shutil.copy2(upstream / 'src/stm32/gpio.h', cls.adc_hdr / 'stm32/gpio.h')
        cls.query_enabled = False
        cls.adc_local = False
        cls.old_cwd = Path.cwd()
        cls.old_srctree = os.environ.get('srctree')
        os.environ['srctree'] = str(cls.work)
        os.chdir(cls.work)
        cls.addClassCleanup(cls.restore_environment)
        # Accept either pristine or normally installed input without sourcing
        # the same menu twice. Reject malformed/unmanaged hooks as the installer does.
        spec = importlib.util.spec_from_file_location('pin_installer', ROOT/'tools/install.py')
        installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(installer)
        path = cls.work / 'src/Kconfig'
        original, _, block = installer.read_hook(path, 'Kconfig')
        path.write_bytes(original.replace(block, b'') if block else original)
        with (cls.work / 'src/Kconfig').open('a') as out:
            out.write('\nsource "src/klipper_mmu/mcu/Kconfig"\n')
        spec = importlib.util.spec_from_file_location('pin_kconfiglib', upstream / 'lib/kconfiglib/kconfiglib.py')
        cls.kconfig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.kconfig)
        # These cases deliberately exercise the AFC Lite preset; manual is now the default.
        cls.base = (upstream / 'test/configs/stm32h723.config').read_text() + '\nCONFIG_KLIPPER_MMU_BOARD_AFC_LITE=y\n'
        cls.sim = (upstream / 'test/configs/hostsimulator.config').read_text()

    @classmethod
    def restore_environment(cls):
        os.chdir(cls.old_cwd)
        if cls.old_srctree is None:
            os.environ.pop('srctree', None)
        else:
            os.environ['srctree'] = cls.old_srctree

    def configure(self, values='', seed=None, enabled=True):
        config = self.work / '.config'
        config.write_text((self.base if seed is None else seed)
                         + '\nCONFIG_KLIPPER_MMU=y\n'
                         + ('CONFIG_KLIPPER_MMU_PIN_CONFIG=y\n' if enabled else '')
                         + values)
        k = self.kconfig.Kconfig('src/Kconfig', warn_to_stderr=False)
        k.load_config(str(config)); k.write_config(str(config))
        saved = config.read_text()
        k.load_config(str(config)); k.write_config(str(config))
        self.assertEqual(config.read_text(), saved)
        k.write_autoconf(str(self.work / 'autoconf.h'))
        self.query_enabled = 'CONFIG_WANT_HARD_PWM_QUERY=y\n' in saved
        self.adc_local = ('CONFIG_MACH_STM32H723=y\n' in saved
                          and 'CONFIG_WANT_ADC=y\n' in saved
                          and 'CONFIG_WANT_ADC_LOCAL=y\n' in saved)
        return k, saved

    @staticmethod
    def gpio(port, bit):
        return (ord(port) - ord('A')) * 16 + bit

    def adc_cmsis(self):
        build = self.work / 'adc_build'
        if (build / 'internal.h').exists():
            return build
        upstream = Path(os.environ['KLIPPER_SOURCE'])
        vendor = (upstream / 'lib/stm32h7/include/stm32h723xx.h').read_text()
        structs = []
        for name in ('ADC_TypeDef', 'ADC_Common_TypeDef'):
            match = re.search(r'typedef struct\s*\{[^}]+\}\s*' + name + r';', vendor)
            self.assertIsNotNone(match, name)
            structs.append(match.group())
        defines = '\n'.join(line for line in vendor.splitlines()
                            if line.startswith('#define ADC_'))
        build.mkdir(parents=True)
        (build / 'stm32').mkdir()
        (build / 'internal.h').write_text(
            '#ifndef PIN_ADC_INTERNAL_H\n#define PIN_ADC_INTERNAL_H\n'
            '#include <stdint.h>\n#include "autoconf.h"\n'
            '#define __IO volatile\n#define __IOM volatile\n'
            '#define __IM volatile\n#define __OM volatile\n'
            + '\n'.join(structs) + '\n' + defines + '\n'
            'extern ADC_TypeDef test_adcs[3];\n'
            'extern ADC_Common_TypeDef test_common[2];\n'
            '#define ADC1 (&test_adcs[0])\n#define ADC2 (&test_adcs[1])\n'
            '#define ADC3 (&test_adcs[2])\n'
            '#define ADC12_COMMON (&test_common[0])\n'
            '#define ADC3_COMMON (&test_common[1])\n'
            '#define GPIO(port, bit) (((port) - \'A\') * 16 + (bit))\n'
            '#define GPIO_INPUT 0\n#define GPIO_OUTPUT 1\n'
            '#define GPIO_OPEN_DRAIN 2\n#define GPIO_ANALOG 3\n'
            '#define MODIFY_REG(reg, clear, set) ((reg) = ((reg) & ~(clear)) | (set))\n'
            'int is_enabled_pclock(uint32_t base);\n'
            'void enable_pclock(uint32_t base);\n'
            'void gpio_peripheral(uint32_t pin, uint32_t function, int32_t pull);\n'
            '#endif\n')
        for dest in (build / 'adc_local.h', build / 'stm32/adc_local.h'):
            shutil.copy2(upstream / 'src/stm32/adc_local.h', dest)
        for dest in (build / 'gpio.h', build / 'stm32/gpio.h'):
            shutil.copy2(upstream / 'src/stm32/gpio.h', dest)
        return build

    def compile_adc_driver(self):
        upstream = Path(os.environ['KLIPPER_SOURCE'])
        build = self.adc_cmsis()
        source_path = upstream / 'src/stm32/stm32h7_adc.c'
        copied = build / 'stm32h7_adc.c'
        payload = source_path.read_bytes()
        copied.write_bytes(payload)
        self.assertEqual(copied.read_bytes(), payload)
        shim = ROOT / 'tests/pin_config/adc_shim'
        flags = ['gcc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                 '-Wno-pointer-to-int-cast', '-Wno-sign-compare',
                 '-fsanitize=address,undefined', '-fno-sanitize-recover=all', '-fno-pie',
                 '-I' + str(self.work), '-I' + str(build), '-I' + str(shim),
                 '-I' + str(self.pwm)]
        objects = []
        for source, obj in ((copied, self.work / 'adc.o'),
                            (shim / 'watch.c', self.work / 'adc_watch.o')):
            result = subprocess.run([*flags, '-c', str(source), '-o', str(obj)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            objects.append(str(obj))
        return objects

    def compile(self, success=True, feeder=False, tmc=False, switches=False, rewind=False, resources=False, adc=False, rewind_outputs=False):
        sources = ('mcu/feeder_config.c', 'src/step_dir_driver.c', 'tests/pin_config/feeder.c') if feeder else (
            'mcu/pin_config.c', 'src/resource_plan.c', 'tests/pin_config/capture.c')
        if tmc:
            sources = ('mcu/tmc_config.c', 'src/tmc2209.c', 'src/tmc_uart_config.c', 'tests/pin_config/tmc.c')
        if switches:
            sources = ('mcu/switch_config.c', 'mcu/pin_config.c', 'src/resource_plan.c',
                       'tests/pin_config/switch.c')
        if rewind:
            sources = ('mcu/rewind_config.c', 'src/pwm_bridge_driver.c', 'tests/pin_config/rewind.c')
        if rewind_outputs:
            sources = ('mcu/rewind_config.c', 'src/pwm_bridge_driver.c', 'tests/pin_config/rewind_outputs.c')
        extra = []
        if resources:
            sources = ('mcu/h723_resources.c', 'mcu/pin_config.c', 'src/resource_plan.c',
                       'tests/pin_config/resources.c')
            if self.query_enabled or self.adc_local:
                sources += ('tests/pin_config/resources_io.c',)
            if self.query_enabled:
                obj = self.work / 'pwm.o'
                driver = subprocess.run([
                    'gcc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                    '-Wno-pointer-to-int-cast', '-fsanitize=address,undefined',
                    '-fno-sanitize-recover=all', '-fno-pie',
                    '-I'+str(self.work), '-I'+str(self.pwm),
                    '-c', str(self.pwm/'hard_pwm.c'), '-o', str(obj)],
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(driver.returncode, 0, driver.stderr)
                extra.append(str(obj))
            if self.adc_local:
                extra.extend(self.compile_adc_driver())
        if adc:
            sources = ('mcu/adc_config.c', 'src/adc_pair.c', 'tests/pin_config/adc.c')
        command = ['gcc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-pedantic',
                   '-Wconversion', '-Wsign-conversion', '-fsanitize=address,undefined',
                   '-fno-sanitize-recover=all', '-fno-pie', '-no-pie', '-g',
                   '-I' + str(self.work), '-I' + str(self.product / 'tests/pin_config'),
                   '-I' + str(self.product / 'mcu'), '-I' + str(self.product / 'include'),
                   '-I'+str(self.pwm), '-I'+str(self.adc_hdr),
                   *(str(self.product / p) for p in sources), *extra, '-o', str(self.work / 'pins')]
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode == 0, success, result.stderr)
        return result

    def capture(self, *args):
        result = subprocess.run([str(self.work / 'pins'), *args], capture_output=True,
                                text=True, timeout=15, env={**os.environ, 'ASAN_OPTIONS':'detect_leaks=0:halt_on_error=1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def adc_routes(self):
        data = self.capture('adc-routes')
        self.assertEqual(data['registers'], 1)
        return {row[0]: (row[1], row[2]) for row in data['routes']}

    REMAP_DIFF = ('CONFIG_KLIPPER_MMU_PINS_HUB="PE12"\n'
                  'CONFIG_KLIPPER_MMU_PINS_BAY="PE13,PB0,PB1,PB2"\n')

    def test_reference_map_matches_literal_afc_pins(self):
        self.configure('CONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PF1"\n')
        self.compile(); p = self.capture()
        self.assertEqual(p['result'], 0)
        expected = {1:[52,44,66,63], 2:[51,43,67,62], 3:[54,49,64,39],
                    4:[53,48,65,38], 5:[37,16,17,18], 16:[72,73,74,75],
                    6:[36], 7:[81], 8:[76], 9:[77],
                    10:[1,7,31,61], 11:[0,6,30,60], 12:[2,5,29,59]}
        self.assertEqual(p['claims'], [[pin,role] for role,pins in expected.items() for pin in pins])
        for role in range(17):
            values = expected.get(role, [])
            self.assertEqual(p['pins'][role], values + [65535] * (MAX_LANES - len(values)))
        self.assertNotEqual(self.capture('null')['result'], 0)

    def test_shared_pin_view_matches_copy_api_and_keeps_stable_identity(self):
        self.configure()
        self.compile()
        self.assertEqual(self.capture('view'), self.capture())

    def test_shared_pin_view_failure_does_not_publish_partial_map(self):
        self.configure('CONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PB0"\n')
        self.compile()
        result = self.capture('view')
        self.assertEqual(result['result'], -5)
        self.assertEqual(result['claims'], [])

    def test_shared_pin_view_is_unavailable_when_pin_config_is_disabled(self):
        self.configure(enabled=False)
        self.compile()
        self.assertEqual(self.capture('view')['result'], -3)

    def test_emu_five_lane_manual_map_has_no_physical_hub_claim(self):
        pins = {
            'STEP': 'PA0,PA1,PA2,PA3,PA4',
            'DIR': 'PA5,PA6,PA7,PA8,PA9',
            'ENABLE': 'PA10,PA11,PA12,PA13,PA14',
            'UART': 'PB0,PB1,PB2,PB3,PB4',
            'BAY': 'PB5,PB6,PB7,PB8,PB9',
            'LOAD': 'PB10,PB11,PB12,PB13,PB14',
            'TOOLHEAD': 'PC0',
            'BUFFER_A': 'PC1',
            'BUFFER_B': 'PC2',
        }
        values = ('CONFIG_KLIPPER_MMU_DEVICE_EMU=y\n'
                  'CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y\n')
        values += ''.join(f'CONFIG_KLIPPER_MMU_PINS_{role}="{assigned}"\n'
                          for role, assigned in pins.items())
        k, _ = self.configure(values)
        self.assertEqual(k.syms['KLIPPER_MMU_LANE_COUNT'].str_value, '5')
        self.compile()
        captured = self.capture()
        self.assertEqual(captured['result'], 0)
        self.assertEqual(captured['pins'][6], [65535] * MAX_LANES)  # no HUB GPIO
        self.assertEqual(captured['pins'][16][:5], [26, 27, 28, 29, 30])
        self.assertEqual(len(captured['claims']), 33)
        self.compile(resources=True)
        self.assertEqual(self.capture()['result'], 0)

    REWIND = ('CONFIG_KLIPPER_MMU_REWIND_BRIDGE_CEILING=800\n'
              'CONFIG_KLIPPER_MMU_REWIND_DEADTIME_US=5000\n'
              'CONFIG_KLIPPER_MMU_REWIND_LATENESS_US=50\n')

    REWIND_OUTPUTS = ('CONFIG_KLIPPER_MMU_REWIND_CYCLE_US=50\n'
                      'CONFIG_KLIPPER_MMU_REWIND_ENABLE_HIGH="1,0,1,0"\n'
                      'CONFIG_KLIPPER_MMU_REWIND_FORWARD_HIGH="0,1,0,1"\n'
                      'CONFIG_KLIPPER_MMU_REWIND_REVERSE_HIGH="1,1,0,0"\n')

    def test_rewind_outputs_require_explicit_period_and_levels(self):
        unset = ('CONFIG_KLIPPER_MMU_DEVICE_CUSTOM=y\n'
                 'CONFIG_KLIPPER_MMU_POWERED_REWIND=y\n')
        self.configure(unset); self.compile(rewind_outputs=True)
        self.assertEqual(self.capture()['result'], -3)
        self.assertEqual(self.capture('null')['result'], -1)
        for omitted in ('CYCLE_US', 'ENABLE_HIGH', 'FORWARD_HIGH', 'REVERSE_HIGH'):
            values = ''.join(line + '\n' for line in self.REWIND_OUTPUTS.splitlines()
                             if 'REWIND_' + omitted not in line)
            self.configure(unset + values); self.compile(rewind_outputs=True)
            self.assertEqual(self.capture()['result'], -3, omitted)
        self.configure(); self.compile(rewind_outputs=True)
        self.assertEqual(self.capture()['result'], 0)

    def test_rewind_outputs_saved_polarity_and_device_overrides(self):
        expected = [[50,1,0,1], [50,0,1,1], [50,1,0,0], [50,0,1,0]] + [[0,0,0,0]]*(MAX_LANES - 4)
        for device in ('BOXTURTLE', 'NIGHTOWL', 'CUSTOM', 'CLAYMORE'):
            k, saved = self.configure(self.REWIND_OUTPUTS
                + f'CONFIG_KLIPPER_MMU_DEVICE_{device}=y\nCONFIG_KLIPPER_MMU_POWERED_REWIND=y\n')
            self.assertEqual(k.syms['KLIPPER_MMU_REWIND_CYCLE_US'].str_value, '50')
            self.assertIn('CONFIG_KLIPPER_MMU_REWIND_ENABLE_HIGH="1,0,1,0"\n', saved)
            self.compile(rewind_outputs=True)
            self.assertEqual(self.capture(), {'result':0, 'lanes':expected})

    def test_rewind_outputs_reject_incomplete_or_malformed_lane_lists(self):
        for role in ('ENABLE', 'FORWARD', 'REVERSE'):
            for value in ('1,0,1', '1,0,1,0,1', '1,0,1,', '1,0,1,2', '1,0,1, 0'):
                self.configure(self.REWIND_OUTPUTS
                    + f'CONFIG_KLIPPER_MMU_REWIND_{role}_HIGH="{value}"\n')
                self.compile(rewind_outputs=True)
                self.assertEqual(self.capture()['result'], -1, (role, value))

    def test_rewind_outputs_period_and_lane_boundaries(self):
        for period, lanes in ((1,1), (1000000,8)):
            values = ','.join(str(i%2) for i in range(lanes))
            config = f'CONFIG_KLIPPER_MMU_REWIND_CYCLE_US={period}\nCONFIG_KLIPPER_MMU_LANE_COUNT={lanes}\n'
            config += ''.join(f'CONFIG_KLIPPER_MMU_REWIND_{role}_HIGH="{values}"\n'
                              for role in ('ENABLE', 'FORWARD', 'REVERSE'))
            self.configure(config); self.compile(rewind_outputs=True)
            expected = [[period,i%2,i%2,i%2] for i in range(lanes)] + [[0,0,0,0]]*(MAX_LANES - lanes)
            self.assertEqual(self.capture(), {'result':0, 'lanes':expected})

    def test_rewind_outputs_passive_disabled_and_simulation_unavailable(self):
        for values, seed, enabled in [
                (self.REWIND_OUTPUTS, self.base, False),
                (self.REWIND_OUTPUTS, self.sim, True),
                (self.REWIND_OUTPUTS + 'CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL=y\n', self.base, True)]:
            k, _ = self.configure(values, seed=seed, enabled=enabled)
            self.assertEqual(k.syms['KLIPPER_MMU_REWIND_CYCLE_US'].visibility, 0)
            self.compile(rewind_outputs=True)
            self.assertEqual(self.capture()['result'], -3)

    def test_rewind_unset_limits_require_explicit_configuration(self):
        unset = ('CONFIG_KLIPPER_MMU_DEVICE_CUSTOM=y\n'
                 'CONFIG_KLIPPER_MMU_POWERED_REWIND=y\n')
        self.configure(unset); self.compile(rewind=True)
        self.assertEqual(self.capture()['result'], -3)
        self.assertEqual(self.capture('null')['result'], -1)
        for omitted in ('BRIDGE_CEILING', 'DEADTIME_US', 'LATENESS_US'):
            values = ''.join(line + '\n' for line in self.REWIND.splitlines()
                             if 'REWIND_' + omitted not in line)
            self.configure(unset + values); self.compile(rewind=True)
            self.assertEqual(self.capture()['result'], -3, omitted)
        self.configure(); self.compile(rewind=True)
        self.assertEqual(self.capture(), {'result': 0, 'limits': [600, 50, 50]})

    def test_rewind_saved_limits_and_profile_overrides(self):
        for device in ('BOXTURTLE', 'NIGHTOWL', 'CUSTOM'):
            for lanes in (1, 8):
                k, saved = self.configure(self.REWIND + f'CONFIG_KLIPPER_MMU_DEVICE_{device}=y\n'
                                         f'CONFIG_KLIPPER_MMU_LANE_COUNT={lanes}\n'
                                         'CONFIG_KLIPPER_MMU_POWERED_REWIND=y\n')
                self.assertEqual(k.syms['KLIPPER_MMU_DEVICE_' + device].str_value, 'y')
                self.assertIn('CONFIG_KLIPPER_MMU_REWIND_DEADTIME_US=5000\n', saved)
                self.compile(rewind=True)
                self.assertEqual(self.capture(), {'result':0, 'limits':[800,5000,50]})

    def test_rewind_passive_disabled_and_simulation_unavailable(self):
        for values, seed, enabled in [
                (self.REWIND, self.base, False),
                (self.REWIND, self.sim, True),
                (self.REWIND + 'CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL=y\n', self.base, True),
                (self.REWIND + '# CONFIG_KLIPPER_MMU_POWERED_REWIND is not set\n', self.base, True)]:
            k, _ = self.configure(values, seed=seed, enabled=enabled)
            self.assertEqual(k.syms['KLIPPER_MMU_REWIND_BRIDGE_CEILING'].visibility, 0)
            self.compile(rewind=True); self.assertEqual(self.capture()['result'], -3)

    def test_rewind_profile_cannot_exceed_bridge_ceiling(self):
        for duty, result in [(0,0), (799,0), (800,0), (801,-5), (1000,-5)]:
            self.configure(self.REWIND + f'CONFIG_KLIPPER_MMU_REWIND_DUTY={duty}\n')
            self.compile(rewind=True)
            self.assertEqual(self.capture()['result'], result)

    def test_rewind_timing_and_duty_boundaries(self):
        for duty, deadtime, lateness in [(1,1,1), (1000,2147483647,1000)]:
            self.configure(f'CONFIG_KLIPPER_MMU_REWIND_BRIDGE_CEILING={duty}\n'
                           f'CONFIG_KLIPPER_MMU_REWIND_DUTY={duty}\n'
                           f'CONFIG_KLIPPER_MMU_REWIND_DEADTIME_US={deadtime}\n'
                           f'CONFIG_KLIPPER_MMU_REWIND_LATENESS_US={lateness}\n')
            self.compile(rewind=True)
            self.assertEqual(self.capture(), {'result':0, 'limits':[duty,deadtime,lateness]})

    ADC = ('CONFIG_KLIPPER_MMU_DIFF_BUFFER=y\n'
           'CONFIG_KLIPPER_MMU_ADC_CONVERSION_TIMEOUT_US=1000\n'
           'CONFIG_KLIPPER_MMU_ADC_PAIR_SPAN_US=2500\n'
           'CONFIG_KLIPPER_MMU_ADC_SAMPLE_AGE_US=10000\n')

    def test_adc_requires_all_explicit_limits(self):
        # The AFC Lite preset now defaults these three limits (so a plain
        # menuconfig BoxTurtle build is never left unavailable); a board
        # without that preset must still configure every limit explicitly.
        custom = ('# CONFIG_KLIPPER_MMU_BOARD_AFC_LITE is not set\n'
                 'CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y\n')
        self.configure(custom + 'CONFIG_KLIPPER_MMU_DIFF_BUFFER=y\n'); self.compile(adc=True)
        self.assertEqual(self.capture()['result'], -3)
        self.assertEqual(self.capture('null')['result'], -1)
        for omitted in ('CONVERSION_TIMEOUT', 'PAIR_SPAN', 'SAMPLE_AGE'):
            values = ''.join(line + '\n' for line in self.ADC.splitlines()
                             if 'ADC_' + omitted not in line)
            self.configure(custom + values); self.compile(adc=True)
            self.assertEqual(self.capture()['result'], -3, omitted)

    def test_adc_afc_lite_defaults_are_never_unset(self):
        k, saved = self.configure('CONFIG_KLIPPER_MMU_DIFF_BUFFER=y\n')
        self.assertIn('CONFIG_KLIPPER_MMU_ADC_CONVERSION_TIMEOUT_US=1000\n', saved)
        self.assertIn('CONFIG_KLIPPER_MMU_ADC_PAIR_SPAN_US=3000\n', saved)
        self.assertIn('CONFIG_KLIPPER_MMU_ADC_SAMPLE_AGE_US=20000\n', saved)
        self.compile(adc=True)
        self.assertEqual(self.capture()['result'], 0)

    def test_adc_saved_limits_across_profiles(self):
        for device in ('BOXTURTLE', 'NIGHTOWL', 'CUSTOM'):
            k, saved = self.configure(self.ADC + f'CONFIG_KLIPPER_MMU_DEVICE_{device}=y\n')
            self.assertEqual(k.syms['KLIPPER_MMU_DEVICE_' + device].str_value, 'y')
            self.assertIn('CONFIG_KLIPPER_MMU_ADC_PAIR_SPAN_US=2500\n', saved)
            self.compile(adc=True)
            self.assertEqual(self.capture(), {'result':0, 'limits':[4095,1000,2500,10000]})

    def test_adc_disabled_switched_and_simulation_unavailable(self):
        for values, seed, enabled in [
                (self.ADC, self.base, False), (self.ADC, self.sim, True),
                (self.ADC + '# CONFIG_KLIPPER_MMU_DIFF_BUFFER is not set\n', self.base, True)]:
            k, _ = self.configure(values, seed=seed, enabled=enabled)
            self.assertEqual(k.syms['KLIPPER_MMU_ADC_PAIR_SPAN_US'].visibility, 0)
            self.compile(adc=True); self.assertEqual(self.capture()['result'], -3)

    def test_adc_span_and_profile_age_consistency(self):
        for age, result in [(2499,-1),(2500,-5),(3500,-5),(3501,0),
                            (199999,0),(200000,0),(200001,-5)]:
            self.configure(self.ADC + f'CONFIG_KLIPPER_MMU_ADC_SAMPLE_AGE_US={age}\n')
            self.compile(adc=True)
            self.assertEqual(self.capture()['result'], result)
        self.configure(self.ADC + 'CONFIG_KLIPPER_MMU_SENSOR_AGE=1\n')
        self.compile(adc=True); self.assertEqual(self.capture()['result'], -5)

    def test_adc_timing_boundaries_and_total_pair_deadline(self):
        for conversion, span, age, profile_age in [
                (1,1,1002,2), (2147483647,59998999,60000000,60000)]:
            self.configure('CONFIG_KLIPPER_MMU_DIFF_BUFFER=y\n'
                           f'CONFIG_KLIPPER_MMU_ADC_CONVERSION_TIMEOUT_US={conversion}\n'
                           f'CONFIG_KLIPPER_MMU_ADC_PAIR_SPAN_US={span}\n'
                           f'CONFIG_KLIPPER_MMU_ADC_SAMPLE_AGE_US={age}\n'
                           f'CONFIG_KLIPPER_MMU_SENSOR_AGE={profile_age}\n')
            self.compile(adc=True)
            self.assertEqual(self.capture(), {'result':0, 'limits':[4095,conversion,span,age]})

    NO_LOAD_SENSORS = 'CONFIG_KLIPPER_MMU_LOAD_SENSORS=n\n'
    RESOURCE_BASE = (NO_LOAD_SENSORS + 'CONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PF1"\n'
                     'CONFIG_WANT_HARD_PWM_QUERY=y\n'
                     'CONFIG_WANT_HARD_PWM_LOCAL_PAIR=y\n')

    def test_resources_reference_pwm_timers_and_external_conflicts(self):
        self.configure(self.RESOURCE_BASE); self.compile(resources=True)
        p = self.capture(); self.assertEqual(p['result'], 0)
        self.assertEqual(len(p['claims']), 40)
        self.assertEqual(p['claims'][-4:], [[2,2,0,32],[2,3,0,32],[2,12,0,32],[2,4,0,32]])
        self.assertEqual(p['rewind'], [[2,2],[2,1],[3,2],[3,1],
                                     [12,2],[12,1],[4,2],[4,1]] + [[0,0]]*(2 * MAX_LANES - 8))
        self.assertEqual(self.capture('timer')['external'], -5)
        self.assertEqual(self.capture('free')['external'], 0)
        self.assertEqual(self.capture('adc')['external'], 0)
        self.assertEqual(self.capture('null')['result'], -1)

    def test_resources_query_is_additive_and_required_only_for_powered_rewind(self):
        for missing in ('CONFIG_WANT_HARD_PWM_QUERY=n\n',
                        'CONFIG_WANT_HARD_PWM_LOCAL_PAIR=n\n'):
            values = self.RESOURCE_BASE + missing
            self.configure(values + 'CONFIG_KLIPPER_MMU_DEVICE_CUSTOM=y\n'
                           'CONFIG_KLIPPER_MMU_POWERED_REWIND=y\n')
            self.compile(resources=True)
            p = self.capture()
            self.assertEqual(p['result'], -3)
            self.assertEqual(p['claims'], [])
            self.configure(values + 'CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL=y\n')
            self.compile(resources=True)
            self.assertEqual(self.capture()['result'], 0)
            self.assertEqual(self.capture()['rewind'], [[0,0]]*(2 * MAX_LANES))

    def test_resources_routes_match_upstream_first_choice(self):
        self.configure('CONFIG_WANT_HARD_PWM_QUERY=y\n', enabled=False); self.compile(resources=True)
        source = (Path(os.environ['KLIPPER_SOURCE']) / 'src/stm32/hard_pwm.c').read_text()
        h7 = source.split('#elif CONFIG_MACH_STM32H7')[1].split('#endif')[0]
        rows = re.findall(r"\{TIM(\d+), GPIO\('([A-H])',\s*(\d+)\),\s*(\d+), GPIO_FUNCTION\((\d+)\)\}", h7)
        expected = {}
        for timer, port, bit, channel, af in rows:
            pin = (ord(port)-ord('A'))*16 + int(bit)
            expected.setdefault(pin, [int(timer),int(channel)])
        self.assertEqual(len(expected), 49)
        self.assertEqual(self.capture('routes')['routes'], [[p,*route] for p,route in sorted(expected.items())])

    def test_resources_reject_pwm_channel_aliases_and_unsupported_pins(self):
        for reverse in ('PB3,PA6,PB14,PD12', 'PF0,PA6,PB14,PD12'):
            self.configure(self.RESOURCE_BASE + f'CONFIG_KLIPPER_MMU_PINS_REWIND_REVERSE="{reverse}"\n')
            self.compile(); self.assertEqual(self.capture()['result'], 0)  # distinct GPIOs
            self.compile(resources=True); p = self.capture()
            self.assertEqual((p['result'], p['reason'], p['first'], p['second']), (-1, 2, 24, 28))
            self.assertEqual(p['claims'], [])

    def test_resources_one_rewind_lane_per_timer(self):
        # PB10 is TIM2 CH3, distinct from lane 0's TIM2 CH1/CH2. GPIO-only
        # claims are valid; timer-wide updates make the two lanes conflict.
        values = self.RESOURCE_BASE + 'CONFIG_KLIPPER_MMU_PINS_REWIND_FORWARD="PA1,PB10,PB15,PD13"\n'
        self.configure(values); self.compile(); self.assertEqual(self.capture()['result'], 0)
        self.compile(resources=True); p = self.capture()
        self.assertEqual((p['result'], p['reason'], p['first'], p['second']), (-5, 3, 24, 25))
        self.assertEqual(p['claims'], [])

        # Same lane, same TIM2 on distinct channels remains valid, including
        # another GPIO alias of TIM2 CH1. No cross-lane timer was added.
        values = self.RESOURCE_BASE + 'CONFIG_KLIPPER_MMU_PINS_REWIND_REVERSE="PA15,PA6,PB14,PD12"\n'
        self.configure(values); self.compile(resources=True); p = self.capture()
        self.assertEqual(p['result'], 0)
        self.assertEqual(p['rewind'][:2], [[2,2], [2,1]])
        self.assertEqual(p['claims'][-4:], [[2,2,0,32],[2,3,0,32],[2,12,0,32],[2,4,0,32]])

        # Distinct supported timers in one lane are valid. PH10 is TIM5 CH1.
        values = self.RESOURCE_BASE + 'CONFIG_KLIPPER_MMU_PINS_REWIND_REVERSE="PH10,PA6,PB14,PD12"\n'
        self.configure(values); self.compile(resources=True); p = self.capture()
        self.assertEqual(p['result'], 0)
        self.assertEqual(p['rewind'][:2], [[2,2], [5,1]])
        self.assertEqual(p['claims'][-5:], [[2,2,0,32],[2,5,0,32],[2,3,0,32],[2,12,0,32],[2,4,0,32]])
        self.assertEqual(len(p['claims']), 41)

        # PF6 routes to TIM16, which the pair validator does not accept.
        values = self.RESOURCE_BASE + 'CONFIG_KLIPPER_MMU_PINS_REWIND_REVERSE="PF6,PA6,PB14,PD12"\n'
        self.configure(values); self.compile(resources=True); p = self.capture()
        self.assertEqual((p['result'], p['reason'], p['first'], p['second']), (-8, 2, 24, 28))
        self.assertEqual(p['claims'], [])

    def test_resources_diff_pair_claims_whole_adc_unit(self):
        values = (self.RESOURCE_BASE + 'CONFIG_WANT_ADC_LOCAL=y\n'
                  'CONFIG_KLIPPER_MMU_DIFF_BUFFER=y\n'
                  'CONFIG_KLIPPER_MMU_PINS_HUB="PC4"\n'
                  'CONFIG_KLIPPER_MMU_PINS_BAY="PC5,PB0,PB1,PB2"\n')
        self.configure(values); self.compile(resources=True)
        collided = self.capture()
        self.assertEqual((collided['result'], collided['reason'],
                          collided['first'], collided['second']), (-5, 3, 16, 23))
        self.assertEqual(collided['claims'], [])
        values = (self.RESOURCE_BASE + 'CONFIG_WANT_ADC_LOCAL=y\n'
                  'CONFIG_KLIPPER_MMU_DIFF_BUFFER=y\n' + self.REMAP_DIFF)
        self.configure(values); self.compile(resources=True); p = self.capture()
        routes = self.adc_routes()
        unit, ch_a = routes[self.gpio('C', 4)]
        other, ch_b = routes[self.gpio('C', 5)]
        self.assertEqual((unit, other), (1, 1))
        self.assertEqual(p['result'], 0)
        self.assertEqual(p['claims'][-3:], [[5,unit,0,33],[6,unit,ch_a,33],[6,unit,ch_b,33]])
        self.assertEqual(len(p['claims']), 43)
        self.assertEqual(self.capture('adc')['external'], -5)

    def test_resources_diff_local_off_is_unavailable(self):
        values = (self.RESOURCE_BASE + 'CONFIG_WANT_ADC_LOCAL=n\n'
                  'CONFIG_KLIPPER_MMU_DIFF_BUFFER=y\n' + self.REMAP_DIFF)
        _, saved = self.configure(values)
        self.assertIn('# CONFIG_WANT_ADC_LOCAL is not set\n', saved)
        self.compile(resources=True)
        p = self.capture()
        self.assertEqual(p['result'], -3)
        self.assertEqual(p['claims'], [])

    def test_resources_adc_pairs_use_installed_routes(self):
        armed = (self.RESOURCE_BASE + 'CONFIG_WANT_ADC_LOCAL=y\n'
                 'CONFIG_KLIPPER_MMU_DIFF_BUFFER=y\n' + self.REMAP_DIFF)
        self.configure(armed); self.compile(resources=True)
        routes = self.adc_routes()
        expected_unit = {
            ('C', 0): 1, ('C', 1): 1, ('C', 4): 1, ('C', 5): 1,
            ('F', 13): 2, ('F', 14): 2, ('F', 3): 3, ('F', 4): 3,
        }
        for (port, bit), unit in expected_unit.items():
            self.assertEqual(routes[self.gpio(port, bit)][0], unit, (port, bit))
        self.assertNotIn(self.gpio('E', 9), routes)
        for pair in ((('C', 0), ('C', 1)), (('C', 4), ('C', 5)),
                     (('F', 13), ('F', 14)), (('F', 3), ('F', 4))):
            (ap, ab), (bp, bb) = pair
            pin_a, pin_b = self.gpio(ap, ab), self.gpio(bp, bb)
            unit_a, ch_a = routes[pin_a]
            unit_b, ch_b = routes[pin_b]
            self.assertEqual(unit_a, unit_b)
            self.assertNotEqual(ch_a, ch_b)
            values = (armed + f'CONFIG_KLIPPER_MMU_PINS_BUFFER_A="P{ap}{ab}"\n'
                      f'CONFIG_KLIPPER_MMU_PINS_BUFFER_B="P{bp}{bb}"\n')
            self.configure(values); self.compile(resources=True)
            p = self.capture()
            self.assertEqual(p['result'], 0, pair)
            self.assertEqual(p['claims'][-3:],
                             [[5, unit_a, 0, 33], [6, unit_a, ch_a, 33], [6, unit_b, ch_b, 33]])
            self.assertEqual(self.capture('adc')['external'], -5)
        cross = (armed + 'CONFIG_KLIPPER_MMU_PINS_BUFFER_A="PC4"\n'
                 'CONFIG_KLIPPER_MMU_PINS_BUFFER_B="PF13"\n')
        self.configure(cross); self.compile(resources=True)
        p = self.capture()
        self.assertEqual((p['result'], p['reason'], p['first'], p['second']), (-1, 2, 22, 23))
        self.assertEqual(p['claims'], [])
        missing = (armed + 'CONFIG_KLIPPER_MMU_PINS_BUFFER_A="PE9"\n'
                   'CONFIG_KLIPPER_MMU_PINS_BUFFER_B="PE10"\n')
        self.configure(missing); self.compile(resources=True)
        p = self.capture()
        self.assertEqual((p['result'], p['reason'], p['first'], p['second']), (-1, 2, 22, 23))
        self.assertEqual(p['claims'], [])

    def test_resources_adc_local_compiles_without_pwm_query(self):
        values = ('CONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PF1"\n'
                  '# CONFIG_WANT_HARD_PWM_QUERY is not set\n'
                  '# CONFIG_WANT_HARD_PWM_LOCAL_PAIR is not set\n'
                  'CONFIG_WANT_ADC_LOCAL=y\nCONFIG_KLIPPER_MMU_DIFF_BUFFER=y\n'
                  'CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL=y\n' + self.REMAP_DIFF +
                  'CONFIG_KLIPPER_MMU_PINS_BUFFER_A="PC0"\n'
                  'CONFIG_KLIPPER_MMU_PINS_BUFFER_B="PC1"\n')
        self.configure(values)
        self.assertEqual(self.query_enabled, False)
        self.assertEqual(self.adc_local, True)
        self.compile(resources=True)
        routes = self.adc_routes()
        unit, ch_a = routes[self.gpio('C', 0)]
        other, ch_b = routes[self.gpio('C', 1)]
        self.assertEqual((unit, other), (1, 1))
        p = self.capture()
        self.assertEqual(p['result'], 0)
        self.assertEqual(p['claims'][-3:], [[5, unit, 0, 33], [6, unit, ch_a, 33], [6, unit, ch_b, 33]])
        self.assertEqual(p['rewind'], [[0, 0]] * (2 * MAX_LANES))
        self.assertTrue(all(c[0] == 1 for c in p['claims'][:-3]))

    def test_resources_diff_passive_and_non_h723(self):
        values = (self.RESOURCE_BASE + 'CONFIG_WANT_ADC_LOCAL=y\n'
                  'CONFIG_KLIPPER_MMU_DIFF_BUFFER=y\nCONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL=y\n'
                  + self.REMAP_DIFF)
        self.configure(values); self.compile(resources=True)
        p = self.capture()
        self.assertEqual(p['result'], 0)
        self.assertEqual(p['rewind'], [[0, 0]] * (2 * MAX_LANES))
        routes = self.adc_routes()
        unit, ch_a = routes[self.gpio('C', 4)]
        _, ch_b = routes[self.gpio('C', 5)]
        self.assertEqual(p['claims'][-3:], [[5, unit, 0, 33], [6, unit, ch_a, 33], [6, unit, ch_b, 33]])
        foreign = ('CONFIG_WANT_ADC=y\nCONFIG_WANT_ADC_LOCAL=y\n'
                   'CONFIG_KLIPPER_MMU_DIFF_BUFFER=y\nCONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PF1"\n')
        self.configure(foreign, seed=self.sim, enabled=True)
        self.compile(resources=True)
        p = self.capture()
        self.assertEqual(p['result'], -3)
        self.assertEqual(p['claims'], [])

    def test_resources_passive_and_unimplemented_feedback(self):
        self.configure(self.RESOURCE_BASE + 'CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL=y\n')
        self.compile(resources=True); p = self.capture()
        self.assertEqual(p['result'], 0); self.assertEqual(len(p['claims']), 24)
        self.assertTrue(all(c[0] == 1 for c in p['claims']))
        self.assertEqual(p['rewind'], [[0,0]]*(2 * MAX_LANES))
        self.assertEqual(self.capture('timer')['external'], 0)
        # The encoder has a real GPIO driver now (mcu/encoder.c): it owns no
        # timer/ADC route, so it reaches this plan like any other GPIO-only
        # capability -- one extra claim per configured wire, one wire by
        # default (Happy Hare's own ERCF/Tradrack shape), two if enabled.
        self.configure(self.RESOURCE_BASE + 'CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL=y\n'
                       'CONFIG_KLIPPER_MMU_ENCODER=y\nCONFIG_KLIPPER_MMU_PINS_ENCODER_A="PF2"\n')
        self.compile(resources=True); p = self.capture()
        self.assertEqual(p['result'], 0); self.assertEqual(len(p['claims']), 25)
        self.assertTrue(all(c[0] == 1 for c in p['claims']))
        self.configure(self.RESOURCE_BASE + 'CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL=y\n'
                       'CONFIG_KLIPPER_MMU_ENCODER=y\nCONFIG_KLIPPER_MMU_PINS_ENCODER_A="PF2"\n'
                       'CONFIG_KLIPPER_MMU_ENCODER_DUAL_PIN=y\nCONFIG_KLIPPER_MMU_PINS_ENCODER_B="PF3"\n')
        self.compile(resources=True); p = self.capture()
        self.assertEqual(p['result'], 0); self.assertEqual(len(p['claims']), 26)
        self.assertTrue(all(c[0] == 1 for c in p['claims']))
        # Current sensing still has no driver at all: refused, not downgraded.
        # (MEASURED_CURRENT depends on POWERED_REWIND -- it is rewind current
        # feedback -- so that has to be on too, with its own required pins,
        # before MEASURED_CURRENT=y is even visible to accept.)
        self.configure(self.RESOURCE_BASE + 'CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL=y\n'
                       'CONFIG_KLIPPER_MMU_POWERED_REWIND=y\n'
                       # Same validated pair-timer pins as
                       # test_resources_one_and_eight_lanes_isolate_timers below.
                       'CONFIG_KLIPPER_MMU_PINS_REWIND_FORWARD="PA1,PA7,PB15,PD13"\n'
                       'CONFIG_KLIPPER_MMU_PINS_REWIND_REVERSE="PA0,PA6,PB14,PD12"\n'
                       'CONFIG_KLIPPER_MMU_PINS_REWIND_ENABLE="PG0,PG1,PG2,PG3"\n'
                       'CONFIG_KLIPPER_MMU_MEASURED_CURRENT=y\nCONFIG_KLIPPER_MMU_PINS_CURRENT="PF4,PF5,PF6,PF7"\n')
        self.compile(resources=True)
        self.assertEqual(self.capture()['result'], -8)

    def test_resources_disabled_and_simulation(self):
        for seed in (self.base,self.sim):
            self.configure(seed=seed, enabled=False); self.compile(resources=True)
            self.assertEqual(self.capture()['result'], -3)

    def test_resources_one_and_eight_lanes_isolate_timers(self):
        forward = ['PA1','PA7','PB15','PD13','PH11','PA9','PE6','PF8']
        reverse = ['PA0','PA6','PB14','PD12','PH10','PA8','PE5','PF9']
        for lanes in (1, 5, 8):
            used = set(forward[:lanes] + reverse[:lanes])
            spare = iter(f'P{port}{bit}' for port in 'ABCDEFGH' for bit in range(16)
                         if f'P{port}{bit}' not in used)
            values = ('CONFIG_WANT_HARD_PWM_QUERY=y\nCONFIG_WANT_HARD_PWM_LOCAL_PAIR=y\n'
                      'CONFIG_KLIPPER_MMU_DEVICE_CUSTOM=y\nCONFIG_KLIPPER_MMU_BOARD_CUSTOM=y\n'
                      f'CONFIG_KLIPPER_MMU_LANE_COUNT={lanes}\nCONFIG_KLIPPER_MMU_POWERED_REWIND=y\n')
            for role, count in [('STEP',lanes),('DIR',lanes),('ENABLE',lanes),('UART',lanes),
                                ('BAY',lanes),('HUB',1),('TOOLHEAD',1),('BUFFER_A',1),
                                ('BUFFER_B',1),('REWIND_ENABLE',lanes)]:
                values += f'CONFIG_KLIPPER_MMU_PINS_{role}="{",".join(next(spare) for _ in range(count))}"\n'
            values += f'CONFIG_KLIPPER_MMU_PINS_REWIND_FORWARD="{",".join(forward[:lanes])}"\n'
            values += f'CONFIG_KLIPPER_MMU_PINS_REWIND_REVERSE="{",".join(reverse[:lanes])}"\n'
            self.configure(values); self.compile(resources=True); p = self.capture()
            if lanes == 8:
                self.assertEqual(p['result'], -8)
                self.assertEqual(p['claims'], [])
                continue
            self.assertEqual(p['result'], 0)
            self.assertEqual([c[1] for c in p['claims'] if c[0]==2],
                             [2] if lanes==1 else [2,3,12,4,5])
            self.assertEqual(len(p['claims']), 13 if lanes==1 else 49)

    def test_default_disabled_and_simulation_cannot_reserve_pins(self):
        for seed in (self.base, self.sim):
            k, _ = self.configure(seed=seed, enabled=seed == self.sim)
            self.assertEqual(k.syms['KLIPPER_MMU_PIN_CONFIG'].str_value, 'n')
            self.compile(); self.assertEqual(self.capture()['result'], -3)  # unavailable

    def test_missing_arrival_pin_is_a_build_error(self):
        self.configure('CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y\n')
        result = self.compile(success=False)
        self.assertIn('toolhead pin assignment required', result.stderr)

    def test_diff_defaults_remap_legacy_sensor_aliases(self):
        extra = 'CONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PF1"\nCONFIG_KLIPPER_MMU_DIFF_BUFFER=y\n'
        legacy = (extra + 'CONFIG_KLIPPER_MMU_PINS_HUB="PC4"\n'
                  'CONFIG_KLIPPER_MMU_PINS_BAY="PC5,PB0,PB1,PB2"\n')
        self.configure(legacy); self.compile(); bad = self.capture()
        self.assertEqual(bad['reason'], 3)  # duplicate resource
        self.assertEqual((bad['first'],bad['second']), (16,27))  # legacy bay0 vs DiffB after LOAD claims
        k, saved = self.configure(extra)
        self.compile(); p = self.capture(); self.assertEqual(p['result'],0)
        self.assertEqual(k.syms['KLIPPER_MMU_PINS_HUB'].str_value, 'PE12')
        self.assertEqual(k.syms['KLIPPER_MMU_PINS_BAY'].str_value, 'PE13,PB0,PB1,PB2')
        self.assertIn('CONFIG_KLIPPER_MMU_PINS_HUB="PE12"\n', saved)
        self.assertIn('CONFIG_KLIPPER_MMU_PINS_BAY="PE13,PB0,PB1,PB2"\n', saved)
        self.assertEqual((p['pins'][5][0],p['pins'][6][0]), (77,76))

    def test_noncanonical_and_wrong_length_lists_rejected(self):
        for pins in ('PD4', 'PD4,PC12,PE2,PD15,PF0', 'PD4,PC12,,PD15',
                     'PD4,PC12,PE2,PD15,', 'PD4,PC12,PE02,PD15',
                     'PD4,PC12,PI0,PD15', 'PD4,PC12,PH16,PD15',
                     'PD4,PC12, PE2,PD15', 'PD4,PC12,!PE2,PD15',
                     'PD4,PC12,M3_STEP,PD15', 'PD4,PC12,P,PD15'):
            with self.subTest(pins=pins):
                self.configure(f'CONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PF1"\nCONFIG_KLIPPER_MMU_PINS_STEP="{pins}"\n')
                self.compile(); p=self.capture(); self.assertEqual(p['result'],-1)
                self.assertEqual(p['reason'],2)

    def test_duplicate_and_unimplemented_adc_route_rejected(self):
        self.configure('CONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PF1"\nCONFIG_KLIPPER_MMU_PINS_DIR="PD4,PC11,PE3,PD14"\n')
        self.compile(); p=self.capture(); self.assertEqual(p['reason'],3)
        self.assertEqual((p['first'],p['second']),(0,4))
        # PB0 is a valid GPIO. Route capability is not a pin-parser failure.
        # Bay no longer owns PB0, so a collision cannot hide that acceptance.
        self.configure('CONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PF1"\n'
                       'CONFIG_KLIPPER_MMU_DIFF_BUFFER=y\n'
                       'CONFIG_KLIPPER_MMU_PINS_HUB="PE12"\n'
                       'CONFIG_KLIPPER_MMU_PINS_BAY="PE13,PF2,PF3,PF4"\n'
                       'CONFIG_KLIPPER_MMU_PINS_BUFFER_A="PB0"\n'
                       'CONFIG_KLIPPER_MMU_PINS_BUFFER_B="PC5"\n')
        self.compile(); p=self.capture(); self.assertEqual(p['result'], 0)
        self.assertEqual((p['pins'][8][0], p['pins'][9][0]), (16, 37))

    def test_passive_and_optional_measurements(self):
        extra='CONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PF1"\nCONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL=y\n'
        self.configure(extra); self.compile(); p=self.capture()
        self.assertEqual(p['result'],0); self.assertEqual(len(p['claims']),24)
        for role in range(10,16): self.assertEqual(p['pins'][role],[65535]*MAX_LANES)
        self.configure(extra + 'CONFIG_KLIPPER_MMU_ENCODER=y\nCONFIG_KLIPPER_MMU_PINS_ENCODER_A="PF2"\nCONFIG_KLIPPER_MMU_ENCODER_DUAL_PIN=y\nCONFIG_KLIPPER_MMU_PINS_ENCODER_B="PF3"\nCONFIG_KLIPPER_MMU_POWERED_REWIND=y\nCONFIG_KLIPPER_MMU_MEASURED_CURRENT=y\nCONFIG_KLIPPER_MMU_PINS_CURRENT="PF4,PF5,PF6,PF7"\n')
        self.compile(); p=self.capture(); self.assertEqual(p['result'],0)
        self.assertEqual(p['pins'][13][0],82); self.assertEqual(p['pins'][14][0],83)
        self.assertEqual(p['pins'][15][:4],[84,85,86,87])

    def test_saved_pin_overrides_survive_board_choice(self):
        _,saved=self.configure('CONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PF1"\nCONFIG_KLIPPER_MMU_PINS_HUB="PE7"\n')
        self.configure('CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y\n',seed=saved)
        self.compile(); p=self.capture(); self.assertEqual(p['result'],0)
        self.assertEqual(p['pins'][6][0],71)

    def test_eight_lane_custom_capacity(self):
        values = ('CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y\nCONFIG_KLIPPER_MMU_LANE_COUNT=8\n'
                  'CONFIG_KLIPPER_MMU_ENCODER=y\nCONFIG_KLIPPER_MMU_ENCODER_DUAL_PIN=y\n'
                  'CONFIG_KLIPPER_MMU_MEASURED_CURRENT=y\n')
        groups = [('STEP',8),('DIR',8),('ENABLE',8),('UART',8),('BAY',8),
                  ('HUB',1),('TOOLHEAD',1),('BUFFER_A',1),('BUFFER_B',1),
                  ('REWIND_FORWARD',8),('REWIND_REVERSE',8),('REWIND_ENABLE',8),
                  ('ENCODER_A',1),('ENCODER_B',1),('CURRENT',8)]
        next_pin = 0
        for name, count in groups:
            pins = ','.join(f'P{chr(65 + p // 16)}{p % 16}' for p in range(next_pin,next_pin+count))
            values += f'CONFIG_KLIPPER_MMU_PINS_{name}="{pins}"\n'
            next_pin += count
        self.configure(values); self.compile(); p=self.capture()
        self.assertEqual(p['result'],0)
        self.assertEqual([claim[0] for claim in p['claims']],list(range(78)))

    def test_feeder_defaults_and_disabled_isolation(self):
        self.configure(); self.compile(feeder=True)
        data = self.capture(); self.assertEqual(data['result'], 0)
        self.assertEqual(data['lanes'][:4], [[0,1,0,10,10,20364]] * 4)
        self.assertEqual(data['lanes'][4:], [[0] * 6] * (MAX_LANES - 4))
        self.assertEqual(self.capture('null')['result'], -1)
        for seed in (self.base, self.sim):
            self.configure(seed=seed, enabled=seed == self.sim)
            self.compile(feeder=True); self.assertEqual(self.capture()['result'], -3)

    def test_feeder_mixed_levels_and_saved_override(self):
        values = ('CONFIG_KLIPPER_MMU_FEEDER_ENABLE_HIGH="1,0,1,0"\n'
                  'CONFIG_KLIPPER_MMU_FEEDER_STEP_HIGH="0,1,0,1"\n'
                  'CONFIG_KLIPPER_MMU_FEEDER_FORWARD_HIGH="1,1,0,0"\n'
                  'CONFIG_KLIPPER_MMU_STEP_HIGH_US=6\nCONFIG_KLIPPER_MMU_STEP_LOW_US=14\n'
                  
                  
                  
                  'CONFIG_KLIPPER_MMU_STEP_MAX_SPEED_MM_S=68\n')
        _, saved = self.configure(values)
        for extra in ('', 'CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y\n'):
            self.configure(extra, seed=saved); self.compile(feeder=True)
            data = self.capture(); self.assertEqual(data['result'],0)
            self.assertEqual(data['lanes'][:4], [
                levels + [6,14,49455]
                for levels in ([1,0,1],[0,1,1],[1,0,0],[0,1,0])])

    def test_feeder_lists_require_exact_lane_count_and_levels(self):
        for levels in ('', '0', '0,0,0', '0,0,0,0,0', '0,0,0,',
                       '0,0,0,2', '0,0,0, 1', '0,0,0,!1', '0,0,0,01'):
            with self.subTest(levels=levels):
                self.configure(f'CONFIG_KLIPPER_MMU_FEEDER_FORWARD_HIGH="{levels}"\n')
                self.compile(feeder=True); self.assertEqual(self.capture()['result'], -1)
        self.configure('CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL=y\n')
        self.compile(feeder=True); self.assertEqual(self.capture()['result'], -1)
        self.configure('CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL=y\n'
                        'CONFIG_KLIPPER_MMU_FEEDER_FORWARD_HIGH="1,1,1,1"\n')
        self.compile(feeder=True); self.assertEqual(self.capture()['result'], 0)

    def test_feeder_speed_and_pulse_budget_conflicts(self):
        for setting, value, expected in [('STEP_HIGH_US', 51, -1),
                                         ('STEP_LOW_US', 51, -1),
                                         ('STEP_MAX_SPEED_MM_S', 5, -5),
                                         ('RETRACT_SPEED_MM_S', 29, -5)]:
            with self.subTest(setting=setting):
                self.configure(f'CONFIG_KLIPPER_MMU_{setting}={value}\n')
                self.compile(feeder=True); self.assertEqual(self.capture()['result'], expected)
        # The smallest speed the menu can express is 1 mm/s (727 microsteps/s
        # here), so the tightest pulse pair that can carry it is 600/600 us.
        self.configure('CONFIG_KLIPPER_MMU_STEP_HIGH_US=600\n'
                       'CONFIG_KLIPPER_MMU_STEP_LOW_US=600\n'
                       'CONFIG_KLIPPER_MMU_STEP_MAX_SPEED_MM_S=1\n'
                       'CONFIG_KLIPPER_MMU_FEED_SPEED_MM_S=1\nCONFIG_KLIPPER_MMU_RETRACT_SPEED_MM_S=1\n')
        self.compile(feeder=True); self.assertEqual(self.capture()['result'], 0)

    def test_feeder_single_and_eight_lane_custom(self):
        for lanes in (1,8):
            levels = ','.join('1' if i % 2 else '0' for i in range(lanes))
            values = f'CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y\nCONFIG_KLIPPER_MMU_LANE_COUNT={lanes}\n'
            values += ''.join(f'CONFIG_KLIPPER_MMU_FEEDER_{name}="{levels}"\n'
                              for name in ('ENABLE_HIGH','STEP_HIGH','FORWARD_HIGH'))
            self.configure(values); self.compile(feeder=True); data=self.capture()
            self.assertEqual(data['result'], 0)
            self.assertEqual(data['lanes'][:lanes], [[i%2]*3 + [10,10,20364]
                                                     for i in range(lanes)])
            self.assertEqual(data['lanes'][lanes:], [[0]*10] * (MAX_LANES - lanes))

    def test_tmc_requires_explicit_limit_and_resistor(self):
        for values, enabled, expected in [('', True, 0), ('', False, -3),
                ('CONFIG_KLIPPER_MMU_DEVICE_CUSTOM=y\n', True, -3),
                ('CONFIG_KLIPPER_MMU_TMC_CURRENT_CEILING=1000\n'
                 'CONFIG_KLIPPER_MMU_TMC_RSENSE=0\n', True, -3)]:
            self.configure(values, enabled=enabled); self.compile(tmc=True)
            self.assertEqual(self.capture()['result'], expected)
        self.configure('CONFIG_KLIPPER_MMU_TMC_CURRENT_CEILING=1000\n', seed=self.sim)
        self.compile(tmc=True); self.assertEqual(self.capture()['result'], -3)

    def test_tmc_uart_shared_menu_option(self):
        k, _ = self.configure('CONFIG_KLIPPER_MMU_TMC_CURRENT_CEILING=1000\n')
        self.assertIn('KLIPPER_MMU_TMC_UART_SHARED', k.syms)
        self.assertEqual(k.syms['KLIPPER_MMU_TMC_UART_SHARED'].str_value, 'y')
        k, _ = self.configure('CONFIG_KLIPPER_MMU_DEVICE_CUSTOM=y\n'
                              'CONFIG_KLIPPER_MMU_TMC_CURRENT_CEILING=1000\n')
        self.assertEqual(k.syms['KLIPPER_MMU_TMC_UART_SHARED'].str_value, 'n')

    def test_tmc_saved_settings_feed_shared_uart_validation(self):
        self.configure('CONFIG_KLIPPER_MMU_TMC_CURRENT_CEILING=1000\n')
        self.compile(tmc=True); data = self.capture(); self.assertEqual(data['result'], 0)
        self.assertEqual(data['lanes'][:4], [[0,110,1000,800,200,16,1,0,20000,100000,520000000,1]] * 4)
        self.assertEqual(data['lanes'][4:], [[0] * 12] * (MAX_LANES - 4))
        self.assertEqual(data['clock_validation'], [-1,0,-1,0,0,0])
        self.assertEqual(self.capture('null')['result'], -1)

    def test_tmc_current_microstep_and_deadline_rejection(self):
        baseline = 'CONFIG_KLIPPER_MMU_TMC_CURRENT_CEILING=1000\n'
        for setting, value in [('TMC_CURRENT_CEILING',799), ('TMC_RSENSE',1000),
                               ('TMC_MICROSTEPS',3), ('HOLD_CURRENT',0),
                               ('HOLD_CURRENT',1), ('RUN_CURRENT',1),
                               ('TMC_DIAGNOSTIC_US',10000)]:
            with self.subTest(setting=setting, value=value):
                self.configure(baseline + f'CONFIG_KLIPPER_MMU_{setting}={value}\n')
                self.compile(tmc=True); self.assertEqual(self.capture()['result'], -1)
        # Upstream Kconfig discards an out-of-range hold target before C sees it.
        k, _ = self.configure(baseline + 'CONFIG_KLIPPER_MMU_HOLD_CURRENT=801\n')
        saved_hold = int(k.syms['KLIPPER_MMU_HOLD_CURRENT'].str_value)
        self.assertLessEqual(saved_hold, 800)
        self.compile(tmc=True); data = self.capture(); self.assertEqual(data['result'], 0)
        self.assertEqual(data['lanes'][0][4], saved_hold)
        for microsteps in (1,256):
            self.configure(baseline + f'CONFIG_KLIPPER_MMU_TMC_MICROSTEPS={microsteps}\n'
                           'CONFIG_KLIPPER_MMU_TMC_TIMEOUT_US=6600\n'
                           'CONFIG_KLIPPER_MMU_TMC_DIAGNOSTIC_US=6600\n')
            self.compile(tmc=True); self.assertEqual(self.capture()['result'], 0)

    def test_tmc_address_lists_and_custom_lane_counts(self):
        baseline = 'CONFIG_KLIPPER_MMU_TMC_CURRENT_CEILING=1000\n'
        for addresses in ('', '0', '0,0,0,0,0', '0,0,0,', '0,0,0,4',
                          '0,0,0, 1', '0,0,0,01', '0,0,0,-1'):
            with self.subTest(addresses=addresses):
                self.configure(baseline + f'CONFIG_KLIPPER_MMU_TMC_ADDRESSES="{addresses}"\n')
                self.compile(tmc=True); self.assertEqual(self.capture()['result'], -1)
        for lanes in (1,8):
            addresses = ','.join(str(i % 4) for i in range(lanes))
            pullups = ','.join('1' if i % 2 == 0 else '0' for i in range(lanes))
            values = (baseline + 'CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y\n'
                      'CONFIG_KLIPPER_MMU_TMC_RSENSE=110\n'
                      f'CONFIG_KLIPPER_MMU_LANE_COUNT={lanes}\n'
                      f'CONFIG_KLIPPER_MMU_TMC_ADDRESSES="{addresses}"\n'
                      f'CONFIG_KLIPPER_MMU_TMC_UART_PULLUP="{pullups}"\n')
            self.configure(values); self.compile(tmc=True); data = self.capture()
            self.assertEqual(data['result'],0)
            self.assertEqual([c[0] for c in data['lanes'][:lanes]], [i % 4 for i in range(lanes)])
            self.assertEqual([c[11] for c in data['lanes'][:lanes]], [1 if i % 2 == 0 else 0 for i in range(lanes)])
            self.assertEqual(data['lanes'][lanes:], [[0] * 12] * (MAX_LANES - lanes))

    def test_tmc_overrides_survive_board_change(self):
        values = ('CONFIG_KLIPPER_MMU_TMC_CURRENT_CEILING=1000\n'
                  'CONFIG_KLIPPER_MMU_TMC_RSENSE=150\n'
                  'CONFIG_KLIPPER_MMU_TMC_ADDRESSES="0,1,2,3"\n'
                  'CONFIG_KLIPPER_MMU_TMC_UART_PULLUP="0,1,0,1"\n'
                  'CONFIG_KLIPPER_MMU_TMC_MICROSTEPS=32\n'
                  '# CONFIG_KLIPPER_MMU_TMC_INTERPOLATE is not set\n'
                  'CONFIG_KLIPPER_MMU_TMC_STEALTHCHOP=y\n')
        _,saved = self.configure(values)
        self.configure('CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y\n', seed=saved)
        self.compile(tmc=True); data = self.capture(); self.assertEqual(data['result'],0)
        self.assertEqual(data['lanes'][:4], [[i,150,1000,800,200,32,0,1,20000,100000,520000000, i % 2]
                                            for i in range(4)])
        self.assertEqual(data['lanes'][4:], [[0] * 12] * (MAX_LANES - 4))

    def test_tmc_pullup_validation_and_custom_wiring(self):
        baseline = 'CONFIG_KLIPPER_MMU_TMC_CURRENT_CEILING=1000\n'
        # Unset / empty pull-up in custom mode returns UNAVAILABLE (-3)
        self.configure(baseline + 'CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y\n'
                       'CONFIG_KLIPPER_MMU_TMC_RSENSE=110\n')
        self.compile(tmc=True); self.assertEqual(self.capture()['result'], -3)
        self.configure(baseline + 'CONFIG_KLIPPER_MMU_TMC_UART_PULLUP=""\n')
        self.compile(tmc=True); self.assertEqual(self.capture()['result'], -3)

        # Malformed entries/count return INVALID_PARAM (-1)
        for bad in ('0', '0,1', '0,1,0', '0,1,0,1,0', '0,1,0,1,',
                    '0,1,0,2', '0,1,0,-1', '0,1,0,a', '0,1,0, 1', ' 0,1,0,1', '0,1,0,01'):
            with self.subTest(pullup=bad):
                self.configure(baseline + f'CONFIG_KLIPPER_MMU_TMC_UART_PULLUP="{bad}"\n')
                self.compile(tmc=True); self.assertEqual(self.capture()['result'], -1)

        # Mixed 0/1 lanes
        self.configure(baseline + 'CONFIG_KLIPPER_MMU_TMC_UART_PULLUP="1,0,0,1"\n')
        self.compile(tmc=True); data = self.capture(); self.assertEqual(data['result'], 0)
        self.assertEqual([c[11] for c in data['lanes'][:4]], [1, 0, 0, 1])
        self.assertEqual(data['lanes'][4:], [[0] * 12] * (MAX_LANES - 4))

        # 1 and 8 lanes
        for lanes in (1, 8):
            addresses = ','.join(str(i % 4) for i in range(lanes))
            pullups = ','.join(str((i + 1) % 2) for i in range(lanes))
            values = (baseline + 'CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y\n'
                      'CONFIG_KLIPPER_MMU_TMC_RSENSE=110\n'
                      f'CONFIG_KLIPPER_MMU_LANE_COUNT={lanes}\n'
                      f'CONFIG_KLIPPER_MMU_TMC_ADDRESSES="{addresses}"\n'
                      f'CONFIG_KLIPPER_MMU_TMC_UART_PULLUP="{pullups}"\n')
            self.configure(values); self.compile(tmc=True); data = self.capture()
            self.assertEqual(data['result'], 0)
            self.assertEqual([c[11] for c in data['lanes'][:lanes]], [(i + 1) % 2 for i in range(lanes)])
            self.assertEqual(data['lanes'][lanes:], [[0] * 12] * (MAX_LANES - lanes))

    SELECTOR_UART_DEVICES = {
        'ERCF_1_1': (400, 200), 'ERCF_2_0': (400, 200), 'ERCF_3_0': (700, 200),
        'TRADRACK': (630, 200), 'CHAMELEON_3D': (630, 200), 'HTLF': (600, 300),
        'MMX6': (500, 200), 'BTT_VIVID': (800, 300), 'CLAYMORE': (800, 200),
    }

    def test_selector_gate_switches_indexed_only(self):
        # INDEXED's per-gate index switches (klipper_mmu/selector.h): one
        # active-high/pull entry per lane, sampled the same way as BAY, but
        # the role -- and its electrical settings -- exist only under
        # KLIPPER_MMU_SELECTOR_KIND_INDEXED (BTT ViViD).
        values = (self.SWITCH_BASE +
                  'CONFIG_KLIPPER_MMU_DEVICE_BTT_VIVID=y\n'
                  'CONFIG_KLIPPER_MMU_SELECTOR_UART=n\n'
                  'CONFIG_KLIPPER_MMU_BAY_SENSORS=n\n'
                  # Not BOXTURTLE, so the AFC Lite preset leaves these unset
                  # (-1/-2); a real electrical setting is required either way.
                  'CONFIG_KLIPPER_MMU_HUB_ACTIVE_HIGH=1\n'
                  'CONFIG_KLIPPER_MMU_HUB_PULL=1\n'
                  'CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_ACTIVE_HIGH=1\n'
                  'CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_PULL=1\n'
                  'CONFIG_KLIPPER_MMU_PINS_STEP="PD4"\n'
                  'CONFIG_KLIPPER_MMU_PINS_DIR="PD3"\n'
                  'CONFIG_KLIPPER_MMU_PINS_ENABLE="PD6"\n'
                  'CONFIG_KLIPPER_MMU_PINS_UART="PD5"\n'
                  'CONFIG_KLIPPER_MMU_PINS_SELECTOR_STEP="PG4"\n'
                  'CONFIG_KLIPPER_MMU_PINS_SELECTOR_DIR="PG5"\n'
                  'CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENABLE="PG6"\n'
                  'CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENDSTOP="PG7"\n'
                  'CONFIG_KLIPPER_MMU_PINS_SELECTOR_GATE="PG0,PG1,PG2,PG3"\n'
                  'CONFIG_KLIPPER_MMU_SELECTOR_GATE_ACTIVE_HIGH="1,1,0,0"\n'
                  'CONFIG_KLIPPER_MMU_SELECTOR_GATE_PULL="1,1,-1,-1"\n')
        self.configure(values); self.compile(switches=True)
        data = self.capture(); self.assertEqual(data['result'], 0)
        expect = [[self.gpio('G', i), pull, high] for i, (pull, high) in
                 enumerate([(1, 1), (1, 1), (-1, 0), (-1, 0)])]
        self.assertEqual(data['selector_gate'][:4], expect)
        self.assertEqual(data['selector_gate'][4:], [[65535, 0, 0]] * (MAX_LANES - 4))

        # Claymore (AFC's own forward-only INDEXED variant, no gate order):
        # the same per-gate index-switch role, plus its per-lane powered
        # rewind and BAY switches (LOAD1-4 -- see mcu/Kconfig.devices; no
        # separate prep pin exists, so BAY_SENSORS stays at its ordinary
        # default rather than being disabled the way ViViD's block above
        # disables it) all compile together on one shared drive/selector pair.
        claymore = (values.replace('CONFIG_KLIPPER_MMU_DEVICE_BTT_VIVID=y\n',
                                   'CONFIG_KLIPPER_MMU_DEVICE_CLAYMORE=y\n')
                   .replace('CONFIG_KLIPPER_MMU_BAY_SENSORS=n\n', '')
                   # Not BOXTURTLE either, so the AFC Lite preset's BAY
                   # electrical defaults (BOXTURTLE-only) also leave these
                   # unset; Claymore's real LOAD1-4 pins (mapped to BAY, see
                   # above) need an explicit electrical setting like every
                   # other role here.
                   + 'CONFIG_KLIPPER_MMU_PINS_BAY="PF2,PF3,PF4,PF5"\n'
                     'CONFIG_KLIPPER_MMU_BAY_ACTIVE_HIGH="1,1,1,1"\n'
                     'CONFIG_KLIPPER_MMU_BAY_PULL="1,1,1,1"\n'
                     'CONFIG_KLIPPER_MMU_POWERED_REWIND=y\n' + self.REWIND_OUTPUTS)
        self.configure(claymore); self.compile(switches=True)
        data = self.capture(); self.assertEqual(data['result'], 0)
        self.assertEqual(data['selector_gate'][:4], expect)
        self.compile(rewind_outputs=True)
        self.assertEqual(self.capture()['result'], 0)

        # Not an INDEXED build (LINEAR here): the role and its electrical
        # settings are hidden by their own `depends on`, so they resolve
        # empty regardless of any prior selection.
        k, _ = self.configure(self.SELECTOR_PINS)
        self.assertEqual(k.syms['KLIPPER_MMU_PINS_SELECTOR_GATE'].str_value, '')
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_GATE_ACTIVE_HIGH'].str_value, '')
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_GATE_PULL'].str_value, '')

    def test_selector_uart_default_per_device(self):
        # Every selector-stepper device in the catalogue has a selector TMC
        # UART by default (installer/Kconfig.pins:149-176; no "None" choice).
        for device in self.SELECTOR_UART_DEVICES:
            with self.subTest(device=device):
                k, _ = self.configure(f'CONFIG_KLIPPER_MMU_DEVICE_{device}=y\n')
                self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_STEPPER'].str_value, 'y')
                self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_UART'].str_value, 'y')
        # A servo-only selector has no selector stepper at all, so no UART either.
        k, _ = self.configure('CONFIG_KLIPPER_MMU_DEVICE_MMX=y\n')
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_STEPPER'].str_value, 'n')
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_UART'].str_value, 'n')
        # Independent per-lane feeders (no selector at all): still off.
        k, _ = self.configure()
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_UART'].str_value, 'n')

    SELECTOR_PINS = ('CONFIG_KLIPPER_MMU_SELECTOR_KIND_LINEAR=y\n'
                     # A selector's gear is one shared driver (KM_GEAR_DRIVERS
                     # == 1), so its own STEP/DIR/ENABLE/UART lists collapse
                     # to a single pin each -- the AFC Lite 4-lane preset no
                     # longer fits.
                     'CONFIG_KLIPPER_MMU_PINS_STEP="PD4"\n'
                     'CONFIG_KLIPPER_MMU_PINS_DIR="PD3"\n'
                     'CONFIG_KLIPPER_MMU_PINS_ENABLE="PD6"\n'
                     'CONFIG_KLIPPER_MMU_PINS_UART="PD5"\n'
                     # PF0..PF5: outside every AFC Lite default claim (rewind,
                     # bay/load/hub/toolhead/buffer all live on PA/PB/PC/PE).
                     'CONFIG_KLIPPER_MMU_PINS_SELECTOR_STEP="PF0"\n'
                     'CONFIG_KLIPPER_MMU_PINS_SELECTOR_DIR="PF1"\n'
                     'CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENABLE="PF2"\n'
                     'CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENDSTOP="PF3"\n'
                     'CONFIG_KLIPPER_MMU_PINS_SERVO="PF5"\n')  # LINEAR's default grip servo

    def test_selector_uart_pin_reserved_only_when_enabled(self):
        # Off (today's default for a custom wiring): no selector UART claim,
        # the gear's own KM_PIN_UART role (4) is untouched either way.
        self.configure(self.SELECTOR_PINS)
        self.compile(); p = self.capture()
        self.assertEqual(p['result'], 0)
        self.assertEqual(p['pins'][20], [65535] * MAX_LANES)  # KM_PIN_SELECTOR_UART
        self.assertNotIn(20, [c[1] for c in p['claims']])

        # On: reserved at role 20 (KM_PIN_SELECTOR_UART), lane 0 only.
        self.configure(self.SELECTOR_PINS + 'CONFIG_KLIPPER_MMU_SELECTOR_UART=y\n'
                       'CONFIG_KLIPPER_MMU_PINS_SELECTOR_UART="PF4"\n')
        self.compile(); p = self.capture()
        self.assertEqual(p['result'], 0)
        pin = self.gpio('F', 4)
        self.assertEqual(p['pins'][20], [pin] + [65535] * (MAX_LANES - 1))
        self.assertIn([pin, 20], p['claims'])

        # On without a pin: the build refuses it, never a silent plain step/dir selector.
        self.configure(self.SELECTOR_PINS + 'CONFIG_KLIPPER_MMU_SELECTOR_UART=y\n')
        result = self.compile(success=False)
        self.assertIn('selector_uart pin assignment required', result.stderr)

    def test_selector_uart_shares_gear_pin_at_distinct_address(self):
        # Happy Hare's classic ERCF 1.1 EASY-BRD wiring: the selector's own
        # TMC2209 shares the gear's UART pin, distinguished only by address
        # (installer/boards/Kconfig.easy_brd_rp2040:24-25; docs/selectors.md).
        shared = (self.SELECTOR_PINS + 'CONFIG_KLIPPER_MMU_SELECTOR_UART=y\n'
                 'CONFIG_KLIPPER_MMU_PINS_SELECTOR_UART="PD5"\n'  # == the gear's own UART pin.
                 'CONFIG_KLIPPER_MMU_TMC_ADDRESSES="0"\n')

        # Distinct addresses: accepted, and only one claim exists for the
        # shared pin -- a PIN claim is exclusive by instance regardless of
        # owner, so a second one for the same pin would always be rejected.
        self.configure(shared + 'CONFIG_KLIPPER_MMU_SELECTOR_UART_ADDRESS=1\n')
        self.compile(); p = self.capture()
        self.assertEqual(p['result'], 0)
        pin = self.gpio('D', 5)
        self.assertEqual(p['pins'][4], [pin] + [65535] * (MAX_LANES - 1))   # KM_PIN_UART
        self.assertEqual(p['pins'][20], [pin] + [65535] * (MAX_LANES - 1))  # KM_PIN_SELECTOR_UART
        self.assertEqual(p['claims'].count([pin, 4]), 1)
        self.assertNotIn([pin, 20], p['claims'])

        # Equal addresses on the one shared wire cannot be told apart:
        # refused exactly like any other accidental pin reuse.
        self.configure(shared + 'CONFIG_KLIPPER_MMU_SELECTOR_UART_ADDRESS=0\n')
        self.compile(); p = self.capture()
        self.assertEqual((p['result'], p['reason']), (-5, 3))  # CONFLICT, DUPLICATE_RESOURCE

        # An unset/malformed gear address can't be proven distinct either
        # (the AFC Lite preset baked into every fixture here otherwise
        # always supplies a real "0,0,0,0", so this needs an explicit
        # override to genuinely exercise the empty case).
        self.configure(self.SELECTOR_PINS + 'CONFIG_KLIPPER_MMU_SELECTOR_UART=y\n'
                       'CONFIG_KLIPPER_MMU_PINS_SELECTOR_UART="PD5"\n'
                       'CONFIG_KLIPPER_MMU_TMC_ADDRESSES=""\n'
                       'CONFIG_KLIPPER_MMU_SELECTOR_UART_ADDRESS=1\n')
        self.compile(); p = self.capture()
        self.assertEqual((p['result'], p['reason']), (-5, 3))

        # Any *other* pin reuse (not the gear's own UART pin) stays a plain
        # conflict regardless of addresses.
        self.configure(self.SELECTOR_PINS + 'CONFIG_KLIPPER_MMU_SELECTOR_UART=y\n'
                       'CONFIG_KLIPPER_MMU_PINS_SELECTOR_UART="PF1"\n'  # == selector DIR
                       'CONFIG_KLIPPER_MMU_TMC_ADDRESSES="0"\n'
                       'CONFIG_KLIPPER_MMU_SELECTOR_UART_ADDRESS=1\n')
        self.compile(); p = self.capture()
        self.assertEqual((p['result'], p['reason']), (-5, 3))

    ERCF_3_0_PINS = ('CONFIG_KLIPPER_MMU_DEVICE_ERCF_3_0=y\n'
                     'CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y\n'
                     'CONFIG_KLIPPER_MMU_BAY_SENSORS=n\n'
                     'CONFIG_KLIPPER_MMU_PINS_STEP="PD0"\n'
                     'CONFIG_KLIPPER_MMU_PINS_DIR="PD1"\n'
                     'CONFIG_KLIPPER_MMU_PINS_ENABLE="PD2"\n'
                     'CONFIG_KLIPPER_MMU_PINS_UART="PD3"\n'
                     'CONFIG_KLIPPER_MMU_PINS_HUB="PC0"\n'
                     'CONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PC1"\n'
                     'CONFIG_KLIPPER_MMU_PINS_BUFFER_A="PC2"\n'
                     'CONFIG_KLIPPER_MMU_PINS_BUFFER_B="PC3"\n'
                     'CONFIG_KLIPPER_MMU_PINS_ENCODER_A="PC6"\n'
                     'CONFIG_KLIPPER_MMU_ENCODER_PULL=1\n'
                     'CONFIG_KLIPPER_MMU_PINS_SELECTOR_STEP="PD4"\n'
                     'CONFIG_KLIPPER_MMU_PINS_SELECTOR_DIR="PD5"\n'
                     'CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENABLE="PD6"\n'
                     'CONFIG_KLIPPER_MMU_PINS_SELECTOR_UART="PD7"\n'
                     'CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENDSTOP="PC4"\n'
                     'CONFIG_KLIPPER_MMU_PINS_SERVO="PC5"\n'
                     'CONFIG_KLIPPER_MMU_TMC_ADDRESSES="0"\n'
                     'CONFIG_KLIPPER_MMU_TMC_UART_PULLUP="1"\n')

    def test_ercf_3_0_jack_rabbit_claims_no_servo_pin(self):
        # Jack Rabbit clears KLIPPER_MMU_SELECTOR_LINEAR_HAS_SERVO (mcu/
        # Kconfig.selector), so KLIPPER_MMU_SERVO resolves to n and the
        # servo RESERVE (mcu/pin_config.c "#if CONFIG_KLIPPER_MMU_SERVO")
        # drops out entirely: KLIPPER_MMU_PINS_SERVO becomes invisible and
        # is ignored even though this fixture still supplies a value for it,
        # exactly like the INDEXED-only gate role in
        # test_selector_gate_switches_indexed_only above.
        self.configure(self.ERCF_3_0_PINS)
        self.compile(); p = self.capture()
        self.assertEqual(p['result'], 0)
        servo_pin = self.gpio('C', 5)
        self.assertEqual(p['pins'][22], [servo_pin] + [65535] * (MAX_LANES - 1))
        self.assertIn([servo_pin, 22], p['claims'])
        base_claim_count = len(p['claims'])

        self.configure(self.ERCF_3_0_PINS + 'CONFIG_KLIPPER_MMU_ERCF_JACK_RABBIT=y\n')
        self.compile(); p = self.capture()
        self.assertEqual(p['result'], 0)
        self.assertEqual(p['pins'][22], [65535] * MAX_LANES)  # KM_PIN_SERVO
        self.assertNotIn(22, [claim[1] for claim in p['claims']])
        self.assertEqual(len(p['claims']), base_claim_count - 1)
        # PC5 (the would-be servo pin) is free for reuse, not phantom-claimed.
        self.assertNotIn(servo_pin, [claim[0] for claim in p['claims']])

    def test_selector_tmc_address_pullup_and_currents(self):
        # Disabled selector stepper: unavailable, same as the gear without pins.
        self.configure('CONFIG_KLIPPER_MMU_TMC_CURRENT_CEILING=1000\n')
        self.compile(tmc=True); data = self.capture()
        self.assertEqual(data['selector']['result'], -3)
        self.assertIsNone(data['selector']['lane'])

        baseline = ('CONFIG_KLIPPER_MMU_SELECTOR_KIND_LINEAR=y\n'
                    'CONFIG_KLIPPER_MMU_SELECTOR_UART=y\n'
                    'CONFIG_KLIPPER_MMU_TMC_CURRENT_CEILING=1000\n')
        # Defaults: address 0 (installer/boards/Kconfig.mmb_2_0 and
        # Kconfig.mellow_easy_brd_can_1 never emit uart_address, so Klipper's
        # own tmc_uart.py default of 0 applies), no pull-up, run/hold 500/200.
        self.configure(baseline)
        self.compile(tmc=True); data = self.capture()
        self.assertEqual(data['selector']['result'], 0)
        self.assertEqual(data['selector']['lane'],
                         [0, 110, 1000, 500, 200, 16, 1, 0, 20000, 100000, 520000000, 0])

        # Every option editable and independent of the gear's own settings.
        # A selector's single shared gear is KM_GEAR_DRIVERS==1: one address.
        self.configure(baseline + 'CONFIG_KLIPPER_MMU_SELECTOR_UART_ADDRESS=2\n'
                       'CONFIG_KLIPPER_MMU_SELECTOR_UART_PULLUP=y\n'
                       'CONFIG_KLIPPER_MMU_SELECTOR_RUN_CURRENT=630\n'
                       'CONFIG_KLIPPER_MMU_SELECTOR_HOLD_CURRENT=300\n'
                       'CONFIG_KLIPPER_MMU_TMC_ADDRESSES="1"\n'
                       'CONFIG_KLIPPER_MMU_TMC_UART_PULLUP="0"\n')
        self.compile(tmc=True); data = self.capture()
        self.assertEqual(data['selector']['lane'],
                         [2, 110, 1000, 630, 300, 16, 1, 0, 20000, 100000, 520000000, 1])
        # The gear's own shared lane-0 config is untouched by the selector's
        # own separate options.
        self.assertEqual(data['lanes'][0][:5], [1, 110, 1000, 800, 200])
        self.assertEqual(data['lanes'][1:], [[0] * 12] * (MAX_LANES - 1))

    def test_selector_tmc_per_device_currents(self):
        for device, (run, hold) in self.SELECTOR_UART_DEVICES.items():
            with self.subTest(device=device):
                self.configure(f'CONFIG_KLIPPER_MMU_DEVICE_{device}=y\n'
                               'CONFIG_KLIPPER_MMU_TMC_CURRENT_CEILING=1000\n')
                self.compile(tmc=True); data = self.capture()
                self.assertEqual(data['selector']['result'], 0)
                self.assertEqual(data['selector']['lane'][3:5], [run, hold])

    SWITCH_BASE = (NO_LOAD_SENSORS + 'CONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PF1"\n'
                   'CONFIG_KLIPPER_MMU_TOOLHEAD_ACTIVE_HIGH=0\n'
                   'CONFIG_KLIPPER_MMU_TOOLHEAD_PULL=1\n'
                   'CONFIG_KLIPPER_MMU_BUFFER_ACTIVE_HIGH="1,1"\n'
                   'CONFIG_KLIPPER_MMU_BUFFER_PULL="1,1"\n'
                   'CONFIG_KLIPPER_MMU_BUFFER_LOW_B=y\n')

    def test_switch_defaults_and_explicit_mechanical_mapping(self):
        self.configure(self.SWITCH_BASE); self.compile(switches=True)
        data = self.capture(); self.assertEqual(data['result'], 0)
        self.assertEqual(data['inputs'], [[p,1,0] for p in (37,16,17,18)]
                         + [[65535,0,0]]*(MAX_LANES - 4) + [[36,1,1],[81,1,0],[77,1,1],[76,1,1]])
        self.assertEqual(self.capture('null')['result'], -1)
        self.configure(self.SWITCH_BASE + 'CONFIG_KLIPPER_MMU_BUFFER_LOW_A=y\n')
        self.compile(switches=True)
        self.assertEqual(self.capture()['inputs'][-2:], [[76,1,1],[77,1,1]])

    def test_switch_unset_and_disabled_are_unavailable(self):
        for setting in ['TOOLHEAD_ACTIVE_HIGH=-1', 'TOOLHEAD_PULL=-2', 'HUB_ACTIVE_HIGH=-1',
                        'HUB_PULL=-2', 'BAY_ACTIVE_HIGH=""', 'BAY_PULL=""',
                        'BUFFER_ACTIVE_HIGH=""', 'BUFFER_PULL=""', 'BUFFER_LOW_UNSET=y']:
            with self.subTest(setting=setting):
                self.configure(self.SWITCH_BASE + f'CONFIG_KLIPPER_MMU_{setting}\n')
                self.compile(switches=True); self.assertEqual(self.capture()['result'], -3)
        for seed in (self.base, self.sim):
            self.configure(seed=seed, enabled=seed == self.sim)
            self.compile(switches=True); self.assertEqual(self.capture()['result'], -3)

    def test_optional_load_pins_and_electrical_settings(self):
        self.configure(self.SWITCH_BASE); self.compile(switches=True)
        baseline = self.capture()
        self.assertEqual(baseline['result'], 0)
        self.assertEqual(baseline['load'], [[65535, 0, 0]] * MAX_LANES)
        self.compile(); baseline_claims = self.capture()['claims']

        enabled = (self.SWITCH_BASE + 'CONFIG_KLIPPER_MMU_LOAD_SENSORS=y\n'
                   'CONFIG_KLIPPER_MMU_PINS_LOAD="PF2,PF3,PF4,PF5"\n'
                   'CONFIG_KLIPPER_MMU_LOAD_ACTIVE_HIGH="1,0,1,0"\n'
                   'CONFIG_KLIPPER_MMU_LOAD_PULL="1,-1,0,1"\n')
        self.configure(enabled); self.compile(switches=True)
        data = self.capture()
        self.assertEqual(data['result'], 0)
        self.assertEqual(data['load'][:4], [[82,1,1],[83,-1,0],[84,0,1],[85,1,0]])
        self.assertEqual(data['inputs'], baseline['inputs'])
        self.compile(); pins = self.capture()
        self.assertEqual(len(pins['claims']), len(baseline_claims) + 4)
        self.assertEqual([claim[0] for claim in pins['claims'] if claim not in baseline_claims],
                         [82,83,84,85])
        self.assertEqual(pins['pins'][16][:4], [82,83,84,85])

        for name, value, expected in (
            ('PINS_LOAD', 'PF2,PF3,PF4', -1),
            ('PINS_LOAD', 'PF2,PF3,PF4,PC4', -5),
            ('LOAD_ACTIVE_HIGH', '1,0,1', -1),
            ('LOAD_ACTIVE_HIGH', '1,0,1,2', -1),
            ('LOAD_PULL', '1,-1,0', -1),
            ('LOAD_PULL', '1,-2,0,1', -1),
            ('LOAD_PULL', '', -3),
        ):
            with self.subTest(name=name, value=value):
                self.configure(enabled + f'CONFIG_KLIPPER_MMU_{name}="{value}"\n')
                self.compile(switches=True)
                self.assertEqual(self.capture()['result'], expected)

    def test_boxturtle_afc_lite_defaults_enable_load_inputs(self):
        k, saved = self.configure()
        self.assertEqual(k.syms['KLIPPER_MMU_LOAD_SENSORS'].str_value, 'y')
        self.assertEqual(k.syms['KLIPPER_MMU_PINS_LOAD'].str_value,
                         'PE8,PE9,PE10,PE11')
        self.assertEqual(k.syms['KLIPPER_MMU_PINS_TOOLHEAD'].str_value, 'PE7')
        self.assertNotIn('CONFIG_KLIPPER_MMU_LOAD_SENSORS=n', saved)
        self.assertIn('CONFIG_KLIPPER_MMU_LOAD_SENSORS=y\n', saved)
        self.compile(switches=True)
        switches = self.capture()
        self.assertEqual(switches['result'], 0)
        self.assertEqual(switches['load'][:4], [[72,1,1],[73,1,1],[74,1,1],[75,1,1]])

        self.configure('CONFIG_WANT_ADC_LOCAL=y\nCONFIG_KLIPPER_MMU_DIFF_BUFFER=y\n')
        self.compile(resources=True)
        diff = self.capture()
        self.assertEqual(diff['result'], 0)
        self.compile()
        diff_pins = self.capture()
        self.assertEqual(diff_pins['pins'][5][:4], [77,16,17,18])  # BAY0 is SW12/PE13
        self.assertEqual(diff_pins['pins'][6][0], 76)              # HUB is SW11/PE12
        self.assertEqual(diff_pins['pins'][7][0], 71)              # toolhead is SW6/PE7
        self.assertEqual(diff_pins['pins'][16][:4], [72,73,74,75]) # LOAD stays SW7-SW10

    def test_switch_lists_require_canonical_values_and_exact_count(self):
        bad = [('BAY_ACTIVE_HIGH','0,0,0'), ('BAY_ACTIVE_HIGH','0,0,0,0,0'),
               ('BAY_ACTIVE_HIGH','0,0,0,-1'), ('BAY_ACTIVE_HIGH','0,0,0,01'),
               ('BAY_PULL','1,1,1,-0'), ('BAY_PULL','1,1,1,-2'),
               ('BAY_PULL','1,1,1,+1'), ('BAY_PULL','1,1,1, 1'),
               ('BAY_PULL','1,1,1,-'), ('BAY_PULL','1,1,1,1,'),
               ('BUFFER_ACTIVE_HIGH','1'), ('BUFFER_ACTIVE_HIGH','1,2'),
               ('BUFFER_PULL','1,1,1'), ('BUFFER_PULL','1,')]
        for name, value in bad:
            with self.subTest(name=name, value=value):
                self.configure(self.SWITCH_BASE + f'CONFIG_KLIPPER_MMU_{name}="{value}"\n')
                self.compile(switches=True); self.assertEqual(self.capture()['result'], -1)

    def test_switch_electrical_overrides_survive_preset_change(self):
        values = (self.SWITCH_BASE + 'CONFIG_KLIPPER_MMU_BAY_ACTIVE_HIGH="1,0,1,0"\n'
                  'CONFIG_KLIPPER_MMU_BAY_PULL="-1,0,1,-1"\n'
                  'CONFIG_KLIPPER_MMU_HUB_ACTIVE_HIGH=0\nCONFIG_KLIPPER_MMU_HUB_PULL=-1\n'
                  'CONFIG_KLIPPER_MMU_TOOLHEAD_ACTIVE_HIGH=1\nCONFIG_KLIPPER_MMU_TOOLHEAD_PULL=0\n'
                  'CONFIG_KLIPPER_MMU_BUFFER_ACTIVE_HIGH="0,1"\n'
                  'CONFIG_KLIPPER_MMU_BUFFER_PULL="-1,0"\n')
        _, saved = self.configure(values)
        for extra in ('', 'CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y\nCONFIG_KLIPPER_MMU_DEVICE_CUSTOM=y\n'):
            self.configure(extra, seed=saved); self.compile(switches=True)
            data = self.capture(); self.assertEqual(data['result'],0)
            self.assertEqual(data['inputs'][:4], [[37,-1,1],[16,0,0],[17,1,1],[18,-1,0]])
            self.assertEqual(data['inputs'][-4:], [[36,-1,0],[81,0,1],[77,0,1],[76,-1,0]])

    def test_switch_analog_separation_and_pin_conflicts(self):
        # New analog selection has no switch settings to infer or apply to ADC.
        values = ('CONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PF1"\n'
                  'CONFIG_KLIPPER_MMU_TOOLHEAD_ACTIVE_HIGH=1\nCONFIG_KLIPPER_MMU_TOOLHEAD_PULL=0\n'
                  'CONFIG_KLIPPER_MMU_DIFF_BUFFER=y\n'
                  'CONFIG_KLIPPER_MMU_PINS_HUB="PC4"\n'
                  'CONFIG_KLIPPER_MMU_PINS_BAY="PC5,PB0,PB1,PB2"\n')
        self.configure(values); self.compile(switches=True)
        self.assertEqual(self.capture()['result'], -5)  # actual pin conflict
        values = (self.SWITCH_BASE + 'CONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PF1"\n'
                  'CONFIG_KLIPPER_MMU_TOOLHEAD_ACTIVE_HIGH=1\nCONFIG_KLIPPER_MMU_TOOLHEAD_PULL=0\n'
                  'CONFIG_KLIPPER_MMU_DIFF_BUFFER=y\n' + self.REMAP_DIFF)
        k,_ = self.configure(values); self.compile(switches=True)
        data = self.capture(); self.assertEqual(data['result'], 0)
        self.assertEqual(data['inputs'][-2:], [[65535,0,0],[65535,0,0]])
        self.assertEqual(k.syms['KLIPPER_MMU_BUFFER_PULL'].visibility, 0)
        self.configure(self.SWITCH_BASE + 'CONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PC4"\n')
        self.compile(switches=True); self.assertEqual(self.capture()['result'], -5)

    def test_switch_passive_without_adc_local(self):
        self.configure(self.SWITCH_BASE + 'CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL=y\n'
                       'CONFIG_WANT_ADC_LOCAL=n\n'
                       'CONFIG_KLIPPER_MMU_BAY_ACTIVE_HIGH="0,0,0,0"\n'
                       'CONFIG_KLIPPER_MMU_BAY_PULL="1,1,1,1"\n'
                       'CONFIG_KLIPPER_MMU_HUB_ACTIVE_HIGH=1\n'
                       'CONFIG_KLIPPER_MMU_HUB_PULL=1\n')
        self.assertEqual(self.adc_local, False)
        self.compile(switches=True)
        self.assertEqual(self.capture()['result'], 0)

    def test_switch_custom_one_and_eight_lanes(self):
        for lanes in (1,8):
            values = (self.SWITCH_BASE + 'CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y\n'
                      f'CONFIG_KLIPPER_MMU_LANE_COUNT={lanes}\n'
                      'CONFIG_KLIPPER_MMU_HUB_ACTIVE_HIGH=1\nCONFIG_KLIPPER_MMU_HUB_PULL=1\n'
                      f'CONFIG_KLIPPER_MMU_BAY_ACTIVE_HIGH="{",".join(["0"]*lanes)}"\n'
                      f'CONFIG_KLIPPER_MMU_BAY_PULL="{",".join(["1"]*lanes)}"\n')
            groups = [('STEP',lanes),('DIR',lanes),('ENABLE',lanes),('UART',lanes),('BAY',lanes),
                      ('HUB',1),('TOOLHEAD',1),('BUFFER_A',1),('BUFFER_B',1),
                      ('REWIND_FORWARD',lanes),('REWIND_REVERSE',lanes),('REWIND_ENABLE',lanes)]
            next_pin = 0
            for name, count in groups:
                pins = ','.join(f'P{chr(65 + p // 16)}{p % 16}' for p in range(next_pin,next_pin+count))
                values += f'CONFIG_KLIPPER_MMU_PINS_{name}="{pins}"\n'
                next_pin += count
            self.configure(values); self.compile(switches=True)
            data = self.capture(); self.assertEqual(data['result'], 0)
            self.assertEqual(data['inputs'][:lanes], [[p,1,0] for p in range(4*lanes,5*lanes)])
            self.assertEqual(data['inputs'][lanes:MAX_LANES], [[65535,0,0]]*(MAX_LANES - lanes))

    def test_qidi_box_no_tmc_uart_pin_and_single_switch_buffer(self):
        # QIDI Box (docs/devices/qidi_box.md): a tension-only buffer, so
        # KLIPPER_MMU_PINS_BUFFER_B is not offered or required -- unlike
        # BoxTurtle's AFC Lite preset, which needs both buffer pins. Its gear
        # has no TMC UART chip (KLIPPER_MMU_GEAR_TMC_UART defaults off for
        # it, mcu/Kconfig.control), so KLIPPER_MMU_PINS_UART is not offered
        # or claimed either -- a user is never asked to invent a pin for
        # hardware this device does not have.
        values = ('CONFIG_KLIPPER_MMU_DEVICE_QIDI_BOX=y\n'
                  'CONFIG_KLIPPER_MMU_PINS_STEP="PA0,PA1,PA2,PA3"\n'
                  'CONFIG_KLIPPER_MMU_PINS_DIR="PA4,PA5,PA6,PA7"\n'
                  'CONFIG_KLIPPER_MMU_PINS_ENABLE="PA8,PA9,PA10,PA11"\n'
                  'CONFIG_KLIPPER_MMU_PINS_BAY="PB0,PB1,PB2,PB3"\n'
                  'CONFIG_KLIPPER_MMU_BAY_ACTIVE_HIGH="0,0,0,0"\n'
                  'CONFIG_KLIPPER_MMU_BAY_PULL="1,1,1,1"\n'
                  'CONFIG_KLIPPER_MMU_PINS_HUB="PB4"\n'
                  'CONFIG_KLIPPER_MMU_HUB_ACTIVE_HIGH=0\n'
                  'CONFIG_KLIPPER_MMU_HUB_PULL=1\n'
                  'CONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PB5"\n'
                  'CONFIG_KLIPPER_MMU_TOOLHEAD_ACTIVE_HIGH=0\n'
                  'CONFIG_KLIPPER_MMU_TOOLHEAD_PULL=1\n'
                  'CONFIG_KLIPPER_MMU_BUFFER_SINGLE_SWITCH=y\n'
                  'CONFIG_KLIPPER_MMU_PINS_BUFFER_A="PB6"\n'
                  'CONFIG_KLIPPER_MMU_BUFFER_ACTIVE_HIGH="0"\n'
                  'CONFIG_KLIPPER_MMU_BUFFER_PULL="1"\n')
        k, _ = self.configure(values)
        self.assertEqual(k.syms['KLIPPER_MMU_GEAR_TMC_UART'].str_value, 'n')
        self.assertEqual(k.syms['KLIPPER_MMU_PINS_UART'].visibility, 0)
        self.assertEqual(k.syms['KLIPPER_MMU_PINS_UART'].str_value, '')
        self.assertEqual(k.syms['KLIPPER_MMU_PINS_BUFFER_B'].visibility, 0)
        self.assertEqual(k.syms['KLIPPER_MMU_PINS_BUFFER_B'].str_value, '')

        self.compile()
        data = self.capture()
        self.assertEqual(data['result'], 0)
        self.assertEqual(data['pins'][4][:4], [65535] * 4)   # KM_PIN_UART: unclaimed
        self.assertEqual(data['pins'][9][:1], [65535])       # KM_PIN_BUFFER_B: unclaimed
        self.assertEqual(data['pins'][8][:1], [self.gpio('B', 6)])  # KM_PIN_BUFFER_A itself

        self.compile(switches=True)
        switches = self.capture()
        self.assertEqual(switches['result'], 0)
        # [bay*4, hub, toolhead, buffer_low, buffer_high]: buffer_high is
        # KM_SWITCH_PIN_UNUSED, never claimed or fabricated (mcu/switch_gpio.c).
        self.assertEqual(switches['inputs'][-2:],
                         [[self.gpio('B', 6), 1, 0], [65535, 0, 0]])

    FPS = 'CONFIG_WANT_ADC_LOCAL=y\nCONFIG_KLIPPER_MMU_DIFF_BUFFER=y\nCONFIG_KLIPPER_MMU_FPS_ARRIVAL=y\n'

    def test_fps_arrival_may_omit_toolhead_claim_and_settings(self):
        # Without FPS arrival the switch, its pin and its settings stay required.
        k, _ = self.configure('CONFIG_WANT_ADC_LOCAL=y\nCONFIG_KLIPPER_MMU_DIFF_BUFFER=y\n'
                              'CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR=n\n')
        self.assertEqual(k.syms['KLIPPER_MMU_TOOLHEAD_SENSOR'].str_value, 'y')
        self.assertEqual(k.syms['KLIPPER_MMU_PINS_TOOLHEAD'].str_value, 'PE7')
        k, _ = self.configure(self.FPS + 'CONFIG_KLIPPER_MMU_TOOLHEAD_PULL=-2\n')
        self.assertEqual(k.syms['KLIPPER_MMU_TOOLHEAD_SENSOR'].str_value, 'y')
        self.compile(switches=True); self.assertEqual(self.capture()['result'], -3)
        self.configure(self.FPS)
        self.compile(); fitted = self.capture()
        self.compile(switches=True); fitted_switches = self.capture()
        self.compile(resources=True); fitted_resources = self.capture()
        self.assertEqual(fitted['pins'][7][0], 71)
        self.assertEqual(fitted_switches['inputs'][MAX_LANES + 1], [71, 1, 0])

        k, _ = self.configure(self.FPS + 'CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR=n\n'
                              'CONFIG_KLIPPER_MMU_TOOLHEAD_PULL=-2\n')
        self.assertEqual(k.syms['KLIPPER_MMU_PINS_TOOLHEAD'].visibility, 0)
        self.assertEqual(k.syms['KLIPPER_MMU_PINS_TOOLHEAD'].str_value, '')
        self.assertEqual(k.syms['KLIPPER_MMU_TOOLHEAD_PULL'].visibility, 0)
        self.compile(); p = self.capture()
        self.assertEqual(p['result'], 0)
        self.assertEqual(p['pins'][7], [65535] * MAX_LANES)  # no dummy toolhead pin
        self.assertEqual(p['claims'], [c for c in fitted['claims'] if c[1] != 7])
        self.assertEqual(len(p['claims']), len(fitted['claims']) - 1)
        self.compile(switches=True); s = self.capture()
        self.assertEqual(s['result'], 0)
        self.assertEqual(s['inputs'][MAX_LANES + 1], [65535, 0, 0])
        self.assertEqual(s['inputs'][:MAX_LANES + 1] + s['inputs'][MAX_LANES + 2:],
                         fitted_switches['inputs'][:MAX_LANES + 1] + fitted_switches['inputs'][MAX_LANES + 2:])
        self.assertEqual(s['load'], fitted_switches['load'])
        self.compile(resources=True); r = self.capture()
        self.assertEqual(r['result'], 0)
        self.assertEqual(len(r['claims']), len(fitted_resources['claims']) - 1)

    def test_emu_virtual_hub_uses_one_load_claim(self):
        values = ('CONFIG_KLIPPER_MMU_DEVICE_EMU=y\n'
                  'CONFIG_KLIPPER_MMU_LANE_COUNT=1\n'
                  'CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y\n'
                  'CONFIG_KLIPPER_MMU_PINS_STEP="PA0"\n'
                  'CONFIG_KLIPPER_MMU_PINS_DIR="PA1"\n'
                  'CONFIG_KLIPPER_MMU_PINS_ENABLE="PA2"\n'
                  'CONFIG_KLIPPER_MMU_PINS_UART="PA3"\n'
                  'CONFIG_KLIPPER_MMU_PINS_BAY="PA4"\n'
                  'CONFIG_KLIPPER_MMU_PINS_LOAD="PA5"\n'
                  'CONFIG_KLIPPER_MMU_PINS_HUB=""\n'
                  'CONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PA6"\n'
                  'CONFIG_KLIPPER_MMU_PINS_BUFFER_A="PA7"\n'
                  'CONFIG_KLIPPER_MMU_PINS_BUFFER_B="PA8"\n'
                  'CONFIG_KLIPPER_MMU_LOAD_ACTIVE_HIGH="0"\n'
                  'CONFIG_KLIPPER_MMU_LOAD_PULL="-1"\n'
                  'CONFIG_KLIPPER_MMU_BAY_ACTIVE_HIGH="1"\n'
                  'CONFIG_KLIPPER_MMU_BAY_PULL="0"\n'
                  'CONFIG_KLIPPER_MMU_TOOLHEAD_ACTIVE_HIGH=1\n'
                  'CONFIG_KLIPPER_MMU_TOOLHEAD_PULL=1\n'
                  'CONFIG_KLIPPER_MMU_BUFFER_ACTIVE_HIGH="0,1"\n'
                  'CONFIG_KLIPPER_MMU_BUFFER_PULL="0,0"\n'
                  'CONFIG_KLIPPER_MMU_BUFFER_LOW_A=y\n')
        k, saved = self.configure(values)
        self.assertEqual(k.syms['KLIPPER_MMU_LOAD_SENSORS'].str_value, 'y')
        # EMU is folded into KLIPPER_MMU_PROFILE_IMPLEMENTED (mcu/Kconfig.
        # devices; owner follow-up 2026-09-26: its "(planned)" label was
        # stale -- see docs/plan/device-and-stm32-rollout.md's EMU row).
        # Every pin claim checked below is unaffected: PROFILE_IMPLEMENTED
        # only ever gated mcu/config.c's runtime profile build, not this
        # native pin-claim capture.
        self.assertEqual(k.syms['KLIPPER_MMU_PROFILE_IMPLEMENTED'].str_value, 'y')
        self.assertEqual(k.syms['KLIPPER_MMU_LANE_COUNT'].str_value, '1')
        self.assertIn('CONFIG_KLIPPER_MMU_PINS_LOAD="PA5"', saved)
        self.compile(); pins = self.capture()
        self.assertEqual(pins['result'], 0)
        self.assertEqual(pins['pins'][6][0], 65535)
        self.assertEqual(pins['pins'][16][0], 5)
        self.assertEqual(sum(c == [5, 16] for c in pins['claims']), 1)
        self.assertFalse(any(c[1] == 6 for c in pins['claims']))
        self.compile(switches=True); switches = self.capture()
        self.assertEqual(switches['result'], 0)
        self.assertEqual(switches['inputs'][MAX_LANES], [65535, 0, 0])
        self.assertEqual(switches['load'][0], [5, -1, 0])
        self.configure(values + 'CONFIG_KLIPPER_MMU_PINS_LOAD="PA4"\n')
        self.compile(); self.assertEqual(self.capture()['result'], -5)
        preset = values.replace('CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y\n',
                                'CONFIG_KLIPPER_MMU_BOARD_AFC_LITE=y\n').replace(
                                    'CONFIG_KLIPPER_MMU_PINS_HUB=""\n', '')
        k, saved = self.configure(preset)
        self.assertEqual(k.syms['KLIPPER_MMU_PINS_HUB'].str_value, '')
        self.assertNotIn('CONFIG_KLIPPER_MMU_PINS_HUB="PC4"', saved)
        self.compile(); self.assertEqual(self.capture()['result'], 0)


if __name__ == '__main__':
    unittest.main()
