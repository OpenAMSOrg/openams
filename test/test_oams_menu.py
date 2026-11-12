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
    inlet_sensor, klipper, load_macros, module_from_source, oams_manager,
    printing_with_resume)

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
                 lanes=("fps",), user_cfg=None, display=True,
                 stock_menu=None):
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

        # The manager sees every section of the printer's config, the menu
        # sections included.
        printer.section_names = (names + ["oams_manager"]
                                 + list(self.fileconfig.sections()))
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
        macro = "gcode_macro _OAMS_MENU"
        printer.objects[macro] = klipper.gcode_macro.GCodeMacro(
            self.config.getsection(macro))
        self.menu = self.lcd = None
        if display:
            section = self.config.getsection("display")
            section.get("lcd_type")         # read by the LCD driver
            self.display = Display()
            # Klipper loads its own menu.cfg from the folder menu.py is in.
            module_file = kmenu.menu.__dict__["__file__"]
            if stock_menu is not None:
                (tmp_path / "stock").mkdir()
                (tmp_path / "stock" / "menu.cfg").write_text(stock_menu)
                kmenu.menu.__dict__["__file__"] = str(
                    tmp_path / "stock" / "menu.py")
            try:
                self.menu = kmenu.menu.MenuManager(section, self.display)
            finally:
                kmenu.menu.__dict__["__file__"] = module_file
            self.lcd = Lcd(printer, self.menu, self.display)
        # Klipper is ready once every object is loaded, the menu included.
        printer.send_event("klippy:ready")
        self.manager._monitor(printer.reactor.monotonic())
        printer.trace.clear()

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
AMS2_PRO = {"actions": AMS_ACTIONS + ("dryer_start", "dryer_stop"),
            "dryer": DRYER, "family": "ams2",
            "capabilities": {"dryer_target_min_c": 45.0,
                             "dryer_target_max_c": 65.0}}
AMS_HT = dict(AMS2_PRO, family="ams_ht",
              capabilities={"dryer_target_min_c": 45.0,
                            "dryer_target_max_c": 85.0})
TWO_UNITS = [("unit1", 1, "fps", {}), ("unit2", 2, "fps", AMS2_PRO)]
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
    assert sections and all(s.startswith(("menu __main __openams",
                                          "menu __main __oams_stop"))
                            for s in sections)
    for section in sections:
        assert b.menu.lookup_menuitem(section[len("menu "):]) is not None
    # OpenAMS is fourth among Klipper's own main entries (after Tune,
    # OctoPrint and SD Card), and its Stop row is first.
    children = b.menu.lookup_children("__main")
    assert children[:5] == ["__main __oams_stop", "__main __tune",
                            "__main __octoprint", "__main __sdcard",
                            "__main __openams"]
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
    assert main[1] == "OpenAMS" and main[-1] == "My macro"
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


def test_the_stop_entries_are_the_ones_the_host_listens_for(bench):
    b = bench()
    assert oams_manager.MENU_STOP_EVENTS == tuple(
        "menu:%s:populate" % b.menu.lookup_menuitem(name).get_ns()
        for name in ("__main __openams __stop", "__main __oams_stop"))
    assert oams_manager.MENU_STOP_EVENT == oams_manager.MENU_STOP_EVENTS[0]


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
    assert b.lcd.home() == ["..", "Stop", "Load", "Extrude 10mm",
                            "Clear errors", "Calibrate", "Follower",
                            "Settings", "Status"]
    assert b.lcd.open("Load") == ["..", "Load tool", "Load spool",
                                  "Load, no cut"]


def test_a_loaded_lane_adds_unload(bench):
    b = bench()
    run(b, ["OAMSM_TOOLCHANGE GROUP=T1"])
    assert b.units["unit1"].current_spool == 2
    assert b.lcd.home() == ["..", "Stop", "Load", "Unload", "Extrude 10mm",
                            "Clear errors", "Calibrate", "Follower",
                            "Settings", "Status"]
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
    assert b.lcd.home() == ["..", "Stop", "Load", "Unload", "Extrude 10mm",
                            "Clear errors", "Calibrate", "Follower",
                            "Settings", "Status"]


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
    assert "Retry" in entries and "Clear errors" in entries
    assert b.lcd.labels("Retry") == [
        ("Retry", []), ("Retry T1?", ["OAMSM_RETRY FPS=fps"])]
    b.units["unit1"].load_result = 0
    run(b, ["OAMSM_RETRY FPS=fps"])
    assert b.units["unit1"].current_spool == 2
    assert "Retry" not in b.lcd.home()


