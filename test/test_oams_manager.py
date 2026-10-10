# Manager-layer tests with a fake Klipper printer/config harness.
#
# These exercise the manager's config validation (force-load + topology) and the
# runtime group-edit commands without a real Klipper instance, by driving the
# methods on a bare OAMSManager with fakes.
#
#   cd openams && python3 -m unittest discover -s test

import importlib
import os
import sys
import tempfile
import types
import unittest

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if "oamspkg" not in sys.modules:
    _pkg = types.ModuleType("oamspkg")
    _pkg.__path__ = [_SRC]
    sys.modules["oamspkg"] = _pkg
sys.modules.setdefault("mcu", types.ModuleType("mcu"))

S = importlib.import_module("oamspkg.oams_state")
T = importlib.import_module("oamspkg.oams_topology")
manager_mod = importlib.import_module("oamspkg.oams_manager")
filament_group_mod = importlib.import_module("oamspkg.filament_group")
OAMSManager = manager_mod.OAMSManager
FilamentGroup = filament_group_mod.FilamentGroup


class FakeError(Exception):
    pass


class FakeSection:
    def __init__(self, name):
        self._name = name

    def get_name(self):
        return self._name


class FakeConfig:
    def __init__(self, section_names):
        self._names = section_names

    def get_prefix_sections(self, prefix):
        return [FakeSection(n) for n in self._names if n.startswith(prefix)]

    def error(self, msg):
        return FakeError(msg)


class FakeOam:
    def __init__(self, idx, fps=None):
        self.oams_idx = idx
        self.fps_name = fps
        self.hub_hes_value = [0, 0, 0, 0]
        self.f1s_hes_value = [0, 0, 0, 0]
        self.filament_path_length = 600
        self.path_length_mm = 600.0
        self.protocol_version = None
        self.oams_load_spool_cmd = None
        self._use_gen_protocol = False
        self._bay_links = [(None, "none") for _ in range(4)]

    def bay_link(self, bay):
        return self._bay_links[bay]

    def set_bay_link(self, bay, spool_id, source):
        self._bay_links[bay] = (spool_id, "none" if spool_id is None else source)

    def bay_filament(self, bay):
        values = getattr(self, "_bay_filaments", None)
        if values is None:
            return None
        return dict(values[bay]) if values[bay] else None

    def set_bay_filament(self, bay, material, temp_c, source):
        if not hasattr(self, "_bay_filaments"):
            self._bay_filaments = [None] * len(self.hub_hes_value)
        if material is None and temp_c is None:
            self._bay_filaments[bay] = None
            return
        self._bay_filaments[bay] = {"material": material, "temp_c": temp_c,
                                   "source": source}

    @property
    def firmware_owns_liveness(self):
        return self.protocol_version is not None and self.protocol_version >= 3

    @property
    def connected(self):
        return self.oams_load_spool_cmd is not None

    def protocol_summary(self):
        return "protocol=%s" % (self.protocol_version
                                if self.protocol_version is not None
                                else "legacy")

    def bay_loadable(self, bay_index):
        return bool(self.f1s_hes_value[bay_index])


class FakeFollower:
    """A [follower] unit as the manager sees it (single bay, switches)."""

    def __init__(self, idx, fps=None, path_length=500.0):
        self.oams_idx = idx
        self.fps_name = fps
        self.f1s_hes_value = [0, 0, 0, 0]     # [pre, -, -, -]
        self.hub_hes_value = [0, 0, 0, 0]     # [post, -, -, -]
        self.path_length_mm = path_length
        self.filament_path_length = path_length
        self.protocol_version = 1
        self.fps_stale = False
        self.ff_underrun = False
        self._connected = True
        self.follower_calls = []

    @property
    def connected(self):
        return self._connected

    @property
    def firmware_owns_liveness(self):
        return True

    def protocol_summary(self):
        return "protocol=FOLLOWER v1"

    def bay_loadable(self, bay_index):
        return bool(self.f1s_hes_value[bay_index])

    def set_oams_follower(self, enable, direction):
        self.follower_calls.append((enable, direction))


class FakeFps:
    def __init__(self, name):
        self.fps_name = name
        self.extruder = None

    def get_status(self, eventtime):
        value = getattr(self, "get_value", lambda: None)()
        return {"fps_value": value}


class FakeGroup:
    def __init__(self, bay_specs):
        self.bay_specs = bay_specs


class FakeConfigfile:
    def __init__(self):
        self.sets = []

    def set(self, section, option, value):
        self.sets.append((section, option, value))


class FakePrinter:
    def __init__(self, objects, configfile):
        # objects: {module: [(full_name, obj), ...]}
        self._objects = objects
        self._configfile = configfile
        self.loaded = []
        self.command_error = FakeError

    def load_object(self, config, name):
        self.loaded.append(name)

    def lookup_objects(self, module):
        return list(self._objects.get(module, []))

    def lookup_object(self, name):
        if name == "configfile":
            return self._configfile
        raise KeyError(name)


class FakeGcmd:
    def __init__(self, params):
        self._p = params
        self.responses = []

    def get(self, key, default="__required__"):
        if key in self._p:
            return self._p[key]
        if default == "__required__":
            raise FakeError("missing %s" % key)
        return default

    def get_int(self, key, minval=None, maxval=None):
        return int(self._p[key])

    def error(self, msg):
        return FakeError(msg)

    def respond_info(self, msg):
        self.responses.append(msg)


class FakeRuntime:
    def __init__(self, lanes):
        self._sys = S.SystemState(lanes=lanes)

    def get_state(self):
        return self._sys


DEFAULT_GROUPS_FILE = """\
# OpenAMS groups
[filament_group T0]
group: oams1-0

[filament_group T1]
group: oams1-1
"""


def build_manager(objects, section_names, lanes=None,
                  groups_file=DEFAULT_GROUPS_FILE):
    """A bare manager with topology built from fakes, ready for edit commands.
    Group edits are persisted to a real temp file so the writeback is exercised
    end to end; the path is on mgr.openams_config_path."""
    mgr = OAMSManager.__new__(OAMSManager)
    mgr.config = FakeConfig(section_names)
    mgr.printer = FakePrinter(objects, FakeConfigfile())
    mgr.reactor = types.SimpleNamespace(monotonic=lambda: 0.0)
    mgr._build_topology()
    if lanes is None:
        lanes = {fps: S.LaneState() for fps in mgr.topo.fps_names}
    mgr.runtime = FakeRuntime(lanes)
    fd, path = tempfile.mkstemp(suffix=".cfg")
    os.write(fd, groups_file.encode())
    os.close(fd)
    mgr.openams_config_path = path
    return mgr


