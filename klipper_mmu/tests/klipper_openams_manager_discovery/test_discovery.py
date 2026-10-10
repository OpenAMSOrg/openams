# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise the OPENAMS_SOURCE manager and FPS source with Klipper-MMU units."""

from pathlib import Path
import sys
import types
import unittest

from host.openams_readonly_unit import OpenAMSReadOnlyUnit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openams_source import load

S, manager_module, fps_module = load('oams_state', 'oams_manager', 'fps')


class FakeConfig:
    error = ValueError

    def __init__(self, names):
        self.sections = [types.SimpleNamespace(get_name=lambda name=name: name)
                         for name in names]

    def get_prefix_sections(self, prefix):
        assert prefix == ''
        return self.sections


class FakePrinter:
    def __init__(self, objects):
        self.objects = objects
        self.loaded = []

    def load_object(self, config, name):
        self.loaded.append(name)

    def lookup_objects(self, module):
        return self.objects.get(module, ())


def unit(index, fps='fps0', family='boxturtle', bays=4):
    return types.SimpleNamespace(oams_idx=index, fps_name=fps,
                                 device_family=family, bay_count=bays,
                                 motion_available=False,
                                 hardware_snapshot_ready=True)


def observer(openams_unit):
    return types.SimpleNamespace(get_openams_unit=lambda: openams_unit)


def gcode_command(**params):
    """The G-code object an OAMSM_* handler receives: error() plus the
    parameter readers Klipper always provides."""
    return types.SimpleNamespace(
        params=params, responses=[],
        get=lambda key, default=None: params.get(key, default),
        get_int=lambda key, default=None, minval=None, maxval=None:
            int(params[key]),
        error=ValueError,
        respond_info=lambda message: None)


