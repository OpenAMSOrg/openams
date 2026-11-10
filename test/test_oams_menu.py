"""The OpenAMS entries of the printer's LCD menu (oams_menu.cfg), loaded and
driven through Klipper's own menu code.

Klipper's config reader, config check, menu, menu keys and G-code macro modules
come from the pinned revision of test_openams_klipper.py (set
OPENAMS_KLIPPER_DIR to run this module). The menu runs against the real
OAMSManager over fake units and a fake 16x4 display: every name is rendered and
every command produced the way a printer's display would produce it, and the
commands are checked against the live topology and run through Klipper's G-code
dispatcher.
"""
import copy
import os
import re
import shutil
import sys
import types
import unittest

try:
    import pytest
except ImportError:
    raise unittest.SkipTest(
        "pytest not installed; the klipper-integration CI job runs this module"
    )

from test_openams_klipper import (  # noqa: F401  (klipper is a fixture)
    KLIPPER_REV, ROOT, S, Config, Fps, Printer, Status, Unit, committed_source,
    klipper, load_macros, module_from_source, oams_manager)

MENU_FILE = ROOT / "oams_menu.cfg"
COLS = 16
# The visible width of an entry on a 16-column display: one column is the
# cursor, and an input never scrolls.
WIDTH = COLS - 1
AMS_ACTIONS = ("clear_errors", "load", "unload", "follower", "calibrate_ptfe")


@pytest.fixture(scope="module")
def kmenu(tmp_path_factory):
    directory = os.environ.get("OPENAMS_KLIPPER_DIR")
    if not directory:
        pytest.skip("set OPENAMS_KLIPPER_DIR for pinned Klipper integration")

    def source(path):
        return committed_source(directory, KLIPPER_REV, path)

    package = "openams_test_klipper_display"
    pkg = types.ModuleType(package)
    pkg.__path__ = []
    sys.modules[package] = pkg
    keys = module_from_source(package + ".menu_keys",
                              source("klippy/extras/display/menu_keys.py"))
    sys.modules[package + ".menu_keys"] = pkg.menu_keys = keys
    # menu.py reads its own default menu.cfg from the folder it lives in.
    folder = tmp_path_factory.mktemp("klipper-display")
    (folder / "menu.cfg").write_text(source("klippy/extras/display/menu.cfg"))
    menu = types.ModuleType(package + ".menu")
    menu.__package__ = package
    menu.__file__ = str(folder / "menu.py")
    exec(compile(source("klippy/extras/display/menu.py"), menu.__file__,
                 "exec"), menu.__dict__)
    configfile = module_from_source("openams_test_configfile",
                                    source("klippy/configfile.py"))
    return types.SimpleNamespace(menu=menu, configfile=configfile)


class MenuPrinter(Printer):
    """The harness printer, with the two printer methods the menu calls."""

    def send_event(self, name, *args):
        for handler in self.events.get(name, []):
            handler(*args)

    def add_object(self, name, obj):
        self.objects[name] = obj


class ConfigFile(Status):
    """The configfile object: the status the macros read, and read_config,
    which the menu uses for Klipper's own menu.cfg."""

    def __init__(self, kmenu, printer, values):
        super().__init__(**values)
        self.kmenu, self.printer = kmenu, printer

    def read_config(self, filename):
        reader = self.kmenu.configfile.ConfigFileReader()
        fileconfig = reader.build_fileconfig(
            reader.read_config_file(filename), filename)
        return self.kmenu.configfile.ConfigWrapper(
            self.printer, fileconfig, {}, "printer")


class MenuUnit(Unit):
    """A harness unit that publishes what a real one does for the menu: its
    supported actions, capabilities, dryer and tag attribution."""

    def __init__(self, printer, oams_idx, actions=AMS_ACTIONS, dryer=None,
                 capabilities=None, tag_state=False, family="ams1"):
        super().__init__(printer, oams_idx)
        self.device_family = family
        self.actions = list(actions)
        self.dryer = dryer
        self.caps = dict(capabilities or {})
        self.tag_state = tag_state
        self._telemetry = {}

    def get_webhook_status(self):
        status = super().get_webhook_status()
        status.update(oams_idx=self.oams_idx, current_spool=self.current_spool,
                      supported_actions=list(self.actions),
                      capabilities=dict(self.caps, dryer=self.dryer is not None),
                      dryer=copy.deepcopy(self.dryer))
        if self.tag_state:
            for bay in status["bays"]:
                bay["tag_state"] = {"state": "unknown", "candidates": []}
        return status


class Display:
    def __init__(self, cols=COLS, rows=4):
        self.cols, self.rows = cols, rows
        self.clear()

    def clear(self):
        self.grid = [[" "] * self.cols for _ in range(self.rows)]

    def get_dimensions(self):
        return self.cols, self.rows

    def draw_text(self, row, col, text, eventtime):
        for i, char in enumerate(text):
            if 0 <= col + i < self.cols:
                self.grid[row][col + i] = char
        return col + len(text)

    def request_redraw(self):
        pass

    def lines(self):
        return ["".join(row) for row in self.grid]