def test_clear_errors_and_cancel_load_are_plain_entries(bench):
    b = bench()
    b.lcd.home()
    b.lcd.select("Clear errors")
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


def test_a_unit_fault_is_cleared_with_clear_errors_alone(bench):
    # One way to clear (2026-11-12): no Clear fault entry; Clear errors
    # clears the unit's latch and resyncs the lanes.
    b = bench(units=TWO_UNITS, groups=TWO_UNIT_GROUPS)
    b.units["unit2"]._telemetry = {"domain": {"system_fault_name": "rfid_fault"}}
    entries = b.lcd.home()
    assert "Clear fault" not in entries
    assert "Clear errors" in entries


def test_abandon_forgets_a_failed_panel_load_only(bench):
    b = bench()
    b.units["unit1"].load_result = S.OAMS_OP_CODE_ERROR_UNSPECIFIED
    with pytest.raises(b.printer.command_error):
        b.printer.gcode.run_script("OAMSM_TOOLCHANGE GROUP=T1")
    fault = b.lane()["toolchange_fault"]
    entries = b.lcd.home()
    if not fault["panel"]:
        # The print's own (here: a toolchange outside a panel run) has none.
        assert "Abandon" not in entries
        b.manager._toolchange_faults["fps"]["panel"] = True
        entries = b.lcd.home()
    assert "Abandon" in entries
    assert b.lcd.labels("Abandon") == [
        ("Abandon", []), ("Abandon T1?", ["OAMSM_ABANDON FPS=fps"])]


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


# Bambu Lab's AMS presets as oams_menu.cfg carries them: (row, C, hours)
# idle, and the C while a print runs (None: not offered then). The AMS 2 Pro
# rows from ABS on are OpenAMS's own: 65 C for twice the AMS HT time.
AMS2_PRO_PRESETS = [("PLA", 45, 12, 45), ("PETG", 65, 12, 55),
                    ("TPU", 65, 12, 45), ("ABS", 65, 16, 65),
                    ("PC", 65, 16, 65), ("PETCF", 65, 24, 65),
                    ("PA", 65, 24, 65), ("PVA", 65, 36, 65),
                    ("PLA-S", 60, 12, 50)]
AMS_HT_PRESETS = [("PLA", 45, 12, 45), ("PETG", 65, 12, 55),
                  ("TPU", 75, 18, 45), ("ABS", 80, 8, 75), ("PC", 80, 8, 80),
                  ("PETCF", 80, 12, None), ("PA", 85, 12, None),
                  ("PVA", 85, 18, 70), ("PLA-S", 60, 12, 50)]


def test_start_dryer_offers_the_ams_2_pro_presets_and_starts_in_one_go(bench):
    b = bench(units=TWO_UNITS, groups=TWO_UNIT_GROUPS)
    b.lcd.home()
    assert b.lcd.open("Dryer") == ["..", "Start dryer", "Stop dryer", "Custom"]
    assert b.lcd.labels("Start dryer") == [("Start dryer", [])] + [
        ("%s %dC %dh?" % (name, temp, hours),
         ["OAMS_DRYER_START OAMS=2 TARGET=%d DURATION=%d"
          % (temp, hours * 3600)])
        for name, temp, hours, _ in AMS2_PRO_PRESETS]
    b.units["unit2"].dryer = dict(DRYER, state="timed_dry", target_c=65.0)
    assert b.lcd.pick("Start dryer", 0) == ("PLA drying", [])
    assert b.lcd.pick("Stop dryer", 0) == (
        "Stop unit2 65C?", ["OAMS_DRYER_STOP OAMS=2"])
    b.units["unit2"].dryer = dict(DRYER, adapter=False)
    assert b.lcd.pick("Start dryer", 1) == ("PETG no power", [])


