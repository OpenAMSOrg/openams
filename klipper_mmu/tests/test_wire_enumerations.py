# SPDX-License-Identifier: GPL-3.0-or-later
"""The host enumeration table against firmware exports and connect decisions."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from host import wire
from host.wire import ENUMERATIONS, check_enumerations, members

# Collects each DECL_ENUMERATION of mcu/enumerations.c instead of Klipper's
# compile-time request section; the entry names and values are unchanged.
SHIM = r'''#include <stdio.h>
#define KM_SHIM_JOIN2(a, b) a##b
#define KM_SHIM_JOIN(a, b) KM_SHIM_JOIN2(a, b)
#define DECL_ENUMERATION(ENUM, NAME, VALUE)                                  \
    static void __attribute__((constructor)) KM_SHIM_JOIN(km_enum_, __LINE__)(void) \
    { printf("%s %s %d\n", ENUM, NAME, (int)(VALUE)); }                      \
    _Static_assert(1, "declaration")
'''


def exported():
    return {name: members(namespace) for name, namespace in ENUMERATIONS.items()}


class EnumerationTableTest(unittest.TestCase):
    def test_exact_firmware_names_are_verified(self):
        firmware = exported()
        firmware['pin'] = {'PA0': 0}  # Klipper's own enumerations are ignored.
        firmware['km_event_type']['FUTURE'] = 19  # Unknown to this host.
        self.assertIs(check_enumerations(firmware), True)

    def test_image_without_them_is_legacy(self):
        self.assertIs(check_enumerations({}), False)
        self.assertIs(check_enumerations({'pin': {'PA0': 0}}), False)

    def test_missing_or_contradicting_names_are_rejected(self):
        cases = []
        firmware = exported()
        del firmware['km_stage']
        cases.append((firmware, 'km_stage STOPPED missing'))
        firmware = exported()
        del firmware['km_admission']['ALREADY_COMPLETED']
        cases.append((firmware, 'km_admission ALREADY_COMPLETED missing'))
        firmware = exported()
        firmware['km_op_state']['LOADED'] = 3
        cases.append((firmware, 'km_op_state LOADED is 3, host expects 2'))
        firmware = exported()
        firmware['km_client']['HOST'] = True
        cases.append((firmware, 'km_client HOST is True'))
        for firmware, reason in cases:
            with self.subTest(reason):
                with self.assertRaisesRegex(ValueError, reason):
                    check_enumerations(firmware)

    def test_names_leave_every_message_field_numeric(self):
        # Klipper's msgproto.lookup_params decodes a parameter through an
        # enumeration whose name equals the parameter or its `_name` suffix.
        formats = [value for value in vars(wire).values()
                   if isinstance(value, str) and value.startswith('km_')]
        formats += list(wire.STATUS_REPLIES) + list(wire.SENSOR_REPLIES)
        params = {arg.split('=')[0] for fmt in formats for arg in fmt.split()[1:]}
        self.assertIn('event_type', params)
        for enum in ENUMERATIONS:
            self.assertFalse([p for p in params
                              if p == enum or p.endswith('_' + enum)], enum)

    def test_firmware_exports_exactly_the_host_table(self):
        with tempfile.TemporaryDirectory(prefix='km-wire-enums-') as temp:
            work = Path(temp)
            (work / 'command.h').write_text(SHIM)
            (work / 'main.c').write_text('int main(void) { return 0; }\n')
            build = subprocess.run(
                ['gcc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-I', str(work),
                 '-I', str(ROOT / 'include'), '-I', str(ROOT / 'mcu'),
                 str(ROOT / 'mcu/enumerations.c'), str(work / 'main.c'),
                 '-o', str(work / 'enumerations')],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(work / 'enumerations')], capture_output=True,
                                 text=True, timeout=15, check=True)
        firmware = {}
        for line in run.stdout.splitlines():
            enum, name, value = line.split()
            self.assertNotIn(name, firmware.setdefault(enum, {}))
            firmware[enum][name] = int(value)
        self.assertEqual(firmware, exported())


if __name__ == '__main__':
    unittest.main()
