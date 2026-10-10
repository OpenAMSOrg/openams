# SPDX-License-Identifier: GPL-3.0-or-later
import unittest

from host.openams_device_observation import observe_openams_device


def status(**overrides):
    value = {
        'connected': True, 'state': 'attached', 'instance': 0x123456789abcdef,
        'last_query': 9,
        'snapshot': {'instance': 0x123456789abcdef, 'query': 9, 'state': 0,
                     'operation': 0, 'lane_active': False,
                     'path_occupied': True, 'system_fault': 0,
                     'motion_fault': 0},
    }
    value.update(overrides)
    return value


class ObservationTest(unittest.TestCase):
    def test_reports_only_observed_controller_facts(self):
        observation = observe_openams_device(status())
        self.assertEqual(observation.instance, 0x123456789abcdef)
        self.assertEqual(observation.query, 9)
        self.assertTrue(observation.path_occupied)
        self.assertFalse(hasattr(observation, 'hardware_snapshot_ready'))
        self.assertFalse(hasattr(observation, 'f1s_hes_value'))
        self.assertFalse(hasattr(observation, 'hub_hes_value'))

    def test_disconnect_and_reboot_with_stale_snapshot_withhold_observation(self):
        old = status()['snapshot']
        self.assertIsNone(observe_openams_device(status(connected=False)))
        self.assertIsNone(observe_openams_device(
            status(instance=0x123456789abcdee, snapshot=old)))
        self.assertIsNone(observe_openams_device(status(snapshot=None)))

    def test_malformed_snapshot_withholds_observation(self):
        snap = status()['snapshot']
        snap['lane_active'] = 0
        self.assertIsNone(observe_openams_device(status(snapshot=snap)))
        snap['lane_active'] = False
        snap['query'] = True
        self.assertIsNone(observe_openams_device(status(snapshot=snap)))


if __name__ == '__main__':
    unittest.main()
