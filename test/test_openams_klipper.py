"""Real Klipper G-code/macro integration, with no hardware or service access,
against the pure-core OAMSManager.

Run with OPENAMS_KLIPPER_DIR pointing to a checkout containing KLIPPER_REV.
Sources are read from that commit, never from a user's modified working tree.

klipper_openams master compared these macros against a manager built straight
from its own old, monolithic OAMSManager (loaded from git history for the
comparison only). That manager's config/printer-object shape no longer exists
in this architecture (topology built from [fps]/[oams ...]/[filament_group]
sections, a runtime/reducer instead of direct driver calls), so the two
byte-trace/state comparison tests master ran here
(test_legacy_execution_matches_master and
test_current_macros_preserve_master_outcomes, which both need that old
manager to get a baseline) are dropped rather than reconstructed. What they
protected -- correct command ordering, pause/raise safety, and final state
per case -- is exercised directly against the current manager below instead
(the test_ui_* cases, the cold-extruder/unknown-group guards, and the paused-
toolchange/serial-vs-concurrent-path cases), consistent with
test_current_macros_preserve_master_outcomes's own docstring, which already
pins per-case properties "instead of the byte-for-byte trace."
"""
import configparser
import contextlib
import importlib
import os
import re
from pathlib import Path
import subprocess
import sys
import types
import unittest

try:
    import pytest
except ImportError:
    raise unittest.SkipTest(
        "pytest not installed; the klipper-integration CI job runs this module"
    )

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if "oamspkg" not in sys.modules:
    _pkg = types.ModuleType("oamspkg")
    _pkg.__path__ = [_SRC]
    sys.modules["oamspkg"] = _pkg
sys.modules.setdefault("mcu", types.ModuleType("mcu"))

S = importlib.import_module("oamspkg.oams_state")
T = importlib.import_module("oamspkg.oams_topology")
oams_manager = importlib.import_module("oamspkg.oams_manager")

ROOT = Path(__file__).resolve().parents[1]
KLIPPER_REV = "214fdb2877ff4640e9316a663c1b8b2e232c86fd"
CONTROLS = ("START", "END", "WAIT")


def committed_source(repo, revision, path):
    return subprocess.check_output(
        ["git", "-C", str(repo), "show", revision + ":" + path], text=True)


def module_from_source(name, source):
    module = types.ModuleType(name)
    module.__package__ = name.rpartition(".")[0]
    exec(compile(source, name, "exec"), module.__dict__)
    return module


@pytest.fixture(scope="module")
def klipper():
    directory = os.environ.get("OPENAMS_KLIPPER_DIR")
    if not directory:
        pytest.skip("set OPENAMS_KLIPPER_DIR for pinned Klipper integration")
    return types.SimpleNamespace(**{
        name: module_from_source(name, committed_source(directory, KLIPPER_REV, path))
        for name, path in (("gcode", "klippy/gcode.py"),
                           ("gcode_macro", "klippy/extras/gcode_macro.py"))
    })


class Reactor:
    NOW = 0.
    NEVER = 1.e30

    def __init__(self):
        self.now = 0.0
        self.timers = []
        # Called after every pause: the harness heats the heaters here, the
        # way time passing does on a printer.
        self.on_pause = None

    def monotonic(self):
        return self.now

    def pause(self, wake_time):
        self.now = wake_time
        if self.on_pause is not None:
            self.on_pause(wake_time)

    def mutex(self):
        return contextlib.nullcontext()

    def assert_no_pause(self):
        return contextlib.nullcontext()

    def register_timer(self, callback, when=NOW):
        timer = [callback, when]
        self.timers.append(timer)
        return timer

    def unregister_timer(self, timer):
        if timer in self.timers:
            self.timers.remove(timer)

    def completion(self):
        return Completion()

    def register_async_callback(self, callback):
        callback(self.now)


class Completion:
    """Every op this harness starts completes synchronously (the fake OAMS
    unit calls back into the runtime before start_load_spool/etc. return), so
    wait() never actually has to block."""

    def __init__(self):
        self._done = False
        self._result = None

    def test(self):
        return self._done

    def complete(self, result):
        self._done = True
        self._result = result

    def wait(self, *args):
        return self._result


class FakeSection:
    def __init__(self, name):
        self._name = name

    def get_name(self):
        return self._name


class Config:
    def __init__(self, printer, name, values=None):
        self.printer, self.name = printer, name
        self.values = values or {}
        self.error = ValueError

    def get_printer(self):
        return self.printer

    def get_name(self):
        return self.name

    def get(self, key, default=None):
        return self.values.get(key, default)

    def getfloat(self, key, default, minval=None, maxval=None, above=None):
        return float(self.get(key, default))

    def getboolean(self, key, default=None):
        value = self.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return value

    def get_prefix_sections(self, prefix):
        return [FakeSection(n) for n in self.printer.section_names
                if n.startswith(prefix)]

    def get_prefix_options(self, prefix):
        return [key for key in self.values if key.startswith(prefix)]


class Status:
    def __init__(self, **values):
        self.values = values

    def get_status(self, now):
        return dict(self.values)


class Printer:
    config_error = ValueError

    def __init__(self, klipper):
        self.objects = {}
        self.section_names = []
        self.events = {}
        self.trace = []
        self.received_controls = []
        self.reactor = Reactor()
        self.command_error = klipper.gcode.CommandError
        self.gcode = klipper.gcode.GCodeDispatch(self)
        self.objects["gcode"] = self.gcode
        self.objects["gcode_macro"] = klipper.gcode_macro.PrinterGCodeMacro(
            Config(self, "gcode_macro"))
        self.objects["toolhead"] = Status(homed_axes="xyz", extruder="extruder")
        self.objects["exclude_object"] = Status(current_object=None, excluded_objects=[])
        self.objects["filament_switch_sensor extruder_in"] = Status(filament_detected=True)
        self.objects["filament_switch_sensor extruder_out"] = Status(filament_detected=False)
        self.objects["extruder"] = Status(can_extrude=True, temperature=220.0, target=220.0)
        self.objects["pause_resume"] = Status(is_paused=False)
        # A print owns its own temperatures; the macros never change them.
        self.objects["print_stats"] = Status(state="standby")
        # Klipper publishes lowercase section names with their accessed options.
        self.objects["configfile"] = Status(settings={
            "filament_group t0": {"group": "unit1-0, unit1-1"},
            "filament_group t1": {"group": "unit1-2, unit1-3"},
            "extruder": {"max_temp": 300.0, "min_extrude_temp": 170.0},
            "extruder1": {"max_temp": 300.0, "min_extrude_temp": 170.0},
        })
        self.objects["webhooks"] = types.SimpleNamespace(register_endpoint=lambda *a: None)
        # Set to (target, temperature) to make the next G28 cool the extruder
        # the way a homing override that runs a probe calibration does.
        self.homing_cools = None
        self.reactor.on_pause = self.heat_up
        for command in ("G0", "G1", "G4", "G90", "M400", "M83",
                        "SET_STEPPER_ENABLE", "SAVE_GCODE_STATE", "RESTORE_GCODE_STATE",
                        "CLEAN_NOZZLE"):
            self.gcode.register_command(command, self.record)
        self.gcode.register_command("G28", self.home)
        self.gcode.register_command("SET_HEATER_TEMPERATURE", self.set_heater)
        self.gcode.register_command("TEMPERATURE_WAIT", self.temperature_wait)
        self.gcode.register_command("PAUSE", self.pause)
        self.gcode.register_command("RESPOND", lambda command: None)
        # Stock Klipper has no routine controls. Any rendered control is a bug.
        for command in CONTROLS:
            self.gcode.register_command(command, self.control)

    def record(self, command):
        self.trace.append(command.get_commandline())

    def home(self, command):
        self.record(command)
        self.objects["toolhead"].values["homed_axes"] = "xyz"
        if self.homing_cools is not None:
            target, temperature = self.homing_cools
            self.homing_cools = None
            extruder = self.objects["extruder"].values
            extruder.update(target=target, temperature=temperature,
                            can_extrude=temperature >= 170)

    def set_heater(self, command):
        self.record(command)
        self.objects[command.get("HEATER")].values["target"] = command.get_float("TARGET")

    def temperature_wait(self, command):
        self.record(command)
        values = self.objects[command.get("SENSOR")].values
        values.update(temperature=command.get_float("MINIMUM"), can_extrude=True)

    def heat_up(self, now):
        """Time passes during a heat wait: every heater reaches its target."""
        for obj in self.objects.values():
            values = getattr(obj, "values", None)
            if (isinstance(values, dict) and "target" in values
                    and values.get("temperature", 0) < values["target"]):
                values.update(temperature=values["target"], can_extrude=True)

    def spy_heat_wait(self):
        """Record every OAMSM_HEAT_WAIT line in the trace, then run it."""
        handlers = self.gcode.gcode_handlers
        inner = handlers["OAMSM_HEAT_WAIT"]

        def spy(command):
            self.record(command)
            return inner(command)
        handlers["OAMSM_HEAT_WAIT"] = spy

    def pause(self, command):
        self.record(command)
        self.objects["pause_resume"].values["is_paused"] = True

    def control(self, command):
        self.received_controls.append(command.get_commandline())
        raise AssertionError("stock Klipper received " + command.get_commandline())

    def enable_gco_routines(self):
        """Advertise a live extension and an ordered _TX without providing it."""
        self.objects["gco_routines"] = Status()
        self.objects["configfile"].values["settings"]["gcode_macro _tx"] = {
            "render_mode": "ordered"}

    def get_start_args(self):
        return {}

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def lookup_objects(self, module=None):
        return [(name, obj) for name, obj in self.objects.items()
                if module is None or name.split()[0] == module]

    def load_object(self, config, name):
        return self.objects[name]

    def register_event_handler(self, name, handler):
        self.events.setdefault(name, []).append(handler)

    def send_event(self, name):
        for handler in self.events.get(name, []):
            handler()

    def invoke_shutdown(self, message):
        raise AssertionError(message)


class Fps:
    def __init__(self, fps_name="fps", extruder_name="extruder"):
        self.fps_name = fps_name
        self.extruder_name = extruder_name
        self.extruder = types.SimpleNamespace(last_position=0.0)
        self.value = 0.5

    def get_status(self, now):
        return {"fps_value": self.value}