def test_each_unit_type_offers_its_own_list_clamped_to_its_limits(bench):
    units = [("pro", 1, "fps", AMS2_PRO), ("ht", 2, "fps", AMS_HT),
             ("ams1", 3, "fps", {})]
    groups = [("T0", [("pro", 0)]), ("T1", [("ht", 0)])]
    b = bench(units=units, groups=groups)
    b.lcd.home()
    b.lcd.open("Dryer")
    labels = b.lcd.labels("Start dryer")
    expected = [("Start dryer", [])]
    for unit, idx, presets, high in (("pro", 1, AMS2_PRO_PRESETS, 65),
                                     ("ht", 2, AMS_HT_PRESETS, 85)):
        for name, temp, hours, _ in presets:
            assert 45 <= temp <= high and 1 <= hours <= 72
            expected.append(("%s %s %dC?" % (unit, name, temp),
                             ["OAMS_DRYER_START OAMS=%d TARGET=%d DURATION=%d"
                              % (idx, temp, hours * 3600)]))
    # The AMS 1 has no dryer and offers nothing.
    assert labels == expected


def test_during_a_print_a_preset_runs_at_its_print_temperature(bench):
    units = [("pro", 1, "fps", AMS2_PRO), ("ht", 2, "fps", AMS_HT)]
    b = bench(units=units, groups=[("T0", [("pro", 0)])])
    b.printing()
    b.lcd.home()
    b.lcd.open("Dryer")
    labels = b.lcd.labels("Start dryer")
    expected = [("Start dryer", [])]
    for unit, idx, presets in (("pro", 1, AMS2_PRO_PRESETS),
                               ("ht", 2, AMS_HT_PRESETS)):
        for name, temp, hours, printing in presets:
            if printing is None:
                expected.append(("%s %s idle only" % (unit, name), []))
            else:
                expected.append(("%s %s %dC?" % (unit, name, printing),
                                 ["OAMS_DRYER_START OAMS=%d TARGET=%d"
                                  " DURATION=%d"
                                  % (idx, printing, hours * 3600)]))
    assert labels == expected


def test_a_unit_types_presets_are_the_owners_to_change(bench):
    b = bench(units=TWO_UNITS, groups=TWO_UNIT_GROUPS, user_cfg=(
        "[gcode_macro _OAMS_MENU]\n"
        "variable_dryer_presets_ams2: [[\"PLA\", 50, 8, 45]]\n"))
    b.check_unused()
    b.lcd.home()
    b.lcd.open("Dryer")
    assert b.lcd.labels("Start dryer") == [
        ("Start dryer", []),
        ("PLA 50C 8h?", ["OAMS_DRYER_START OAMS=2 TARGET=50 DURATION=28800"])]


def test_one_unit_can_have_presets_of_its_own(bench):
    b = bench(units=TWO_UNITS, groups=TWO_UNIT_GROUPS, user_cfg=(
        "[gcode_macro _OAMS_MENU]\n"
        "variable_dryer_presets_unit2: [[\"PA\", 80, 12, None]]\n"))
    b.check_unused()
    b.lcd.home()
    b.lcd.open("Dryer")
    # Clamped to the unit's 65 C.
    assert b.lcd.labels("Start dryer") == [
        ("Start dryer", []),
        ("PA 65C 12h?", ["OAMS_DRYER_START OAMS=2 TARGET=65 DURATION=43200"])]


