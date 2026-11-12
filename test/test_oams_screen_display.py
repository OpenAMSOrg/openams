"""The LCD's AMS status screen (oams_screen.cfg) on Klipper's own display code.

The shipped file is loaded through Klipper's config reader, display templates
and G-code template engine, and drawn by Klipper's display group into the
UC1701 driver's real framebuffer and font (no MCU: the driver's output is
never sent). The data comes from the real ``openams_screen`` object over a
two-unit manager, plus each state's ``openams_ui`` projection. Every state of
the approved round-4 design is checked on the framebuffer: each tile's glyphs
by name, its frame or solid box pixel by pixel, and the clear pixels between
content and frame, under the header and between tiles.

Klipper's modules come from the pinned revision of test_openams_klipper.py
(set OPENAMS_KLIPPER_DIR to run it). Set OAMS_SCREEN_PNG_DIR to also save
each state as a 4x PNG.
"""
import contextlib
import os
import pathlib
import re
import struct
import subprocess
import sys
import tempfile
import types
import unittest
import zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCREEN = ROOT / "oams_screen.cfg"
sys.path.insert(0, str(ROOT / "tools" / "lcd_screen"))
import gen_oams_screen as gen                            # noqa: E402

from test_oams_manager import S                          # noqa: E402
from test_openams_screen import (                         # noqa: E402
    PrintStats, PrinterWithStats, set_lane, two_unit_manager)

KLIPPER_DIR = os.environ.get("OPENAMS_KLIPPER_DIR")
# The same pinned revision the other Klipper integration tests use.
KLIPPER_REV = re.search(
    r'^KLIPPER_REV = "([0-9a-f]+)"',
    (ROOT / "test" / "test_openams_klipper.py").read_text(), re.M).group(1)
DISPLAY_SECTION = """\
[display]
lcd_type: uc1701
cs_pin: EXP1_3
a0_pin: EXP1_4
rst_pin: EXP1_5
encoder_pins: ^EXP2_5, ^EXP2_3
click_pin: ^!EXP1_2
contrast: 63
display_group: %s

[include oams_screen.cfg]
""" % gen.GROUP


def klipper_source(path):
    return subprocess.check_output(
        ["git", "-C", KLIPPER_DIR, "show", "%s:%s" % (KLIPPER_REV, path)],
        text=True)


def load(name, path, filename=None):
    module = types.ModuleType(name)
    module.__package__ = name.rpartition(".")[0]
    module.__file__ = filename or name
    sys.modules[name] = module
    exec(compile(klipper_source(path), module.__file__, "exec"),
         module.__dict__)
    return module


def stub(name, **attrs):
    module = types.ModuleType(name)
    module.__path__ = []
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class Klipper:
    """Klipper's config reader, template engine and display package."""

    def __init__(self, folder):
        self.configfile = load("oamsscreen_configfile", "klippy/configfile.py")
        self.gcode_macro = load("oamsscreen_gcode_macro",
                                "klippy/extras/gcode_macro.py")
        root = stub("oamsscreen", bus=stub("oamsscreen.bus"))
        disp = stub("oamsscreen.display")
        root.display = disp
        disp.font8x14 = load("oamsscreen.display.font8x14",
                             "klippy/extras/display/font8x14.py")
        disp.uc1701 = load("oamsscreen.display.uc1701",
                           "klippy/extras/display/uc1701.py")
        # Display drivers this test never builds.
        Unused = type("Unused", (), {})
        disp.aip31068_spi = stub("oamsscreen.display.aip31068_spi",
                                 aip31068_spi=Unused)
        disp.hd44780 = stub("oamsscreen.display.hd44780", HD44780=Unused)
        disp.hd44780_spi = stub("oamsscreen.display.hd44780_spi",
                                hd44780_spi=Unused)
        disp.st7920 = stub("oamsscreen.display.st7920", ST7920=Unused,
                           EmulatedST7920=Unused)
        disp.menu = stub("oamsscreen.display.menu", MenuManager=Unused)
        # display.py reads its own display.cfg from the folder it is in.
        (folder / "display.cfg").write_text(
            klipper_source("klippy/extras/display/display.cfg"))
        self.display = load("oamsscreen.display.display",
                            "klippy/extras/display/display.py",
                            str(folder / "display.py"))