class Unit:
    """A single 4-bay OAMS unit whose firmware always replies synchronously
    (unlike real hardware), so runtime.request(...).wait() never blocks."""

    def __init__(self, printer, oams_idx=1, fps_name=None):
        self.printer = printer
        self.oams_idx = oams_idx
        self.fps_name = fps_name
        self.f1s_hes_value = [True] * 4
        self.hub_hes_value = [False] * 4
        self.path_length_mm = 600.0
        self.capabilities = {}
        self.current_spool = None
        self.load_result = 0
        self.unload_result = True
        self.connected = True
        self.firmware_owns_liveness = False
        # What the Spoolman component pushes for each bay (memory only).
        self.bay_filaments = [None] * 4

    def bay_loadable(self, bay):
        return bool(self.f1s_hes_value[bay])

    def bay_filament(self, bay):
        """The pushed {material, temp_c, source} of one bay, or None."""
        value = self.bay_filaments[bay]
        return dict(value) if value is not None else None

    def set_bay_filament(self, bay, material, temp_c, source):
        if material is None and temp_c is None:
            self.bay_filaments[bay] = None
            return
        self.bay_filaments[bay] = {"material": material, "temp_c": temp_c,
                                   "source": source}

    def get_webhook_status(self):
        return {"connected": True,
                "bays": [{"index": bay,
                          "ready": bool(self.f1s_hes_value[bay]),
                          "loaded": bool(self.hub_hes_value[bay]),
                          "calibration_state": "configured",
                          "spool_id": None,
                          "positioning": False,
                          "filament": self.bay_filament(bay)}
                         for bay in range(4)]}

    def clear_errors(self):
        pass

    def bind_runtime(self, runtime, fps_name):
        self.runtime = runtime
        self.fps_name = fps_name
        self.on_action_complete = (
            lambda code, value, gen: runtime.dispatch(
                S.OpCompleted(fps_name, code, value, gen=gen)))

    def start_load_spool(self, bay, gen=None):
        self.printer.trace.append("feed:%d" % bay)
        code = self.load_result or S.OAMS_OP_CODE_SUCCESS
        if code == S.OAMS_OP_CODE_SUCCESS:
            self.current_spool = bay
            self.hub_hes_value[bay] = True
        self.on_action_complete(code, None, gen)

    def start_unload_spool(self, gen=None):
        self.printer.trace.append("unload")
        if self.unload_result:
            self.current_spool = None
            self.hub_hes_value = [False] * len(self.hub_hes_value)
            code = S.OAMS_OP_CODE_SUCCESS
        else:
            code = S.OAMS_OP_CODE_ERROR_UNSPECIFIED
        self.on_action_complete(code, None, gen)

    def load_spool_cancel(self):
        self.printer.trace.append("cancel_load")

    def set_oams_follower(self, enable, direction):
        self.printer.trace.append("follower:%d:%d" % (enable, direction))


def load_macros(printer, klipper):
    """Load the sample variable section and every oams_macros.cfg macro."""
    sample = configparser.RawConfigParser(inline_comment_prefixes=("#", ";"))
    sample.read_string((ROOT / "oams_sample.cfg").read_text())
    name = "gcode_macro _oams_macro_variables"
    printer.objects[name] = klipper.gcode_macro.GCodeMacro(
        Config(printer, name, dict(sample[name])))
    config = configparser.RawConfigParser(inline_comment_prefixes=("#", ";"))
    config.read_string((ROOT / "oams_macros.cfg").read_text())
    for section in config.sections():
        printer.objects[section] = klipper.gcode_macro.GCodeMacro(
            Config(printer, section, dict(config[section])))


@pytest.fixture
def setup(klipper):
    def build(loaded=None, legacy_macros=False, legacy_sample=False):
        printer = Printer(klipper)
        unit = Unit(printer)
        if loaded is not None:
            unit.current_spool = loaded
            unit.hub_hes_value[loaded] = True
        printer.objects["fps"] = Fps()
        printer.objects["oams unit1"] = unit
        printer.objects["filament_group T0"] = types.SimpleNamespace(
            bay_specs=[("unit1", 0), ("unit1", 1)])
        printer.objects["filament_group T1"] = types.SimpleNamespace(
            bay_specs=[("unit1", 2), ("unit1", 3)])
        printer.section_names = ["fps", "oams unit1", "filament_group T0",
                                 "filament_group T1", "oams_manager"]
        manager = oams_manager.OAMSManager(Config(printer, "oams_manager"))
        printer.objects["oams_manager"] = manager
        load_macros(printer, klipper)
        printer.send_event("klippy:ready")
        printer.spy_heat_wait()
        # The fake reactor never fires its own timers; drive the startup
        # resync tick by hand so manager.ready becomes True, as the real
        # monitor timer would on the first real reactor iteration.
        manager._monitor(printer.reactor.monotonic())
        # A loaded unit at startup has its forward follower re-established on
        # that first tick (the restart case); that is the printer at rest, not
        # part of what a test runs, so the trace starts after it.
        printer.trace.clear()
        return printer, manager, unit
    return build


@pytest.fixture
def setup_two_lanes(klipper):
    """Two FPS lanes, each with its own extruder and one unit: lane1 on the
    default 'extruder' owns group T0 (unit1), lane2 on 'extruder1' owns group
    T1 (unit2). Nothing else differs from `setup`."""
    def build(loaded=None):
        printer = Printer(klipper)
        printer.objects["extruder1"] = Status(can_extrude=True, temperature=220.0, target=220.0)
        unit1 = Unit(printer, oams_idx=1, fps_name="lane1")
        unit2 = Unit(printer, oams_idx=2, fps_name="lane2")
        if loaded is not None:
            unit1.current_spool = loaded
            unit1.hub_hes_value[loaded] = True
        printer.objects["fps lane1"] = Fps("lane1", "extruder")
        printer.objects["fps lane2"] = Fps("lane2", "extruder1")
        printer.objects["oams unit1"] = unit1
        printer.objects["oams unit2"] = unit2
        printer.objects["filament_group T0"] = types.SimpleNamespace(
            bay_specs=[("unit1", 0), ("unit1", 1)])
        printer.objects["filament_group T1"] = types.SimpleNamespace(
            bay_specs=[("unit2", 0), ("unit2", 1)])
        printer.section_names = ["fps lane1", "fps lane2", "oams unit1",
                                 "oams unit2", "filament_group T0",
                                 "filament_group T1", "oams_manager"]
        manager = oams_manager.OAMSManager(Config(printer, "oams_manager"))
        printer.objects["oams_manager"] = manager
        load_macros(printer, klipper)
        printer.send_event("klippy:ready")
        printer.spy_heat_wait()
        manager._monitor(printer.reactor.monotonic())
        return printer, manager, unit1, unit2
    return build


def is_reload(command):
    return command.startswith("G1 E") and not command.startswith("G1 E-")


@pytest.mark.parametrize("target", ["GROUP=T1 SLOT=-1", "GROUP=T1 SLOT=99", "GROUP=T1 SLOT=0",
                                    "GROUP=T1 SLOT=oops", "GROUP=T1 SLOT=2.9", "GROUP=missing SLOT=2"])
def test_ui_invalid_target_has_no_motion(setup, target):
    printer, manager, unit = setup(loaded=0)
    printer.objects["toolhead"].values["homed_axes"] = ""
    with pytest.raises(printer.command_error):
        printer.gcode.run_script("OPENAMS_LOAD " + target)
    assert printer.trace == []
    assert unit.current_spool == 0


def test_ui_switches_selected_bay_within_same_group(setup):
    printer, manager, unit = setup(loaded=0)
    printer.gcode.run_script("OPENAMS_LOAD GROUP=T0 SLOT=1")
    assert manager.get_status(0.)["current_group"] == "T0"
    assert unit.current_spool == 1
    assert printer.trace.index("unload") < printer.trace.index("feed:1")


def test_confirm_unloaded_without_fps_falls_back_to_the_global_check(setup):
    # A helper called without FPS= (not through SAFE_UNLOAD_FILAMENT) must
    # still refuse to carry on while a lane is loaded: it falls back to the
    # manager's current_group, as before the lane-aware macros.
    printer, manager, _ = setup(loaded=0)
    assert manager.get_status(0.)["current_group"] == "T0"
    with pytest.raises(printer.command_error, match="did not finish unloading"):
        printer.gcode.run_script("_OAMS_CONFIRM_UNLOADED STRICT=1")


def test_ui_unload_failure_stops_before_new_feed(setup):
    printer, _, unit = setup(loaded=0)
    unit.unload_result = False
    with pytest.raises(printer.command_error, match="unloading failed"):
        printer.gcode.run_script("OPENAMS_LOAD GROUP=T1 SLOT=2")
    assert "feed:2" not in printer.trace
    assert "CLEAN_NOZZLE" not in printer.trace


def test_ui_load_failure_stops_before_extruder_reload(setup):
    printer, _, unit = setup()
    unit.load_result = S.OAMS_OP_CODE_ERROR_BUSY
    with pytest.raises(printer.command_error):
        printer.gcode.run_script("OPENAMS_LOAD GROUP=T1 SLOT=2")
    # It heated and waited, but the failed load never reached the extruder.
    assert printer.trace == [
        "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=235.0",
        "OAMSM_HEAT_WAIT HEATER=extruder MINIMUM=230.0 FPS=fps",
        "SAVE_GCODE_STATE NAME=oams_toolchange", "feed:2"]


def test_ui_sensor_failure_aborts_outer_macro(setup):
    printer, _, _ = setup()
    printer.objects["gcode_macro _oams_macro_variables"].variables["fs_extruder_in"] = True
    printer.objects["filament_switch_sensor extruder_in"].values["filament_detected"] = False
    with pytest.raises(printer.command_error, match="Filament not detected"):
        printer.gcode.run_script("OPENAMS_LOAD GROUP=T1 SLOT=2")
    assert not any(command.startswith("G1") for command in printer.trace)
    assert "CLEAN_NOZZLE" not in printer.trace


def test_a_screen_runs_the_toolchange_it_is_advertised(setup):
    # HelixScreen runs whatever commands.load/unload name (docs/UI_API.md):
    # with oams_macros.cfg loaded that is the toolchange, so a screen's
    # unload cuts and its load cleans the nozzle, as T<n> does.
    printer, manager, unit = setup(loaded=0)
    commands = manager.get_status(0.)["commands"]
    assert (commands["load"], commands["unload"]) == ("OPENAMS_LOAD", "OPENAMS_UNLOAD")
    reports = stage_reports(printer, manager)
    printer.gcode.run_script(commands["unload"] + " FPS=fps")
    steps = [r for r, _ in reports if " NAME=" in r]
    assert steps.index("OAMSM_STAGE NAME=cut FPS=fps") < steps.index(
        "OAMSM_STAGE NAME=retract FPS=fps")
    assert unit.current_spool is None
    printer.trace.clear()
    printer.gcode.run_script(commands["load"] + " GROUP=T1 SLOT=2")
    assert unit.current_spool == 2
    assert printer.trace.index("feed:2") < printer.trace.index("CLEAN_NOZZLE")