class ManagerDiscovery(unittest.TestCase):
    manager = manager_module.OAMSManager

    def build(self, mmus=(), oams=(), followers=()):
        objects = dict(fps=[('fps fps0', types.SimpleNamespace(fps_name='fps0'))],
                       oams=oams, klipper_mmu=mmus, follower=followers,
                       filament_group=[])
        names = ['fps fps0'] + [name for bucket in (oams, mmus, followers)
                                for name, _device in bucket]
        obj = self.manager.__new__(self.manager)
        obj.config = FakeConfig(names)
        obj.printer = FakePrinter(objects)
        obj._build_topology()
        return obj

    def test_configured_mmu_is_loaded_and_uses_shared_topology(self):
        mmu = unit(7, family='boxturtle', bays=4)
        obj = self.build(mmus=[('klipper_mmu box', observer(mmu))])
        self.assertIn('klipper_mmu box', obj.printer.loaded)
        self.assertIs(obj.oams['box'], mmu)
        self.assertIs(obj.oam_by_idx[7], mmu)
        self.assertEqual(obj.topo.family_of('box'), 'boxturtle')
        self.assertEqual(obj.topo.bays_of('box'), 4)
        self.assertEqual(obj.topo.lane_of_oams('box'), 'fps0')

    def test_existing_oams_and_followers_remain_discoverable(self):
        legacy = unit(1, family='ams2', bays=4)
        follower = unit(2, family='inline_follower', bays=1)
        obj = self.build(oams=[('oams old', legacy)],
                         followers=[('follower spool', follower)])
        self.assertEqual(set(obj.oams), {'old', 'spool'})
        self.assertEqual(obj.topo.kind_of('spool'), 'follower')

    def test_unconfigured_observer_does_not_create_a_unit(self):
        obj = self.build(mmus=[('klipper_mmu box', observer(None))])
        self.assertEqual(obj.oams, {})
        self.assertIn('klipper_mmu box', obj.printer.loaded)

    def test_shared_name_and_index_conflicts_are_rejected(self):
        for mmu in (('klipper_mmu old', observer(unit(2))),
                    ('klipper_mmu box', observer(unit(1)))):
            with self.subTest(mmu=mmu[0]), self.assertRaises(ValueError):
                self.build(oams=[('oams old', unit(1))], mmus=[mmu])

    def test_read_only_unit_blocks_ready_and_command_preflight(self):
        obj = self.build(mmus=[('klipper_mmu box', observer(unit(7)))])
        obj._startup_resync_pending = True
        obj.ready = False
        obj.runtime = types.SimpleNamespace(
            dispatch=lambda action: self.fail('resync dispatched'))
        self.assertFalse(obj._finish_startup_resync_if_ready())
        self.assertFalse(obj.ready)
        with self.assertRaisesRegex(ValueError, 'motion unavailable for box'):
            obj._require_hardware_snapshot(
                types.SimpleNamespace(error=ValueError))

    def test_unload_stops_followers_before_motion_unavailable_error(self):
        for command in ('cmd_UNLOAD_FILAMENT', 'cmd_UNLOAD_FROM_TOOLHEAD'):
            with self.subTest(command=command):
                events = []
                obj = self.manager.__new__(self.manager)
                obj.oams = {'box': unit(7)}
                obj.fpss = {'fps0': object(), 'fps1': object()}
                obj._stop_followers = lambda fpss: events.append(
                    ('stop', tuple(fpss)))
                def error(message):
                    events.append(('error', message))
                    return ValueError(message)
                gcmd = gcode_command()
                gcmd.error = error
                with self.assertRaisesRegex(ValueError, 'motion unavailable for box'):
                    getattr(obj, command)(gcmd)
                self.assertEqual(events, [
                    ('stop', ('fps0', 'fps1')),
                    ('error', 'OpenAMS motion unavailable for box'),
                ])

    def test_real_configured_unit_is_discoverable_and_remains_read_only(self):
        physical = types.SimpleNamespace(physical_connection_identity=lambda: None)
        mmu = OpenAMSReadOnlyUnit('oams box', physical, 7, 'fps0',
                                  'boxturtle', 4, 842.5)
        obj = self.build(mmus=[('klipper_mmu box', observer(mmu))])
        self.assertIs(obj.oams['box'], mmu)
        self.assertIs(mmu.motion_available, False)
        obj._startup_resync_pending = True
        obj.ready = False
        obj.runtime = types.SimpleNamespace(
            dispatch=lambda action: self.fail('resync dispatched'))
        self.assertFalse(obj._finish_startup_resync_if_ready())
        with self.assertRaisesRegex(ValueError, 'motion unavailable for box'):
            obj._require_hardware_snapshot(
                types.SimpleNamespace(error=ValueError))
        with self.assertRaisesRegex(RuntimeError, 'motion is not available'):
            mmu.start_load_spool(0)
        with self.assertRaisesRegex(RuntimeError, 'fault recovery is not available'):
            mmu.clear_errors()
        with self.assertRaisesRegex(ValueError, 'resolved FPS'):
            mmu.fps_name = 'another_lane'
        self.assertEqual(mmu.fps_name, 'fps0')


    def test_read_only_reports_show_stale_mmu_feedback_as_unavailable(self):
        for value, shown in ((None, 'value=unavailable'), (0.375, 'value=0.375')):
            with self.subTest(value=value):
                fps = types.SimpleNamespace(
                    extruder_name='extruder', extruder=object(),
                    get_status=lambda now, value=value: {'fps_value': value})
                obj = self.manager.__new__(self.manager)
                obj.reactor = types.SimpleNamespace(monotonic=lambda: 12.5)
                # _webhook_status publishes the UI API v1 payload, which reads
                # the registered gcode commands through the printer.
                obj.printer = types.SimpleNamespace(
                    lookup_object=lambda name: types.SimpleNamespace(
                        get_status=lambda now: {'commands': {}}))
                obj.fpss, obj.oams, obj.ready, obj.group_lane = {'fps0': fps}, {}, True, {}
                obj.topo = types.SimpleNamespace(groups=[], fps_names=['fps0'])
                obj.runtime = types.SimpleNamespace(get_state=S.SystemState)
                sent, replies = {}, []
                obj._webhook_status(types.SimpleNamespace(send=sent.update))
                self.assertEqual(sent['status']['openams']['fps']['fps0']['value'], value)
                obj.cmd_SELFTEST(types.SimpleNamespace(respond_info=replies.append))
                self.assertIn('FPS fps0: ' + shown, replies[0])

    def test_toolhead_unload_macro_waits_for_confirmed_reverse_follow(self):
        for confirmed in (True, False):
            with self.subTest(confirmed=confirmed):
                state = dict(now=0., lane=S.LaneState(op=S.OP_LOADED, unit=(1, 0),
                                                      following=True))
                calls = []

                def dispatch(action):
                    calls.append(action)
                    state['lane'] = S.LaneState(
                        op=S.OP_LOADED, unit=(1, 0), following=confirmed or None,
                        direction=S.FOLLOWER_REVERSE)
                obj = self.manager.__new__(self.manager)
                obj.oams, obj.fpss, obj.toolhead_unload_macro = {}, {}, 'UNLOAD_FILAMENT'
                obj.reactor = types.SimpleNamespace(
                    monotonic=lambda: state['now'],
                    pause=lambda until: state.update(now=until))
                obj.runtime = types.SimpleNamespace(
                    dispatch=dispatch,
                    get_state=lambda: S.SystemState(lanes={'fps0': state['lane']}))
                obj._resolve_fps = lambda gcmd, prefer_op, required: 'fps0'
                obj._run_toolhead_macro = calls.append
                obj.cmd_UNLOAD_FILAMENT = lambda gcmd: calls.append('ams_unload')
                obj._stop_followers = lambda lanes: calls.append(('stop', lanes))
                gcmd = gcode_command()
                if confirmed:
                    obj.cmd_UNLOAD_FROM_TOOLHEAD(gcmd)
                    self.assertEqual(calls[1:], ['UNLOAD_FILAMENT', 'ams_unload'])
                    continue
                with self.assertRaisesRegex(ValueError, 'was not confirmed'):
                    obj.cmd_UNLOAD_FROM_TOOLHEAD(gcmd)
                self.assertEqual(calls[1:], [('stop', ['fps0'])])


