"""Toolchange recovery, second round (2026-11-09 review): a failed
toolchange's retry never resumes a print with the wrong filament or none, and
never homes a printer that lost its position.

Uses the Klipper integration harness of test_openams_klipper.py (set
OPENAMS_KLIPPER_DIR to run it).
"""
import importlib.util
import sys
import unittest

try:
    import pytest
except ImportError:
    raise unittest.SkipTest(
        "pytest not installed; the klipper-integration CI job runs this module"
    )

from test_openams_klipper import (  # noqa: F401  (klipper and setup are fixtures)
    ROOT, Status, fault_of, hook, inlet_sensor, is_reload, klipper,
    load_user_macros, printing_with_resume, setup)

CUT_MOVE = "G0 X2 Y355 F1000"
TWO_DAYS = 172800


def paused(printer):
    printer.objects["print_stats"].values["state"] = "paused"


def outlet_sensor(printer, enabled, sees):
    printer.objects["gcode_macro _oams_macro_variables"].variables["fs_extruder_out"] = enabled
    printer.objects["filament_switch_sensor extruder_out"] = Status(filament_detected=sees)


class OutletSeesBay(Status):
    """An outlet sensor that sees filament only once BAY is loaded: clear for
    the cut check of the old filament, then filament after the new one's
    reload."""

    def __init__(self, unit, bay):
        super().__init__()
        self.unit, self.bay = unit, bay

    def get_status(self, now):
        return {"filament_detected": self.unit.current_spool == self.bay}


def outlet_sees_bay(printer, unit, bay):
    printer.objects["gcode_macro _oams_macro_variables"].variables["fs_extruder_out"] = True
    printer.objects["filament_switch_sensor extruder_out"] = OutletSeesBay(unit, bay)


def output(printer):
    """Everything sent to the console from here on."""
    lines = []
    printer.gcode.register_output_handler(lines.append)
    return lines


def fail_once(printer, command, message, original):
    """COMMAND raises MESSAGE the next time, then is ORIGINAL again."""
    gcode = printer.gcode

    def once(gcmd):
        gcode.register_command(command, None)
        gcode.register_command(command, original)
        raise gcmd.error(message)
    gcode.register_command(command, None)
    gcode.register_command(command, once)


class IdleTimeout(Status):
    def __init__(self, timeout):
        super().__init__(state="Idle", printing_time=0.0)
        self.idle_timeout = timeout


def idle_timeout(printer, timeout=600.0):
    """Klipper's idle_timeout, with SET_IDLE_TIMEOUT recorded in the trace."""
    fake = IdleTimeout(timeout)
    printer.objects["idle_timeout"] = fake

    def set_timeout(gcmd):
        printer.trace.append(gcmd.get_commandline())
        fake.idle_timeout = gcmd.get_float("TIMEOUT")
    printer.gcode.register_command("SET_IDLE_TIMEOUT", set_timeout)
    return fake


def moves(trace):
    return [c for c in trace if c.startswith(("G0", "G1", "G28", "feed:", "unload"))]


def reloads(trace):
    return [c for c in trace if is_reload(c)]


def failed_print_toolchange(setup, loaded=0):
    """A print paused by a T1 whose inlet check failed after the AMS fed it."""
    printer, manager, unit = setup(loaded=loaded)
    pause_resume = printing_with_resume(printer, manager)
    inlet_sensor(printer, False)
    printer.gcode.run_script("T1")
    assert pause_resume.is_paused and fault_of(manager)["group"] == "T1"
    paused(printer)
    return printer, manager, unit, pause_resume


# --- A manual action never replaces or drops the print's toolchange ----------

def test_a_successful_unload_in_a_paused_print_keeps_the_prints_toolchange(setup):
    printer, manager, unit, pause_resume = failed_print_toolchange(setup)
    printer.gcode.run_script("OPENAMS_UNLOAD")
    assert unit.current_spool is None
    fault = fault_of(manager)
    assert (fault["group"], fault["panel"]) == ("T1", False)
    inlet_sensor(printer, True)
    printer.gcode.run_script("RESUME")
    assert pause_resume.resumed == 1 and fault_of(manager) is None
    assert unit.current_spool == 2