class Chip:
    """The UC1701 driver's framebuffer, font and glyph handling, with no
    MCU behind it (the screen is drawn, never flushed)."""

    def __new__(cls, klipper):
        base = klipper.display.uc1701.DisplayBase

        class Headless(base):
            def __init__(self):
                base.__init__(self, types.SimpleNamespace(
                    send=lambda *args, **kwargs: None))

            def pixels(self):
                pix = [[False] * 128 for _ in range(64)]
                for page in range(8):
                    for x in range(128):
                        byte = self.vram[page][x]
                        for bit in range(8):
                            if byte & (1 << bit):
                                pix[page * 8 + bit][x] = True
                return pix

            def cell(self, row, col, width=1):
                top = bytes(self.vram[row * 2][col * 8:(col + width) * 8])
                bot = bytes(self.vram[row * 2 + 1][col * 8:(col + width) * 8])
                return top, bot

            def glyph_at(self, row, col):
                key = self.cell(row, col, 2)
                for name, (top, bot) in self.icons.items():
                    if (bytes(top), bytes(bot)) == key:
                        return name
                return None

            def text_row(self, row):
                """A row read as text: '%' a glyph cell, '#' a character
                outside ASCII; an inverted cell reads as its character."""
                chars = {}
                for code, (top, bot) in enumerate(self.font):
                    key = (bytes(top), bytes(bot))
                    if chars.get(key, "#") == "#":
                        # A printable character wins a shared bitmap.
                        chars[key] = chr(code) if 32 <= code < 127 else "#"
                inverted = {(bytes(b ^ 0xff for b in t),
                             bytes(b ^ 0xff for b in u)): c
                            for (t, u), c in chars.items()}
                line = ""
                for col in range(16):
                    key = self.cell(row, col)
                    line += chars.get(key) or inverted.get(key) or "%"
                return line
        return Headless()


class Reactor:
    def __init__(self):
        self.now = 0.

    def monotonic(self):
        return self.now

    @contextlib.contextmanager
    def assert_no_pause(self):
        yield


class Status:
    def __init__(self, **values):
        self.values = values

    def get_status(self, eventtime):
        return dict(self.values)


class Printer:
    def __init__(self, klipper):
        self.klipper = klipper
        self.reactor = Reactor()
        self.objects = {}
        self.command_error = RuntimeError
        self.config_error = RuntimeError
        self.objects["gcode"] = types.SimpleNamespace(
            error=RuntimeError, respond_info=lambda msg: None)

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def lookup_objects(self, module=None):
        return list(self.objects.items())

    def load_object(self, config, name):
        return self.objects[name]

    def add_object(self, name, obj):
        self.objects[name] = obj


class ConfigFile:
    def __init__(self, klipper, printer):
        self.klipper, self.printer = klipper, printer

    def read_config(self, filename):
        configfile = self.klipper.configfile
        reader = configfile.ConfigFileReader()
        fileconfig = reader.build_fileconfig(
            reader.read_config_file(filename), filename)
        return configfile.ConfigWrapper(self.printer, fileconfig, {},
                                        "printer")


# ---------------------------------------------------------------------------
# States: voron350's units, an AMS 1 (ams1, T0-T3) and an AMS 2 Pro (ams2,
# T4-T7) on one lane.
# ---------------------------------------------------------------------------

GROUPS = [{"n": "T%d" % (u * 4 + b), "b": ["ams%d-%d" % (u + 1, b)],
           "s": "r"} for u in range(2) for b in range(4)]
IDLE_LANE = {"id": "fps1", "ext": "extruder", "op": "UNLOADED",
             "group": None, "bay": None, "stage": None, "runout": "idle",
             "from": None, "to": None, "ms": True}
NO_DRYER = {"state": "off", "target": 0, "min": 0, "heat": 0, "fan": 0,
            "fault": "none", "adapter": True}
SPOOLS = {"ams1": [("PLA", 210, 78), ("ABS", 250, 45), ("PETG", 240, 12),
                   None],
          "ams2": [("PLA", 210, 100), ("PETG", 240, 62), ("ASA", 255, 30),
                   ("PLA", 210, 8)]}