class FPSSource(unittest.TestCase):
    def connect(self, source):
        class Printer:
            config_error = ValueError
            get_reactor = register_event_handler = lambda self, *args: None

            def lookup_object(self, name, default=None):
                return {'extruder': object(), 'klipper_mmu box': source}.get(name, default)

        class Config:
            error = ValueError
            get_printer = lambda self, printer=Printer(): printer
            get_name = lambda self: 'fps fps0'
            getint = getfloat = getboolean = lambda self, name, default=None: default

            def get(self, name, default=None):
                return 'klipper_mmu box' if name == 'source_oams' else default

        fps = fps_module.FPS(Config())
        fps._handle_connect()
        return fps

    def test_mmu_unit_is_the_source_and_stale_status_is_unavailable(self):
        unit = types.SimpleNamespace(fps_value=0.375)
        fps = self.connect(observer(unit))
        self.assertIs(fps.source_oams, unit)
        self.assertEqual(fps.get_status(0), {'fps_value': 0.375})
        fps._reversed = True
        self.assertEqual(fps.get_value(), 0.625)

        class Stale:
            @property
            def fps_value(self):
                raise ValueError('stale or invalid MMU buffer cache')

        fps.source_oams = Stale()
        self.assertEqual(fps.get_status(0), {'fps_value': None})
        with self.assertRaisesRegex(ValueError, 'stale or invalid'):
            fps.get_value()
        with self.assertRaisesRegex(ValueError, 'no OpenAMS unit'):
            self.connect(observer(None))


if __name__ == '__main__':
    unittest.main()