def test_a_failed_panel_load_in_a_paused_print_leaves_the_prints_toolchange(setup):
    printer, manager, unit, pause_resume = failed_print_toolchange(setup)
    with pytest.raises(printer.command_error):
        printer.gcode.run_script("OPENAMS_LOAD GROUP=T0 SLOT=1")
    fault = fault_of(manager)
    assert (fault["group"], fault["slot"], fault["panel"]) == ("T1", None, False)
    inlet_sensor(printer, True)
    printer.gcode.run_script("RESUME")
    assert pause_resume.resumed == 1 and unit.current_spool == 2


def test_a_failed_panel_unload_in_a_paused_print_is_not_retried_by_resume(setup):
    printer, manager, unit = setup(loaded=0)
    pause_resume = printing_with_resume(printer, manager)
    printer.gcode.run_script("PAUSE")
    paused(printer)
    outlet_sensor(printer, True, True)      # the cut check fails
    with pytest.raises(printer.command_error):
        printer.gcode.run_script("OPENAMS_UNLOAD")
    assert unit.current_spool == 0
    outlet_sensor(printer, False, False)
    printer.trace.clear()
    printer.gcode.run_script("RESUME")
    assert pause_resume.resumed == 1 and unit.current_spool == 0
    assert "unload" not in printer.trace


def test_a_console_toolchange_to_the_failed_group_finishes_it(setup):
    # The lane reads loaded with T1 after the fed failure: a plain T1 would be
    # a same-group no-op that leaves the toolhead empty. It runs as the retry.
    printer, manager, unit, pause_resume = failed_print_toolchange(setup)
    inlet_sensor(printer, True)
    printer.trace.clear()
    printer.gcode.run_script("T1")
    assert reloads(printer.trace)
    assert not [c for c in printer.trace if c.startswith(("feed:", "unload", "G0"))]
    assert fault_of(manager) is None and unit.current_spool == 2
    printer.trace.clear()
    printer.gcode.run_script("RESUME")
    assert pause_resume.resumed == 1 and moves(printer.trace) == []


def test_a_second_toolchange_to_the_failed_group_outside_a_print_loads_the_toolhead(setup):
    printer, manager, unit = setup(loaded=0)
    inlet_sensor(printer, False)
    with pytest.raises(printer.command_error):
        printer.gcode.run_script("T1")
    inlet_sensor(printer, True)
    printer.trace.clear()
    printer.gcode.run_script("T1")
    assert reloads(printer.trace)
    assert fault_of(manager) is None and unit.current_spool == 2


# --- RESUME never prints on with the wrong filament or none ------------------

@pytest.mark.parametrize("action, spool", [
    ("OPENAMS_UNLOAD", None),
    ("OPENAMS_LOAD GROUP=T0 SLOT=0", 0),
], ids=["unloaded", "other-group"])
def test_resume_refuses_while_the_prints_group_is_not_loaded(setup, action, spool):
    printer, manager, unit = setup(loaded=0)
    pause_resume = printing_with_resume(printer, manager)
    printer.gcode.run_script("T1")
    assert unit.current_spool == 2 and fault_of(manager) is None
    printer.gcode.run_script("PAUSE")
    paused(printer)
    printer.gcode.run_script(action)
    assert unit.current_spool == spool
    with pytest.raises(printer.command_error, match="T1"):
        printer.gcode.run_script("RESUME")
    assert pause_resume.resumed == 0 and pause_resume.is_paused
    printer.gcode.run_script("T1")
    printer.gcode.run_script("RESUME")
    assert pause_resume.resumed == 1 and unit.current_spool == 2


# --- A printer that lost its position is never homed under the print ---------