def read_file(path):
    with open(path) as f:
        return f.read()


def single_lane_objects():
    return {
        "fps": [("fps", FakeFps("fps1"))],
        "oams": [("oams oams1", FakeOam(1))],
        "filament_group": [
            ("filament_group T0", FakeGroup([("oams1", 0)])),
            ("filament_group T1", FakeGroup([("oams1", 1)])),
        ],
    }


def single_lane_sections():
    return ["fps", "oams oams1", "filament_group T0", "filament_group T1",
            "oams_manager"]


class BuildTopologyTests(unittest.TestCase):
    def test_valid_single_lane(self):
        mgr = build_manager(single_lane_objects(), single_lane_sections())
        self.assertEqual(mgr.topo.lane_of_oams("oams1"), "fps1")
        self.assertEqual(mgr.group_lane["T0"], "fps1")
        self.assertEqual(mgr.groups_by_lane["fps1"]["T1"], ((1, 1),))
        self.assertEqual([o.oams_idx for o in mgr.lane_oams["fps1"]], [1])
        # every fps/oams/filament_group section was force-loaded
        self.assertIn("oams oams1", mgr.printer.loaded)
        self.assertIn("filament_group T0", mgr.printer.loaded)

    def test_invalid_group_reference_is_config_error(self):
        objs = single_lane_objects()
        objs["filament_group"] = [
            ("filament_group T0", FakeGroup([("oamsX", 0)]))]
        with self.assertRaises(FakeError) as cm:
            build_manager(objs, single_lane_sections())
        self.assertIn("unknown unit", str(cm.exception))

    def test_order_independence(self):
        # filament_group sections listed BEFORE the oams section: must still
        # validate, because the group is self-contained and the manager
        # force-loads + validates centrally.
        objs = single_lane_objects()
        sections = ["filament_group T0", "filament_group T1", "fps",
                    "oams oams1", "oams_manager"]
        mgr = build_manager(objs, sections)
        self.assertEqual(mgr.group_lane["T0"], "fps1")

    def test_multi_lane_requires_explicit_fps(self):
        objs = {
            "fps": [("fps a", FakeFps("a")), ("fps b", FakeFps("b"))],
            "oams": [("oams oams1", FakeOam(1, fps=None))],
            "filament_group": [],
        }
        with self.assertRaises(FakeError) as cm:
            build_manager(objs, ["fps a", "fps b", "oams oams1"])
        self.assertIn("must set 'fps:'", str(cm.exception))


class GroupEditTests(unittest.TestCase):
    def test_create_group_persists_empty(self):
        mgr = build_manager(single_lane_objects(), single_lane_sections())
        mgr.cmd_CREATE_GROUP(FakeGcmd({"GROUP": "NEW"}))
        self.assertIn("NEW", mgr.topo.groups)
        text = read_file(mgr.openams_config_path)
        self.assertIn("[filament_group NEW]", text)
        # original sections preserved
        self.assertIn("[filament_group T0]", text)
        self.assertIn("# OpenAMS groups", text)

    def test_assign_bay_moves_and_persists(self):
        mgr = build_manager(single_lane_objects(), single_lane_sections())
        mgr.cmd_ASSIGN_BAY(FakeGcmd({"GROUP": "T0", "OAMS": "oams1", "BAY": 1}))
        # bay (oams1,1) moved from T1 into T0; model + file both updated
        self.assertIn(("oams1", 1), mgr.topo.groups["T0"])
        self.assertNotIn(("oams1", 1), mgr.topo.groups["T1"])
        self.assertEqual(mgr.groups_by_lane["fps1"]["T0"], ((1, 0), (1, 1)))
        text = read_file(mgr.openams_config_path)
        self.assertIn("group: oams1-0,oams1-1", text)   # T0 updated in place
        self.assertIn("[filament_group T1]\ngroup:", text)  # T1 emptied

    def test_assign_unknown_oams_errors(self):
        mgr = build_manager(single_lane_objects(), single_lane_sections())
        with self.assertRaises(FakeError):
            mgr.cmd_ASSIGN_BAY(
                FakeGcmd({"GROUP": "T0", "OAMS": "oamsX", "BAY": 0}))

    def test_edit_refused_while_lane_busy(self):
        lanes = {"fps1": S.LaneState(op=S.OP_LOADING, unit=(1, 0), op_gen=1)}
        mgr = build_manager(single_lane_objects(), single_lane_sections(),
                            lanes=lanes)
        with self.assertRaises(FakeError) as cm:
            mgr.cmd_ASSIGN_BAY(
                FakeGcmd({"GROUP": "T0", "OAMS": "oams1", "BAY": 1}))
        self.assertIn("busy", str(cm.exception))

    def test_edit_refused_while_group_loaded(self):
        lanes = {"fps1": S.LaneState(op=S.OP_LOADED, group="T0", unit=(1, 0))}
        mgr = build_manager(single_lane_objects(), single_lane_sections(),
                            lanes=lanes)
        with self.assertRaises(FakeError) as cm:
            mgr.cmd_UNASSIGN_BAY(
                FakeGcmd({"GROUP": "T0", "OAMS": "oams1", "BAY": 0}))
        self.assertIn("loaded", str(cm.exception))

    def test_unassign_bay_persists(self):
        mgr = build_manager(single_lane_objects(), single_lane_sections())
        mgr.cmd_UNASSIGN_BAY(
            FakeGcmd({"GROUP": "T0", "OAMS": "oams1", "BAY": 0}))
        self.assertEqual(mgr.topo.groups["T0"], ())
        text = read_file(mgr.openams_config_path)
        self.assertIn("[filament_group T0]\ngroup:", text)
        self.assertNotIn("oams1-0", text)

    def test_delete_group_removes_section(self):
        mgr = build_manager(single_lane_objects(), single_lane_sections())
        mgr.cmd_DELETE_GROUP(FakeGcmd({"GROUP": "T0"}))
        self.assertNotIn("T0", mgr.topo.groups)
        text = read_file(mgr.openams_config_path)
        self.assertNotIn("[filament_group T0]", text)
        self.assertIn("[filament_group T1]", text)      # sibling kept

    def test_write_failure_leaves_model_unchanged(self):
        mgr = build_manager(single_lane_objects(), single_lane_sections())
        mgr.openams_config_path = "/nonexistent_dir/oams.cfg"
        before = mgr.topo.groups
        with self.assertRaises(FakeError):
            mgr.cmd_ASSIGN_BAY(
                FakeGcmd({"GROUP": "T0", "OAMS": "oams1", "BAY": 1}))
        # file write failed -> running model untouched (no divergence)
        self.assertEqual(mgr.topo.groups, before)

    def test_edit_refused_while_donor_group_loaded(self):
        # T0 is loaded and printing; moving one of ITS bays into another group
        # via ASSIGN must be refused just like UNASSIGN/DELETE on T0 would be.
        lanes = {"fps1": S.LaneState(op=S.OP_LOADED, group="T0", unit=(1, 0))}
        mgr = build_manager(single_lane_objects(), single_lane_sections(),
                            lanes=lanes)
        with self.assertRaises(FakeError) as cm:
            mgr.cmd_ASSIGN_BAY(
                FakeGcmd({"GROUP": "T1", "OAMS": "oams1", "BAY": 0}))
        self.assertIn("loaded", str(cm.exception))
        self.assertIn(("oams1", 0), mgr.topo.groups["T0"])   # unchanged

    def test_edit_refused_while_destination_group_loaded(self):
        lanes = {"fps1": S.LaneState(op=S.OP_LOADED, group="T0", unit=(1, 0))}
        mgr = build_manager(single_lane_objects(), single_lane_sections(),
                            lanes=lanes)
        with self.assertRaises(FakeError) as cm:
            mgr.cmd_ASSIGN_BAY(
                FakeGcmd({"GROUP": "T0", "OAMS": "oams1", "BAY": 2}))
        self.assertIn("loaded", str(cm.exception))

    def test_edit_refused_when_group_section_lives_elsewhere(self):
        # The model knows T0 (defined in printer.cfg, say) but the writeback
        # file does not contain its section: appending a copy would create a
        # duplicate section Klipper rejects at restart, so the edit must be
        # refused and the model left unchanged.
        mgr = build_manager(single_lane_objects(), single_lane_sections(),
                            groups_file="# no group sections here\n")
        before = mgr.topo.groups
        with self.assertRaises(FakeError) as cm:
            mgr.cmd_ASSIGN_BAY(
                FakeGcmd({"GROUP": "T0", "OAMS": "oams1", "BAY": 2}))
        self.assertIn("openams_config_path", str(cm.exception))
        self.assertEqual(mgr.topo.groups, before)
        self.assertEqual(read_file(mgr.openams_config_path),
                         "# no group sections here\n")       # file untouched

    def test_create_still_allowed_when_file_lacks_groups(self):
        # Creating a genuinely NEW group appends; that is safe even when the
        # existing groups live in another file.
        mgr = build_manager(single_lane_objects(), single_lane_sections(),
                            groups_file="# no group sections here\n")
        mgr.cmd_CREATE_GROUP(FakeGcmd({"GROUP": "NEW"}))
        self.assertIn("[filament_group NEW]",
                      read_file(mgr.openams_config_path))