def test_custom_dryer_keeps_its_temperature_and_time(bench):
    b = bench(units=TWO_UNITS, groups=TWO_UNIT_GROUPS)
    b.lcd.home()
    b.lcd.open("Dryer")
    assert b.lcd.open("Custom") == ["..", "Dry temp: 55C", "Dry time: 4h",
                                    "Start custom"]
    assert b.lcd.labels("Start custom") == [
        ("Start custom", []),
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
    assert b.lcd.pick("Start custom", 0) == (
        "unit2 65C 8h?", ["OAMS_DRYER_START OAMS=2 TARGET=65 DURATION=28800"])


def test_rotate_shows_only_where_a_unit_can_and_adds_rotate(bench):
    # Cortex Bridge firmware 20 lists dryer_rotate; the switch then appears
    # and a start from the menu turns the spools.
    rotating = dict(AMS2_PRO, actions=AMS2_PRO["actions"] + ("dryer_rotate",))
    b = bench(units=[("unit1", 1, "fps", {}), ("unit2", 2, "fps", rotating)],
              groups=TWO_UNIT_GROUPS)
    b.lcd.home()
    assert b.lcd.open("Dryer") == ["..", "Start dryer", "Stop dryer", "Custom",
                                   "Rotate: off"]
    assert b.lcd.pick("Start dryer", 0) == (
        "PLA 45C 12h?", ["OAMS_DRYER_START OAMS=2 TARGET=45 DURATION=43200"])
    b.lcd.select("Rotate: off")
    b.lcd.key("click")
    b.lcd.key("down")       # as for Dry temp above: down steps the input up
    assert b.lcd.label() == "Rotate: on"
    b.lcd.key("click")
    assert b.lcd.scripts[-1] == (
        "SET_GCODE_VARIABLE MACRO=_OAMS_MENU VARIABLE=dryer_rotate VALUE=1")
    run(b, b.lcd.scripts[-1:])
    assert b.lcd.pick("Start dryer", 0) == (
        "PLA 45C 12h?",
        ["OAMS_DRYER_START OAMS=2 TARGET=45 DURATION=43200 ROTATE=1"])
    b.lcd.open("Custom")
    assert b.lcd.pick("Start custom", 0) == (
        "unit2 55C 4h?",
        ["OAMS_DRYER_START OAMS=2 TARGET=55 DURATION=14400 ROTATE=1"])


def test_rotate_stays_hidden_and_off_on_a_unit_that_cannot(bench):
    b = bench(units=TWO_UNITS, groups=TWO_UNIT_GROUPS)
    run(b, ["SET_GCODE_VARIABLE MACRO=_OAMS_MENU VARIABLE=dryer_rotate VALUE=1"])
    b.lcd.home()
    assert b.lcd.open("Dryer") == ["..", "Start dryer", "Stop dryer", "Custom"]
    assert b.lcd.pick("Start dryer", 0)[1] == [
        "OAMS_DRYER_START OAMS=2 TARGET=45 DURATION=43200"]


def test_a_temperature_below_the_units_floor_is_raised_to_it(bench):
    b = bench(units=TWO_UNITS, groups=TWO_UNIT_GROUPS)
    run(b, ["SET_GCODE_VARIABLE MACRO=_OAMS_MENU VARIABLE=dryer_temp VALUE=30"])
    b.lcd.home()
    b.lcd.open("Dryer", "Custom")
    assert b.lcd.pick("Start custom", 0)[1] == [
        "OAMS_DRYER_START OAMS=2 TARGET=45 DURATION=14400"]


def test_a_setting_switches_with_one_click(bench):
    b = bench()
    b.lcd.home()
    assert b.lcd.open("Settings") == [
        "..", "Autoreload: on", "Tag insert: on", "Tag start: on",
        "New spools: on", "Apply PA: off", "Sync trays: on"]
    b.lcd.select("Autoreload: on")
    b.lcd.key("click")
    assert b.lcd.scripts == [
        "OAMSM_SET_OPTION NAME=auto_reload_on_runout VALUE=0"]
    b.manager.settings["auto_reload_on_runout"] = False
    assert b.lcd.entries()[1] == "Autoreload: off"
    b.lcd.select("Apply PA: off")
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
# The AMS status screen (oams_screen.cfg)
# ---------------------------------------------------------------------------

def with_screen(b, lcd_type="uc1701", group="_default_16x4", loaded=True):
    """Klipper's published config for a 128x64 display, with oams_screen.cfg
    loaded (or not): the entries read both."""
    values = b.printer.objects["configfile"].values
    values["settings"]["display"] = {"lcd_type": lcd_type,
                                     "display_group": group}
    values["config"] = ({"display_data oams_tiles head": {}} if loaded
                        else {})
    return b


def lines(script):
    return [line.strip() for line in script.splitlines() if line.strip()]


def test_ams_status_shows_the_screen_and_printer_status_goes_back(bench):
    b = with_screen(bench(units=TWO_UNITS, groups=TWO_UNIT_GROUPS))
    entries = b.lcd.home()
    assert entries[:4] == ["..", "Stop", "AMS status", "Screen: auto"]
    assert "Printer status" not in entries
    b.lcd.select("AMS status")
    b.lcd.key("click")
    assert lines(b.lcd.scripts[-1]) == [
        "SET_DISPLAY_GROUP GROUP=oams_tiles",
        "SET_GCODE_VARIABLE MACRO=_OAMS_MENU VARIABLE=screen VALUE=1"]
    assert not b.menu.is_running()
    # Klipper runs both; the variable is what the entries read back.
    run(b, lines(b.lcd.scripts[-1])[1:])
    entries = b.lcd.home()
    assert entries[:4] == ["..", "Stop", "Printer status", "Screen: auto"]
    assert "AMS status" not in entries
    b.lcd.select("Printer status")
    b.lcd.key("click")
    assert lines(b.lcd.scripts[-1]) == [
        "SET_DISPLAY_GROUP GROUP=_default_16x4",
        "SET_GCODE_VARIABLE MACRO=_OAMS_MENU VARIABLE=screen VALUE=0"]
    assert not b.menu.is_running()


def test_a_screen_made_the_default_starts_on_printer_status(bench):
    """[display] display_group: oams_tiles shows the AMS screen from the
    start, so the entry that applies is Printer status, back to Klipper's."""
    b = with_screen(bench(units=TWO_UNITS, groups=TWO_UNIT_GROUPS),
                    group="oams_tiles")
    assert "Printer status" in b.lcd.home()
    b.lcd.select("Printer status")
    b.lcd.key("click")
    assert lines(b.lcd.scripts[-1])[0] == \
        "SET_DISPLAY_GROUP GROUP=_default_16x4"


def test_the_screen_entries_need_a_128x64_display_and_the_screen(bench):
    for options in ({"lcd_type": "hd44780"}, {"loaded": False}):
        b = with_screen(bench(units=TWO_UNITS, groups=TWO_UNIT_GROUPS),
                        **options)
        entries = b.lcd.home()
        assert not {"AMS status", "Printer status", "Screen: auto"} & \
            set(entries), options
    # The stock published config (no display settings) shows none either.
    b = bench(units=TWO_UNITS, groups=TWO_UNIT_GROUPS)
    assert "AMS status" not in b.lcd.home()


def test_one_unit_has_no_unit_picker(bench):
    b = with_screen(bench())
    entries = b.lcd.home()
    assert "AMS status" in entries
    assert not [e for e in entries if e.startswith("Screen:")]


def test_the_unit_picker_pins_through_oamsm_screen(bench):
    b = with_screen(bench(units=TWO_UNITS, groups=TWO_UNIT_GROUPS))
    b.lcd.home()
    picks = b.lcd.labels("Screen: auto")
    assert [label for label, _ in picks] == [
        "Screen: auto", "Show auto?", "Show unit1?", "Show unit2?"]
    assert [scripts for _, scripts in picks] == [
        [], ["OAMSM_SCREEN UNIT=AUTO"], ["OAMSM_SCREEN UNIT=unit1"],
        ["OAMSM_SCREEN UNIT=unit2"]]
    run(b, ["OAMSM_SCREEN UNIT=unit2"])
    assert b.manager.screen.pin == "unit2"
    assert "Screen: unit2" in b.lcd.entries()
    run(b, ["OAMSM_SCREEN UNIT=AUTO"])
    assert b.manager.screen.pin is None


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


# The hand purge (OpenAMS › Extrude 10mm), the one entry that is not an
# OpenAMS command: forward only, relative, with the G-code state put back.
EXTRUDE_LINES = ("SAVE_GCODE_STATE NAME=_oams_menu_extrude", "M83",
                 "G1 E10 F240", "RESTORE_GCODE_STATE NAME=_oams_menu_extrude")


def check_command(bench, line):
    """`line` is one OpenAMS command with arguments the live topology accepts."""
    if line in EXTRUDE_LINES:
        return
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
        "OAMSM_ABANDON": {"FPS"},
        "OAMSM_FOLLOWER": {"ENABLE", "DIRECTION", "FPS"},
        "OAMS_UNLOAD_SPOOL": {"OAMS", "SPOOL"},
        "OAMS_RFID_SCAN": {"OAMS", "BAY", "ASSERT"},
        "OAMS_CALIBRATE_PTFE_LENGTH": {"OAMS", "SPOOL"},
        "OAMS_CALIBRATE_HUB_HES": {"OAMS", "SPOOL"},
        "OAMS_CLEAR_FAULT": {"OAMS"},
        "OAMS_DRYER_START": {"OAMS", "TARGET", "DURATION", "ROTATE"},
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


def every_script(bench, path=()):
    """Every picker value and command of every OpenAMS list now shown, the
    nested lists (Dryer › Custom) included. The Stop lists are left alone:
    opening one is the stop."""
    lcd = bench.lcd
    scripts = []
    rows = (lcd.home() if not path else (lcd.home(), lcd.open(*path))[1])[1:]
    for entry in rows:
        lcd.home()
        if path:
            lcd.open(*path)
        lcd.select(entry)
        item = lcd.container().selected_item()
        if isinstance(item, bench.kmenu.menu.MenuList):
            if entry not in ("Stop", "Stop OpenAMS"):
                scripts += every_script(bench, path + (entry,))
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


# ---------------------------------------------------------------------------
# Where OpenAMS sits in the main menu, and the Stop at its top
# ---------------------------------------------------------------------------

# Our fork's main menu (OpenAMSOrg/klipper-optional-improvements): Pause
# print, Resume print and Cancel print come before Klipper's own entries.
FORK_PRINT_ROWS = """
[menu __main __pause]
type: command
enable: {'print_stats' in printer and printer.print_stats.state == 'printing'}
name: Pause print
gcode: PAUSE

[menu __main __resume]
type: command
enable: {'print_stats' in printer and printer.print_stats.state == 'paused'}
name: Resume print
gcode: RESUME

[menu __main __cancel]
type: list
enable: {'print_stats' in printer and printer.print_stats.state in ['printing', 'paused']}
name: Cancel print

[menu __main __cancel __yes]
type: command
name: Yes, cancel it
gcode: CANCEL_PRINT
"""


def fork_like_menu():
    stock = committed_source(os.environ["OPENAMS_KLIPPER_DIR"], KLIPPER_REV,
                             "klippy/extras/display/menu.cfg")
    anchor = "[menu __main]\ntype: list\nname: Main\n"
    assert anchor in stock
    return stock.replace(anchor, anchor + FORK_PRINT_ROWS)


def main_rows(bench):
    if bench.menu.is_running():
        bench.menu.exit(force=True)
    bench.lcd.key("click")
    return bench.lcd.entries()


def test_openams_sits_below_sd_card_on_stock_klipper(bench):
    b = bench()
    # SD Card is hidden here (no virtual_sdcard); Filament is taken out.
    assert main_rows(b) == ["..", "OctoPrint", "OpenAMS", "Control",
                            "Temperature", "Setup"]


@pytest.mark.parametrize("state, expected", [
    ("standby", ["..", "OpenAMS", "OctoPrint", "Control", "Temperature",
                 "Setup"]),
    ("printing", ["..", "Pause print", "Cancel print", "OpenAMS", "Tune",
                  "OctoPrint", "Control", "Temperature"]),
    ("paused", ["..", "Resume print", "Cancel print", "OpenAMS", "OctoPrint",
                "Control", "Temperature", "Setup"]),
])
def test_with_the_forks_print_rows_openams_never_lands_between_them(
        bench, state, expected):
    b = bench(stock_menu=fork_like_menu())
    b.printing(state)
    b.printer.objects["idle_timeout"].values["state"] = (
        "Printing" if state == "printing" else "Idle")
    assert main_rows(b) == expected


def test_with_the_forks_own_menu_cfg(bench):
    """The same, against the fork's menu.cfg itself (set
    OPENAMS_KLIPPER_FORK_DIR to a checkout of it)."""
    fork = os.environ.get("OPENAMS_KLIPPER_FORK_DIR")
    if not fork:
        pytest.skip("set OPENAMS_KLIPPER_FORK_DIR to check the fork's menu.cfg")
    text = open(os.path.join(fork, "klippy/extras/display/menu.cfg")).read()
    b = bench(stock_menu=text)
    b.printer.objects["print_stats"].values["state"] = "paused"
    rows = main_rows(b)
    assert rows[:4] == ["..", "Resume print", "Cancel print", "OpenAMS"]
    b.check_unused()


def test_stop_openams_leads_the_main_menu_while_a_sequence_runs(bench):
    b = bench()
    assert "Stop OpenAMS" not in main_rows(b)
    run(b, ["OAMSM_STAGE PLAN=heat,feed FPS=fps", "OAMSM_STAGE NAME=heat FPS=fps"])
    # Open the menu, turn once, click: three actions.
    b.menu.exit(force=True)
    b.lcd.key("click")
    assert b.lcd.entries()[1] == "Stop OpenAMS"
    b.lcd.key("down")
    b.lcd.key("click")
    assert b.lcd.entries() == ["..", "Stop sent"]
    assert "fps" in b.manager._stop_requested
    with pytest.raises(b.printer.command_error, match="Stopped by the user"):
        b.printer.gcode.run_script("OAMSM_CHECK_STOP FPS=fps")


def test_a_failed_plan_is_not_a_running_one(bench):
    b = bench()
    run(b, ["OAMSM_STAGE PLAN=heat,feed FPS=fps", "OAMSM_STAGE NAME=heat FPS=fps",
            "OAMSM_STAGE FAILED=1 FPS=fps"])
    assert "Stop OpenAMS" not in main_rows(b)


# ---------------------------------------------------------------------------
# The stock raw-extrude entries, where OpenAMS runs
# ---------------------------------------------------------------------------

def test_the_stock_filament_list_and_move_e_are_taken_out(bench):
    b = bench()
    disabled = b.kmenu.menu.MenuDisabled
    for name in oams_manager.STOCK_RAW_EXTRUDE_MENU:
        assert isinstance(b.menu.lookup_menuitem(name), disabled), name
    assert "Filament" not in main_rows(b)
    b.check_unused()


def test_an_owners_own_filament_entry_stays_and_klipper_starts(bench):
    """Klipper merges two sections of one name: had oams_menu.cfg disabled
    the list, this owner's command would end up with type disabled and a
    gcode option nothing reads, and Klipper would refuse to start."""
    b = bench(user_cfg=("[menu __main __filament]\ntype: command\n"
                        "name: Load filament\ngcode: M117 load\n"))
    b.check_unused()
    item = b.menu.lookup_menuitem("__main __filament")
    assert isinstance(item, b.kmenu.menu.MenuCommand)
    assert "Load filament" in main_rows(b)


def test_entries_the_owner_added_under_filament_keep_the_list(bench):
    b = bench(user_cfg=("[menu __main __filament __mine]\ntype: command\n"
                        "name: Mine\ngcode: M117 mine\n"))
    b.check_unused()
    assert "Filament" in main_rows(b)
    assert "Mine" in b.lcd.open("Filament")


def test_without_a_display_nothing_is_adjusted(bench):
    b = bench(display=False)
    assert b.printer.lookup_object("menu", None) is None


def test_extrude_is_hot_only_and_forward(bench):
    b = bench()
    b.lcd.home()
    b.lcd.select("Extrude 10mm")
    b.lcd.key("click")
    assert b.lcd.scripts == ["\n".join(EXTRUDE_LINES)]
    b.printer.objects["extruder"].values["can_extrude"] = False
    assert "Extrude (cold)" in b.lcd.entries()
    b.lcd.select("Extrude (cold)")
    b.lcd.key("click")
    assert len(b.lcd.scripts) == 1
    b.printing()
    assert not [row for row in b.lcd.home() if row.startswith("Extrude")]


# ---------------------------------------------------------------------------
# A failed toolchange in a print, on the status screen
# ---------------------------------------------------------------------------

def with_display_status(bench):
    printer = bench.printer
    printer.objects["display_status"] = Status(message=None, progress=0.)
    lines = []

    def set_text(gcmd):
        lines.append(gcmd.get_commandline())
        printer.objects["display_status"].values["message"] = gcmd.get(
            "MSG", None)
    printer.gcode.register_command("SET_DISPLAY_TEXT", set_text)
    return lines


def shown(bench):
    return bench.printer.objects["display_status"].values["message"]


def test_a_failed_toolchange_shows_on_the_status_screen_until_resume(bench):
    b = bench()
    run(b, ["OAMSM_TOOLCHANGE GROUP=T0"])
    lines = with_display_status(b)
    printing_with_resume(b.printer, b.manager)
    inlet_sensor(b.printer, False)
    b.printer.gcode.run_script("T1")
    assert b.printer.objects["pause_resume"].is_paused
    assert shown(b) == "T1 failed: feed"
    assert lines == ['SET_DISPLAY_TEXT MSG="T1 failed: feed"']
    inlet_sensor(b.printer, True)
    b.printer.gcode.run_script("RESUME")
    assert not b.printer.objects["pause_resume"].is_paused
    assert shown(b) is None


def test_a_newer_message_is_left_alone(bench):
    b = bench()
    run(b, ["OAMSM_TOOLCHANGE GROUP=T0"])
    with_display_status(b)
    printing_with_resume(b.printer, b.manager)
    inlet_sensor(b.printer, False)
    b.printer.gcode.run_script("T1")
    b.printer.gcode.run_script('SET_DISPLAY_TEXT MSG="layer 12"')
    inlet_sensor(b.printer, True)
    b.printer.gcode.run_script("RESUME")
    assert shown(b) == "layer 12"


def test_a_cancelled_print_takes_its_notice_with_it(bench):
    b = bench()
    run(b, ["OAMSM_TOOLCHANGE GROUP=T0"])
    with_display_status(b)
    printing_with_resume(b.printer, b.manager)
    inlet_sensor(b.printer, False)
    b.printer.gcode.run_script("T1")
    assert shown(b) == "T1 failed: feed"
    callbacks = []
    b.printer.reactor.register_callback = callbacks.append
    b.printing("cancelled")
    b.manager._monitor(b.printer.reactor.monotonic())
    for callback in callbacks:
        callback(b.printer.reactor.monotonic())
    assert shown(b) is None


def fail_t1_in_a_print(bench):
    """T1 fails in a print without RESUME wrapped (OpenAMS warns about that
    configuration), and the print pauses as Klipper's PAUSE leaves it."""
    run(bench, ["OAMSM_TOOLCHANGE GROUP=T0"])
    with_display_status(bench)
    bench.printing()
    inlet_sensor(bench.printer, False)
    bench.printer.gcode.run_script("T1")
    assert bench.printer.objects["pause_resume"].is_paused
    bench.printing("paused")
    assert shown(bench) == "T1 failed: feed"


def monitor_tick(bench):
    callbacks = []
    bench.printer.reactor.register_callback = callbacks.append
    bench.manager._monitor(bench.printer.reactor.monotonic())
    for callback in callbacks:
        callback(bench.printer.reactor.monotonic())


def test_the_notice_stays_while_the_print_fault_does(bench):
    b = bench()
    fail_t1_in_a_print(b)
    monitor_tick(b)
    assert shown(b) == "T1 failed: feed"


def test_a_retry_then_an_unwrapped_resume_clears_the_notice(bench):
    b = bench()
    fail_t1_in_a_print(b)
    inlet_sensor(b.printer, True)
    b.printer.gcode.run_script("OAMSM_RETRY")
    assert "toolchange_fault" not in b.lane()
    # Klipper's own RESUME, not wrapped by OpenAMS: nothing of OpenAMS runs.
    b.printer.objects["pause_resume"].values["is_paused"] = False
    b.printing("printing")
    monitor_tick(b)
    assert shown(b) is None


def test_a_retry_then_a_cancel_clears_the_notice(bench):
    b = bench()
    fail_t1_in_a_print(b)
    inlet_sensor(b.printer, True)
    b.printer.gcode.run_script("OAMSM_RETRY")
    b.printing("cancelled")
    monitor_tick(b)
    assert shown(b) is None


def test_a_cleared_fault_never_wipes_a_newer_message(bench):
    b = bench()
    fail_t1_in_a_print(b)
    inlet_sensor(b.printer, True)
    b.printer.gcode.run_script("OAMSM_RETRY")
    b.printer.gcode.run_script('SET_DISPLAY_TEXT MSG="layer 40"')
    monitor_tick(b)
    assert shown(b) == "layer 40"