@pytest.mark.parametrize("command", ["RESUME", "OAMSM_RETRY"])
def test_a_retry_in_a_paused_print_refuses_when_the_printer_lost_its_position(setup, command):
    printer, manager, unit, pause_resume = failed_print_toolchange(setup)
    printer.objects["toolhead"].values["homed_axes"] = ""   # idle timeout: M84
    inlet_sensor(printer, True)
    printer.trace.clear()
    with pytest.raises(printer.command_error, match="position"):
        printer.gcode.run_script(command)
    assert moves(printer.trace) == []
    assert pause_resume.resumed == 0 and fault_of(manager)["group"] == "T1"
    printer.gcode.run_script("G28")
    printer.gcode.run_script("RESUME")
    assert pause_resume.resumed == 1 and unit.current_spool == 2


def test_a_retry_outside_a_print_still_homes_first(setup):
    printer, manager, unit = setup(loaded=0)
    inlet_sensor(printer, False)
    with pytest.raises(printer.command_error):
        printer.gcode.run_script("OPENAMS_LOAD GROUP=T1 SLOT=2")
    printer.objects["toolhead"].values["homed_axes"] = ""
    inlet_sensor(printer, True)
    printer.trace.clear()
    printer.gcode.run_script("OAMSM_RETRY")
    assert "G28" in printer.trace and fault_of(manager) is None


# --- The idle timeout keeps the position for two days, then comes back -------

def test_a_paused_toolchange_keeps_the_motors_on_for_two_days(setup):
    printer, manager, unit = setup(loaded=0)
    pause_resume = printing_with_resume(printer, manager)
    idle = idle_timeout(printer, 600.0)
    inlet_sensor(printer, False)
    printer.gcode.run_script("T1")
    assert idle.idle_timeout == TWO_DAYS
    pause = printer.trace.index("PAUSE")
    assert pause < next(i for i, c in enumerate(printer.trace)
                        if c.startswith("SET_IDLE_TIMEOUT"))
    paused(printer)
    with pytest.raises(printer.command_error):
        printer.gcode.run_script("RESUME")          # fails again
    assert idle.idle_timeout == TWO_DAYS
    inlet_sensor(printer, True)
    printer.gcode.run_script("RESUME")
    assert pause_resume.resumed == 1 and idle.idle_timeout == 600.0


def test_the_idle_timeout_comes_back_when_the_paused_print_is_cancelled(setup):
    printer, manager, unit = setup(loaded=0)
    printing_with_resume(printer, manager)
    idle = idle_timeout(printer, 900.0)
    inlet_sensor(printer, False)
    printer.gcode.run_script("T1")
    paused(printer)
    assert idle.idle_timeout == TWO_DAYS
    printer.objects["print_stats"].values["state"] = "cancelled"
    manager._monitor(printer.reactor.monotonic())
    assert idle.idle_timeout == 900.0


def mainsail_resume(printer, restore):
    """Mainsail's RESUME macro: it puts back the idle timeout its PAUSE saved,
    then calls Klipper's resume (RESUME_BASE), the one OpenAMS wraps."""
    def resume(gcmd):
        printer.gcode.run_script_from_command("SET_IDLE_TIMEOUT TIMEOUT=%s" % restore)
        printer.gcode.run_script_from_command("RESUME_BASE")
    printer.gcode.register_command("RESUME", None)
    printer.gcode.register_command("RESUME", resume)


# Found on voron350 (2026-11-09): Mainsail's RESUME puts its own idle timeout
# back before RESUME_BASE, where OpenAMS refuses, so the print stayed paused
# with a one-hour timeout for someone coming back to press RESUME.