def ui(name, idx, family, states, env, lane, dryer=None, fault=None):
    return {"v": 1, "unit": name, "idx": idx, "family": family,
            "online": True, "ready": fault is None, "cal": "c",
            "lane": dict(lane),
            "bays": [{"id": idx * 4 + i, "s": s, "cal": "c", "a": "",
                      "sp": None} for i, s in enumerate(states)],
            "groups": GROUPS, "env": env, "dryer": dryer, "fault": fault,
            "nf": 1 if fault else 0, "act": []}


def state(name):
    """(manager, {object name: status}, eventtime, expected looks)."""
    mgr = two_unit_manager()
    spools = {unit: list(bays) for unit, bays in SPOOLS.items()}
    s1 = ["r", "r", "r", "e"]
    s2 = ["r", "r", "r", "r"]
    lane = dict(IDLE_LANE)
    env2 = {"t": 25, "rh": 18}
    dryer2 = dict(NO_DRYER)
    fault2 = None
    print_state = "standby"
    looks = ["f", "f", "f", "f"]
    second = 1.0
    if name in ("loaded", "drying", "unknown", "loading_solid",
                "loading_outlined"):
        print_state = "printing"
    if name in ("loaded", "drying", "unknown"):
        s2[1] = "l"
        lane.update(op="LOADED", group="T5", bay="ams2-1")
        set_lane(mgr, op=S.OP_LOADED, unit=(1, 1), group="T5")
        looks[1] = "i"
    if name.startswith("loading"):
        lane.update(op="LOADING", group="T5", bay="ams2-1",
                    stage=[["cut", "retract", "feed"], 2, None, None, {}])
        set_lane(mgr, op=S.OP_LOADING, unit=(1, 1), group="T5")
        if name == "loading_solid":
            looks[1] = "i"
        else:
            second = 2.0
    if name == "unload_heating":
        # An unload heating before the AMS moves (voron350, 2026-11-12: the
        # screen showed nothing until the AMS's own unload began).
        s2[1] = "l"
        plan = ("home", "heat", "cut", "retract")
        lane.update(op="LOADED", group="T5", bay="ams2-1",
                    stage=[list(plan), 1, None, None, {}])
        set_lane(mgr, op=S.OP_LOADED, unit=(1, 1), group="T5",
                 stage_plan=plan, stage_index=1)
        looks[1] = "i"
    if name == "load_heating":
        # A load heating on an empty lane: no bay is picked and the lane has
        # no group yet; the plan's GROUP= names the tool and its unit.
        lane.update(stage=[["heat", "feed"], 0, None, None, {}])
        set_lane(mgr, op=S.OP_UNLOADED, stage_group="T5",
                 stage_plan=("heat", "feed"), stage_index=0)
    if name == "fault":
        print_state = "paused"
        s2[1] = "x"
        lane.update(group="T5", stage=[["cut", "retract", "feed"], 2,
                                       "feed", None, {}])
        set_lane(mgr, op=S.OP_UNLOADED, message="load failed",
                 fault_unit=(1, 1), fault_text="feed failed")
        fault2 = {"sev": "pause", "code": "load failed",
                  "text": "feed failed", "unit": "ams2", "bay": 1,
                  "act": ["retry_load", "clear_errors"]}
        looks[1] = "x"
    if name == "drying":
        dryer2 = {"state": "timed_dry", "target": 55, "min": 179,
                  "heat": 60, "fan": 40, "fault": "none", "adapter": True}
        env2 = {"t": 55, "rh": 9}
    if name == "unknown":
        s2[2] = "e"
        spools["ams2"][2] = None
        spools["ams2"][3] = None       # a spool, no tag, no Spoolman link
    for unit, bays in spools.items():
        for bay, spool in enumerate(bays):
            if spool:
                mgr.oams[unit].set_bay_filament(bay, spool[0], spool[1],
                                                "table",
                                                remaining_pct=spool[2])
    mgr.printer = PrinterWithStats(mgr.printer, PrintStats(print_state))
    objects = {
        "openams_ui ams1": ui("ams1", 0, "ams1", s1, {"t": 24, "rh": 41},
                              lane),
        "openams_ui ams2": ui("ams2", 1, "ams2", s2, env2, lane, dryer2,
                              fault2),
        "print_stats": {"state": print_state},
        "toolhead": {"estimated_print_time": second},
    }
    return mgr, objects, second, looks