class Lcd:
    """Drives the menu with the display's keys and reads what it shows."""

    def __init__(self, printer, menu, display):
        self.printer, self.menu, self.display = printer, menu, display
        self.scripts = []
        menu.queue_gcode = self._queue

    def _queue(self, script):
        if script:
            self.scripts.append(script)

    def now(self):
        return self.printer.reactor.monotonic()

    def key(self, name):
        self.menu.key_event(name, self.now())

    def container(self):
        return self.menu.stack_peek()

    def entries(self):
        self.menu.update_context(self.now())
        return [item.render_name() for item in self.container()]

    def screen(self):
        self.display.clear()
        self.menu.screen_update_event(self.now())
        return self.display.lines()

    def home(self):
        """The OpenAMS list, from wherever the menu is."""
        if self.menu.is_running():
            self.menu.exit(force=True)
        self.key("click")
        return self.open("OpenAMS")

    def open(self, *path):
        for name in path:
            self.select(name)
            self.key("click")
        return self.entries()

    def select(self, name):
        names = self.entries()
        assert name in names, "%r not in %r" % (name, names)
        target = names.index(name)
        while self.container().selected < target:
            self.key("down")
        while self.container().selected > target:
            self.key("up")

    def label(self):
        """The selected entry's name, as the display shows it now."""
        self.menu.update_context(self.now())
        return self.container().selected_item().render_name(True)

    def pick(self, entry, value):
        """Click a picker, turn it to `value` (from its own name at -1) and
        click again. Returns (the label it showed, the scripts it queued)."""
        self.select(entry)
        before = len(self.scripts)
        self.key("click")
        item = self.container().selected_item()
        assert item.is_editing()
        for _ in range(value + 1):
            self.key("down")
        shown = self.label()
        self.key("click")
        assert not item.is_editing()
        return shown, self.scripts[before:]

    def labels(self, entry):
        """Every value of a picker, from its own name to its last target, and
        what a click on each queues: [(label, scripts)]."""
        self.select(entry)
        item = self.container().selected_item()
        self.menu.update_context(self.now())
        last = int(item._eval_max(item.get_context()))
        return [self.pick(entry, value) for value in range(-1, last + 1)]


class Bench:
    """A printer with lanes, units and groups, its manager, and the LCD menu
    loaded from oams_menu.cfg next to a [display] section."""

    def __init__(self, kmenu, klipper, tmp_path, units, groups,
                 lanes=("fps",), user_cfg=None, display=True):
        self.kmenu = kmenu
        printer = self.printer = MenuPrinter(klipper)
        names = []
        for index, lane in enumerate(lanes):
            extruder = "extruder" if index == 0 else "extruder%d" % index
            if index:
                printer.objects[extruder] = Status(
                    can_extrude=True, temperature=220.0, target=220.0)
            name = "fps" if lane == "fps" else "fps " + lane
            printer.objects[name] = Fps(lane, extruder)
            names.append(name)
        self.units = {}
        for unit_name, idx, lane, options in units:
            unit = MenuUnit(printer, idx, **options)
            unit.fps_name = lane
            printer.objects["oams " + unit_name] = unit
            self.units[unit_name] = unit
            names.append("oams " + unit_name)
        for group, bays in groups:
            printer.objects["filament_group " + group] = types.SimpleNamespace(
                bay_specs=list(bays))
            names.append("filament_group " + group)
        printer.section_names = names + ["oams_manager"]
        self.manager = oams_manager.OAMSManager(Config(printer, "oams_manager"))
        printer.objects["oams_manager"] = self.manager
        load_macros(printer, klipper)
        printer.objects["configfile"] = ConfigFile(
            kmenu, printer, printer.objects["configfile"].values)
        # Klipper publishes each group's section; _TX looks a group up there.
        settings = printer.objects["configfile"].values["settings"]
        for group, bays in groups:
            settings["filament_group " + group.lower()] = {
                "group": ", ".join("%s-%d" % bay for bay in bays)}
        printer.objects["buttons"] = object()
        # Klipper's own main menu reads it.
        printer.objects["idle_timeout"] = Status(state="Idle")
        printer.send_event("klippy:ready")
        self.manager._monitor(printer.reactor.monotonic())
        printer.trace.clear()

        # The config as Klipper reads it: printer.cfg with the include.
        shutil.copy(MENU_FILE, tmp_path / "oams_menu.cfg")
        text = ""
        if display:
            text += "[display]\nlcd_type: st7920\n\n"
        text += "[include oams_menu.cfg]\n"
        if user_cfg is not None:
            # The owner's own menu file, included after OpenAMS's, so its
            # sections override the ones they name.
            (tmp_path / "menu.cfg").write_text(user_cfg)
            text += "[include menu.cfg]\n"
        path = tmp_path / "printer.cfg"
        path.write_text(text)
        reader = kmenu.configfile.ConfigFileReader()
        self.fileconfig = reader.build_fileconfig_with_includes(
            path.read_text(), str(path))
        self.validate = kmenu.configfile.ConfigValidate(printer)
        self.access = self.validate.start_access_tracking(
            reader.build_fileconfig("", "autosave"))
        self.config = kmenu.configfile.ConfigWrapper(
            printer, self.fileconfig, self.access, "printer")
        macro = "gcode_macro _OAMS_MENU"
        printer.objects[macro] = klipper.gcode_macro.GCodeMacro(
            self.config.getsection(macro))
        self.menu = self.lcd = None
        if display:
            section = self.config.getsection("display")
            section.get("lcd_type")         # read by the LCD driver
            self.display = Display()
            self.menu = kmenu.menu.MenuManager(section, self.display)
            self.lcd = Lcd(printer, self.menu, self.display)

    def check_unused(self):
        """Klipper's own startup check of the whole config."""
        self.validate.check_unused(self.fileconfig)

    def printing(self, state="printing"):
        self.printer.objects["print_stats"].values["state"] = state

    def lane(self, name="fps"):
        return self.manager.get_status(0.)["lanes_by_fps"][name]