def test_ui_unload_of_empty_machine_does_not_move(setup):
    printer, _, _ = setup()
    printer.gcode.run_script("OPENAMS_UNLOAD")
    assert printer.trace == []


def test_ui_unload_homes_heats_before_extrusion_and_uses_relative_mode(setup):
    printer, _, _ = setup(loaded=0)
    printer.objects["toolhead"].values["homed_axes"] = ""
    printer.gcode.run_script("OPENAMS_UNLOAD")
    # Homing first, then heating, then the unload itself in relative mode.
    assert printer.trace[0:5] == [
        "G28", "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=235.0",
        "OAMSM_HEAT_WAIT HEATER=extruder MINIMUM=230.0 FPS=fps",
        "SAVE_GCODE_STATE NAME=oams_unload", "M83"]
    assert "unload" in printer.trace


def test_reload_runs_fast_then_slow_into_the_melt_zone(setup):
    printer, _, unit = setup()
    variables = printer.objects["gcode_macro _oams_macro_variables"].variables
    variables["reload_fast_speed"] = 2400
    variables["reload_slow_length"] = 12
    total = (variables["extrusion_reload_length"] + variables["retract_length"]
             + variables["hotend_meltzone_compensation"])
    printer.gcode.run_script("OPENAMS_LOAD GROUP=T1 SLOT=3")
    reloads = [command for command in printer.trace if is_reload(command)]
    assert reloads[-2:] == ["G1 E%.2f F2400" % (total - 12),
                            "G1 E12.00 F%s" % variables["reload_speed"]]
    assert unit.current_spool == 3


def test_reload_without_a_fast_speed_is_one_move(setup):
    printer, _, unit = setup()
    variables = printer.objects["gcode_macro _oams_macro_variables"].variables
    variables.pop("reload_fast_speed", None)
    total = (variables["extrusion_reload_length"] + variables["retract_length"]
             + variables["hotend_meltzone_compensation"])
    printer.gcode.run_script("OPENAMS_LOAD GROUP=T1 SLOT=3")
    reloads = [command for command in printer.trace if is_reload(command)]
    assert reloads[-1] == "G1 E%s F%s" % (total, variables["reload_speed"])


def test_ui_load_from_empty_uses_relative_extrusion(setup):
    printer, _, unit = setup()
    printer.gcode.run_script("OPENAMS_LOAD GROUP=T1 SLOT=3")
    first_extrusion = next(i for i, command in enumerate(printer.trace)
                           if command.startswith("G1 "))
    assert printer.trace[first_extrusion - 1] == "M83"
    assert unit.current_spool == 3


def test_ui_failed_cut_sensor_stops_before_hardware_unload(setup):
    printer, _, unit = setup(loaded=0)
    printer.objects["gcode_macro _oams_macro_variables"].variables["fs_extruder_out"] = True
    printer.objects["filament_switch_sensor extruder_out"].values["filament_detected"] = True
    with pytest.raises(printer.command_error, match="Filament still detected"):
        printer.gcode.run_script("OPENAMS_LOAD GROUP=T1 SLOT=2")
    assert "unload" not in printer.trace
    assert "feed:2" not in printer.trace


@pytest.mark.parametrize("command", ["OPENAMS_LOAD SLOT=2", "OPENAMS_LOAD GROUP=T1"])
def test_ui_missing_required_parameters_does_not_move(setup, command):
    printer, _, _ = setup(loaded=0)
    with pytest.raises(printer.command_error, match="requires"):
        printer.gcode.run_script(command)
    assert printer.trace == []


def test_ui_not_ready_does_not_move(setup):
    printer, manager, unit = setup(loaded=0)
    manager.ready = False
    with pytest.raises(printer.command_error, match="not ready"):
        printer.gcode.run_script("OPENAMS_LOAD GROUP=T1 SLOT=2")
    assert printer.trace == []
    assert unit.current_spool == 0


def render(printer, macro, **params):
    """Render a macro exactly as GCodeMacro.cmd would, without dispatching it."""
    macro = printer.objects["gcode_macro " + macro]
    context = dict(macro.variables)
    context.update(macro.template.create_template_context())
    context["params"] = params
    context["rawparams"] = " ".join("%s=%s" % item for item in params.items())
    return [line.strip() for line in macro.template.render(context).splitlines()
            if line.strip()]


@pytest.mark.parametrize("params, load", [
    ({"GROUP": "T1"}, "OAMSM_LOAD_FILAMENT GROUP=T1"),
    ({"GROUP": "T1", "SLOT": "2"}, "OAMSM_LOAD_FILAMENT GROUP=T1 SLOT=2"),
])
def test_concurrent_branch_renders_literal_controls_only_when_enabled(setup, params, load):
    printer, _, _ = setup()
    assert not set(CONTROLS) & set(render(printer, "_TX", **params))
    printer.enable_gco_routines()
    lines = render(printer, "_TX", **params)
    start = lines.index("START")
    strict = 1 if "SLOT" in params else 0
    assert lines[start:] == [
        "START", load, "END", "M400", "WAIT",
        "_OAMS_CONTINUE_AFTER_LOAD GROUP=T1 SLOT=%s STRICT=%d STARTED_PAUSED=0 FPS=fps CLEAN=1"
        % (params.get("SLOT", -1), strict)]
    # Stock Klipper would receive the controls, which the fake rejects.
    with pytest.raises(AssertionError):
        printer.gcode.run_script("_TX " + " ".join("%s=%s" % item for item in params.items()))
    assert printer.received_controls == ["START"]


@pytest.mark.parametrize("loaded", [None, 0])
@pytest.mark.parametrize("command", ["_TX GROUP=T1", "OPENAMS_LOAD GROUP=T1 SLOT=2"])
def test_paused_toolchange_takes_serial_path_even_with_extension(setup, loaded, command):
    printer, manager, unit = setup(loaded=loaded)
    printer.enable_gco_routines()
    printer.objects["pause_resume"].values["is_paused"] = True
    printer.gcode.run_script(command)
    assert (manager.get_status(0.)["current_group"], unit.current_spool) == ("T1", 2)
    trace = printer.trace
    assert "PAUSE" not in trace
    # The nozzle is cleaned once the reload has reached the hot zone.
    assert trace.index("feed:2") < trace.index("G1 E31.2 F1000") < trace.index("CLEAN_NOZZLE")
    assert trace[-1] == "RESTORE_GCODE_STATE NAME=oams_toolchange MOVE=1 MOVE_SPEED=100"


def test_clean_after_reload_false_keeps_the_earlier_order(setup):
    printer, manager, unit = setup()
    printer.objects["gcode_macro _oams_macro_variables"].variables["clean_after_reload"] = False
    printer.gcode.run_script("OPENAMS_LOAD GROUP=T1 SLOT=2")
    trace = printer.trace
    assert unit.current_spool == 2
    assert trace.count("CLEAN_NOZZLE") == 1
    assert trace.index("feed:2") < trace.index("CLEAN_NOZZLE") < trace.index("G1 E31.2 F1000")


def test_concurrent_branch_with_the_earlier_order_cleans_during_the_load(setup):
    printer, _, _ = setup()
    printer.objects["gcode_macro _oams_macro_variables"].variables["clean_after_reload"] = False
    printer.enable_gco_routines()
    lines = render(printer, "_TX", GROUP="T1")
    start = lines.index("START")
    assert lines[start:] == [
        "START", "OAMSM_LOAD_FILAMENT GROUP=T1", "END", "CLEAN_NOZZLE", "M400", "WAIT",
        "_OAMS_CONTINUE_AFTER_LOAD GROUP=T1 SLOT=-1 STRICT=0 STARTED_PAUSED=0 FPS=fps"]


def test_pause_during_unload_stops_before_feed(setup):
    printer, manager, unit = setup(loaded=0)
    unload_spool = unit.start_unload_spool

    def unload_then_pause(gen=None):
        printer.objects["pause_resume"].values["is_paused"] = True
        return unload_spool(gen=gen)
    unit.start_unload_spool = unload_then_pause
    with pytest.raises(printer.command_error, match="printer was paused"):
        printer.gcode.run_script("T1")
    assert "feed:2" not in printer.trace and "feed:3" not in printer.trace
    assert not any(is_reload(command) or command == "CLEAN_NOZZLE" for command in printer.trace)


def first_motion(trace):
    return next(i for i, c in enumerate(trace)
                if c.startswith(("G1 E", "G0 ", "CUT", "OAMSM_FOLLOWER")) or c == "unload")


def heat_commands(trace, extruder="extruder", target=235.0):
    """The index of the SET_HEATER_TEMPERATURE that heats to `target`, and the
    index of the OAMSM_HEAT_WAIT that follows it."""
    heat = trace.index("SET_HEATER_TEMPERATURE HEATER=%s TARGET=%s"
                       % (extruder, target))
    wait = next(i for i, c in enumerate(trace)
                if c.startswith("OAMSM_HEAT_WAIT"))
    return heat, wait


HEAT_235 = 'OAMSM_STAGE NAME=heat DETAIL="235 °C" FPS=fps'


@pytest.mark.parametrize("variant", ["cannot_extrude", "below_minimum"])
@pytest.mark.parametrize("command, loaded, steps", [
    ("T1", 0, "cut,retract,feed"), ("T1", None, "feed"),
    ("OPENAMS_LOAD GROUP=T1 SLOT=2", 0, "cut,retract,feed"),
    ("OPENAMS_LOAD GROUP=T1 SLOT=2", None, "feed"),
    ("SAFE_UNLOAD_FILAMENT", 0, "cut,retract"), ("OPENAMS_UNLOAD", 0, "cut,retract")])
def test_a_cold_extruder_is_heated_before_any_motion(setup, variant, command, loaded, steps):
    # What the machine can do itself, it does: the flow heats and waits before
    # it moves any filament (the old behavior was a console error).
    printer, manager, unit = setup(loaded=loaded)
    printer.objects["toolhead"].values["homed_axes"] = ""
    if variant == "cannot_extrude":
        printer.objects["extruder"].values["can_extrude"] = False
    else:
        printer.objects["gcode_macro _oams_macro_variables"].variables[
            "minimum_extrude_temperature"] = 230
    reports = stage_reports(printer, manager)
    printer.gcode.run_script(command)
    trace = printer.trace
    heat, wait = heat_commands(trace)
    assert trace.index("G28") < heat < wait < first_motion(trace)
    assert trace[wait] == "OAMSM_HEAT_WAIT HEATER=extruder MINIMUM=230.0 FPS=fps"
    assert reports[0] == ("OAMSM_STAGE PLAN=home,heat,%s FPS=fps" % steps, None)
    assert (reports[1], reports[2]) == (("OAMSM_STAGE NAME=home FPS=fps", 0),
                                        (HEAT_235, 1))


