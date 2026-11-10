"""Toolchange recovery, third round (2026-11-10 stack review): a manual action
during the pause never leaves RESUME a stale picture of the lane, a print that
cannot pause ends instead of printing on, a retry that fails early forgets
nothing, and nothing homes a paused print that lost its position.

Uses the Klipper integration harness of test_openams_klipper.py (set
OPENAMS_KLIPPER_DIR to run it).
"""
import unittest

try:
    import pytest
except ImportError:
    raise unittest.SkipTest(
        "pytest not installed; the klipper-integration CI job runs this module"
    )

from test_openams_klipper import (  # noqa: F401  (klipper and setup are fixtures)
    S, fault_of, hook, inlet_sensor, klipper, load_user_macros,
    printing_with_resume, setup)
from test_recovery_review import (
    CUT_MOVE, TWO_DAYS, PrintFile, fail_once, idle_timeout, mainsail_resume,
    moves, outlet_sensor, output, paused, reloads)


def feeds(trace):
    return [c for c in trace if c.startswith("feed:")]


def print_feed_failure(setup, loaded=0):
    """A print paused by a T1 whose feed failed: nothing reached the gears."""
    printer, manager, unit = setup(loaded=loaded)
    pause_resume = printing_with_resume(printer, manager)
    unit.load_result = S.OAMS_OP_CODE_ERROR_BUSY
    printer.gcode.run_script("T1")
    fault = fault_of(manager)
    assert pause_resume.is_paused and (fault["step"], fault["fed"]) == ("feed", False)
    unit.load_result = 0
    paused(printer)
    return printer, manager, unit, pause_resume


# --- #1: a manual Load of the failed group during the pause -----------------

def test_a_panel_load_of_the_failed_group_finishes_the_prints_toolchange(setup):
    # It loaded the group to the nozzle: RESUME must not rewind it against
    # the extruder gears (the bare rewind is for a feed that stopped short).
    printer, manager, unit, pause_resume = print_feed_failure(setup)
    printer.gcode.run_script("OPENAMS_LOAD GROUP=T1 SLOT=3")
    assert unit.current_spool == 3 and fault_of(manager) is None
    printer.trace.clear()
    printer.gcode.run_script("RESUME")
    assert pause_resume.resumed == 1 and moves(printer.trace) == []


def test_a_failed_panel_load_of_the_failed_group_leaves_resume_the_toolhead_part(setup):
    printer, manager, unit, pause_resume = print_feed_failure(setup)
    inlet_sensor(printer, False)
    with pytest.raises(printer.command_error, match="inlet"):
        printer.gcode.run_script("OPENAMS_LOAD GROUP=T1 SLOT=3")
    fault = fault_of(manager)
    assert (fault["group"], fault["slot"], fault["panel"], fault["fed"]) == \
        ("T1", None, False, True)
    inlet_sensor(printer, True)
    printer.trace.clear()
    printer.gcode.run_script("RESUME")
    assert pause_resume.resumed == 1 and unit.current_spool == 3
    assert reloads(printer.trace) and "unload" not in printer.trace
    assert feeds(printer.trace) == []


def test_a_failed_panel_unload_of_a_cut_filament_is_never_reloaded_by_resume(setup):
    # The print's T1 failed after its feed (fed: the toolhead part was left).
    # A panel Unload then cut the filament and failed at the rewind: RESUME
    # must take the cut filament out, not reload it as if it were at the gears.
    printer, manager, unit = setup(loaded=0)
    pause_resume = printing_with_resume(printer, manager)
    inlet_sensor(printer, False)
    printer.gcode.run_script("T1")
    assert fault_of(manager)["fed"] is True
    paused(printer)
    unit.unload_result = False
    with pytest.raises(printer.command_error):
        printer.gcode.run_script("OPENAMS_UNLOAD")
    assert unit.current_spool == 2
    unit.unload_result = True
    inlet_sensor(printer, True)
    printer.trace.clear()
    printer.gcode.run_script("RESUME")
    assert pause_resume.resumed == 1 and unit.current_spool in (2, 3)
    trace = printer.trace
    assert trace.index(CUT_MOVE) < trace.index("unload") < trace.index(feeds(trace)[0])