def test_a_resume_refused_for_the_prints_group_keeps_the_motors_on(setup):
    printer, manager, unit = setup(loaded=0)
    pause_resume = printing_with_resume(printer, manager)
    mainsail_resume(printer, 600.0)
    idle = idle_timeout(printer, 600.0)
    printer.gcode.run_script("T1")
    printer.gcode.run_script("PAUSE")
    paused(printer)
    printer.gcode.run_script("OPENAMS_UNLOAD")
    with pytest.raises(printer.command_error, match="T1"):
        printer.gcode.run_script("RESUME")
    assert pause_resume.is_paused and idle.idle_timeout == TWO_DAYS
    printer.gcode.run_script("T1")
    printer.gcode.run_script("RESUME")
    assert pause_resume.resumed == 1 and idle.idle_timeout == 600.0


def test_a_retry_that_fails_again_under_mainsails_resume_keeps_the_motors_on(setup):
    printer, manager, unit = setup(loaded=0)
    pause_resume = printing_with_resume(printer, manager)
    mainsail_resume(printer, 600.0)
    idle = idle_timeout(printer, 600.0)
    inlet_sensor(printer, False)
    printer.gcode.run_script("T1")
    paused(printer)
    with pytest.raises(printer.command_error):
        printer.gcode.run_script("RESUME")          # fails again
    assert pause_resume.is_paused and idle.idle_timeout == TWO_DAYS
    inlet_sensor(printer, True)
    printer.gcode.run_script("RESUME")
    assert pause_resume.resumed == 1 and idle.idle_timeout == 600.0


def test_a_resume_refused_for_a_lost_position_leaves_the_timeout_to_the_macro(setup):
    # The print cannot resume any more: nothing is worth two more days of heat.
    printer, manager, unit = setup(loaded=0)
    pause_resume = printing_with_resume(printer, manager)
    mainsail_resume(printer, 600.0)
    idle = idle_timeout(printer, 600.0)
    inlet_sensor(printer, False)
    printer.gcode.run_script("T1")
    paused(printer)
    printer.objects["toolhead"].values["homed_axes"] = ""
    with pytest.raises(printer.command_error, match="position"):
        printer.gcode.run_script("RESUME")
    assert pause_resume.is_paused and idle.idle_timeout == 600.0


def test_a_successful_toolchange_leaves_the_idle_timeout_alone(setup):
    printer, manager, unit = setup(loaded=0)
    printing_with_resume(printer, manager)
    idle = idle_timeout(printer, 600.0)
    printer.gcode.run_script("T1")
    assert idle.idle_timeout == 600.0
    assert not [c for c in printer.trace if c.startswith("SET_IDLE_TIMEOUT")]


# --- A retry continues from where it stopped, and nothing done twice ---------

@pytest.mark.parametrize("sensor", ["none", "sees"])
def test_a_retry_skips_a_reload_that_already_succeeded(setup, sensor):
    printer, manager, unit = setup(loaded=0)
    pause_resume = printing_with_resume(printer, manager)
    if sensor == "sees":
        outlet_sees_bay(printer, unit, 2)
    fail_once(printer, "CLEAN_NOZZLE", "the brush is missing", printer.record)
    printer.gcode.run_script("T1")
    assert pause_resume.is_paused and reloads(printer.trace)
    assert fault_of(manager)["fed"] is True
    paused(printer)
    printer.trace.clear()
    printer.gcode.run_script("RESUME")
    assert pause_resume.resumed == 1 and fault_of(manager) is None
    assert reloads(printer.trace) == []
    assert "CLEAN_NOZZLE" in printer.trace and unit.current_spool == 2


def test_a_retry_skips_the_reload_when_the_after_feed_hook_failed(setup, klipper):
    printer, manager, unit = setup(loaded=0)
    pause_resume = printing_with_resume(printer, manager)
    load_user_macros(printer, klipper, (
        "[gcode_macro _OAMS_HOOK_AFTER_FEED]\n"
        "variable_fail: 1\n"
        "gcode:\n"
        '    {% if printer["gcode_macro _OAMS_HOOK_AFTER_FEED"].fail %}\n'
        '      { action_raise_error("the purge bucket is full") }\n'
        "    {% endif %}\n"
        "    G0 X401 Y401\n"))
    printer.gcode.run_script("T1")
    assert pause_resume.is_paused and fault_of(manager)["fed"] is True
    paused(printer)
    printer.objects["gcode_macro _OAMS_HOOK_AFTER_FEED"].variables["fail"] = 0
    printer.trace.clear()
    printer.gcode.run_script("RESUME")
    assert pause_resume.resumed == 1 and reloads(printer.trace) == []
    assert "G0 X401 Y401" in printer.trace