@pytest.mark.parametrize("command, steps", [("OPENAMS_UNLOAD", "cut,retract"),
                                            ("T1", "cut,retract,feed")])
def test_homing_that_leaves_the_extruder_cold_heats_before_any_motion(setup, command, steps):
    # Homing that ends with no target and a cold extruder is no longer a dead
    # end: the flow heats afterwards and carries on.
    printer, manager, unit = setup(loaded=0)
    printer.objects["toolhead"].values["homed_axes"] = ""
    printer.objects["extruder"].values["target"] = 0.0
    printer.homing_cools = (0.0, 60.0)
    reports = stage_reports(printer, manager)
    printer.gcode.run_script(command)
    trace = printer.trace
    heat, wait = heat_commands(trace)
    assert trace.index("G28") < heat < wait < first_motion(trace)
    assert trace[wait] == "OAMSM_HEAT_WAIT HEATER=extruder MINIMUM=230.0 FPS=fps"
    assert reports[0] == ("OAMSM_STAGE PLAN=home,heat,%s FPS=fps" % steps, None)
    assert reports[2] == (HEAT_235, 1)
    # The flow finished, so the target it raised went back to the one it found.
    assert printer.objects["extruder"].values["target"] == 0.0


def test_homing_that_cools_the_extruder_heats_before_any_filament_motion(
        setup):
    printer, _, _ = setup(loaded=0)
    printer.objects["toolhead"].values["homed_axes"] = ""
    printer.homing_cools = (150.0, 150.0)
    printer.gcode.run_script("OPENAMS_UNLOAD")
    trace = printer.trace
    heat, wait = heat_commands(trace)
    assert trace.index("G28") < heat < wait < first_motion(trace)
    assert trace[wait] == "OAMSM_HEAT_WAIT HEATER=extruder MINIMUM=230.0 FPS=fps"
    assert printer.objects["extruder"].values["target"] == 220.0


def test_same_group_toolchange_that_homes_needs_no_heat(setup):
    printer, _, _ = setup(loaded=0)
    printer.objects["toolhead"].values["homed_axes"] = ""
    printer.homing_cools = (0.0, 60.0)
    printer.gcode.run_script("T0")
    assert printer.trace == ["G28"]


def test_unknown_group_is_rejected_before_any_motion(setup):
    printer, _, unit = setup(loaded=0)
    printer.objects["toolhead"].values["homed_axes"] = ""
    with pytest.raises(printer.command_error, match="Unknown OpenAMS filament group T7"):
        printer.gcode.run_script("_TX GROUP=T7")
    assert printer.trace == []
    assert unit.current_spool == 0


def test_two_lanes_t1_loads_lane_b_and_never_unloads_lane_a(setup_two_lanes):
    printer, manager, unit1, unit2 = setup_two_lanes()
    printer.gcode.run_script("T0")
    assert unit1.current_spool == 0
    mark = len(printer.trace)
    printer.gcode.run_script("T1")
    assert "unload" not in printer.trace[mark:]
    assert unit1.current_spool == 0
    assert unit2.current_spool == 0
    lanes = manager.get_status(0.)["lanes_by_fps"]
    assert (lanes["lane1"]["group"], lanes["lane2"]["group"]) == ("T0", "T1")


def test_two_lanes_unload_follows_the_active_extruder(setup_two_lanes):
    printer, _, unit1, unit2 = setup_two_lanes()
    printer.gcode.run_script("T0")
    printer.gcode.run_script("T1")
    printer.objects["toolhead"].values["extruder"] = "extruder1"
    printer.gcode.run_script("OPENAMS_UNLOAD")
    assert unit2.current_spool is None
    assert unit1.current_spool == 0


def test_two_lanes_cold_lane_b_extruder_heats_t1(setup_two_lanes):
    # Lane b's own extruder is cold and lane b is already homed, so the plan
    # is heat,feed with no home step.
    printer, manager, unit1, unit2 = setup_two_lanes()
    printer.objects["extruder1"].values["can_extrude"] = False
    reports = stage_reports(printer, manager, fps="lane2")
    printer.gcode.run_script("T1")
    trace = printer.trace
    heat, wait = heat_commands(trace, extruder="extruder1")
    assert heat == 0 and wait < first_motion(trace)
    assert trace[wait] == "OAMSM_HEAT_WAIT HEATER=extruder1 MINIMUM=230.0 FPS=lane2"
    assert reports[0] == ("OAMSM_STAGE PLAN=heat,feed FPS=lane2", None)
    assert reports[1] == ('OAMSM_STAGE NAME=heat DETAIL="235 °C" FPS=lane2', 0)
    assert (unit1.current_spool, unit2.current_spool) == (None, 0)


def set_variable(printer, name, value):
    printer.objects["gcode_macro _oams_macro_variables"].variables[name] = value


def test_unload_heats_to_the_loaded_bays_pushed_temperature(setup):
    printer, manager, unit = setup(loaded=0)
    unit.set_bay_filament(0, "PLA", 245.0, "spool")
    printer.objects["extruder"].values.update(target=210.0, temperature=210.0)
    reports = stage_reports(printer, manager)
    printer.gcode.run_script("OPENAMS_UNLOAD")
    assert reports == [
        ("OAMSM_STAGE PLAN=heat,cut,retract FPS=fps", None),
        ('OAMSM_STAGE NAME=heat DETAIL="245 °C" FPS=fps', 0),
        ("OAMSM_STAGE NAME=cut FPS=fps", 1),
        ("OAMSM_STAGE NAME=retract FPS=fps", 2),
        ("OAMSM_STAGE DONE=1 FPS=fps", None),
    ]
    heat, wait = heat_commands(printer.trace, target=245.0)
    assert heat < wait < first_motion(printer.trace)
    assert printer.trace[wait] == "OAMSM_HEAT_WAIT HEATER=extruder MINIMUM=240.0 FPS=fps"
    assert printer.objects["extruder"].values["target"] == 210.0


def test_load_into_an_empty_toolhead_heats_to_the_incoming_bays_temperature(setup):
    printer, manager, unit = setup()
    unit.set_bay_filament(2, "ABS", 250.0, "table")
    printer.objects["extruder"].values.update(target=200.0, temperature=200.0)
    printer.gcode.run_script("T1")
    heat, wait = heat_commands(printer.trace, target=250.0)
    assert heat < wait < first_motion(printer.trace)
    assert printer.trace[wait] == "OAMSM_HEAT_WAIT HEATER=extruder MINIMUM=245.0 FPS=fps"
    assert unit.current_spool == 2


def test_a_targeted_load_heats_to_that_slots_bay(setup):
    printer, manager, unit = setup()
    unit.set_bay_filament(2, "ABS", 250.0, "spool")
    unit.set_bay_filament(3, "ASA", 260.0, "spool")
    printer.gcode.run_script("OPENAMS_LOAD GROUP=T1 SLOT=3")
    assert unit.current_spool == 3
    heat, _ = heat_commands(printer.trace, target=260.0)
    assert heat < first_motion(printer.trace)


def test_a_swap_heats_to_the_higher_of_the_two_filaments(setup):
    # The old material has to melt out while the new one is pushed through.
    printer, manager, unit = setup(loaded=0)
    unit.set_bay_filament(0, "PLA", 210.0, "spool")
    unit.set_bay_filament(2, "ASA", 270.0, "spool")
    printer.gcode.run_script("T1")
    heat, wait = heat_commands(printer.trace, target=270.0)
    assert heat < first_motion(printer.trace)
    assert printer.trace[wait] == "OAMSM_HEAT_WAIT HEATER=extruder MINIMUM=265.0 FPS=fps"


def test_an_unknown_material_heats_to_the_configured_temperature(setup):
    printer, manager, unit = setup(loaded=0)
    set_variable(printer, "unknown_material_temperature", 245.0)
    reports = stage_reports(printer, manager)
    printer.gcode.run_script("OPENAMS_UNLOAD")
    assert reports[1] == ('OAMSM_STAGE NAME=heat DETAIL="245 °C" FPS=fps', 0)
    heat_commands(printer.trace, target=245.0)


@pytest.mark.parametrize("command", ["OPENAMS_UNLOAD TEMP=260", "T1 TEMP=260",
                                    "OPENAMS_LOAD GROUP=T1 SLOT=2 TEMP=260"])
def test_temp_overrides_the_pushed_temperatures(setup, command):
    printer, _, unit = setup(loaded=0)
    unit.set_bay_filament(0, "PLA", 210.0, "spool")
    unit.set_bay_filament(2, "ABS", 240.0, "spool")
    printer.gcode.run_script(command)
    heat_commands(printer.trace, target=260.0)


@pytest.mark.parametrize("max_temp, operation, expected", [(300.0, 240.0, 240.0),
                                                           (260.0, 0.0, 255.0)])
def test_the_ceiling_clamps_the_working_temperature(setup, max_temp, operation, expected):
    printer, _, unit = setup(loaded=0)
    printer.objects["configfile"].values["settings"]["extruder"]["max_temp"] = max_temp
    set_variable(printer, "max_operation_temperature", operation)
    unit.set_bay_filament(0, "ASA", 280.0, "spool")
    printer.gcode.run_script("OPENAMS_UNLOAD")
    heat_commands(printer.trace, target=expected)


def test_the_floor_raises_the_working_temperature(setup):
    printer, _, unit = setup(loaded=0)
    set_variable(printer, "minimum_extrude_temperature", 250.0)
    unit.set_bay_filament(0, "PLA", 210.0, "spool")
    printer.gcode.run_script("OPENAMS_UNLOAD")
    heat, wait = heat_commands(printer.trace, target=250.0)
    assert printer.trace[wait] == "OAMSM_HEAT_WAIT HEATER=extruder MINIMUM=250.0 FPS=fps"


def test_a_floor_above_the_ceiling_refuses_before_any_motion(setup):
    # Nothing the machine can do: this is a configuration error, and saying so
    # is what an error is for.
    printer, _, unit = setup(loaded=0)
    set_variable(printer, "minimum_extrude_temperature", 250.0)
    set_variable(printer, "max_operation_temperature", 240.0)
    with pytest.raises(printer.command_error, match="above the temperature ceiling"):
        printer.gcode.run_script("OPENAMS_UNLOAD")
    assert printer.trace == []
    assert unit.current_spool == 0


def test_a_hot_enough_extruder_needs_no_heat_step(setup):
    printer, manager, unit = setup(loaded=0)
    unit.set_bay_filament(0, "PLA", 210.0, "spool")
    printer.objects["extruder"].values.update(target=220.0, temperature=220.0)
    reports = stage_reports(printer, manager)
    printer.gcode.run_script("OPENAMS_UNLOAD")
    assert reports[0] == ("OAMSM_STAGE PLAN=cut,retract FPS=fps", None)
    assert not [c for c in printer.trace
                if c.startswith(("SET_HEATER", "OAMSM_HEAT_WAIT"))]


