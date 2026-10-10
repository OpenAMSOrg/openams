# SPDX-License-Identifier: GPL-3.0-or-later
"""EMU's five-lane virtual-hub profile is separate from startup qualification."""
import importlib.util
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("km_config_tests", ROOT / "tests/test_config.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Base = module.ConfigTest


class EmuPlannedTest(unittest.TestCase):
    setUpClass = classmethod(Base.setUpClass.__func__)
    restore_environment = classmethod(Base.restore_environment.__func__)
    configure = Base.configure

    def test_hosted_startup_gates_are_selectable_for_emu(self):
        k, _ = self.configure(
            'CONFIG_KLIPPER_MMU_DEVICE_EMU=y\n'
            'CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y\n'
            'CONFIG_KLIPPER_MMU_PIN_CONFIG=y\n'
            'CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H723=y\n'
            'CONFIG_KLIPPER_MMU_DIAGNOSTIC_STARTUP=y\n')
        for name in ('WANT_GPIO_LOCAL', 'WANT_TMCUART_LOCAL',
                     'KLIPPER_MMU_TMC_UART_SHARED',
                     'KLIPPER_MMU_DIAGNOSTIC_STARTUP'):
            self.assertEqual(k.syms[name].str_value, 'y', name)
        self.assertEqual(k.syms['KLIPPER_MMU_ACTUATION_OPT_IN'].str_value, 'n')
        self.assertEqual(k.syms['KLIPPER_MMU_PINS_HUB'].str_value, '')

    def test_emu_profile_uses_load_as_logical_hub(self):
        k, saved = self.configure(
            'CONFIG_KLIPPER_MMU_DEVICE_EMU=y\n'
            'CONFIG_KLIPPER_MMU_PIN_CONFIG=y\n'
            'CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y\n'
            'CONFIG_KLIPPER_MMU_LOAD_SENSORS=n\n'
            'CONFIG_KLIPPER_MMU_POWERED_REWIND=y\n'
            'CONFIG_KLIPPER_MMU_PINS_LOAD="PA5"\n')
        # EMU is folded into KLIPPER_MMU_PROFILE_IMPLEMENTED (mcu/Kconfig.
        # devices; owner follow-up 2026-09-26: its "(planned)" label was
        # stale). km_config_profile succeeding below no longer needs an
        # EMU-only bypass around that gate -- it is the ordinary path every
        # other implemented device takes.
        self.assertEqual(k.syms['KLIPPER_MMU_PROFILE_IMPLEMENTED'].str_value, 'y')
        self.assertEqual(k.syms['KLIPPER_MMU_LANE_COUNT'].str_value, '5')
        self.assertEqual(k.syms['KLIPPER_MMU_LOAD_SENSORS'].str_value, 'y')
        self.assertEqual(k.syms['KLIPPER_MMU_POWERED_REWIND'].str_value, 'n')
        self.assertIn('CONFIG_KLIPPER_MMU_PINS_LOAD="PA5"', saved)
        fixture = self.work / 'emu_planned.c'
        fixture.write_text('''
#include "config.h"
#include <assert.h>
#include <string.h>
int main(void) {
    km_device_profile_t p;
    assert(km_config_profile(&p, 0) == KM_OK);
    assert(p.lane_count == 5);
    assert(p.has_load_sensors);
    assert(p.has_hub_sensor);
    assert(p.load_is_virtual_hub);
    assert(!p.has_powered_rewind);
    return 0;
}
''')
        binary = self.work / 'emu_planned'
        command = ['gcc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                   '-I' + str(self.work),
                   '-I' + str(self.product / 'tests/config'),
                   '-I' + str(self.product / 'mcu'),
                   '-I' + str(self.product / 'include'),
                   str(self.product / 'mcu/config.c'),
                   str(self.product / 'src/device_profile.c'),
                   str(self.product / 'src/buffer_sensor.c'),
                   str(fixture), '-o', str(binary)]
        built = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(built.returncode, 0, built.stderr)
        run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_other_planned_profile_still_fails_without_mutating_output(self):
        # BTT ViViD and CLAYMORE both became implemented (INDEXED selector
        # work was separate scope); OPENAMS (existing OpenAMS host/transport
        # integration is separate from a local MCU device driver -- see
        # tests/test_config.py's own use of it for the same reason) is the
        # current still-planned example.
        k, _ = self.configure('CONFIG_KLIPPER_MMU_DEVICE_OPENAMS=y\n')
        self.assertEqual(k.syms['KLIPPER_MMU_PROFILE_IMPLEMENTED'].str_value, 'n')
        fixture = self.work / 'other_planned.c'
        fixture.write_text('''
#include "config.h"
#include <assert.h>
#include <string.h>
int main(void) {
    km_device_profile_t p, before;
    memset(&p, 0xa5, sizeof(p)); before = p;
    assert(km_config_profile(&p, 0) == KM_ERR_NOT_SUPPORTED);
    assert(memcmp(&p, &before, sizeof(p)) == 0);
    return 0;
}
''')
        binary = self.work / 'other_planned'
        built = subprocess.run([
            'gcc', '-std=c11', '-Wall', '-Wextra', '-Werror',
            '-I' + str(self.work), '-I' + str(self.product / 'tests/config'),
            '-I' + str(self.product / 'mcu'), '-I' + str(self.product / 'include'),
            str(self.product / 'mcu/config.c'),
            str(self.product / 'src/device_profile.c'),
            str(self.product / 'src/buffer_sensor.c'),
            str(fixture), '-o', str(binary)],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(built.returncode, 0, built.stderr)
        run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(EmuPlannedTest)


if __name__ == '__main__':
    unittest.main()