def test_a_retry_repeats_a_reload_the_outlet_sensor_never_saw(setup):
    printer, manager, unit = setup(loaded=0)
    pause_resume = printing_with_resume(printer, manager)
    outlet_sensor(printer, True, False)     # clear for the cut, then never sees
    printer.gcode.run_script("T1")
    assert pause_resume.is_paused and fault_of(manager)["fed"] is True
    paused(printer)
    outlet_sees_bay(printer, unit, 2)
    printer.trace.clear()
    printer.gcode.run_script("RESUME")
    assert reloads(printer.trace) and pause_resume.resumed == 1
    assert not [c for c in printer.trace if c.startswith(("feed:", "unload"))]


def test_a_same_group_toolchange_that_failed_to_home_runs_again_from_the_start(setup):
    printer, manager, unit = setup(loaded=2)
    pause_resume = printing_with_resume(printer, manager)
    printer.objects["toolhead"].values["homed_axes"] = ""
    fail_once(printer, "G28", "Endstop x still triggered after retract", printer.home)
    printer.gcode.run_script("T1")
    assert pause_resume.is_paused and fault_of(manager)["fed"] is False
    paused(printer)
    printer.gcode.run_script("G28")
    printer.trace.clear()
    printer.gcode.run_script("RESUME")
    assert pause_resume.resumed == 1 and unit.current_spool == 2
    assert reloads(printer.trace) == []
    assert not [c for c in printer.trace if c.startswith(("feed:", "unload"))]


def test_a_targeted_load_to_another_bay_of_the_loaded_group_retries_the_whole_swap(setup):
    printer, manager, unit = setup(loaded=2)
    outlet_sensor(printer, True, True)      # the cut check fails
    with pytest.raises(printer.command_error):
        printer.gcode.run_script("OPENAMS_LOAD GROUP=T1 SLOT=3")
    assert unit.current_spool == 2 and fault_of(manager)["fed"] is False
    outlet_sensor(printer, False, False)
    printer.trace.clear()
    printer.gcode.run_script("OAMSM_RETRY")
    assert unit.current_spool == 3 and fault_of(manager) is None
    trace = printer.trace
    assert trace.index(CUT_MOVE) < trace.index("unload") < trace.index("feed:3")


# --- Messages, refusals and the pause itself ---------------------------------

def test_the_pause_message_names_oamsm_retry(setup):
    printer, manager, unit = setup(loaded=0)
    printing_with_resume(printer, manager)
    lines = output(printer)
    inlet_sensor(printer, False)
    printer.gcode.run_script("T1")
    assert any("OAMSM_RETRY" in line for line in lines)


def test_without_the_resume_wrap_a_failed_toolchange_still_pauses_and_says_how_to_retry(setup):
    printer, manager, unit = setup(loaded=0)
    assert not manager.__dict__.get("_resume_wrapped")
    printer.objects["print_stats"].values["state"] = "printing"
    lines = output(printer)
    inlet_sensor(printer, False)
    printer.gcode.run_script("T1")          # nothing reaches the print
    assert printer.objects["pause_resume"].values["is_paused"]
    assert any("OAMSM_RETRY" in line for line in lines)


class PrintFile(Status):
    """virtual_sdcard: whether the command running came from the print file."""

    def __init__(self):
        super().__init__()
        self.from_file = False

    def is_cmd_from_sd(self):
        return self.from_file