def test_a_hotter_preheat_is_kept(setup):
    printer, manager, unit = setup(loaded=0)
    unit.set_bay_filament(0, "PLA", 210.0, "spool")
    printer.objects["extruder"].values.update(target=250.0, temperature=180.0)
    reports = stage_reports(printer, manager)
    printer.gcode.run_script("OPENAMS_UNLOAD")
    assert reports[1] == ('OAMSM_STAGE NAME=heat DETAIL="250 °C" FPS=fps', 0)
    # It waits for the user's own 250 and never lowers it.
    assert not [c for c in printer.trace if c.startswith("SET_HEATER")]
    wait = next(i for i, c in enumerate(printer.trace)
                if c.startswith("OAMSM_HEAT_WAIT"))
    assert printer.trace[wait] == "OAMSM_HEAT_WAIT HEATER=extruder MINIMUM=245.0 FPS=fps"


def test_a_print_with_a_hot_target_leaves_the_heater_alone(setup):
    printer, manager, unit = setup(loaded=0)
    printer.objects["print_stats"].values["state"] = "printing"
    printer.objects["extruder"].values.update(target=210.0, temperature=210.0)
    unit.set_bay_filament(0, "ASA", 270.0, "spool")
    reports = stage_reports(printer, manager)
    printer.gcode.run_script("OPENAMS_UNLOAD")
    assert reports[0] == ("OAMSM_STAGE PLAN=cut,retract FPS=fps", None)
    assert not [c for c in printer.trace
                if c.startswith(("SET_HEATER", "OAMSM_HEAT_WAIT"))]
    assert printer.objects["extruder"].values["target"] == 210.0


def test_a_print_with_a_cold_target_heats_to_the_filament(setup):
    printer, _, unit = setup(loaded=0)
    printer.objects["print_stats"].values["state"] = "printing"
    printer.objects["extruder"].values.update(target=0.0, temperature=60.0)
    set_variable(printer, "minimum_extrude_temperature", 170.0)
    unit.set_bay_filament(0, "PLA", 240.0, "spool")
    printer.gcode.run_script("OPENAMS_UNLOAD")
    heat_commands(printer.trace, target=240.0)
    # The print owns its temperature, so nothing is put back afterwards.
    assert printer.objects["extruder"].values["target"] == 240.0


def test_a_paused_print_is_heated_like_an_idle_printer(setup):
    printer, manager, unit = setup(loaded=0)
    printer.objects["print_stats"].values["state"] = "paused"
    printer.objects["pause_resume"].values["is_paused"] = True
    unit.set_bay_filament(0, "PLA", 245.0, "spool")
    printer.objects["extruder"].values.update(target=0.0, temperature=60.0)
    printer.gcode.run_script("OPENAMS_UNLOAD")
    heat, wait = heat_commands(printer.trace, target=245.0)
    assert heat < wait < first_motion(printer.trace)
    # Idle handling all the way: the target it found goes back on success.
    assert printer.objects["extruder"].values["target"] == 0.0


def test_a_successful_idle_unload_puts_the_target_back_to_off(setup):
    printer, _, unit = setup(loaded=0)
    printer.objects["extruder"].values.update(target=0.0, temperature=60.0,
                                              can_extrude=False)
    printer.gcode.run_script("OPENAMS_UNLOAD")
    assert printer.objects["extruder"].values["target"] == 0.0
    assert printer.trace[-1] == "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=0.0"


def test_a_failed_flow_leaves_the_heater_on_for_the_retry(setup):
    printer, _, unit = setup(loaded=0)
    printer.objects["extruder"].values.update(target=0.0, temperature=60.0,
                                              can_extrude=False)
    unit.unload_result = False
    with pytest.raises(printer.command_error):
        printer.gcode.run_script("OPENAMS_UNLOAD")
    assert printer.objects["extruder"].values["target"] == 235.0


def test_home_is_a_step_only_when_the_flow_homes(setup):
    printer, manager, unit = setup(loaded=0)
    reports = stage_reports(printer, manager)
    printer.gcode.run_script("OPENAMS_UNLOAD")
    assert [report[0] for report in reports[:2]] == [
        "OAMSM_STAGE PLAN=heat,cut,retract FPS=fps", HEAT_235]
    assert "G28" not in printer.trace

    printer, manager, unit = setup(loaded=0)
    printer.objects["toolhead"].values["homed_axes"] = ""
    reports = stage_reports(printer, manager)
    printer.gcode.run_script("OPENAMS_UNLOAD")
    assert [report[0] for report in reports[:3]] == [
        "OAMSM_STAGE PLAN=home,heat,cut,retract FPS=fps",
        "OAMSM_STAGE NAME=home FPS=fps", HEAT_235]
    assert [report[1] for report in reports[:3]] == [None, 0, 1]
    assert printer.trace.index("G28") < first_motion(printer.trace)


def test_a_flow_that_homes_announces_heat_and_skips_it_when_hot(setup):
    # Homing can change the heater (a probe calibration with its own target),
    # so a flow that homes always announces heat; finding the extruder hot
    # enough afterwards it skips the step and sets no heater.
    printer, manager, unit = setup(loaded=0)
    unit.set_bay_filament(0, "PLA", 210.0, "spool")
    printer.objects["toolhead"].values["homed_axes"] = ""
    printer.objects["extruder"].values.update(target=220.0, temperature=220.0)
    reports = stage_reports(printer, manager)
    printer.gcode.run_script("OPENAMS_UNLOAD")
    assert reports[0][0] == "OAMSM_STAGE PLAN=home,heat,cut,retract FPS=fps"
    assert not any("NAME=heat" in report[0] for report in reports)
    assert not any(command.startswith("SET_HEATER_TEMPERATURE")
                   for command in printer.trace)
    assert unit.current_spool is None


def stage_reports(printer, manager, fps="fps"):
    """Record every OAMSM_STAGE the macros dispatch, as (command line, the
    lane's published ``index``) pairs, in order.

    The manager owns OAMSM_STAGE, so wrap the handler the dispatcher already
    holds (the wrapper that parses FPS=/NAME=/...) and let the real one run."""
    reports = []
    handlers = printer.gcode.gcode_handlers
    inner = handlers["OAMSM_STAGE"]

    def spy(gcmd):
        inner(gcmd)
        published = manager.get_status(0.)["lanes_by_fps"][fps]["stage"]
        reports.append((gcmd.get_commandline(),
                        published["index"] if published else None))
    handlers["OAMSM_STAGE"] = spy
    return reports


def published_stage(manager, fps="fps"):
    return manager.get_status(0.)["lanes_by_fps"][fps]["stage"]


def test_stage_reports_a_switch_as_cut_retract_feed(setup):
    # C2 5.3: a group switch announces heat,cut,retract,feed (heat first, the
    # extruder is colder than the unknown material's 235 C), enters each step
    # in order and ends the plan on success.
    printer, manager, unit = setup(loaded=0)
    reports = stage_reports(printer, manager)
    printer.gcode.run_script("OPENAMS_LOAD GROUP=T1 SLOT=2")
    assert reports == [
        ("OAMSM_STAGE PLAN=heat,cut,retract,feed FPS=fps", None),
        (HEAT_235, 0),
        ("OAMSM_STAGE NAME=cut FPS=fps", 1),
        ("OAMSM_STAGE NAME=retract FPS=fps", 2),
        ("OAMSM_STAGE NAME=feed FPS=fps", 3),
        ("OAMSM_STAGE DONE=1 FPS=fps", None),
    ]
    assert published_stage(manager) is None
    assert unit.current_spool == 2


def test_stage_reports_a_standalone_unload_as_cut_retract(setup):
    printer, manager, unit = setup(loaded=0)
    reports = stage_reports(printer, manager)
    printer.gcode.run_script("OPENAMS_UNLOAD")
    assert reports == [
        ("OAMSM_STAGE PLAN=heat,cut,retract FPS=fps", None),
        (HEAT_235, 0),
        ("OAMSM_STAGE NAME=cut FPS=fps", 1),
        ("OAMSM_STAGE NAME=retract FPS=fps", 2),
        ("OAMSM_STAGE DONE=1 FPS=fps", None),
    ]
    assert published_stage(manager) is None
    assert unit.current_spool is None


def test_stage_reports_a_load_from_empty_as_feed(setup):
    printer, manager, unit = setup()
    reports = stage_reports(printer, manager)
    printer.gcode.run_script("T1")
    assert reports == [
        ("OAMSM_STAGE PLAN=heat,feed FPS=fps", None),
        (HEAT_235, 0),
        ("OAMSM_STAGE NAME=feed FPS=fps", 1),
        ("OAMSM_STAGE DONE=1 FPS=fps", None),
    ]
    assert published_stage(manager) is None
    assert unit.current_spool == 2


def test_stage_keeps_the_plan_when_the_feed_fails(setup):
    printer, manager, unit = setup()
    unit.load_result = S.OAMS_OP_CODE_ERROR_BUSY
    with pytest.raises(printer.command_error):
        printer.gcode.run_script("T1")
    published = published_stage(manager)
    assert published["plan"] == ["heat", "feed"]
    assert published["index"] == 1
    assert published["failed"] == "feed"


def test_stage_is_silent_when_the_group_is_already_loaded(setup):
    # C2 5.3: _TX for the group that is already loaded announces no plan.
    printer, manager, unit = setup(loaded=0)
    printer.gcode.run_script("T0")
    assert published_stage(manager) is None
    reports = stage_reports(printer, manager)
    printer.gcode.run_script("T0")
    assert reports == []


def test_stage_is_silent_for_an_excluded_object(setup):
    printer, manager, unit = setup(loaded=0)
    printer.objects["exclude_object"].values["current_object"] = "widget"
    printer.objects["exclude_object"].values["excluded_objects"] = ["widget"]
    reports = stage_reports(printer, manager)
    printer.gcode.run_script("T1")
    assert reports == []


def test_stage_reports_failed_toolchange_before_the_abort(setup):
    # C2 5.3 rule 3/7: the load completed but the printer paused, so the macro
    # fails the plan's feed step before it pauses and raises. The second
    # report from _OAMS_ABORT is harmless (the first failure wins).
    printer, manager, unit = setup()
    load_spool = unit.start_load_spool

    def load_then_pause(bay, gen=None):
        printer.objects["pause_resume"].values["is_paused"] = True
        return load_spool(bay, gen=gen)
    unit.start_load_spool = load_then_pause
    reports = stage_reports(printer, manager)
    with pytest.raises(printer.command_error, match="printer was paused"):
        printer.gcode.run_script("T1")
    assert reports == [
        ("OAMSM_STAGE PLAN=heat,feed FPS=fps", None),
        (HEAT_235, 0),
        ("OAMSM_STAGE NAME=feed FPS=fps", 1),
        ("OAMSM_STAGE FAILED=1 FPS=fps", 1),
        ("OAMSM_STAGE FAILED=1 FPS=fps", 1),
    ]
    assert published_stage(manager)["failed"] == "feed"