def test_resume_rewinds_without_a_cut_only_after_a_feed_that_stopped_short(setup, klipper):
    # The print's T1 stopped at the user's own step after the cut. The lane
    # holds T1 now (loaded from the console), but where its filament ends is
    # not known: RESUME takes it out through the cutter, never a bare rewind.
    printer, manager, unit = setup(loaded=0)
    pause_resume = printing_with_resume(printer, manager)
    load_user_macros(printer, klipper, (
        "[gcode_macro _OAMS_HOOK_AFTER_CUT]\n"
        'variable_step: "wipe"\n'
        "variable_fail: 1\n"
        "gcode:\n"
        '    {% if printer["gcode_macro _OAMS_HOOK_AFTER_CUT"].fail %}\n'
        '      { action_raise_error("the brush is jammed") }\n'
        "    {% endif %}\n"))
    printer.gcode.run_script("T1")
    fault = fault_of(manager)
    assert pause_resume.is_paused and (fault["step"], fault["fed"]) == ("wipe", False)
    paused(printer)
    printer.objects["gcode_macro _OAMS_HOOK_AFTER_CUT"].variables["fail"] = 0
    printer.gcode.run_script("OAMSM_UNLOAD_FILAMENT FPS=fps")
    printer.gcode.run_script("OAMSM_LOAD_FILAMENT GROUP=T1")
    assert unit.current_spool == 2 and fault_of(manager)["step"] == "wipe"
    printer.trace.clear()
    printer.gcode.run_script("RESUME")
    assert pause_resume.resumed == 1 and unit.current_spool in (2, 3)
    trace = printer.trace
    assert trace.index(CUT_MOVE) < trace.index("unload")


# --- #2: a print that cannot be paused --------------------------------------

def test_a_failed_toolchange_ends_a_print_that_cannot_be_paused(setup):
    # Without [pause_resume] nothing can pause the print (and the shipped
    # macros, which read printer.pause_resume, fail at once): the error
    # reaches the print, rather than letting it print on unchanged.
    printer, manager, unit = setup(loaded=0)
    printer.objects["print_stats"].values["state"] = "printing"
    del printer.objects["pause_resume"]
    printer.gcode.register_command("PAUSE", None)
    lines = output(printer)
    with pytest.raises(printer.command_error, match="pause_resume"):
        printer.gcode.run_script("T1")
    assert not any("The print is paused" in line for line in lines)
    assert any("could not pause the print" in line for line in lines)


def test_a_failed_toolchange_ends_the_print_when_pause_does_not_pause_it(setup):
    printer, manager, unit = setup(loaded=0)
    pause_resume = printing_with_resume(printer, manager)
    printer.gcode.register_command("PAUSE", None)
    printer.gcode.register_command("PAUSE", lambda gcmd: None)
    inlet_sensor(printer, False)
    with pytest.raises(printer.command_error, match="inlet"):
        printer.gcode.run_script("T1")
    assert not pause_resume.is_paused


def test_startup_warns_when_there_is_no_pause_resume(setup):
    printer, manager, unit = setup(loaded=0)
    warnings = []
    printer.objects["configfile"].runtime_warning = warnings.append
    del printer.objects["pause_resume"]
    printer.send_event("klippy:ready")
    assert any("[pause_resume]" in warning and "ends the print" in warning
               for warning in warnings)


# --- #3: a retry after the reload that fails early --------------------------

def test_a_retry_after_the_reload_that_fails_early_still_skips_the_reload(setup):
    printer, manager, unit = setup(loaded=0)
    pause_resume = printing_with_resume(printer, manager)
    fail_once(printer, "CLEAN_NOZZLE", "the brush is missing", printer.record)
    printer.gcode.run_script("T1")
    assert pause_resume.is_paused and fault_of(manager)["fed"] is True
    paused(printer)
    follower = printer.gcode.gcode_handlers["OAMSM_FOLLOWER"]
    fail_once(printer, "OAMSM_FOLLOWER", "the follower did not answer", follower)
    with pytest.raises(printer.command_error, match="follower"):
        printer.gcode.run_script("RESUME")
    printer.trace.clear()
    printer.gcode.run_script("RESUME")
    assert pause_resume.resumed == 1 and fault_of(manager) is None
    assert reloads(printer.trace) == [] and "CLEAN_NOZZLE" in printer.trace