def one_unit(**options):
    return [("unit1", 1, "fps", options)]


ONE_UNIT_GROUPS = [("T0", [("unit1", 0), ("unit1", 1)]),
                   ("T1", [("unit1", 2), ("unit1", 3)])]

# Two units on one lane through a joiner, a tool per bay; unit2 has a dryer.
DRYER = {"state": "off", "target_c": None, "remaining_s": None,
         "heater_pct": None, "fan_pct": None, "fault": "none", "adapter": True}
TWO_UNITS = [("unit1", 1, "fps", {}),
             ("unit2", 2, "fps", {
                 "actions": AMS_ACTIONS + ("dryer_start", "dryer_stop"),
                 "dryer": DRYER,
                 "capabilities": {"dryer_target_min_c": 45.0,
                                  "dryer_target_max_c": 65.0}})]
TWO_UNIT_GROUPS = [("T%d" % (unit * 4 + bay), [("unit%d" % (unit + 1), bay)])
                   for unit in range(2) for bay in range(4)]


@pytest.fixture
def bench(kmenu, klipper, tmp_path):
    def build(units=None, groups=None, **options):
        return Bench(kmenu, klipper, tmp_path,
                     one_unit() if units is None else units,
                     ONE_UNIT_GROUPS if groups is None else groups, **options)
    return build


def run(bench, scripts):
    for script in scripts:
        bench.printer.gcode.run_script(script)


# ---------------------------------------------------------------------------
# The file, the tree and Klipper's startup check
# ---------------------------------------------------------------------------

def menu_sections():
    reader_lines = MENU_FILE.read_text().splitlines()
    return [line[1:-1] for line in reader_lines
            if line.startswith("[menu ") and line.endswith("]")]


def test_the_file_loads_through_klippers_menu_and_klipper_accepts_it(bench):
    b = bench()
    sections = menu_sections()
    assert sections and all(s.startswith("menu __main __openams")
                            for s in sections)
    for section in sections:
        assert b.menu.lookup_menuitem(section[len("menu "):]) is not None
    # OpenAMS is added at the end of Klipper's own main menu.
    children = b.menu.lookup_children("__main")
    assert children[-1] == "__main __openams"
    assert "__main __control" in children and "__main __setup" in children
    # Every option of the file is read, so Klipper's unused-option check passes.
    for section in b.fileconfig.sections():
        if section.startswith("menu ") or section.startswith("gcode_macro"):
            for option in b.fileconfig.options(section):
                assert (section.lower(), option) in b.access, (section, option)
    b.check_unused()


def test_without_a_display_klipper_refuses_the_menu_sections(bench):
    """The finding behind install.sh: Klipper reads [menu] sections only
    through the primary [display], so without one they are unused sections
    and Klipper refuses to start. install.sh adds the include only where a
    [display] section exists."""
    b = bench(display=False)
    with pytest.raises(b.kmenu.configfile.error,
                       match=r"Section 'menu __main __openams.*' is not a "
                             r"valid config section"):
        b.check_unused()


def test_only_the_primary_display_reads_menu_sections(kmenu):
    """Only [display] itself gets a MenuManager; a [display <name>] never
    does, so install.sh looks for exactly [display]."""
    display = committed_source(os.environ["OPENAMS_KLIPPER_DIR"], KLIPPER_REV,
                               "klippy/extras/display/display.py")
    assert re.search(r"if name == 'display':\n\s+# only load menu for primary"
                     r" display\n\s+self\.menu = menu\.MenuManager\(config,"
                     r" self\)", display)


USER_MENU = """
[menu __main __octoprint]
type: disabled

[menu __main __mine]
type: command
name: My macro
gcode: M117 mine

[menu __main __openams __dryer]
type: disabled
"""


def test_a_users_own_menu_file_keeps_working_beside_it(bench):
    b = bench(units=TWO_UNITS, groups=TWO_UNIT_GROUPS, user_cfg=USER_MENU)
    children = b.menu.lookup_children("__main")
    assert "__main __mine" in children and "__main __openams" in children
    assert isinstance(b.menu.lookup_menuitem("__main __octoprint"),
                      b.kmenu.menu.MenuDisabled)
    b.check_unused()
    b.lcd.key("click")
    main = b.lcd.entries()
    assert "OctoPrint" not in main
    assert main[-2:] == ["OpenAMS", "My macro"]
    # The user hid the dryer list from their own file.
    assert "Dryer" not in b.lcd.open("OpenAMS")


def test_the_whole_entry_can_be_disabled_from_the_owners_file(bench):
    b = bench(user_cfg="[menu __main __openams]\ntype: disabled\n")
    b.check_unused()
    b.lcd.key("click")
    assert "OpenAMS" not in b.lcd.entries()


def test_a_single_picker_cannot_be_disabled_that_way(bench):
    """Klipper merges the owner's section with the shipped one, and a
    disabled entry does not read a picker's options (lcd-menu.md says so)."""
    b = bench(user_cfg="[menu __main __openams __retry]\ntype: disabled\n")
    with pytest.raises(b.kmenu.configfile.error,
                       match="Option '.*' is not valid in section "
                             "'menu __main __openams __retry'"):
        b.check_unused()