def test_concurrent_path_reports_feed_before_the_routine(setup):
    # The feed step is announced outside the background routine, just before
    # START, and the plan is announced even earlier (before any motion).
    printer, manager, unit = setup()
    printer.enable_gco_routines()
    lines = render(printer, "_TX", GROUP="T1")
    plan = next(line for line in lines if line.startswith("_OAMS_PLAN"))
    assert plan.endswith("KIND=load STEPS=feed")
    assert lines.index(plan) < lines.index("SAVE_GCODE_STATE NAME=oams_toolchange")
    start = lines.index("START")
    assert lines[start - 1] == "OAMSM_STAGE NAME=feed FPS=fps"
    # Nothing between START and END reports a stage: the routine owns only the
    # transport, and a concurrent load must not touch the stage machinery.
    end = lines.index("END")
    assert not [line for line in lines[start:end] if line.startswith("OAMSM_STAGE")]


# --- the user's own macros: the four hook points and the cutter override -----

# The move each hook makes, so a test can tell the four apart in printer.trace.
HOOK_MOVES = {"park": "G0 X101 Y101", "wipe": "G0 X201 Y201",
              "check": "G0 X301 Y301", "purge": "G0 X401 Y401"}


def hook(name, step=None, label=None, gcode="    G0 X201 Y201"):
    """A hook as a printer's own file writes it: the step name and display
    label it declares (or none of either), then its body. Klipper reads a
    variable_ option as a Python literal, so the names are quoted."""
    lines = ["[gcode_macro _OAMS_HOOK_%s]" % (name,)]
    if step is not None:
        lines.append('variable_step: "%s"' % (step,))
    if label is not None:
        lines.append('variable_label: "%s"' % (label,))
    return "\n".join(lines + ["gcode:", gcode]) + "\n"


def step_hook(name, step, label=None):
    return hook(name, step, label, gcode="    " + HOOK_MOVES[step])


CUT_OVERRIDE = """
[gcode_macro OAMS_CUT]
gcode:
    G0 X200 Y300
"""


def load_user_macros(printer, klipper, *texts):
    """Load macro sections from the user's own files, the way load_macros
    loads the shipped ones: the section name keeps its own spelling, which is
    what the macros look the hook up by."""
    for text in texts:
        config = configparser.RawConfigParser(inline_comment_prefixes=("#", ";"))
        config.read_string(text)
        for section in config.sections():
            printer.objects[section] = klipper.gcode_macro.GCodeMacro(
                Config(printer, section, dict(config[section])))


@contextlib.contextmanager
def strict_undefined(printer):
    """Render the way the ordered opt-in does: a value the macro reads but
    Klipper does not define is an error instead of an empty one, so every
    optional read needs its |default(...)."""
    import jinja2
    env = printer.objects["gcode_macro"].env
    saved, env.undefined = env.undefined, jinja2.StrictUndefined
    try:
        yield
    finally:
        env.undefined = saved


def run(printer, command, strict=False):
    if strict:
        with strict_undefined(printer):
            printer.gcode.run_script(command)
    else:
        printer.gcode.run_script(command)


def stage_trace(printer, manager, fps="fps"):
    """stage_reports, plus every OAMSM_STAGE also recorded in printer.trace
    (so a test can order the announced steps against the flow's own moves) and
    the lane's published plan and labels as the PLAN command left them."""
    reports = stage_reports(printer, manager, fps)
    inner = printer.gcode.gcode_handlers["OAMSM_STAGE"]
    published = {}

    def spy(gcmd):
        printer.trace.append(gcmd.get_commandline())
        inner(gcmd)
        stage = manager.get_status(0.)["lanes_by_fps"][fps]["stage"]
        if stage is not None and "plan" not in published:
            published["plan"] = list(stage["plan"])
            published["labels"] = dict(stage["labels"])
    printer.gcode.gcode_handlers["OAMSM_STAGE"] = spy
    return reports, published


def respond_messages(printer):
    """Every RESPOND the flow sends, as its command line (the test replaces
    the handler, so the line is what it can read)."""
    lines = []
    printer.gcode.gcode_handlers["RESPOND"] = (
        lambda gcmd: lines.append(gcmd.get_commandline()))
    return lines


LABELED_CUT_OVERRIDE = """
[gcode_macro OAMS_CUT]
variable_label: "Form tip"
gcode:
    G0 X200 Y300
"""


@pytest.mark.parametrize("strict", [False, True], ids=["legacy", "ordered"])
def test_a_cut_override_with_a_label_names_the_cut_step(setup, klipper, strict):
    # An OAMS_CUT that declares variable_label (a printer that forms a tip
    # instead of cutting) has the cut step published and announced in its
    # words; the step itself is still "cut".
    printer, manager, unit = setup(loaded=0)
    load_user_macros(printer, klipper, LABELED_CUT_OVERRIDE)
    reports, published = stage_trace(printer, manager)
    messages = respond_messages(printer)
    run(printer, "OPENAMS_LOAD GROUP=T1 SLOT=2", strict)
    assert published["plan"] == ["heat", "cut", "retract", "feed"]
    assert published["labels"] == {"cut": "Form tip"}
    assert reports[0] == (
        'OAMSM_STAGE PLAN=heat,cut,retract,feed LABEL_CUT="Form tip" FPS=fps', None)
    assert ("OAMSM_STAGE NAME=cut FPS=fps", 1) in reports
    assert any("Form tip" in line for line in messages)
    assert not any("Cutting" in line for line in messages)
    assert "G0 X200 Y300" in printer.trace
    assert unit.current_spool == 2


@pytest.mark.parametrize("strict", [False, True], ids=["legacy", "ordered"])
def test_a_cut_override_without_a_label_keeps_the_cut_wording(setup, klipper, strict):
    printer, manager, unit = setup(loaded=0)
    load_user_macros(printer, klipper, CUT_OVERRIDE)
    reports, published = stage_trace(printer, manager)
    messages = respond_messages(printer)
    run(printer, "OPENAMS_LOAD GROUP=T1 SLOT=2", strict)
    assert published["labels"] == {}
    assert reports[0] == ("OAMSM_STAGE PLAN=heat,cut,retract,feed FPS=fps", None)
    assert any("Cutting" in line for line in messages)


@pytest.mark.parametrize("strict", [False, True], ids=["legacy", "ordered"])
def test_a_hook_with_a_step_is_announced_and_entered_before_it_runs(setup, klipper, strict):
    # C2 5.3: a hook that declares variable_step has that step in the plan, at
    # the hook's own place, with its LABEL_, and the step is entered just
    # before the hook runs.
    printer, manager, unit = setup(loaded=0)
    load_user_macros(printer, klipper, step_hook("AFTER_CUT", "wipe", "Wiping the nozzle"))
    reports, published = stage_trace(printer, manager)
    run(printer, "OPENAMS_LOAD GROUP=T1 SLOT=2", strict)
    assert published["plan"] == ["heat", "cut", "wipe", "retract", "feed"]
    assert published["labels"] == {"wipe": "Wiping the nozzle"}
    assert reports == [
        ('OAMSM_STAGE PLAN=heat,cut,wipe,retract,feed '
         'LABEL_WIPE="Wiping the nozzle" FPS=fps', None),
        (HEAT_235, 0),
        ("OAMSM_STAGE NAME=cut FPS=fps", 1),
        ("OAMSM_STAGE NAME=wipe FPS=fps", 2),
        ("OAMSM_STAGE NAME=retract FPS=fps", 3),
        ("OAMSM_STAGE NAME=feed FPS=fps", 4),
        ("OAMSM_STAGE DONE=1 FPS=fps", None),
    ]
    trace = printer.trace
    assert (trace.index("OAMSM_STAGE NAME=cut FPS=fps")
            < trace.index("OAMSM_STAGE NAME=wipe FPS=fps") < trace.index("G0 X201 Y201")
            < trace.index("OAMSM_STAGE NAME=retract FPS=fps"))
    assert published_stage(manager) is None
    assert unit.current_spool == 2


@pytest.mark.parametrize("strict", [False, True], ids=["legacy", "ordered"])
def test_a_hook_without_a_step_runs_inside_the_step_around_it(setup, klipper, strict):
    printer, manager, unit = setup(loaded=0)
    load_user_macros(printer, klipper, hook("AFTER_CUT"))
    reports, published = stage_trace(printer, manager)
    run(printer, "OPENAMS_LOAD GROUP=T1 SLOT=2", strict)
    assert published["plan"] == ["heat", "cut", "retract", "feed"]
    assert published["labels"] == {}
    assert reports == [
        ("OAMSM_STAGE PLAN=heat,cut,retract,feed FPS=fps", None),
        (HEAT_235, 0),
        ("OAMSM_STAGE NAME=cut FPS=fps", 1),
        ("OAMSM_STAGE NAME=retract FPS=fps", 2),
        ("OAMSM_STAGE NAME=feed FPS=fps", 3),
        ("OAMSM_STAGE DONE=1 FPS=fps", None),
    ]
    trace = printer.trace
    assert (trace.index("OAMSM_STAGE NAME=cut FPS=fps") < trace.index("G0 X201 Y201")
            < trace.index("OAMSM_STAGE NAME=retract FPS=fps"))


@pytest.mark.parametrize("command, loaded, steps, hooks", [
    ("OPENAMS_LOAD GROUP=T1 SLOT=2", 0, "heat,cut,retract,feed", 4),
    ("T1", None, "heat,feed", 2),
    ("OPENAMS_UNLOAD", 0, "heat,cut,retract", 2)])
@pytest.mark.parametrize("strict", [False, True], ids=["legacy", "ordered"])
def test_hooks_that_claim_no_step_leave_the_plans_exactly_as_they_are(
        setup, klipper, strict, command, loaded, steps, hooks):
    printer, manager, unit = setup(loaded=loaded)
    load_user_macros(printer, klipper, hook("BEFORE_HEAT"), hook("AFTER_CUT"),
                     hook("AFTER_RETRACT"), hook("AFTER_FEED"))
    reports, published = stage_trace(printer, manager)
    run(printer, command, strict)
    assert published["plan"] == steps.split(",")
    assert published["labels"] == {}
    # Every point this flow passes ran; the ones it does not pass did not.
    assert len([line for line in printer.trace if line == "G0 X201 Y201"]) == hooks
    assert reports[0][0] == "OAMSM_STAGE PLAN=%s FPS=fps" % (steps,)