class SelfTestTests(unittest.TestCase):
    def _oam(self, idx, connected=True, protocol=3):
        o = FakeOam(idx)
        o.oams_load_spool_cmd = object() if connected else None
        o.protocol_version = protocol
        o._use_gen_protocol = protocol is not None and protocol >= 2
        o.f1s_hes_value = [1, 0, 0, 1]
        return o

    def test_selftest_pass(self):
        objs = single_lane_objects()
        oam = self._oam(1, connected=True, protocol=3)
        objs["oams"] = [("oams oams1", oam)]
        mgr = build_manager(objs, single_lane_sections())
        # FakeFps needs get_value/extruder for the report
        for fps in mgr.fpss.values():
            fps.get_value = lambda: 0.5
            fps.extruder_name = "extruder"
            fps.extruder = object()
        gcmd = FakeGcmd({})
        mgr.cmd_SELFTEST(gcmd)
        out = gcmd.responses[0]
        self.assertIn("RESULT: PASS", out)
        self.assertIn("protocol=3", out)

    def test_selftest_warns_on_disconnected_unit(self):
        objs = single_lane_objects()
        objs["oams"] = [("oams oams1", self._oam(1, connected=False))]
        mgr = build_manager(objs, single_lane_sections())
        for fps in mgr.fpss.values():
            fps.get_value = lambda: 0.0
            fps.extruder_name = "extruder"
            fps.extruder = object()
        gcmd = FakeGcmd({})
        mgr.cmd_SELFTEST(gcmd)
        self.assertIn("WARN", gcmd.responses[0])


class FakeGroupConfig:
    def __init__(self, name, value):
        self._name = name
        self._value = value

    def get_printer(self):
        return None

    def get_name(self):
        return self._name

    def get(self, key, default=None):
        # None models "option absent": Klipper's config.get then returns the
        # caller's default.
        if key == "group" and self._value is not None:
            return self._value
        return default

    def error(self, msg):
        return FakeError(msg)


class FilamentGroupParseTests(unittest.TestCase):
    def _parse(self, value):
        return FilamentGroup(
            FakeGroupConfig("filament_group T0", value)).bay_specs

    def test_basic_list(self):
        self.assertEqual(self._parse("oams1-0,oams1-3"),
                         [("oams1", 0), ("oams1", 3)])

    def test_whitespace_and_quotes_stripped(self):
        self.assertEqual(self._parse(' "oams1-0" , oams1-1 '),
                         [("oams1", 0), ("oams1", 1)])

    def test_empty_and_missing_value(self):
        self.assertEqual(self._parse(""), [])
        self.assertEqual(FilamentGroup(
            FakeGroupConfig("filament_group T0", None)).bay_specs, [])

    def test_oams_name_with_dash_uses_last_segment_as_bay(self):
        self.assertEqual(self._parse("unit-2-3"), [("unit-2", 3)])

    def test_bad_entry_raises(self):
        for bad in ("oams1", "-0", "oams1-x"):
            with self.assertRaises(FakeError, msg=repr(bad)):
                self._parse(bad)

    def test_bay_past_three_parses_and_the_topology_owns_the_range(self):
        # An 8-gate klipper-mmu selector machine's last gate. The unit's real
        # bay count is checked by oams_topology (test_group_bay_out_of_range).
        self.assertEqual(self._parse("box-7"), [("box", 7)])

    def test_roundtrip_with_group_config_value(self):
        # What the topology writes must re-parse to the same bay list.
        topo = T.build_topology(
            ["fps1"], [T.OamsSpec(name="unit-2", idx=1)],
            [("G", [("unit-2", 0), ("unit-2", 3)])])
        value = topo.group_config_value("G")
        self.assertEqual(self._parse(value), [("unit-2", 0), ("unit-2", 3)])