def test_a_later_toolchange_in_the_same_macro_is_left_for_resume(setup, klipper):
    # The rest of a macro runs after PAUSE: a second T<n> in it neither moves
    # nor ends the print, and RESUME runs the toolchange the print wants now.
    printer, manager, unit = setup(loaded=0)
    pause_resume = printing_with_resume(printer, manager)
    sd = printer.objects["virtual_sdcard"] = PrintFile()
    load_user_macros(printer, klipper, (
        "[gcode_macro START_TEST]\n"
        "gcode:\n"
        "    T1\n"
        "    T0\n"))
    inlet_sensor(printer, False)
    sd.from_file = True
    printer.trace.clear()
    printer.gcode.run_script("START_TEST")      # nothing reaches the print
    sd.from_file = False
    assert pause_resume.is_paused
    assert printer.trace.count("feed:2") == 1
    assert not [c for c in printer.trace if c.startswith("feed:0")]
    assert fault_of(manager)["group"] == "T0"
    paused(printer)
    inlet_sensor(printer, True)
    printer.gcode.run_script("RESUME")
    assert pause_resume.resumed == 1 and unit.current_spool in (0, 1)


# Found on voron350 (2026-11-09): a panel load refused before it started (the
# bay is not in the group) stayed recorded through Reset, the panel's Clear
# errors, and Retry repeated the same refusal, so nothing could clear it.

def test_reset_clears_a_panel_load_that_never_started(setup):
    printer, manager, unit = setup(loaded=0)
    with pytest.raises(printer.command_error, match="not assigned"):
        printer.gcode.run_script("OPENAMS_LOAD GROUP=T1 SLOT=0")
    fault = fault_of(manager)
    assert fault["panel"] and fault["step"] is None and not fault["fed"]
    said = output(printer)
    printer.trace.clear()
    printer.gcode.run_script("OAMSM_CLEAR_ERRORS")
    assert fault_of(manager) is None
    assert moves(printer.trace) == [] and unit.current_spool == 0
    assert any("had not started" in line for line in said)


def test_reset_keeps_a_panel_load_that_started(setup):
    printer, manager, unit = setup(loaded=0)
    inlet_sensor(printer, False)
    with pytest.raises(printer.command_error):
        printer.gcode.run_script("OPENAMS_LOAD GROUP=T1 SLOT=2")
    assert fault_of(manager)["fed"]
    said = output(printer)
    printer.gcode.run_script("OAMSM_CLEAR_ERRORS")
    assert fault_of(manager)["group"] == "T1"
    assert any("Retry finishes it" in line for line in said)


def test_abandon_forgets_a_failed_panel_load_and_resets(setup):
    printer, manager, unit = setup(loaded=0)
    inlet_sensor(printer, False)
    with pytest.raises(printer.command_error):
        printer.gcode.run_script("OPENAMS_LOAD GROUP=T1 SLOT=2")
    assert fault_of(manager)["panel"]
    said = output(printer)
    printer.gcode.run_script("OAMSM_CLEAR_ERRORS")
    assert any("Abandon (OAMSM_ABANDON) forgets it" in line for line in said)
    lane = next(iter(manager._tc_faults()))
    printer.trace.clear()
    said = output(printer)
    printer.gcode.run_script("OAMSM_ABANDON")
    assert fault_of(manager) is None
    assert moves(printer.trace) == []
    assert any("abandoned; the filament stays where it stopped" in line
               for line in said)
    with pytest.raises(printer.command_error, match="nothing on lane"):
        printer.gcode.run_script("OAMSM_ABANDON FPS=%s" % lane)
    with pytest.raises(printer.command_error, match="nothing is waiting"):
        printer.gcode.run_script("OAMSM_ABANDON")


def test_abandon_refuses_the_paused_prints_own_toolchange(setup):
    printer, manager, unit, _ = failed_print_toolchange(setup)
    assert not fault_of(manager)["panel"]
    with pytest.raises(printer.command_error, match="is the print's"):
        printer.gcode.run_script("OAMSM_ABANDON")
    assert fault_of(manager) is not None