@pytest.mark.parametrize("command, loaded, steps, ran", [
    ("OPENAMS_LOAD GROUP=T1 SLOT=2", 0,
     "park,heat,cut,wipe,retract,check,feed,purge", ["park", "wipe", "check", "purge"]),
    ("T1", None, "park,heat,feed,purge", ["park", "purge"]),
    ("OPENAMS_UNLOAD", 0, "park,heat,cut,wipe,retract", ["park", "wipe"])])
@pytest.mark.parametrize("strict", [False, True], ids=["legacy", "ordered"])
def test_a_hook_only_joins_the_flows_that_pass_its_point(
        setup, klipper, strict, command, loaded, steps, ran):
    # A load into an empty toolhead has no cut and no retract, so neither hook
    # is in its plan and neither runs; an unload has no feed, so the feed hook
    # is not in its plan.
    printer, manager, unit = setup(loaded=loaded)
    load_user_macros(printer, klipper, step_hook("BEFORE_HEAT", "park"),
                     step_hook("AFTER_CUT", "wipe"),
                     step_hook("AFTER_RETRACT", "check"),
                     step_hook("AFTER_FEED", "purge"))
    reports, published = stage_trace(printer, manager)
    run(printer, command, strict)
    plan = published["plan"]
    assert reports[0] == ("OAMSM_STAGE PLAN=%s FPS=fps" % (steps,), None)
    assert plan == steps.split(",")
    trace = printer.trace
    # The flow enters every step of the plan, in order, and the hook's own
    # step is the one just before its body runs.
    entered = [line.split("NAME=")[1].split(" ")[0] for line in trace
               if line.startswith("OAMSM_STAGE NAME=")]
    assert entered == plan
    for step in HOOK_MOVES:
        # A hook runs exactly when its step is in this flow's plan.
        assert (HOOK_MOVES[step] in trace) == (step in ran)


@pytest.mark.parametrize("strict", [False, True], ids=["legacy", "ordered"])
def test_a_hook_that_raises_fails_its_own_step(setup, klipper, strict):
    printer, manager, unit = setup(loaded=0)
    load_user_macros(printer, klipper, hook("AFTER_FEED", "purge", gcode=(
        '    { action_raise_error("the purge failed") }')))
    reports, published = stage_trace(printer, manager)
    with pytest.raises(printer.command_error, match="the purge failed"):
        run(printer, "OPENAMS_LOAD GROUP=T1 SLOT=2", strict)
    # The feed itself succeeded, and the failure is attributed to the step the
    # hook owns.
    assert published["plan"] == ["heat", "cut", "retract", "feed", "purge"]
    assert unit.current_spool == 2
    stage = published_stage(manager)
    assert (stage["failed"], stage["index"]) == ("purge", 4)


@pytest.mark.parametrize("strict", [False, True], ids=["legacy", "ordered"])
def test_a_plan_holds_at_most_eight_steps(setup, klipper, strict):
    # home, heat, cut, retract, feed and four hook steps would be nine: the
    # last hook step stays out of the plan, the hook still runs, and the
    # RESPOND says which step it was.
    printer, manager, unit = setup(loaded=0)
    printer.objects["toolhead"].values["homed_axes"] = ""
    load_user_macros(printer, klipper, hook("BEFORE_HEAT", "park"),
                     step_hook("AFTER_CUT", "wipe"),
                     step_hook("AFTER_RETRACT", "check"),
                     step_hook("AFTER_FEED", "purge"))
    reports, published = stage_trace(printer, manager)
    messages = respond_messages(printer)
    run(printer, "OPENAMS_LOAD GROUP=T1 SLOT=2", strict)
    assert published["plan"] == ["home", "park", "heat", "cut", "wipe",
                                 "retract", "check", "feed"]
    assert len(published["plan"]) == 8
    assert [line for line in messages if "purge" in line]
    assert "G0 X401 Y401" in printer.trace
    assert "OAMSM_STAGE NAME=purge FPS=fps" not in printer.trace
    assert published_stage(manager) is None


@pytest.mark.parametrize("strict", [False, True], ids=["legacy", "ordered"])
def test_oams_cut_replaces_the_built_in_cutter_moves(setup, klipper, strict):
    printer, manager, unit = setup(loaded=0)
    run(printer, "OPENAMS_UNLOAD", strict)
    assert [c for c in printer.trace if c.startswith(("G0", "G90"))] == [
        "G90", "G0 X20 Y355 F5000", "G0 X2 Y355 F1000", "G0 X81 Y355 F5000"]
    printer, manager, unit = setup(loaded=0)
    load_user_macros(printer, klipper, CUT_OVERRIDE)
    reports, _ = stage_trace(printer, manager)
    run(printer, "OPENAMS_UNLOAD", strict)
    # The step is still cut, and only the user's own cutter moves.
    assert [c for c in printer.trace if c.startswith(("G0", "G90"))] == ["G0 X200 Y300"]
    assert ("OAMSM_STAGE NAME=cut FPS=fps", 1) in reports
    assert unit.current_spool is None


EXPECTED_MACROS = {
    "CUT_FILAMENT", "SAFE_UNLOAD_FILAMENT", "_OAMS_FINISH_UNLOAD", "_OAMS_CONFIRM_UNLOADED",
    "_TX", "_OAMS_CONTINUE_AFTER_UNLOAD", "_OAMS_CONTINUE_AFTER_LOAD",
    "_OAMS_CONTINUE_AFTER_INLET", "_OAMS_FINISH_TOOLCHANGE", "_OAMS_FAIL_TOOLCHANGE",
    "_OAMS_ABORT", "_OAMS_RAISE", "_OAMS_PLAN", "_OAMS_HEAT", "_OAMS_RESTORE",
    "_OAMS_HOOK",
    "OPENAMS_LOAD", "OPENAMS_UNLOAD",
    "_LOAD_FS_IN", "_LOAD_FS_OUT", "_UNLOAD_FS_OUT",
    "OAMS_TORTURE_TEST", "OAMS_TOOLCHANGE_TORTURE_TEST", "T0", "T1", "T2", "T3",
    "_OAMS_TOOLHEAD_EJECT"}
NEW_VARIABLES = {"extrusion_unload_additional_length": 250, "unload_speed": 1000,
                 "additional_unload_speed": 5000, "minimum_extrude_temperature": 0,
                 "unknown_material_temperature": 235, "max_operation_temperature": 0}


def parse_config(name):
    config = configparser.RawConfigParser(inline_comment_prefixes=("#", ";"))
    config.read_string((ROOT / name).read_text())
    return config


def test_macro_files_define_expected_sections_and_only_overlay_sets_render_mode():
    macros = parse_config("oams_macros.cfg")
    assert set(macros.sections()) == {"gcode_macro " + name for name in EXPECTED_MACROS}
    for section in macros.sections():
        assert macros[section]["gcode"].strip(), section
        assert "render_mode" not in macros[section], section
    overlay = parse_config("oams_macros_ordered.cfg")
    assert overlay.sections() == ["gcode_macro _TX"]
    assert dict(overlay["gcode_macro _TX"]) == {"render_mode": "ordered"}
    sample = parse_config("oams_sample.cfg")
    assert not any("render_mode" in sample[section] for section in sample.sections())


def test_controls_are_literal_lines_inside_one_guarded_branch():
    macros = parse_config("oams_macros.cfg")
    for section in macros.sections():
        lines = [line.strip() for line in macros[section]["gcode"].splitlines()]
        if section != "gcode_macro _TX":
            assert not any(line.split(" ")[0].upper() in CONTROLS for line in lines), section
            continue
        assert [line for line in lines if line in CONTROLS] == list(CONTROLS)
        start, wait = lines.index("START"), lines.index("WAIT")
        # One guard opens the concurrent branch and nothing between START and
        # WAIT closes or re-branches it: the controls are literal lines.
        guards = [i for i, line in enumerate(lines[:start])
                  if line.startswith("{% if HAS_GCO_ROUTINES")]
        assert len(guards) == 1
        assert not any(line.startswith(("{% el", "{% endif"))
                       for line in lines[start:wait])


def test_new_variables_have_defaults_and_sample_values():
    text = (ROOT / "oams_macros.cfg").read_text()
    used = set(re.findall(r"\bv\.(\w+)", text))
    assert set(NEW_VARIABLES) <= used
    sample = parse_config("oams_sample.cfg")["gcode_macro _oams_macro_variables"]
    known = {option[len("variable_"):] for option in sample}
    for name in used - known - {"reload_speed", "extrusion_reload_length",
                                "retract_length", "hotend_meltzone_compensation",
                                "fs_extruder_in", "fs_extruder_out",
                                "pre_cut_x", "pre_cut_y", "pre_cut_speed",
                                "cut_x", "cut_y", "cut_speed",
                                "extrusion_unload_length"}:
        for match in re.finditer(r"\bv\.%s\b(.{0,9})" % name, text):
            assert match.group(1).startswith("|default("), name
    assert {name: int(sample["variable_" + name]) for name in NEW_VARIABLES} == NEW_VARIABLES
    includes = (ROOT / "oams_sample.cfg").read_text().splitlines()
    index = includes.index("[include oams_macros.cfg]")
    assert includes[index + 1] == "#[include oams_macros_ordered.cfg]"


class StopRequest:
    """A webhook request for openams/stop."""

    def __init__(self, lane=None):
        self.params = {} if lane is None else {"lane": lane}
        self.sent = None

    def get(self, key, default=None):
        return self.params.get(key, default)

    def send(self, data):
        self.sent = data


def request_stop(manager, lane=None):
    request = StopRequest(lane)
    manager._webhook_stop(request)
    return request.sent["result"]


def stop_when_stage(printer, manager, trigger):
    """Request a stop right after OAMSM_STAGE `trigger` (a PLAN= or NAME=
    text) ran, the way the display's Stop arrives while a macro is busy."""
    handlers = printer.gcode.gcode_handlers
    inner = handlers["OAMSM_STAGE"]
    answers = []

    def spy(command):
        inner(command)
        if trigger in command.get_commandline():
            answers.append(request_stop(manager))
    handlers["OAMSM_STAGE"] = spy
    return answers


def cold_extruder(printer):
    printer.objects["extruder"].values.update(target=0.0, temperature=60.0,
                                              can_extrude=False)


def lane_stage(manager):
    return manager.get_status(0.)["lanes_by_fps"]["fps"]