# --- #14: a print's T<n> run as a retry -------------------------------------

def test_a_prints_toolchange_run_as_a_retry_pauses_when_a_fault_stays_latched(setup):
    printer, manager, unit = setup(loaded=0)
    inlet_sensor(printer, False)
    with pytest.raises(printer.command_error):
        printer.gcode.run_script("OPENAMS_LOAD GROUP=T1 SLOT=2")
    pause_resume = printing_with_resume(printer, manager)
    identity = ("stuck",)
    manager.runtime.dispatch(S.FirmwareFault(None, 255, 7, identity))
    printer.gcode.register_command("OAMS_CLEAR_FAULT", lambda gcmd: None)
    printer.gcode.run_script("T1")          # nothing reaches the print
    assert pause_resume.is_paused
    fault = fault_of(manager)
    assert (fault["group"], fault["panel"], fault["fed"]) == ("T1", False, True)
    assert "latched" in fault["reason"]
    paused(printer)
    printer.gcode.register_command("OAMS_CLEAR_FAULT", None)
    printer.gcode.register_command(
        "OAMS_CLEAR_FAULT",
        lambda gcmd: manager.runtime.dispatch(S.FirmwareFaultCleared(255, identity)))
    inlet_sensor(printer, True)
    printer.trace.clear()
    printer.gcode.run_script("RESUME")
    assert pause_resume.resumed == 1 and unit.current_spool == 2
    assert feeds(printer.trace) == []


# --- #15: a print's T<n> and an old panel fault -----------------------------

def test_a_prints_toolchange_does_not_inherit_the_bay_of_a_failed_panel_load(setup):
    printer, manager, unit = setup(loaded=0)
    unit.load_result = S.OAMS_OP_CODE_ERROR_BUSY
    with pytest.raises(printer.command_error):
        printer.gcode.run_script("OPENAMS_LOAD GROUP=T1 SLOT=3")
    fault = fault_of(manager)
    assert (fault["panel"], fault["slot"]) == (True, "3")
    unit.load_result = 0
    unit.f1s_hes_value[3] = False           # bay 3 has since been emptied
    pause_resume = printing_with_resume(printer, manager)
    printer.gcode.run_script("T1")
    assert not pause_resume.is_paused and fault_of(manager) is None
    assert unit.current_spool == 2


# --- #16: a later T<n> keeps where the failed toolchange stopped ------------

def test_a_later_toolchange_keeps_where_the_failed_one_stopped(setup, klipper):
    # T1 failed at the cut check with T0 still loaded, cut. The macro's next
    # T0 is left for RESUME, which must not take the cut T0 for printable.
    printer, manager, unit = setup(loaded=0)
    pause_resume = printing_with_resume(printer, manager)
    sd = printer.objects["virtual_sdcard"] = PrintFile()
    load_user_macros(printer, klipper, (
        "[gcode_macro START_TEST]\n"
        "gcode:\n"
        "    T1\n"
        "    T0\n"))
    outlet_sensor(printer, True, True)      # the cut check fails
    sd.from_file = True
    printer.gcode.run_script("START_TEST")
    sd.from_file = False
    assert pause_resume.is_paused and unit.current_spool == 0
    fault = fault_of(manager)
    assert (fault["group"], fault["step"], fault["fed"]) == ("T0", "cut", False)
    paused(printer)
    outlet_sensor(printer, False, False)
    printer.trace.clear()
    printer.gcode.run_script("RESUME")
    assert pause_resume.resumed == 1 and unit.current_spool in (0, 1)
    trace = printer.trace
    assert trace.index(CUT_MOVE) < trace.index("unload") < trace.index(feeds(trace)[0])
    assert reloads(trace)


# --- #17: nothing homes a paused print --------------------------------------