def mixed_lane_objects():
    """One OAMS + one follower on a single lane; T0 spans both (the follower
    bay is the runout spare)."""
    return {
        "fps": [("fps", FakeFps("fps1"))],
        "oams": [("oams oams1", FakeOam(1))],
        "follower": [("follower belay", FakeFollower(9))],
        "filament_group": [
            ("filament_group T0", FakeGroup([("oams1", 0), ("belay", 0)])),
        ],
    }


def mixed_lane_sections():
    return ["fps", "oams oams1", "follower belay", "filament_group T0",
            "oams_manager"]


class FollowerIntegrationTests(unittest.TestCase):
    def test_follower_merges_into_topology(self):
        mgr = build_manager(mixed_lane_objects(), mixed_lane_sections(),
                            groups_file="[filament_group T0]\n"
                                        "group: oams1-0,belay-0\n")
        self.assertEqual(mgr.topo.kind_of("belay"), "follower")
        self.assertEqual(mgr.oam_by_idx[9].oams_idx, 9)
        self.assertEqual(mgr.groups_by_lane["fps1"]["T0"], ((1, 0), (9, 0)))
        self.assertIn("follower belay", mgr.printer.loaded)  # force-loaded
        # follower lane resolved onto the sole [fps]
        self.assertEqual(mgr.oams["belay"].fps_name, "fps1")

    def test_world_uses_path_length_mm_for_all_units(self):
        mgr = build_manager(mixed_lane_objects(), mixed_lane_sections(),
                            groups_file="[filament_group T0]\n"
                                        "group: oams1-0,belay-0\n")
        mgr.reload_before = 0.0
        mgr.idle_timeout = None
        world = mgr.build_world(0.0)
        lw = world.lanes["fps1"]
        self.assertEqual(lw.path_len[1], 600.0)   # OAMS, already mm in fake
        self.assertEqual(lw.path_len[9], 500.0)   # follower path_length
        # follower switch states feed the (idx, 0) world entries
        self.assertIn((9, 0), lw.loaded)
        self.assertIn((9, 0), lw.ready)

    def test_world_asks_the_unit_rather_than_reading_f1s_directly(self):
        # A unit with no real BAY switch (e.g. a klipper-mmu unit falling back
        # to gate status) can answer bay_loadable() differently from its own
        # raw f1s_hes_value entry; build_world() must use that answer.
        objs = mixed_lane_objects()
        oam = objs["oams"][0][1]
        oam.f1s_hes_value = [0, 0, 0, 0]  # every raw reading falsy/unknown
        oam.bay_loadable = lambda bay: True
        mgr = build_manager(objs, mixed_lane_sections(),
                            groups_file="[filament_group T0]\n"
                                        "group: oams1-0,belay-0\n")
        mgr.reload_before = 0.0
        mgr.idle_timeout = None
        lw = mgr.build_world(0.0).lanes["fps1"]
        self.assertTrue(lw.ready[(1, 0)])

    def test_world_keeps_local_reservation_separate_from_hub(self):
        objs = mixed_lane_objects()
        oam = objs["oams"][0][1]
        oam.local_reserved_lane = 0
        mgr = build_manager(objs, mixed_lane_sections(),
                            groups_file="[filament_group T0]\n"
                                        "group: oams1-0,belay-0\n")
        mgr.reload_before = 0.0
        mgr.idle_timeout = None
        lw = mgr.build_world(0.0).lanes["fps1"]
        self.assertEqual(lw.reserved, {1: 0})
        self.assertFalse(lw.loaded[(1, 0)])

    def test_world_rejects_invalid_local_reservation(self):
        for invalid in (True, False, -1, 4, 0.0, "0"):
            with self.subTest(invalid=invalid):
                objs = mixed_lane_objects()
                objs["oams"][0][1].local_reserved_lane = invalid
                mgr = build_manager(objs, mixed_lane_sections(),
                                    groups_file="[filament_group T0]\n"
                                                "group: oams1-0,belay-0\n")
                mgr.reload_before = 0.0
                mgr.idle_timeout = None
                with self.assertRaisesRegex(RuntimeError,
                                            "invalid local reservation for oams1"):
                    mgr.build_world(0.0)

    def test_selftest_reports_follower(self):
        objs = mixed_lane_objects()
        objs["follower"][0][1].f1s_hes_value = [1, 0, 0, 0]
        mgr = build_manager(objs, mixed_lane_sections(),
                            groups_file="[filament_group T0]\n"
                                        "group: oams1-0,belay-0\n")
        for fps in mgr.fpss.values():
            fps.get_value = lambda: 0.5
            fps.extruder_name = "extruder"
            fps.extruder = object()
        # make the OAMS side healthy so only follower content is at play
        mgr.oams["oams1"].oams_load_spool_cmd = object()
        mgr.oams["oams1"].protocol_version = 3
        gcmd = FakeGcmd({})
        mgr.cmd_SELFTEST(gcmd)
        out = gcmd.responses[0]
        self.assertIn("FOLLOWER belay", out)
        self.assertIn("protocol=FOLLOWER v1", out)
        self.assertIn("pre(ready)=1", out)
        self.assertIn("RESULT: PASS", out)

    def test_selftest_warns_on_uncalibrated_follower_path(self):
        objs = mixed_lane_objects()
        objs["follower"] = [("follower belay",
                             FakeFollower(9, path_length=0.0))]
        mgr = build_manager(objs, mixed_lane_sections(),
                            groups_file="[filament_group T0]\n"
                                        "group: oams1-0,belay-0\n")
        for fps in mgr.fpss.values():
            fps.get_value = lambda: 0.5
            fps.extruder_name = "extruder"
            fps.extruder = object()
        mgr.oams["oams1"].oams_load_spool_cmd = object()
        gcmd = FakeGcmd({})
        mgr.cmd_SELFTEST(gcmd)
        self.assertIn("path_length is 0", gcmd.responses[0])

    def test_assign_bay_to_follower_persists_and_bay1_errors(self):
        mgr = build_manager(mixed_lane_objects(), mixed_lane_sections(),
                            groups_file="[filament_group T0]\n"
                                        "group: oams1-0,belay-0\n")
        mgr.cmd_CREATE_GROUP(FakeGcmd({"GROUP": "SOLO"}))
        mgr.cmd_ASSIGN_BAY(FakeGcmd({"GROUP": "SOLO", "OAMS": "belay",
                                     "BAY": 0}))
        self.assertIn(("belay", 0), mgr.topo.groups["SOLO"])
        self.assertNotIn(("belay", 0), mgr.topo.groups["T0"])   # moved
        self.assertIn("belay-0", read_file(mgr.openams_config_path))
        with self.assertRaises(FakeError) as cm:
            mgr.cmd_ASSIGN_BAY(FakeGcmd({"GROUP": "SOLO", "OAMS": "belay",
                                         "BAY": 1}))
        self.assertIn("single bay", str(cm.exception))

    def test_stop_followers_reaches_follower_units(self):
        mgr = build_manager(mixed_lane_objects(), mixed_lane_sections(),
                            groups_file="[filament_group T0]\n"
                                        "group: oams1-0,belay-0\n")
        calls = []
        mgr.oams["oams1"].set_oams_follower = \
            lambda e, d: calls.append(("oams1", e, d))
        mgr.runtime.dispatch = lambda action: None
        mgr._stop_followers(["fps1"])
        self.assertIn(("oams1", 0, S.FOLLOWER_REVERSE), calls)
        self.assertIn((0, S.FOLLOWER_REVERSE),
                      mgr.oams["belay"].follower_calls)

    def test_direct_stop_clears_only_confirmed_uncertain_units(self):
        mgr = build_manager(
            mixed_lane_objects(), mixed_lane_sections(),
            lanes={"fps1": S.LaneState(
                following=None, uncertain_follower_units=((1, 0), (9, 0)))},
            groups_file="[filament_group T0]\n"
                        "group: oams1-0,belay-0\n")
        actions = []
        mgr.runtime.dispatch = actions.append
        mgr.oams["oams1"].set_oams_follower = lambda e, d: \
            types.SimpleNamespace(follower_confirmed=False)

        mgr._stop_followers(["fps1"])
        applied = [a.unit for a in actions if isinstance(a, S.FollowerApplied)]
        self.assertEqual(applied, [(9, 0)])  # legacy callback returns None

        actions.clear()
        mgr.oams["oams1"].set_oams_follower = lambda e, d: \
            types.SimpleNamespace(follower_confirmed=True)
        mgr._stop_followers(["fps1"])
        applied = [a.unit for a in actions if isinstance(a, S.FollowerApplied)]
        self.assertEqual(applied, [(1, 0), (9, 0)])


    def test_stop_followers_continues_after_cleanup_errors(self):
        mgr = build_manager(mixed_lane_objects(), mixed_lane_sections(),
                            groups_file="[filament_group T0]\\n"
                                        "group: oams1-0,belay-0\\n")
        mgr.runtime.dispatch = lambda action: (_ for _ in ()).throw(
            FakeError("store stop failed"))
        mgr.oams["oams1"].set_oams_follower = \
            lambda e, d: (_ for _ in ()).throw(FakeError("unit stop failed"))

        mgr._stop_followers(["fps1"])

        self.assertIn((0, S.FOLLOWER_REVERSE),
                      mgr.oams["belay"].follower_calls)