STATES = ("idle", "loaded", "loading_solid", "loading_outlined",
          "unload_heating", "load_heating", "fault",
          "drying", "unknown")


# ---------------------------------------------------------------------------
# Framebuffer checks (the round-4 design, gen_oams_screen.footprint)
# ---------------------------------------------------------------------------

def tile_footprint(t, look):
    return {(t * 32 + x, row * 16 + y)
            for row in (1, 2, 3) for x, y in gen.footprint(row, look)}


def check_tile(pix, t, look):
    """(off-design pixels, fewest clear pixels between content and frame,
    diagonals counting; for a solid tile, between a knocked-out pixel and
    the box's edge)."""
    foot = tile_footprint(t, look)
    x0, x1 = t * 32, t * 32 + 31
    xs = [x for x, _ in foot]
    ys = [y for _, y in foot]
    box = (min(xs), max(xs), min(ys), max(ys))
    bad = sum(pix[y][x] for x in range(x0, x1 + 1) for y in range(16, 64)
              if not (box[0] <= x <= box[1] and box[2] <= y <= box[3]))
    if look == "i":
        bad += sum(not pix[y][x] for x, y in foot
                   if x in box[:2] or y in box[2:])
        holes = [(x, y) for x, y in foot if not pix[y][x]]
        gap = min(min(x - box[0], box[1] - x, y - box[2], box[3] - y) - 1
                  for x, y in holes)
    else:
        bad += sum(not pix[y][x] for x, y in foot)
        content = [(x, y) for x in range(box[0], box[1] + 1)
                   for y in range(box[2], box[3] + 1)
                   if pix[y][x] and (x, y) not in foot]
        gap = min(max(abs(x - fx), abs(y - fy)) - 1
                  for x, y in content for fx, fy in foot)
    return bad, gap


def header_gap(pix, looks):
    tops = {}
    for t, look in enumerate(looks):
        for x, y in tile_footprint(t, look):
            tops[x] = min(tops.get(x, 99), y)
    best = 99
    for x in range(128):
        lit = [y for y in range(16) if pix[y][x]]
        if lit:
            for nx in (x - 1, x, x + 1):
                if nx in tops:
                    best = min(best, tops[nx] - max(lit) - 1)
    return best


def tile_gaps(pix):
    lit = [any(pix[y][x] for y in range(16, 64)) for x in range(128)]
    out = []
    for b in (32, 64, 96):
        lo, hi = b - 1, b
        while not lit[lo]:
            lo -= 1
        while not lit[hi]:
            hi += 1
        out.append(hi - lo - 1)
    return out


def write_png(path, pix, scale=4, margin=6):
    lit, panel = (24, 32, 40), (196, 214, 190)
    w, h = 128, 64
    W, H = (w + 2 * margin) * scale, (h + 2 * margin) * scale
    rows = []
    for Y in range(H):
        y = Y // scale - margin
        line = bytearray([0])
        for X in range(W):
            x = X // scale - margin
            on = 0 <= y < h and 0 <= x < w and pix[y][x]
            line += bytes(lit if on else panel)
        rows.append(bytes(line))

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(b"".join(rows), 9)))
        f.write(chunk(b"IEND", b""))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class GeneratedFileTest(unittest.TestCase):
    """oams_screen.cfg is the generator's output, and small enough."""

    def test_the_shipped_file_is_up_to_date(self):
        self.assertEqual(gen.main(["--check"]), 0)

    def test_no_value_carries_a_hash_or_a_semicolon_comment(self):
        for number, line in enumerate(SCREEN.read_text().splitlines(), 1):
            if line.startswith("#"):
                continue
            self.assertNotIn("#", line, number)
            self.assertNotIn(" ;", line, number)

    def test_every_glyph_name_a_template_can_build_exists(self):
        names = set(gen.glyphs())
        looks = gen.LOOKS
        rings = ["p%d" % i for i in range(9)] + ["empty", "unknown",
                                                 "fault", "feed", "back",
                                                 "pos"]
        tools = [str(t) for t in range(gen.TOOLS)] + ["none"]
        mats = [m.lower() for m in gen.MATERIALS] + ["q", "none"]
        tens = ["b", "dash", "10"] + [str(d) for d in range(10)]
        units = ["dash"] + [str(d) for d in range(10)]
        for look in looks:
            wanted = (["oams_%s_%s" % (r, look) for r in rings]
                      + ["oams_t%s_%s" % (t, look) for t in tools]
                      + ["oams_m_%s_%s_%s" % (m, half, look)
                         for m in mats for half in "lr"]
                      + ["oams_nl_%s_%s" % (k, look) for k in tens]
                      + ["oams_nr_%s_%s" % (k, look) for k in units])
            self.assertEqual(set(wanted) - names, set())
        self.assertLessEqual(len(names), 270)