@pytest.mark.parametrize("command", [
    "OPENAMS_UNLOAD", "OPENAMS_LOAD GROUP=T0 SLOT=1", "T0", "T1"])
def test_nothing_homes_a_paused_print_that_lost_its_position(setup, command):
    printer, manager, unit = setup(loaded=2)
    pause_resume = printing_with_resume(printer, manager)
    printer.gcode.run_script("PAUSE")
    paused(printer)
    printer.objects["toolhead"].values["homed_axes"] = ""
    printer.trace.clear()
    with pytest.raises(printer.command_error, match="lost its position"):
        printer.gcode.run_script(command)
    assert moves(printer.trace) == [] and unit.current_spool == 2


@pytest.mark.parametrize("command", ["OAMSM_RETRY", "T1"])
def test_a_retry_of_a_panel_fault_in_a_paused_print_never_homes(setup, command):
    printer, manager, unit = setup(loaded=0)
    inlet_sensor(printer, False)
    with pytest.raises(printer.command_error):
        printer.gcode.run_script("OPENAMS_LOAD GROUP=T1 SLOT=2")
    printing_with_resume(printer, manager)
    printer.gcode.run_script("PAUSE")
    paused(printer)
    printer.objects["toolhead"].values["homed_axes"] = ""
    inlet_sensor(printer, True)
    printer.trace.clear()
    with pytest.raises(printer.command_error, match="lost its position"):
        printer.gcode.run_script(command)
    assert moves(printer.trace) == [] and fault_of(manager)["panel"] is True


# --- #23: a retry counts only with the group loaded -------------------------

def test_resume_loads_the_group_even_when_the_current_object_was_excluded(setup):
    # The print's first toolchange failed; the object was excluded during the
    # pause. The toolchange the print asked for still has to happen.
    printer, manager, unit, pause_resume = print_feed_failure(setup, loaded=None)
    printer.objects["exclude_object"].values.update(
        current_object="PART_1", excluded_objects=["PART_1"])
    printer.gcode.run_script("RESUME")
    assert pause_resume.resumed == 1 and unit.current_spool == 2


def test_a_retry_that_leaves_the_group_unloaded_does_not_count(setup):
    printer, manager, unit, pause_resume = print_feed_failure(setup, loaded=None)
    # A _TX of the printer's own that moves nothing.
    printer.gcode.register_command("_TX", None)
    printer.gcode.register_command("_TX", lambda gcmd: None)
    with pytest.raises(printer.command_error, match="nothing is loaded.*stays paused"):
        printer.gcode.run_script("RESUME")
    assert pause_resume.resumed == 0 and fault_of(manager)["group"] == "T1"


# --- #25: Klipper's own resume refusing -------------------------------------

def test_a_resume_that_klippers_resume_refuses_keeps_the_motors_on(setup):
    printer, manager, unit = setup(loaded=0)
    pause_resume = printing_with_resume(printer, manager)
    mainsail_resume(printer, 600.0)
    idle = idle_timeout(printer, 600.0)
    inlet_sensor(printer, False)
    printer.gcode.run_script("T1")
    paused(printer)
    inlet_sensor(printer, True)
    pause_resume.refuse = "Move out of range"
    with pytest.raises(printer.command_error, match="out of range"):
        printer.gcode.run_script("RESUME")
    assert pause_resume.is_paused and idle.idle_timeout == TWO_DAYS
    pause_resume.refuse = None
    printer.gcode.run_script("RESUME")
    assert pause_resume.resumed == 1 and idle.idle_timeout == 600.0


def test_a_plain_pause_that_klippers_resume_refuses_leaves_the_timeout_alone(setup):
    printer, manager, unit = setup(loaded=0)
    pause_resume = printing_with_resume(printer, manager)
    mainsail_resume(printer, 600.0)
    idle = idle_timeout(printer, 600.0)
    printer.gcode.run_script("PAUSE")
    paused(printer)
    pause_resume.refuse = "Move out of range"
    with pytest.raises(printer.command_error):
        printer.gcode.run_script("RESUME")
    assert idle.idle_timeout == 600.0