class ToolheadSequenceTests(unittest.TestCase):
    def _manager(self):
        mgr = OAMSManager.__new__(OAMSManager)
        mgr.toolhead_load_macro = "LOAD_FILAMENT"
        mgr.toolhead_unload_macro = "UNLOAD_FILAMENT"
        return mgr

    def _reactor(self):
        state = {"now": 0.0}
        return types.SimpleNamespace(
            monotonic=lambda: state["now"],
            pause=lambda until: state.__setitem__("now", until))

    def test_toolhead_macro_waits_for_queued_motion(self):
        mgr = self._manager()
        scripts = []
        gcode = types.SimpleNamespace(run_script_from_command=scripts.append)
        mgr.printer = types.SimpleNamespace(
            lookup_object=lambda name: gcode if name == "gcode" else None)
        mgr._run_toolhead_macro("UNLOAD_FILAMENT")
        self.assertEqual(scripts, ["UNLOAD_FILAMENT", "M400"])

    def test_load_sequences_ams_then_extruder_macro(self):
        mgr = self._manager()
        calls = []
        mgr.cmd_LOAD_FILAMENT = lambda gcmd: calls.append("ams_load")
        mgr._run_toolhead_macro = lambda name: calls.append(name)
        mgr.cmd_LOAD_TO_TOOLHEAD(FakeGcmd({"GROUP": "T0"}))
        self.assertEqual(calls, ["ams_load", "LOAD_FILAMENT"])

    def test_load_callback_failure_leaves_successful_ams_load_intact(self):
        mgr = self._manager()
        calls = []
        mgr.cmd_LOAD_FILAMENT = lambda gcmd: calls.append("ams_load")
        mgr._run_toolhead_macro = lambda name: (_ for _ in ()).throw(
            FakeError("cold extrusion"))
        with self.assertRaises(FakeError) as cm:
            mgr.cmd_LOAD_TO_TOOLHEAD(FakeGcmd({"GROUP": "T0"}))
        self.assertEqual(calls, ["ams_load"])
        self.assertIn("remains loaded", str(cm.exception))

    def test_unload_reverse_follows_then_retracts_then_rewinds(self):
        mgr = self._manager()
        calls = []
        lane = S.LaneState(op=S.OP_LOADED, unit=(1, 0), group="T0",
                           following=True, direction=S.FOLLOWER_FORWARD)
        state = {"lane": lane}
        mgr.runtime = types.SimpleNamespace(
            get_state=lambda: S.SystemState(lanes={"fps1": state["lane"]}),
            dispatch=lambda action: (calls.append(action), state.__setitem__(
                "lane", S.LaneState(op=S.OP_LOADED, unit=(1, 0), group="T0",
                                    following=True,
                                    direction=S.FOLLOWER_REVERSE))))
        mgr.reactor = self._reactor()
        mgr._resolve_fps = lambda gcmd, prefer_op, required: "fps1"
        mgr._run_toolhead_macro = lambda name: calls.append(name)
        mgr.cmd_UNLOAD_FILAMENT = lambda gcmd: calls.append("ams_unload")
        mgr._stop_followers = lambda lanes: calls.append(("stop", lanes))

        mgr.cmd_UNLOAD_FROM_TOOLHEAD(FakeGcmd({"FPS": "fps1"}))

        self.assertIsInstance(calls[0], S.Follow)
        self.assertEqual((calls[0].fps, calls[0].enable, calls[0].direction),
                         ("fps1", 1, S.FOLLOWER_REVERSE))
        self.assertEqual(calls[1:], ["UNLOAD_FILAMENT", "ams_unload"])

    def test_unload_callback_failure_disarms_reverse_follower(self):
        mgr = self._manager()
        calls = []
        lane = S.LaneState(op=S.OP_LOADED, unit=(1, 0), group="T0")
        state = {"lane": lane}
        mgr.runtime = types.SimpleNamespace(
            get_state=lambda: S.SystemState(lanes={"fps1": state["lane"]}),
            dispatch=lambda action: (calls.append(action), state.__setitem__(
                "lane", S.LaneState(op=S.OP_LOADED, unit=(1, 0), group="T0",
                                    following=True,
                                    direction=S.FOLLOWER_REVERSE))))
        mgr.reactor = self._reactor()
        mgr._resolve_fps = lambda gcmd, prefer_op, required: "fps1"
        mgr._run_toolhead_macro = lambda name: (_ for _ in ()).throw(
            FakeError("toolhead macro failed"))
        mgr.cmd_UNLOAD_FILAMENT = lambda gcmd: calls.append("ams_unload")
        mgr._stop_followers = lambda lanes: calls.append(("stop", lanes))

        with self.assertRaises(FakeError):
            mgr.cmd_UNLOAD_FROM_TOOLHEAD(FakeGcmd({"FPS": "fps1"}))

        self.assertIsInstance(calls[0], S.Follow)
        self.assertEqual(calls[1], ("stop", ["fps1"]))
        self.assertNotIn("ams_unload", calls)

    def test_callback_macro_name_rejects_manager_commands(self):
        config = types.SimpleNamespace(
            get=lambda option, default: "OAMSM_UNLOAD_FILAMENT",
            error=lambda message: FakeError(message))
        with self.assertRaises(FakeError):
            OAMSManager._macro_name(config, "toolhead_unload_macro",
                                    "UNLOAD_FILAMENT")