def test_retry_is_refused_while_the_print_is_printing(setup):
    printer, manager, unit = setup(loaded=0)
    inlet_sensor(printer, False)
    with pytest.raises(printer.command_error):
        printer.gcode.run_script("OPENAMS_LOAD GROUP=T1 SLOT=2")
    printing_with_resume(printer, manager)
    inlet_sensor(printer, True)
    printer.trace.clear()
    with pytest.raises(printer.command_error):
        printer.gcode.run_script("OAMSM_RETRY")
    assert printer.trace == [] and fault_of(manager) is not None


def test_a_pause_inside_the_toolchange_is_not_undone_by_a_move_back(setup, klipper):
    printer, manager, unit = setup(loaded=0)
    pause_resume = printing_with_resume(printer, manager)
    # Klipper renders a whole macro before it runs any of it, so the raise
    # has to come from a command after PAUSE, not from the template.
    load_user_macros(printer, klipper, hook("AFTER_CUT", gcode=(
        "    PAUSE\n"
        '    _OAMS_RAISE MSG="the hook gave up"')))
    printer.gcode.run_script("T1")
    assert pause_resume.is_paused
    assert printer.guard[-1] == "RESTORE_GCODE_STATE NAME=oams_guard"


def test_a_pause_command_that_fails_still_pauses_the_print(setup):
    printer, manager, unit = setup(loaded=0)
    pause_resume = printing_with_resume(printer, manager)
    sent = []

    def send_pause_command():
        sent.append(True)
        pause_resume.values["is_paused"] = True
    pause_resume.send_pause_command = send_pause_command

    def broken_pause(gcmd):
        raise gcmd.error("PAUSE: no park position configured")
    printer.gcode.register_command("PAUSE", None)
    printer.gcode.register_command("PAUSE", broken_pause)
    inlet_sensor(printer, False)
    printer.gcode.run_script("T1")          # nothing reaches the print
    assert sent and pause_resume.is_paused
    assert fault_of(manager)["group"] == "T1"


def test_the_toolhead_eject_puts_the_extrusion_mode_back(setup):
    printer, manager, unit = setup(loaded=2)
    printer.gcode.run_script("_OAMS_TOOLHEAD_EJECT")
    trace = printer.trace
    save = next(i for i, c in enumerate(trace) if c.startswith("SAVE_GCODE_STATE"))
    restore = max(i for i, c in enumerate(trace) if c.startswith("RESTORE_GCODE_STATE"))
    extrusion = [i for i, c in enumerate(trace) if c.startswith(("M83", "G1 E"))]
    assert extrusion and save < min(extrusion) and max(extrusion) < restore


# --- The wiki page patcher (private tool; not in the public tree) ------------

PATCH_PAGES = ROOT / "tools" / "docs" / "wiki-0ams" / "patch_pages.py"


@pytest.mark.skipif(not PATCH_PAGES.exists(), reason="private wiki tools")
def test_the_page_patcher_saves_nothing_when_any_page_does_not_match(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("patch_pages", PATCH_PAGES)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    pages = {1: "alpha beta", 2: "gamma"}
    saved = []

    def gql(query, variables=None):
        if query.lstrip().startswith("query"):
            page = variables["id"]
            return {"pages": {"single": {
                "id": page, "path": "p%d" % page, "locale": "en", "title": "t",
                "description": "", "content": pages[page], "isPublished": True,
                "isPrivate": False, "editor": "markdown", "tags": []}}}
        saved.append(variables["id"])
        return {"pages": {"update": {"responseResult": {"succeeded": True, "message": ""}}}}
    monkeypatch.setattr(module, "gql", gql)
    monkeypatch.setattr(module, "HERE", tmp_path)
    patches = tmp_path / "spec.py"
    patches.write_text("PATCHES = {1: [('alpha', 'ALPHA')], 2: [('missing', 'x')]}\n")
    monkeypatch.setattr(sys, "argv", ["patch_pages.py", str(patches), "--yes"])
    with pytest.raises(SystemExit):
        module.main()
    assert saved == []