def test_a_stop_during_the_heat_wait_fails_the_heat_step_and_restores_the_heater(setup):
    printer, manager, unit = setup(loaded=0)
    cold_extruder(printer)
    answers = []
    printer.reactor.on_pause = lambda now: answers.append(request_stop(manager))
    with pytest.raises(printer.command_error, match="Stopped by the user"):
        printer.gcode.run_script("OPENAMS_UNLOAD")
    assert answers[0] == "stopping"
    lane = lane_stage(manager)
    assert lane["stage"]["failed"] == "heat"
    assert lane["message"] == "Stopped by the user"
    # The heat the flow raised is put back, and nothing moved.
    assert printer.objects["extruder"].values["target"] == 0.0
    assert printer.trace[-1] == "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=0.0"
    assert not [c for c in printer.trace if c.startswith("G1")]
    assert unit.current_spool == 0
    assert printer.objects["gcode_macro _OAMS_PLAN"].variables["raised"] == 0
    # The flag is spent: a retry runs to the end.
    printer.reactor.on_pause = printer.heat_up
    printer.gcode.run_script("OPENAMS_UNLOAD")
    assert unit.current_spool is None


def test_a_stop_requested_before_a_later_step_stops_at_that_boundary(setup):
    printer, manager, unit = setup(loaded=0)
    cold_extruder(printer)
    answers = stop_when_stage(printer, manager, "NAME=cut")
    with pytest.raises(printer.command_error, match="Stopped by the user"):
        printer.gcode.run_script("OPENAMS_UNLOAD")
    assert answers == ["stopping"]
    # The cut step had started and is the one that failed; the retract (the
    # firmware unload) never began, and the raised heat is back.
    assert lane_stage(manager)["stage"]["failed"] == "cut"
    assert "unload" not in printer.trace
    assert unit.current_spool == 0
    assert printer.objects["extruder"].values["target"] == 0.0


def test_a_stop_before_the_first_step_stops_a_toolchange_before_any_motion(setup):
    printer, manager, unit = setup(loaded=0)
    stop_when_stage(printer, manager, "PLAN=")
    with pytest.raises(printer.command_error, match="Stopped by the user"):
        printer.gcode.run_script("OPENAMS_LOAD GROUP=T1 SLOT=2")
    assert unit.current_spool == 0
    assert not [c for c in printer.trace if c.startswith(("G1", "unload", "feed"))]


def test_a_stop_during_a_print_never_touches_the_heater(setup):
    printer, manager, unit = setup(loaded=0)
    printer.objects["print_stats"].values["state"] = "printing"
    cold_extruder(printer)
    set_variable(printer, "minimum_extrude_temperature", 170.0)
    answers = []
    printer.reactor.on_pause = lambda now: answers.append(request_stop(manager))
    with pytest.raises(printer.command_error, match="Stopped by the user"):
        printer.gcode.run_script("OPENAMS_UNLOAD")
    assert answers[0] == "stopping"
    # The print's cold target was raised for the flow; the stop leaves it.
    assert [c for c in printer.trace if c.startswith("SET_HEATER")] == [
        "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=235.0"]
    assert printer.objects["extruder"].values["target"] == 235.0
    assert lane_stage(manager)["stage"]["failed"] == "heat"


def test_a_stop_cancels_the_unload_in_flight_and_it_is_not_a_fault(setup):
    printer, manager, unit = setup(loaded=0)
    # The unit never answers on its own, so the unload stays in flight.
    unit.start_unload_spool = lambda gen=None: None
    printer.gcode.run_script("OAMSM_STAGE PLAN=retract FPS=fps")
    printer.gcode.run_script("OAMSM_STAGE NAME=retract FPS=fps")
    manager.runtime.request("fps", S.Unload("fps"))
    lanes = manager.runtime.get_state().lanes
    assert lanes["fps"].op == S.OP_UNLOADING
    assert request_stop(manager) == "stopping"
    # The firmware answers the cancel; a macro that raises ends the sequence.
    unit.on_action_complete(S.OAMS_OP_CODE_CANCEL, None, lanes["fps"].op_gen)
    lane = manager.runtime.get_state().lanes["fps"]
    assert lane.op == S.OP_LOADED and lane.fault_text is None
    printer.send_event("gcode:command_error")
    status = lane_stage(manager)
    assert status["stage"]["failed"] == "retract"
    assert status["message"] == "Stopped by the user"


def test_a_stop_with_nothing_running_answers_so_and_changes_nothing(setup):
    printer, manager, unit = setup(loaded=0)
    assert request_stop(manager) == "nothing running"
    assert request_stop(manager, "fps") == "nothing running"
    assert request_stop(manager, "nope") == "nothing running"
    printer.gcode.run_script("OPENAMS_UNLOAD")
    assert unit.current_spool is None
    assert lane_stage(manager)["message"] != "Stopped by the user"


FAILING_CUT = """
[gcode_macro OAMS_CUT]
variable_fail: 1
gcode:
    {% if printer["gcode_macro OAMS_CUT"].fail %}
      { action_raise_error("no tip profile for ABS") }
    {% endif %}
"""


def last_follower(printer):
    sent = [c for c in printer.trace if c.startswith("follower:")]
    return sent[-1] if sent else None


def test_a_failed_cut_stops_the_reverse_follower_the_unload_armed(setup, klipper):
    # Case 1: the cut raised after the reverse follower was enabled; nothing
    # stopped it and the lane stayed LOADED with following true, direction 0.
    printer, manager, unit = setup(loaded=0)
    load_user_macros(printer, klipper, FAILING_CUT)
    with pytest.raises(printer.command_error, match="no tip profile"):
        printer.gcode.run_script("OPENAMS_UNLOAD")
    assert "follower:1:0" in printer.trace
    assert last_follower(printer) == "follower:0:0"
    lane = manager.runtime.get_state().lanes["fps"]
    assert lane.following is False
    assert lane.op == S.OP_LOADED                  # still physically loaded
    assert lane_stage(manager)["stage"]["failed"] == "cut"
    assert unit.current_spool == 0
    # The UI can retry, and the retry runs to the end.
    printer.objects["gcode_macro OAMS_CUT"].variables["fail"] = 0
    printer.gcode.run_script("OPENAMS_UNLOAD")
    assert unit.current_spool is None


def test_a_failed_flow_during_a_print_is_not_stopped_under_the_print(setup, klipper):
    printer, manager, unit = setup(loaded=0)
    printer.objects["print_stats"].values["state"] = "printing"
    load_user_macros(printer, klipper, FAILING_CUT)
    with pytest.raises(printer.command_error, match="no tip profile"):
        printer.gcode.run_script("OPENAMS_UNLOAD")
    assert "follower:0:0" not in printer.trace     # deferred while printing
    printer.objects["print_stats"].values["state"] = "error"
    manager._monitor(printer.reactor.monotonic())
    assert last_follower(printer) == "follower:0:0"
    assert manager.runtime.get_state().lanes["fps"].following is False


def test_a_stop_by_the_user_leaves_no_follower_running(setup):
    printer, manager, unit = setup(loaded=0)
    cold_extruder(printer)
    printer.reactor.on_pause = lambda now: request_stop(manager)
    with pytest.raises(printer.command_error, match="Stopped by the user"):
        printer.gcode.run_script("OPENAMS_UNLOAD")
    assert manager.runtime.get_state().lanes["fps"].following is False


def test_a_successful_load_keeps_its_forward_follower(setup):
    printer, manager, unit = setup()
    printer.gcode.run_script("OPENAMS_LOAD GROUP=T1 SLOT=3")
    assert manager.runtime.get_state().lanes["fps"].following is True
    manager._monitor(printer.reactor.monotonic())
    assert manager.runtime.get_state().lanes["fps"].following is True


def test_homing_gcode_alone_never_starts_a_runout(setup):
    # Case 3: idle_timeout reads "Printing" while any G-code runs, but
    # print_stats is not a print job; a hub that reads empty is no runout.
    printer, manager, unit = setup(loaded=0)
    printer.objects["idle_timeout"] = Status(state="Printing")
    manager.idle_timeout = printer.objects["idle_timeout"]
    unit.hub_hes_value[0] = False
    manager._monitor(printer.reactor.monotonic())
    manager._monitor(printer.reactor.monotonic())
    assert manager.runtime.get_state().lanes["fps"].runout == S.RUNOUT_IDLE


def test_a_real_print_with_an_empty_hub_still_starts_a_runout(setup):
    printer, manager, unit = setup(loaded=0)
    printer.objects["print_stats"].values["state"] = "printing"
    unit.hub_hes_value[0] = False
    manager.runtime.dispatch(S.Tick())
    assert manager.runtime.get_state().lanes["fps"].runout == S.RUNOUT_PAUSING


def test_no_runout_while_a_flow_is_active_even_in_a_print(setup):
    printer, manager, unit = setup(loaded=0)
    printer.objects["print_stats"].values["state"] = "printing"
    printer.gcode.run_script("OAMSM_STAGE PLAN=home,heat,cut,retract FPS=fps")
    printer.gcode.run_script("OAMSM_STAGE NAME=cut FPS=fps")
    unit.hub_hes_value[0] = False
    manager.runtime.dispatch(S.Tick())
    assert manager.runtime.get_state().lanes["fps"].runout == S.RUNOUT_IDLE


def test_a_hub_flicker_does_not_flash_the_loaded_bay_during_a_flow(setup):
    printer, manager, unit = setup(loaded=0)
    printer.gcode.run_script("OAMSM_STAGE PLAN=home,heat,cut,retract FPS=fps")
    printer.gcode.run_script("OAMSM_STAGE NAME=cut FPS=fps")

    def bay0():
        status = manager.get_status(0.)
        return [b for b in status["devices"]["unit1"]["bays"]
                if b["index"] == 0][0]["loaded"]
    unit.hub_hes_value[0] = False
    assert bay0() is True
    # With no flow running the sensor is shown as it reads.
    printer.gcode.run_script("OAMSM_STAGE DONE=1 FPS=fps")
    assert bay0() is False


if __name__ == "__main__":
    pytest.main([__file__])


def test_the_working_temperature_is_decided_before_g28_and_read_in_the_helper():
    # Under the ordered opt-in, _TX evaluates each line as it runs. The target
    # from before the flow is what _OAMS_PLAN remembers, so it is called above
    # G28 (found on a printer whose homing sets the hotend to 150 C), and the
    # heater's state afterwards is read inside _OAMS_HEAT's own fresh render
    # rather than on its call line.
    macros = parse_config("oams_macros.cfg")
    plans = heats = 0
    for section in macros.sections():
        lines = [line.strip() for line in macros[section]["gcode"].splitlines()]
        g28 = lines.index("G28") if "G28" in lines else len(lines)
        for i, line in enumerate(lines):
            if line.startswith("_OAMS_PLAN"):
                plans += 1
                assert i < g28, (section, line)
            if line.startswith("_OAMS_HEAT"):
                heats += 1
                assert "printer" not in line and "TARGET" not in line, (section, line)
    # 3 plan calls (swap, load and standalone unload) and 2 heat calls.
    assert (plans, heats) == (3, 2)