class StartupHardwareResyncTests(unittest.TestCase):
    def test_build_world_rejects_lost_snapshot_before_reading_sensors(self):
        mgr = build_manager(single_lane_objects(), single_lane_sections())
        unit = mgr.oams["oams1"]
        unit.hardware_snapshot_ready = False
        unit.hub_hes_value = None
        unit.f1s_hes_value = None

        with self.assertRaisesRegex(RuntimeError,
                                    "hardware snapshot unavailable for oams1"):
            mgr.build_world(1.0)

    def test_commands_reject_lost_snapshot_but_allow_cleanup(self):
        mgr = build_manager(single_lane_objects(), single_lane_sections(),
                            lanes={"fps1": S.LaneState(
                                op=S.OP_LOADED, unit=(1, 0), group="T0")})
        unit = mgr.oams["oams1"]
        unit.hardware_snapshot_ready = False
        unit.hub_hes_value = None
        unit.f1s_hes_value = None
        calls = []
        mgr.runtime.dispatch = lambda action: calls.append(action)
        mgr.runtime.request = lambda *args: self.fail("motion requested")
        unit.set_oams_follower = lambda enable, direction: calls.append(
            (enable, direction))

        for command, params in (
                (mgr.cmd_LOAD_FILAMENT, {"GROUP": "T0"}),
                (mgr.cmd_UNLOAD_FILAMENT, {"FPS": "fps1"}),
                (mgr.cmd_FOLLOWER, {"ENABLE": "1", "DIRECTION": "1"}),
                (mgr.cmd_LOAD_TO_TOOLHEAD, {"GROUP": "T0"}),
                (mgr.cmd_UNLOAD_FROM_TOOLHEAD, {"FPS": "fps1"}),
                (mgr.cmd_CLEAR_ERRORS, {})):
            with self.assertRaisesRegex(FakeError, "snapshot unavailable"):
                command(FakeGcmd(params))
        self.assertTrue(all(not isinstance(action, S.Follow) or
                            action.enable == 0 for action in calls
                            if not isinstance(action, tuple)))
        self.assertTrue(any(isinstance(action, S.Follow) for action in calls))

        mgr.cmd_FOLLOWER(FakeGcmd({"ENABLE": "0", "DIRECTION": "1",
                                   "FPS": "fps1"}))
        mgr.cmd_LOAD_FILAMENT_CANCEL(FakeGcmd({"FPS": "fps1"}))

    def test_ambiguous_unload_stops_unknown_and_reverse_followers(self):
        mgr = OAMSManager.__new__(OAMSManager)
        mgr.oams = {}
        mgr.fpss = {name: object() for name in ("unknown", "forward", "reverse")}
        lanes = {
            "unknown": S.LaneState(op=S.OP_LOADED, following=None,
                                   direction=S.FOLLOWER_FORWARD),
            "forward": S.LaneState(op=S.OP_LOADED, following=True,
                                   direction=S.FOLLOWER_FORWARD),
            "reverse": S.LaneState(op=S.OP_LOADED, following=True,
                                   direction=S.FOLLOWER_REVERSE),
        }
        mgr.runtime = types.SimpleNamespace(
            get_state=lambda: S.SystemState(lanes=lanes))
        stopped = []
        mgr._stop_followers = lambda names: stopped.extend(names)

        with self.assertRaisesRegex(FakeError, "Multiple FPS"):
            mgr.cmd_UNLOAD_FILAMENT(FakeGcmd({}))
        self.assertEqual(stopped, ["unknown", "reverse"])

    def test_selftest_reports_unavailable_snapshot_without_sensor_reads(self):
        mgr = build_manager(single_lane_objects(), single_lane_sections())
        unit = mgr.oams["oams1"]
        unit.hardware_snapshot_ready = False
        unit.hub_hes_value = None
        unit.f1s_hes_value = None
        fps = mgr.fpss["fps1"]
        fps.get_value = lambda: 0.0
        fps.extruder_name = "extruder"
        fps.extruder = object()
        gcmd = FakeGcmd({})
        mgr.cmd_SELFTEST(gcmd)
        self.assertIn("bays ready=unavailable loaded=unavailable",
                      gcmd.responses[0])
        self.assertIn("sensor snapshot unavailable", gcmd.responses[0])

    def test_waits_for_real_snapshot_then_resyncs_exactly_once(self):
        mgr = OAMSManager.__new__(OAMSManager)
        unit = FakeOam(1)
        unit.hardware_snapshot_ready = False
        mgr.oams = {"ht": unit}
        mgr._startup_resync_pending = True
        mgr.ready = False
        actions = []
        mgr.runtime = types.SimpleNamespace(dispatch=actions.append)

        self.assertFalse(mgr._finish_startup_resync_if_ready())
        self.assertFalse(mgr.ready)
        self.assertEqual(actions, [])

        unit.hardware_snapshot_ready = True
        self.assertTrue(mgr._finish_startup_resync_if_ready())
        self.assertTrue(mgr.ready)
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], S.ClearErrors)
        self.assertTrue(actions[0].keep_in_flight)

        self.assertTrue(mgr._finish_startup_resync_if_ready())
        self.assertEqual(len(actions), 1)

    def test_monitor_waits_for_fresh_snapshot_after_runtime_drop(self):
        mgr = OAMSManager.__new__(OAMSManager)
        unit = FakeOam(1)
        unit.hardware_snapshot_ready = True
        mgr.oams = {"ht": unit}
        mgr.monitor_interval = 0.5
        mgr._startup_resync_pending = False
        mgr.ready = True
        calls = []
        mgr.runtime = types.SimpleNamespace(
            dispatch=lambda action: calls.append(type(action).__name__),
            tick=lambda: calls.append("tick"))
        mgr._ext_monitor = lambda now: calls.append("hook")

        unit.hardware_snapshot_ready = False
        unit.hub_hes_value = None
        unit.f1s_hes_value = None
        self.assertEqual(mgr._monitor(10.0), 10.5)
        self.assertFalse(mgr.ready)
        self.assertEqual(calls, [])
        self.assertEqual(mgr._monitor(10.5), 11.0)
        self.assertEqual(calls, [])

        unit.hub_hes_value = [0, 0, 0, 0]
        unit.f1s_hes_value = [0, 0, 0, 0]
        unit.hardware_snapshot_ready = True
        mgr._monitor(11.0)
        self.assertTrue(mgr.ready)
        self.assertEqual(calls, ["ClearErrors", "tick", "hook"])
        mgr._monitor(11.5)
        self.assertEqual(calls, ["ClearErrors", "tick", "hook",
                                 "tick", "hook"])

    def test_monitor_keeps_legacy_unit_without_snapshot_flag_running(self):
        mgr = OAMSManager.__new__(OAMSManager)
        mgr.oams = {"legacy": FakeOam(1)}
        mgr.monitor_interval = 0.5
        mgr._startup_resync_pending = False
        mgr.ready = True
        calls = []
        mgr.runtime = types.SimpleNamespace(tick=lambda: calls.append("tick"))
        mgr._ext_monitor = lambda now: None

        mgr._monitor(1.0)
        self.assertEqual(calls, ["tick"])
        self.assertTrue(mgr.ready)

    def test_monitor_refreshes_opt_in_unit_once_per_slow_interval(self):
        mgr = OAMSManager.__new__(OAMSManager)
        unit = FakeOam(1)
        unit.hardware_snapshot_ready = False
        mgr.oams = {"physical": unit}
        mgr.monitor_interval = 0.25
        mgr._startup_resync_pending = True
        mgr.ready = False
        clock = [10.0]
        mgr.reactor = types.SimpleNamespace(monotonic=lambda: clock[0])
        calls = []
        mgr.runtime = types.SimpleNamespace(
            dispatch=lambda action: calls.append("resync"),
            tick=lambda: calls.append("tick"))
        mgr._ext_monitor = lambda now: None

        def refresh():
            calls.append("refresh")
            # The capture waits on the same reactor; a nested timer must not
            # launch another query or run the world from old readings.
            self.assertEqual(mgr._monitor(clock[0]), clock[0] + 0.25)
            unit.hardware_snapshot_ready = True
            return True

        unit.refresh = refresh
        mgr._monitor(clock[0])
        self.assertEqual(calls, ["refresh", "resync", "tick"])
        clock[0] = 10.25
        mgr._monitor(clock[0])
        self.assertEqual(calls, ["refresh", "resync", "tick", "tick"])
        clock[0] = 11.0
        mgr._monitor(clock[0])
        self.assertEqual(calls.count("refresh"), 2)

    def test_failed_refresh_blocks_stale_world_until_success(self):
        mgr = OAMSManager.__new__(OAMSManager)
        unit = FakeOam(1)
        unit.hardware_snapshot_ready = True
        mgr.oams = {"physical": unit}
        mgr.monitor_interval = 0.5
        mgr._startup_resync_pending = False
        mgr.ready = True
        clock = [20.0]
        mgr.reactor = types.SimpleNamespace(monotonic=lambda: clock[0])
        calls = []
        mgr.runtime = types.SimpleNamespace(
            dispatch=lambda action: calls.append("resync"),
            tick=lambda: calls.append("tick"))
        mgr._ext_monitor = lambda now: None
        outcomes = [False, RuntimeError("capture failed"), True]

        def refresh():
            result = outcomes.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        unit.refresh = refresh
        mgr._monitor(clock[0])
        self.assertFalse(mgr.ready)
        self.assertEqual(calls, [])
        clock[0] = 20.5
        mgr._monitor(clock[0])
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(calls, [])
        clock[0] = 21.0
        with self.assertLogs(level="ERROR"):
            mgr._monitor(clock[0])
        self.assertEqual(calls, [])
        clock[0] = 22.0
        mgr._monitor(clock[0])
        self.assertTrue(mgr.ready)
        self.assertEqual(calls, ["resync", "tick"])

    def test_delayed_refresh_uses_completion_time_for_next_tick_and_hook(self):
        mgr = OAMSManager.__new__(OAMSManager)
        unit = FakeOam(1)
        unit.hardware_snapshot_ready = True
        mgr.oams = {"physical": unit}
        mgr.monitor_interval = 0.5
        mgr._startup_resync_pending = False
        mgr.ready = True
        clock = [30.0]
        mgr.reactor = types.SimpleNamespace(monotonic=lambda: clock[0])
        calls = []
        mgr.runtime = types.SimpleNamespace(tick=lambda: calls.append("tick"))
        mgr._ext_monitor = lambda now: calls.append(("hook", now))

        def refresh():
            calls.append("refresh")
            clock[0] += 5.0
            return True

        unit.refresh = refresh
        self.assertEqual(mgr._monitor(30.0), 35.5)
        self.assertEqual(calls, ["refresh", "tick", ("hook", 35.0)])
        self.assertEqual(mgr._snapshot_refresh_due["physical"], 36.0)

        clock[0] = 35.5
        self.assertEqual(mgr._monitor(35.5), 36.0)
        self.assertEqual(calls.count("refresh"), 1)


