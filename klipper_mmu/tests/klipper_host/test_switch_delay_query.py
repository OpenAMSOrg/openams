# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from host import KlipperMMU


INSTANCE = 0x1020304050607080


def observer(reply=None):
    mmu = object.__new__(KlipperMMU)
    mmu._name = 'unit'
    mmu._printer = SimpleNamespace(command_error=ValueError)
    mmu._mcu_lock = threading.Lock()
    mmu._active = mmu._connected = True
    mmu._state = 'attached'
    mmu._instance = INSTANCE
    mmu._generation = 3
    mmu._challenge = 7
    mmu._switch_delay_query = None
    response = dict(challenge=8, instance_hi=INSTANCE >> 32,
                    instance_lo=INSTANCE & 0xffffffff, valid=1, delay_us=75,
                    count=3, max_us=92)
    if reply:
        response.update(reply)
    wrapper = SimpleNamespace(send=Mock(return_value=response))
    mmu._mcu = SimpleNamespace(lookup_query_command=Mock(return_value=wrapper))
    gcmd = SimpleNamespace(respond_info=Mock())
    return mmu, wrapper, gcmd


class SwitchDelayQueryTest(unittest.TestCase):
    def test_exact_reply_and_invalid_sample(self):
        mmu, wrapper, gcmd = observer()
        mmu.cmd_KLIPPER_MMU_SWITCH_DELAY(gcmd)
        wrapper.send.assert_called_once_with([8, INSTANCE >> 32,
                                              INSTANCE & 0xffffffff])
        self.assertIn('delay_us=75', gcmd.respond_info.call_args.args[0])
        self.assertIn('accepted_since_attach=3 max_delay_us=92',
                      gcmd.respond_info.call_args.args[0])
        wrapper.send.return_value = dict(challenge=9, instance_hi=INSTANCE >> 32,
                                         instance_lo=INSTANCE & 0xffffffff,
                                         valid=0, delay_us=0, count=0, max_us=0)
        mmu.cmd_KLIPPER_MMU_SWITCH_DELAY(gcmd)
        self.assertIn('accepted_since_attach=0 max_delay_us=0',
                      gcmd.respond_info.call_args.args[0])
        wrapper.send.return_value = dict(challenge=10, instance_hi=INSTANCE >> 32,
                                         instance_lo=INSTANCE & 0xffffffff,
                                         valid=0, delay_us=0, count=3, max_us=92)
        mmu.cmd_KLIPPER_MMU_SWITCH_DELAY(gcmd)
        self.assertIn('valid=False', gcmd.respond_info.call_args.args[0])
        self.assertIn('accepted_since_attach=3 max_delay_us=92',
                      gcmd.respond_info.call_args.args[0])

    def test_rejects_wrong_challenge_instance_or_generation(self):
        for changed in ({'challenge': 9}, {'instance_lo': 0},
                        {'valid': 2}, {'delay_us': 0x80000000},
                        {'count': 0}, {'max_us': 50}, {'max_us': 0x80000000}):
            mmu, _, gcmd = observer(changed)
            with self.assertRaisesRegex(ValueError, 'invalid switch delay reply'):
                mmu.cmd_KLIPPER_MMU_SWITCH_DELAY(gcmd)
            gcmd.respond_info.assert_not_called()
        mmu, wrapper, gcmd = observer()
        def reconnect(_args):
            mmu._generation += 1
            return dict(challenge=8, instance_hi=INSTANCE >> 32,
                        instance_lo=INSTANCE & 0xffffffff, valid=1, delay_us=75,
                        count=3, max_us=92)
        wrapper.send.side_effect = reconnect
        with self.assertRaisesRegex(ValueError, 'connection changed'):
            mmu.cmd_KLIPPER_MMU_SWITCH_DELAY(gcmd)
        gcmd.respond_info.assert_not_called()

    def test_refuses_unavailable_query(self):
        mmu, wrapper, gcmd = observer()
        mmu._connected = False
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            mmu.cmd_KLIPPER_MMU_SWITCH_DELAY(gcmd)
        wrapper.send.assert_not_called()

    def test_challenge_wraps_to_one_and_rejects_a_pre_wrap_echo(self):
        mmu, wrapper, gcmd = observer({'challenge': 0xffffffff})
        mmu._challenge = 0xfffffffe
        mmu.cmd_KLIPPER_MMU_SWITCH_DELAY(gcmd)
        # The next exchange uses 1, never 0; the old echo does not match it.
        with self.assertRaisesRegex(ValueError, 'invalid switch delay reply'):
            mmu.cmd_KLIPPER_MMU_SWITCH_DELAY(gcmd)
        wrapper.send.return_value = dict(wrapper.send.return_value, challenge=2)
        mmu.cmd_KLIPPER_MMU_SWITCH_DELAY(gcmd)
        self.assertEqual([call.args[0][0] for call in wrapper.send.call_args_list],
                         [0xffffffff, 1, 2])
        self.assertEqual(gcmd.respond_info.call_count, 2)


if __name__ == '__main__':
    unittest.main()