@unittest.skipUnless(KLIPPER_DIR, "set OPENAMS_KLIPPER_DIR for pinned "
                                  "Klipper integration")
class ScreenOnTheDisplayTest(unittest.TestCase):
    """The shipped screen, drawn by Klipper's display code."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        folder = pathlib.Path(cls.tmp.name)
        cls.klipper = Klipper(folder)
        cls.shown = {}
        for name in STATES:
            cls.shown[name] = cls.draw(folder, name)
        out = os.environ.get("OAMS_SCREEN_PNG_DIR")
        if out:
            os.makedirs(out, exist_ok=True)
            for name, (chip, _looks) in cls.shown.items():
                write_png(os.path.join(out, "oams_screen_%s.png" % name),
                          chip.pixels())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def draw(cls, folder, name):
        k = cls.klipper
        printer = Printer(k)
        printer.objects["configfile"] = ConfigFile(k, printer)
        (folder / "oams_screen.cfg").write_text(SCREEN.read_text())
        cfg = folder / "printer.cfg"
        cfg.write_text(DISPLAY_SECTION)
        reader = k.configfile.ConfigFileReader()
        fileconfig = reader.build_fileconfig_with_includes(
            cfg.read_text(), str(cfg))
        config = k.configfile.ConfigWrapper(printer, fileconfig, {},
                                            "printer")
        printer.objects["gcode_macro"] = k.gcode_macro.PrinterGCodeMacro(
            config)
        templates = k.display.lookup_display_templates(
            config.getsection("display"))
        chip = Chip(k)
        chip.set_glyphs(templates.get_display_glyphs())
        mgr, objects, second, looks = state(name)
        printer.objects["openams_screen"] = mgr._screen_object()
        for key, values in objects.items():
            printer.objects[key] = Status(**values)
        lcd = types.SimpleNamespace(lcd_chip=chip)
        lcd.draw_text = lambda *a: k.display.PrinterLCD.draw_text(lcd, *a)
        lcd.draw_progress_bar = lambda *a: \
            k.display.PrinterLCD.draw_progress_bar(lcd, *a)
        group = templates.get_display_data_groups()[gen.GROUP]
        chip.clear()
        group.show(lcd, templates.get_display_templates(), second)
        return chip, looks

    def tiles(self, name):
        chip, _looks = self.shown[name]
        return [[chip.glyph_at(row, col) for col in range(0, 16, 2)]
                for row in (1, 2, 3)]

    def test_idle_shows_ams1_every_tile_outlined(self):
        chip, looks = self.shown["idle"]
        self.assertEqual(chip.text_row(0), "A ams1 %%41% 24#")
        self.assertEqual(self.tiles("idle"), [
            ["oams_p6_f", "oams_t0_f", "oams_p4_f", "oams_t1_f",
             "oams_p1_f", "oams_t2_f", "oams_empty_f", "oams_t3_f"],
            ["oams_m_pla_l_f", "oams_m_pla_r_f", "oams_m_abs_l_f",
             "oams_m_abs_r_f", "oams_m_petg_l_f", "oams_m_petg_r_f",
             "oams_m_none_l_f", "oams_m_none_r_f"],
            ["oams_nl_7_f", "oams_nr_8_f", "oams_nl_4_f", "oams_nr_5_f",
             "oams_nl_1_f", "oams_nr_2_f", "oams_nl_dash_f",
             "oams_nr_dash_f"]])

    def test_a_loaded_bay_is_a_solid_tile(self):
        chip, _ = self.shown["loaded"]
        self.assertEqual(chip.text_row(0), "B ams2 %%18% 25#")
        rows = self.tiles("loaded")
        self.assertEqual([r[2:4] for r in rows],
                         [["oams_p5_i", "oams_t5_i"],
                          ["oams_m_petg_l_i", "oams_m_petg_r_i"],
                          ["oams_nl_6_i", "oams_nr_2_i"]])
        self.assertEqual(rows[2][:2], ["oams_nl_10_f", "oams_nr_0_f"])

    def test_a_load_alternates_solid_and_outlined_with_the_arrow(self):
        solid = self.tiles("loading_solid")
        outlined = self.tiles("loading_outlined")
        self.assertEqual(solid[0][2:4], ["oams_feed_i", "oams_t5_i"])
        self.assertEqual(outlined[0][2:4], ["oams_feed_f", "oams_t5_f"])
        chip, _ = self.shown["loading_solid"]
        self.assertEqual(chip.text_row(0), "%%T5 feeding 3/3")

    def test_an_unload_shows_its_steps_from_heating_on(self):
        # Home, heat and the cut come before the AMS moves: the header names
        # the step from the first one, with the unload arrow.
        chip, _ = self.shown["unload_heating"]
        self.assertEqual(chip.text_row(0), "%%T5 heating 2/4")
        self.assertEqual(chip.glyph_at(0, 0), "oams_back")
        self.assertEqual(self.tiles("unload_heating")[0][2:4],
                         ["oams_p5_i", "oams_t5_i"])

    def test_a_load_shows_heating_before_a_bay_is_picked(self):
        chip, _ = self.shown["load_heating"]
        self.assertEqual(chip.text_row(0), "%%T5 heating 1/2")
        self.assertEqual(chip.glyph_at(0, 0), "oams_feed")

    def test_a_fault_has_the_heavy_frame_and_the_alert_header(self):
        chip, _ = self.shown["fault"]
        self.assertEqual(chip.text_row(0), "!T5 feed failed ")
        # The whole header row is inverted: no column of it is dark top
        # to bottom except where a character's own stroke is.
        pix = chip.pixels()
        self.assertTrue(all(pix[0][x] and pix[15][x] for x in range(128)))
        rows = self.tiles("fault")
        self.assertEqual(rows[0][2:4], ["oams_fault_x", "oams_t5_x"])

    def test_drying_shows_the_dryer_and_time_left(self):
        chip, _ = self.shown["drying"]
        self.assertEqual(chip.text_row(0), "B %%2:59  9% 55#")

    def test_an_empty_and_an_unknown_bay(self):
        rows = self.tiles("unknown")
        self.assertEqual([r[4:8] for r in rows],
                         [["oams_empty_f", "oams_t6_f", "oams_unknown_f",
                           "oams_t7_f"],
                          ["oams_m_none_l_f", "oams_m_none_r_f",
                           "oams_m_q_l_f", "oams_m_q_r_f"],
                          ["oams_nl_dash_f", "oams_nr_dash_f",
                           "oams_nl_dash_f", "oams_nr_dash_f"]])

    def test_frames_solid_tiles_and_clearances_on_the_framebuffer(self):
        for name in STATES:
            chip, looks = self.shown[name]
            pix = chip.pixels()
            with self.subTest(state=name):
                for t, look in enumerate(looks):
                    bad, gap = check_tile(pix, t, look)
                    self.assertEqual(bad, 0, "tile %d off design" % t)
                    self.assertGreaterEqual(gap, 2, "tile %d" % t)
                fault = "x" in looks
                self.assertGreaterEqual(header_gap(pix, looks),
                                        1 if fault else 2)
                self.assertGreaterEqual(min(tile_gaps(pix)),
                                        2 if fault else 3)


if __name__ == "__main__":
    unittest.main()