def test_the_stop_entry_is_the_one_the_host_listens_for(bench):
    b = bench()
    assert oams_manager.MENU_STOP_EVENT == (
        "menu:%s:populate" % b.menu.lookup_menuitem(
            "__main __openams __stop").get_ns())


def test_no_option_value_carries_a_hash_klipper_would_cut(bench):
    """Klipper strips everything after a hash on every line, so a hash in a
    template would silently truncate it."""
    for number, line in enumerate(MENU_FILE.read_text().splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        assert "#" not in line, "line %d: %s" % (number, line)
        assert " ;" not in line, "line %d: %s" % (number, line)


# ---------------------------------------------------------------------------
# What the menu shows, and when
# ---------------------------------------------------------------------------

def test_idle_and_unloaded_shows_what_can_run(bench):
    b = bench()
    assert b.lcd.home() == ["..", "Stop", "Load", "Reset errors", "Calibrate",
                            "Follower", "Settings", "Status"]
    assert b.lcd.open("Load") == ["..", "Load tool", "Load spool",
                                  "Load, no cut"]


def test_a_loaded_lane_adds_unload(bench):
    b = bench()
    run(b, ["OAMSM_TOOLCHANGE GROUP=T1"])
    assert b.units["unit1"].current_spool == 2
    assert b.lcd.home() == ["..", "Stop", "Load", "Unload", "Reset errors",
                            "Calibrate", "Follower", "Settings", "Status"]
    assert b.lcd.open("Unload") == ["..", "Unload", "Unload, no cut", "Rewind"]


@pytest.mark.parametrize("state", ["printing"])
def test_a_print_hides_every_motion_entry(bench, state):
    b = bench(units=TWO_UNITS, groups=TWO_UNIT_GROUPS)
    run(b, ["OAMSM_TOOLCHANGE GROUP=T1"])
    b.printing(state)
    assert b.lcd.home() == ["..", "Stop", "Dryer", "Settings", "Status"]


def test_a_paused_print_allows_manual_recovery(bench):
    b = bench()
    run(b, ["OAMSM_TOOLCHANGE GROUP=T1"])
    b.printing("paused")
    assert b.lcd.home() == ["..", "Stop", "Load", "Unload", "Reset errors",
                            "Calibrate", "Follower", "Settings", "Status"]


def test_without_openams_the_entry_is_hidden(bench):
    b = bench()
    del b.printer.objects["oams_manager"]
    b.lcd.key("click")
    assert "OpenAMS" not in b.lcd.entries()


# ---------------------------------------------------------------------------
# Pickers: every target, its label, and the command it runs
# ---------------------------------------------------------------------------

def test_load_tool_lists_every_group_and_runs_the_toolchange(bench):
    b = bench()
    b.lcd.home()
    b.lcd.open("Load")
    assert b.lcd.labels("Load tool") == [
        ("Load tool", []),
        ("Load T0?", ["OAMSM_TOOLCHANGE GROUP=T0"]),
        ("Load T1?", ["OAMSM_TOOLCHANGE GROUP=T1"])]
    b.lcd.scripts.clear()
    shown, scripts = b.lcd.pick("Load tool", 1)
    run(b, scripts)
    assert b.units["unit1"].current_spool == 2
    assert b.lane()["group"] == "T1"
    # The same group is now loaded: shown, and nothing runs.
    assert b.lcd.pick("Load tool", 1) == ("T1 loaded", [])


def test_only_a_short_click_runs_a_picker(bench):
    """Klipper keeps an input open after a long press, so a long press that
    ran the command would run it again on the click that closes it."""
    b = bench()
    b.lcd.home()
    b.lcd.open("Load")
    b.lcd.select("Load tool")
    b.lcd.key("click")
    b.lcd.key("down")
    b.lcd.key("long_click")
    assert b.lcd.scripts == []
    assert b.lcd.container().selected_item().is_editing()
    b.lcd.key("click")
    assert b.lcd.scripts == ["OAMSM_TOOLCHANGE GROUP=T0"]


def test_load_tool_names_a_group_with_no_ready_spool(bench):
    b = bench()
    b.units["unit1"].f1s_hes_value = [False, False, True, True]
    b.manager._monitor(b.printer.reactor.monotonic())
    b.lcd.home()
    b.lcd.open("Load")
    assert b.lcd.pick("Load tool", 0) == ("T0 no spool", [])


def test_load_spool_covers_every_bay_of_every_unit(bench):
    b = bench(units=TWO_UNITS, groups=TWO_UNIT_GROUPS)
    b.lcd.home()
    b.lcd.open("Load")
    labels = b.lcd.labels("Load spool")
    assert [label for label, _ in labels] == [
        "Load spool", "Load unit1 1?", "Load unit1 2?", "Load unit1 3?",
        "Load unit1 4?", "Load unit2 1?", "Load unit2 2?", "Load unit2 3?",
        "Load unit2 4?"]
    assert labels[6][1] == ["OPENAMS_LOAD GROUP=T5 SLOT=5"]
    run(b, labels[6][1])
    assert b.units["unit2"].current_spool == 1
    assert b.lane()["current_slot"] == 5


def test_load_spool_says_why_a_bay_cannot_load(bench):
    groups = [("T0", [("unit1", 0)]), ("T1", [("unit1", 1), ("unit1", 2)])]
    b = bench(groups=groups)
    b.units["unit1"].f1s_hes_value = [True, True, False, True]
    b.manager._monitor(b.printer.reactor.monotonic())
    run(b, ["OPENAMS_LOAD GROUP=T1 SLOT=1"])
    b.lcd.home()
    b.lcd.open("Load")
    assert b.lcd.labels("Load spool") == [
        ("Load spool", []),
        ("Load unit1 1?", ["OPENAMS_LOAD GROUP=T0 SLOT=0"]),
        ("unit1 2 loaded", []),
        ("unit1 3 empty", []),
        ("unit1 4 no tool", [])]


def test_load_without_the_cutter_only_into_an_empty_lane(bench):
    b = bench()
    b.lcd.home()
    b.lcd.open("Load")
    assert b.lcd.pick("Load, no cut", 0) == (
        "T0, no cut?", ["OAMSM_LOAD_TO_TOOLHEAD GROUP=T0"])
    run(b, ["OAMSM_TOOLCHANGE GROUP=T0"])
    assert b.lcd.pick("Load, no cut", 1) == ("T1 unload 1st", [])


def test_unload_rewind_and_the_no_cut_unload(bench):
    b = bench()
    run(b, ["OAMSM_TOOLCHANGE GROUP=T1"])
    b.lcd.home()
    b.lcd.open("Unload")
    assert b.lcd.labels("Unload") == [
        ("Unload", []), ("Unload T1?", ["OPENAMS_UNLOAD FPS=fps"])]
    assert b.lcd.labels("Unload, no cut") == [
        ("Unload, no cut", []),
        ("T1, no cut?", ["OAMSM_UNLOAD_FROM_TOOLHEAD FPS=fps"])]
    assert b.lcd.labels("Rewind") == [
        ("Rewind", []),
        ("Rewind unit1 3?", ["OAMS_UNLOAD_SPOOL OAMS=1 SPOOL=2"])]
    run(b, ["OPENAMS_UNLOAD FPS=fps"])
    assert b.units["unit1"].current_spool is None
    assert b.lcd.pick("Unload", 0) == ("Nothing loaded", [])
    assert b.lcd.pick("Rewind", 0) == ("unit1 idle", [])


def test_rewind_is_only_for_an_ams_unit(bench):
    """A klipper-mmu unit's unload is the lane unload; it has no
    OAMS_UNLOAD_SPOOL to rewind with."""
    units = [("box", 1, "fps", {"family": "boxturtle",
                                "actions": ("clear_errors", "load", "unload",
                                            "follower")})]
    b = bench(units=units, groups=[("T0", [("box", 0)]), ("T1", [("box", 1)])])
    run(b, ["OAMSM_TOOLCHANGE GROUP=T0"])
    b.lcd.home()
    b.lcd.open("Unload")
    assert b.lcd.labels("Rewind") == [("Rewind", []), ("box n/a", [])]
    assert b.lcd.pick("Unload", 0) == ("Unload T0?", ["OPENAMS_UNLOAD FPS=fps"])


def test_two_lanes_pick_the_lane_by_what_it_holds(bench):
    units = [("unit1", 1, "lane1", {}), ("unit2", 2, "lane2", {})]
    groups = [("T0", [("unit1", 0)]), ("T1", [("unit2", 0)])]
    b = bench(units=units, groups=groups, lanes=("lane1", "lane2"))
    run(b, ["OAMSM_TOOLCHANGE GROUP=T1"])
    b.lcd.home()
    b.lcd.open("Unload")
    assert b.lcd.labels("Unload") == [
        ("Unload", []), ("lane1 empty", []),
        ("Unload T1?", ["OPENAMS_UNLOAD FPS=lane2"])]
    assert b.lcd.labels("Rewind") == [
        ("Rewind", []), ("unit1 idle", []),
        ("Rewind unit2 1?", ["OAMS_UNLOAD_SPOOL OAMS=2 SPOOL=0"])]
    b.lcd.home()
    assert b.lcd.open("Follower") == ["..", "Follower on", "Follower off"]
    assert b.lcd.labels("Follower on") == [
        ("Follower on", []), ("lane1 empty", []),
        ("Follow T1?", ["OAMSM_FOLLOWER ENABLE=1 DIRECTION=1 FPS=lane2"])]
    run(b, ["OAMSM_FOLLOWER ENABLE=1 DIRECTION=1 FPS=lane2"])


def test_retry_appears_with_a_failed_toolchange_and_runs_oamsm_retry(bench):
    b = bench()
    b.units["unit1"].load_result = S.OAMS_OP_CODE_ERROR_UNSPECIFIED
    with pytest.raises(b.printer.command_error):
        b.printer.gcode.run_script("OAMSM_TOOLCHANGE GROUP=T1")
    assert b.lane()["toolchange_fault"]["group"] == "T1"
    entries = b.lcd.home()
    assert "Retry" in entries and "Reset errors" in entries
    assert b.lcd.labels("Retry") == [
        ("Retry", []), ("Retry T1?", ["OAMSM_RETRY FPS=fps"])]
    b.units["unit1"].load_result = 0
    run(b, ["OAMSM_RETRY FPS=fps"])
    assert b.units["unit1"].current_spool == 2
    assert "Retry" not in b.lcd.home()


def test_reset_errors_and_cancel_load_are_plain_entries(bench):
    b = bench()
    b.lcd.home()
    b.lcd.select("Reset errors")
    b.lcd.key("click")
    assert b.lcd.scripts == ["OAMSM_CLEAR_ERRORS"]
    run(b, b.lcd.scripts)
    # Cancel load shows only while a load is in flight.
    b.units["unit1"].start_load_spool = lambda bay, gen=None: None
    b.manager.runtime.request("fps", S.Load("fps", "T0"))
    assert b.manager.runtime.get_state().lanes["fps"].op == S.OP_LOADING
    entries = b.lcd.home()
    assert "Cancel load" in entries
    b.lcd.scripts.clear()
    b.lcd.select("Cancel load")
    b.lcd.key("click")
    assert b.lcd.scripts == ["OAMSM_LOAD_FILAMENT_CANCEL"]


def test_clear_fault_lists_the_units_with_a_fault_to_clear(bench):
    b = bench(units=TWO_UNITS, groups=TWO_UNIT_GROUPS)
    b.units["unit2"]._telemetry = {"domain": {"system_fault_name": "rfid_fault"}}
    entries = b.lcd.home()
    assert "Clear fault" in entries
    assert b.lcd.labels("Clear fault") == [
        ("Clear fault", []), ("unit1 no fault", []),
        ("Clear unit2?", ["OAMS_CLEAR_FAULT OAMS=2"])]


def test_tags_read_a_bay_and_confirm_it(bench):
    units = one_unit(actions=AMS_ACTIONS + ("rfid_scan",), tag_state=True)
    b = bench(units=units)
    b.units["unit1"].f1s_hes_value = [True, False, True, True]
    b.manager._monitor(b.printer.reactor.monotonic())
    b.lcd.home()
    assert b.lcd.open("Tags") == ["..", "Re-read tag", "It is this slot"]
    read = b.lcd.labels("Re-read tag")
    assert read[1] == ("Read unit1 1?", ["OAMS_RFID_SCAN OAMS=1 BAY=0"])
    assert read[2] == ("unit1 2 empty", [])
    assert b.lcd.pick("It is this slot", 2) == (
        "It is unit1 3?", ["OAMS_RFID_SCAN OAMS=1 BAY=2 ASSERT=1"])


def test_tags_without_host_attribution_offer_no_confirm(bench):
    units = one_unit(actions=AMS_ACTIONS + ("rfid_scan",))
    b = bench(units=units)
    b.lcd.home()
    assert b.lcd.open("Tags") == ["..", "Re-read tag"]


def test_calibration_of_the_path_and_the_hub_sensor(bench):
    units = one_unit(actions=AMS_ACTIONS + ("calibrate_hub_hes",))
    b = bench(units=units)
    b.lcd.home()
    assert b.lcd.open("Calibrate") == ["..", "Calibrate path", "Calibrate hub"]
    assert b.lcd.pick("Calibrate path", 1) == (
        "Calib unit1 2?", ["OAMS_CALIBRATE_PTFE_LENGTH OAMS=1 SPOOL=1"])
    assert b.lcd.pick("Calibrate hub", 3) == (
        "Hub unit1 4?", ["OAMS_CALIBRATE_HUB_HES OAMS=1 SPOOL=3"])
    run(b, ["OAMSM_TOOLCHANGE GROUP=T0"])
    # The path is shared: a loaded lane is in use.
    assert b.lcd.pick("Calibrate path", 2) == ("unit1 3 in use", [])


def test_the_dryer_keeps_its_temperature_and_time_and_clamps_per_unit(bench):
    b = bench(units=TWO_UNITS, groups=TWO_UNIT_GROUPS)
    b.lcd.home()
    assert b.lcd.open("Dryer") == ["..", "Dry temp: 55C", "Dry time: 4h",
                                   "Start dryer", "Stop dryer"]
    # Only the dryer units are targets.
    assert b.lcd.labels("Start dryer") == [
        ("Start dryer", []),
        ("unit2 55C 4h?",
         ["OAMS_DRYER_START OAMS=2 TARGET=55 DURATION=14400"])]
    # The temperature input runs from the lowest to the highest unit limit.
    b.lcd.select("Dry temp: 55C")
    b.lcd.key("click")
    for _ in range(5):
        b.lcd.key("down")
    assert b.lcd.label() == "Dry temp: 65C"
    b.lcd.key("click")
    assert b.lcd.scripts[-1] == (
        "SET_GCODE_VARIABLE MACRO=_OAMS_MENU VARIABLE=dryer_temp VALUE=65")
    run(b, b.lcd.scripts[-1:])
    b.lcd.select("Dry time: 4h")
    b.lcd.key("click")
    for _ in range(4):
        b.lcd.key("down")
    b.lcd.key("click")
    run(b, b.lcd.scripts[-1:])
    assert b.lcd.entries()[1:3] == ["Dry temp: 65C", "Dry time: 8h"]
    assert b.lcd.pick("Start dryer", 0) == (
        "unit2 65C 8h?", ["OAMS_DRYER_START OAMS=2 TARGET=65 DURATION=28800"])
    assert b.lcd.pick("Stop dryer", 0) == ("unit2 is off", [])
    b.units["unit2"].dryer = dict(DRYER, state="timed_dry", target_c=65.0)
    assert b.lcd.pick("Stop dryer", 0) == (
        "Stop unit2 65C?", ["OAMS_DRYER_STOP OAMS=2"])
    assert b.lcd.pick("Start dryer", 0) == ("unit2 drying", [])
    b.units["unit2"].dryer = dict(DRYER, adapter=False)
    assert b.lcd.pick("Start dryer", 0) == ("unit2 no power", [])


def test_a_temperature_below_the_units_floor_is_raised_to_it(bench):
    b = bench(units=TWO_UNITS, groups=TWO_UNIT_GROUPS)
    run(b, ["SET_GCODE_VARIABLE MACRO=_OAMS_MENU VARIABLE=dryer_temp VALUE=30"])
    b.lcd.home()
    b.lcd.open("Dryer")
    assert b.lcd.pick("Start dryer", 0)[1] == [
        "OAMS_DRYER_START OAMS=2 TARGET=45 DURATION=14400"]


def test_settings_switch_only_when_changed(bench):
    b = bench()
    b.lcd.home()
    assert b.lcd.open("Settings") == [
        "..", "Autoreload: on", "Tag insert: on", "Tag start: on",
        "New spools: on", "Apply PA: off", "Sync trays: on"]
    b.lcd.select("Autoreload: on")
    b.lcd.key("click")
    b.lcd.key("click")              # unchanged: nothing runs
    assert b.lcd.scripts == []
    b.lcd.key("click")
    b.lcd.key("up")
    assert b.lcd.label() == "Autoreload: off"
    b.lcd.key("click")
    assert b.lcd.scripts == [
        "OAMSM_SET_OPTION NAME=auto_reload_on_runout VALUE=0"]
    b.lcd.select("Apply PA: off")
    b.lcd.key("click")
    b.lcd.key("down")
    b.lcd.key("click")
    assert b.lcd.scripts[-1] == "OAMSM_SET_OPTION NAME=apply_pa_on_load VALUE=1"


def test_status_commands_answer_on_the_console(bench):
    b = bench()
    b.lcd.home()
    assert b.lcd.open("Status") == ["..", "Self-test", "Loaded tools"]
    for entry in ("Self-test", "Loaded tools"):
        b.lcd.select(entry)
        b.lcd.key("click")
    assert b.lcd.scripts == ["OAMSM_SELFTEST", "OAMSM_CURRENT_LOADED_GROUP"]
    lines = []
    b.printer.gcode.register_output_handler(lines.append)
    run(b, ["OAMSM_CURRENT_LOADED_GROUP"])
    assert any("No group is currently loaded" in line for line in lines)


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------

def test_opening_stop_stops_the_running_sequence(bench):
    b = bench()
    run(b, ["OAMSM_STAGE PLAN=heat,feed FPS=fps", "OAMSM_STAGE NAME=heat FPS=fps"])
    lines = []
    b.printer.gcode.register_output_handler(lines.append)
    b.lcd.home()
    assert b.lcd.open("Stop") == ["..", "Stop sent"]
    assert "fps" in b.manager._stop_requested
    assert any("Stop from the printer's menu: stopping" in line
               for line in lines)
    with pytest.raises(b.printer.command_error, match="Stopped by the user"):
        b.printer.gcode.run_script("OAMSM_CHECK_STOP FPS=fps")
    # Clicking "Stop sent" goes back to the OpenAMS list.
    b.lcd.select("Stop sent")
    b.lcd.key("click")
    assert b.lcd.entries()[1] == "Stop"


def test_stop_with_nothing_running_changes_nothing(bench):
    b = bench()
    b.lcd.home()
    b.lcd.open("Stop")
    assert not b.manager._stop_requested
    b.printer.gcode.run_script("OAMSM_CHECK_STOP FPS=fps")


def test_the_menu_stop_never_raises_into_klippers_menu(bench):
    b = bench()

    def broken(lane=None):
        raise RuntimeError("boom")
    b.manager.stop_sequences = broken
    b.lcd.home()
    assert b.lcd.open("Stop") == ["..", "Stop sent"]


# ---------------------------------------------------------------------------
# Every command a menu entry can produce is a valid OpenAMS command line
# ---------------------------------------------------------------------------

COMMAND_RE = re.compile(r"^([A-Z_]+)((?: [A-Z_]+=\S+)*)$")


def check_command(bench, line):
    """`line` is one OpenAMS command with arguments the live topology accepts."""
    match = COMMAND_RE.match(line)
    assert match, line
    command = match.group(1)
    params = dict(p.split("=", 1) for p in match.group(2).split())
    status = bench.manager.get_status(0.)
    topology = status["topology"]
    units = {str(u["id"]): u for u in status["units"]}
    groups = topology["groups"]
    lanes = topology["fps"]
    allowed = {
        "OAMSM_TOOLCHANGE": {"GROUP"},
        "OAMSM_LOAD_TO_TOOLHEAD": {"GROUP"},
        "OPENAMS_LOAD": {"GROUP", "SLOT"},
        "OPENAMS_UNLOAD": {"FPS"},
        "OAMSM_UNLOAD_FROM_TOOLHEAD": {"FPS"},
        "OAMSM_RETRY": {"FPS"},
        "OAMSM_FOLLOWER": {"ENABLE", "DIRECTION", "FPS"},
        "OAMS_UNLOAD_SPOOL": {"OAMS", "SPOOL"},
        "OAMS_RFID_SCAN": {"OAMS", "BAY", "ASSERT"},
        "OAMS_CALIBRATE_PTFE_LENGTH": {"OAMS", "SPOOL"},
        "OAMS_CALIBRATE_HUB_HES": {"OAMS", "SPOOL"},
        "OAMS_CLEAR_FAULT": {"OAMS"},
        "OAMS_DRYER_START": {"OAMS", "TARGET", "DURATION"},
        "OAMS_DRYER_STOP": {"OAMS"},
        "OAMSM_SET_OPTION": {"NAME", "VALUE"},
        "SET_GCODE_VARIABLE": {"MACRO", "VARIABLE", "VALUE"},
        "OAMSM_CLEAR_ERRORS": set(), "OAMSM_LOAD_FILAMENT_CANCEL": set(),
        "OAMSM_SELFTEST": set(), "OAMSM_CURRENT_LOADED_GROUP": set(),
    }
    assert command in allowed, line
    assert set(params) <= allowed[command], line
    # Every command is a documented one (C3), the macro helper aside.
    if command != "SET_GCODE_VARIABLE":
        contract = (ROOT / "docs/contracts/C3_commands.md").read_text()
        macros = (ROOT / "oams_macros.cfg").read_text()
        assert command in contract or "[gcode_macro %s]" % command in macros
    if "GROUP" in params:
        assert params["GROUP"] in groups, line
    if "SLOT" in params:
        slot = int(params["SLOT"])
        owner = [(u["name"], s["bay"]) for u in status["units"]
                 for s in u["slots"] if s["id"] == slot]
        assert owner and "%s-%d" % owner[0] in groups[params["GROUP"]]["bays"]
    if "FPS" in params:
        assert params["FPS"] in lanes, line
    if "OAMS" in params:
        unit = units[params["OAMS"]]
        if command != "OAMS_CLEAR_FAULT":
            # The other OAMS_* commands are the AMS driver's (src/oams.py).
            assert topology["oams"][unit["name"]]["family"] in \
                oams_manager.AMS_FAMILIES, line
        for key in ("SPOOL", "BAY"):
            if key in params:
                assert 0 <= int(params[key]) < len(unit["slots"]), line
    if command == "OAMS_DRYER_START":
        caps = status["devices"][unit["name"]]["capabilities"]
        assert caps.get("dryer_target_min_c", 30) <= int(params["TARGET"]) \
            <= caps.get("dryer_target_max_c", 80)
        assert 1 <= int(params["DURATION"]) <= 7 * 24 * 3600
    if command == "OAMSM_SET_OPTION":
        assert params["NAME"] in oams_manager.SETTING_DEFAULTS
        assert params["VALUE"] in ("0", "1")
    if "ASSERT" in params:
        assert params["ASSERT"] == "1"


def every_script(bench):
    """Every picker value and command of every OpenAMS list now shown."""
    lcd = bench.lcd
    scripts = []
    for entry in lcd.home()[1:]:
        lcd.home()
        lcd.select(entry)
        item = lcd.container().selected_item()
        if isinstance(item, bench.kmenu.menu.MenuList):
            if entry == "Stop":
                continue
            for child in lcd.open(entry)[1:]:
                lcd.select(child)
                scripts += entry_scripts(bench, child)
        else:
            scripts += entry_scripts(bench, entry)
    return scripts


def entry_scripts(bench, entry):
    lcd = bench.lcd
    item = lcd.container().selected_item()
    if isinstance(item, bench.kmenu.menu.MenuInput):
        bench.menu.update_context(lcd.now())
        if item._eval_value(item.get_context()) != -1:
            # A value input (a temperature, a switch): one step each way.
            found = []
            for turn in ("down", "up"):
                before = len(lcd.scripts)
                lcd.key("click")
                lcd.key(turn)
                assert len(lcd.label()) <= WIDTH, lcd.label()
                lcd.key("click")
                found += lcd.scripts[before:]
            return found
        found = []
        for label, scripts in lcd.labels(entry):
            assert len(label) <= WIDTH, label
            found += scripts
        return found
    before = len(lcd.scripts)
    lcd.key("click")
    return lcd.scripts[before:]


SCENARIOS = {
    "one unit idle": dict(),
    "one unit loaded": dict(load="T1"),
    "two units with a dryer, loaded": dict(
        units=TWO_UNITS, groups=TWO_UNIT_GROUPS, load="T5"),
    "two lanes": dict(units=[("unit1", 1, "lane1", {}),
                             ("unit2", 2, "lane2", {})],
                      groups=[("T0", [("unit1", 0)]), ("T1", [("unit2", 1)])],
                      lanes=("lane1", "lane2"), load="T1"),
    "paused": dict(load="T0", state="paused"),
    "printing": dict(units=TWO_UNITS, groups=TWO_UNIT_GROUPS, load="T0",
                     state="printing"),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_entry_expands_to_a_valid_openams_command(bench, name):
    options = dict(SCENARIOS[name])
    load = options.pop("load", None)
    state = options.pop("state", None)
    b = bench(**options)
    if load:
        run(b, ["OAMSM_TOOLCHANGE GROUP=%s" % load])
    if state:
        b.printing(state)
    scripts = every_script(b)
    assert scripts
    for script in scripts:
        for line in script.splitlines():
            check_command(b, line.strip())
    if state == "printing":
        motion = [s for s in scripts if not s.startswith(
            ("OAMSM_SET_OPTION", "SET_GCODE_VARIABLE", "OAMSM_SELFTEST",
             "OAMSM_CURRENT_LOADED_GROUP", "OAMS_DRYER_"))]
        assert motion == []


def test_every_rest_name_fits_a_16_column_display(bench):
    b = bench(units=TWO_UNITS, groups=TWO_UNIT_GROUPS)
    for section in b.fileconfig.sections():
        if not section.startswith("menu "):
            continue
        item = b.menu.lookup_menuitem(section[len("menu "):])
        b.menu.update_context(0.)
        name = item.render_name()
        limit = WIDTH - 1 if isinstance(item, b.kmenu.menu.MenuList) else WIDTH
        assert len(name) <= limit, (section, name)