class RangedGcmd(FakeGcmd):
    """FakeGcmd plus Klipper's `minval`/`maxval` refusal. The shared FakeGcmd
    ignores those bounds (the group-edit tests rely on reaching the topology
    model), so the bay-spool tests opt in to prove the command declares them."""

    def get_int(self, key, minval=None, maxval=None):
        value = int(self._p[key])
        if minval is not None and value < minval:
            raise self.error("Value for %s must be at least %d" % (key, minval))
        if maxval is not None and value > maxval:
            raise self.error("Value for %s must be at most %d" % (key, maxval))
        return value


LINK_GROUPS_FILE = DEFAULT_GROUPS_FILE + "\n[oams oams1]\nptfe_length: 600\n"


class BaySpoolLinkTests(unittest.TestCase):
    """OAMSM_SET_BAY_SPOOL / OAMSM_CLEAR_BAY_SPOOL: bay<->spool link state and
    its persistence in the unit's [oams <name>] section (C3 §4, D2)."""

    def _manager(self, lanes=None):
        return build_manager(single_lane_objects(), single_lane_sections(),
                             lanes=lanes, groups_file=LINK_GROUPS_FILE)

    def test_set_link_persists_and_reports(self):
        mgr = self._manager()
        gcmd = FakeGcmd({"OAMS": "oams1", "BAY": "1", "SPOOL": "42",
                         "SOURCE": "tag"})
        mgr.cmd_SET_BAY_SPOOL(gcmd)
        self.assertEqual(mgr.oams["oams1"].bay_link(1), (42, "tag"))
        text = read_file(mgr.openams_config_path)
        self.assertIn("spool_id_1: 42", text)
        self.assertIn("link_source_1: tag", text)
        self.assertIn("ptfe_length: 600", text)      # sibling option kept
        self.assertIn("[oams oams1]", text)
        self.assertEqual(gcmd.responses[-1],
                         "Linked spool 42 to oams1 Spool 2 (tag).")

    def test_default_source_is_manual(self):
        mgr = self._manager()
        mgr.cmd_SET_BAY_SPOOL(
            FakeGcmd({"OAMS": "oams1", "BAY": "0", "SPOOL": "7"}))
        self.assertEqual(mgr.oams["oams1"].bay_link(0), (7, "manual"))
        self.assertIn("link_source_0: manual",
                      read_file(mgr.openams_config_path))

    def test_set_same_spool_moves_the_link(self):
        mgr = self._manager()
        mgr.cmd_SET_BAY_SPOOL(FakeGcmd({"OAMS": "oams1", "BAY": "1",
                                        "SPOOL": "42", "SOURCE": "tag"}))
        gcmd = FakeGcmd({"OAMS": "oams1", "BAY": "2", "SPOOL": "42",
                         "SOURCE": "manual"})
        mgr.cmd_SET_BAY_SPOOL(gcmd)
        self.assertEqual(mgr.oams["oams1"].bay_link(1), (None, "none"))
        self.assertEqual(mgr.oams["oams1"].bay_link(2), (42, "manual"))
        text = read_file(mgr.openams_config_path)
        self.assertNotIn("spool_id_1:", text)
        self.assertNotIn("link_source_1:", text)
        self.assertIn("spool_id_2: 42", text)
        self.assertIn("link_source_2: manual", text)
        self.assertEqual(
            gcmd.responses[-1],
            "Linked spool 42 to oams1 Spool 3 (manual). Moved from oams1 Spool 2.")

    def test_clear_removes_link_and_is_idempotent(self):
        mgr = self._manager()
        mgr.cmd_SET_BAY_SPOOL(FakeGcmd({"OAMS": "oams1", "BAY": "3",
                                        "SPOOL": "9", "SOURCE": "tag"}))
        gcmd = FakeGcmd({"OAMS": "oams1", "BAY": "3"})
        mgr.cmd_CLEAR_BAY_SPOOL(gcmd)
        self.assertEqual(mgr.oams["oams1"].bay_link(3), (None, "none"))
        text = read_file(mgr.openams_config_path)
        self.assertNotIn("spool_id_3:", text)
        self.assertNotIn("link_source_3:", text)
        self.assertIn("ptfe_length: 600", text)
        self.assertEqual(gcmd.responses[-1], "Cleared the spool link of oams1 Spool 4.")
        # Clearing an already-unlinked bay succeeds (idempotent).
        mgr.cmd_CLEAR_BAY_SPOOL(FakeGcmd({"OAMS": "oams1", "BAY": "3"}))

    def test_bad_arguments_change_nothing(self):
        mgr = self._manager()
        before = read_file(mgr.openams_config_path)
        cases = [
            (FakeGcmd, {"OAMS": "nope", "BAY": "0", "SPOOL": "1"}),
            (RangedGcmd, {"OAMS": "oams1", "BAY": "4", "SPOOL": "1"}),
            (RangedGcmd, {"OAMS": "oams1", "BAY": "0", "SPOOL": "0"}),
            (FakeGcmd, {"OAMS": "oams1", "BAY": "0", "SPOOL": "1",
                        "SOURCE": "bogus"}),
        ]
        for cls, params in cases:
            with self.assertRaises(FakeError):
                mgr.cmd_SET_BAY_SPOOL(cls(params))
        self.assertEqual(mgr.oams["oams1"].bay_link(0), (None, "none"))
        self.assertEqual(read_file(mgr.openams_config_path), before)

    def test_set_allowed_while_lane_busy(self):
        lanes = {"fps1": S.LaneState(op=S.OP_LOADING, unit=(1, 0), op_gen=1)}
        mgr = self._manager(lanes=lanes)
        mgr.cmd_SET_BAY_SPOOL(
            FakeGcmd({"OAMS": "oams1", "BAY": "0", "SPOOL": "5"}))
        self.assertEqual(mgr.oams["oams1"].bay_link(0), (5, "manual"))

    def test_refused_write_leaves_links_unchanged(self):
        # The unit's section is not in the OpenAMS file: the write is refused
        # and neither this bay nor the bay that held the spool changes.
        mgr = build_manager(single_lane_objects(), single_lane_sections())
        mgr.oams["oams1"].set_bay_link(2, 42, "tag")
        with self.assertRaises(Exception):
            mgr.cmd_SET_BAY_SPOOL(
                FakeGcmd({"OAMS": "oams1", "BAY": "1", "SPOOL": "42"}))
        self.assertEqual(mgr.oams["oams1"].bay_link(1), (None, "none"))
        self.assertEqual(mgr.oams["oams1"].bay_link(2), (42, "tag"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
