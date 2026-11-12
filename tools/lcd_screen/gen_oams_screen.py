#!/usr/bin/env python3
"""Generate oams_screen.cfg, the printer LCD's "AMS status" screen.

The screen is a Klipper display group, `oams_tiles`, for a 128x64 display
(16x4 cells): a header row for the unit shown, then one 4x3 tile per bay
(ring and tool, material, remaining percentage). Every bay is outlined, the
loaded bay is a solid tile with its text knocked out, a bay being loaded
alternates the two each second, and a bay a fault names gets a 2-pixel frame.

Every tile row is two 16x16 glyphs, and every glyph exists in three looks
that each carry their share of the tile (f outlined, i solid, x fault), so
the tiles are plain glyph drawing: no XOR, so an ST7920 draws them the same
way. The header's letter badge and alert bar use Klipper's progress bar,
which inverts text on every display.

The screen reads `printer.openams_screen` (src/openams_screen.py: the unit
to show, each bay's material and remaining percent) and the shown unit's
`openams_ui <unit>` (bay states, lane, groups, env, dryer, fault).

    python3 tools/lcd_screen/gen_oams_screen.py           # write the file
    python3 tools/lcd_screen/gen_oams_screen.py --check   # exit 1 if stale

Labels use a 5-pixel bold font condensed from X11 misc-fixed 6x13B (public
domain), a few characters drawn by hand.
"""
import argparse
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
OUTPUT = os.path.join(ROOT, "oams_screen.cfg")
GROUP = "oams_tiles"

# ---------------------------------------------------------------------------
# Font
# ---------------------------------------------------------------------------

with open(os.path.join(HERE, "font_6x13B.json")) as _f:
    _WIDE = json.load(_f)


def _condense(rows):
    """6 pixels to 5: merge the two most alike neighbouring columns."""
    best = min((sum(r[k] != r[k + 1] for r in rows), abs(k - 2), k)
               for k in range(5))[2]
    return ["".join(r[:best]) + ("*" if "*" in (r[best], r[best + 1])
                                 else ".") + "".join(r[best + 2:])
            for r in rows]


HAND = {
    "A": [".***.", "**.**", "**.**", "**.**", "**.**", "*****", "**.**",
          "**.**", "**.**"],
    "G": [".***.", "**.**", "**...", "**...", "**.**", "**.**", "**.**",
          "**.**", ".****"],
    "T": ["*****", ".**..", ".**..", ".**..", ".**..", ".**..", ".**..",
          ".**..", ".**.."],
    "B": ["****.", "**.**", "**.**", "**.**", "****.", "**.**", "**.**",
          "**.**", "****."],
    "K": ["**.**", "**.**", "****.", "***..", "***..", "***..", "****.",
          "**.**", "**.**"],
    "0": [".***.", "**.**", "**.**", "**.**", "**.**", "**.**", "**.**",
          "**.**", ".***."],
    "1": ["..**.", ".***.", "****.", "..**.", "..**.", "..**.", "..**.",
          "..**.", "*****"],
    "4": ["...**", "..***", ".*.**", "*..**", "*****", "...**", "...**",
          "...**", "...**"],
    "6": [".***.", "**...", "**...", "****.", "**.**", "**.**", "**.**",
          "**.**", ".***."],
    "7": ["*****", "...**", "...**", "..**.", "..**.", ".**..", ".**..",
          ".**..", ".**.."],
    "9": [".***.", "**.**", "**.**", "**.**", ".****", "...**", "...**",
          "..**.", ".**.."],
    "%": ["**..*", "**..*", "...*.", "...*.", "..*..", ".*...", ".*...",
          "*..**", "*..**"],
}
FONT = {ch: _condense(rows) for ch, rows in _WIDE.items()}
for _ch, _rows in HAND.items():
    FONT[_ch] = [".....", "....."] + _rows + [".....", "....."]
CW = 5                                         # character width, 1 between

# ---------------------------------------------------------------------------
# Tile geometry (tile x 0-31: the left glyph is x 0-15, the right 16-31)
# ---------------------------------------------------------------------------

# The frame: columns 1 and 29 (3 blank columns between two tiles' frames),
# row 2 of tile row 1 (2 blank pixel lines under the header, its inverted
# bar included) and row 15 of tile row 3. The solid tile fills the same box.
# Content keeps 2 clear pixels inside it on every side.
FL, FR = 1, 29
FT, FB = 2, 15
LOOKS = ("f", "i", "x")                        # outlined, solid, fault


def footprint(row, look):
    """The (x, y) pixels a look draws in one tile row, in tile coordinates:
    the frame's pieces, or the solid box (whose content is knocked out). The
    fault frame is 2 pixels: its sides and top grow outward, so the content
    keeps its clearance; its bottom (the screen's edge) grows inward."""
    top = FT if row == 1 else 0
    bottom = FB if row == 3 else 15
    if look == "i":
        return {(x, y) for x in range(FL, FR + 1)
                for y in range(top, bottom + 1)}
    thick = look == "x"
    sides = (FL - 1, FL, FR, FR + 1) if thick else (FL, FR)
    left, right = (FL - 1, FR + 1) if thick else (FL, FR)
    px = set()
    for y in range(top - 1 if (thick and row == 1) else top, bottom + 1):
        for x in sides:
            px.add((x, y))
    if row == 1:
        for y in ((FT - 1, FT) if thick else (FT,)):
            px.update((x, y) for x in range(left, right + 1))
    if row == 3:
        for y in ((FB - 1, FB) if thick else (FB,)):
            px.update((x, y) for x in range(left, right + 1))
    return px


def blank():
    return [[False] * 32 for _ in range(16)]


def put_text(g, text, x0, y_off=1):
    for i, ch in enumerate(text):
        for y, row in enumerate(FONT[ch]):
            for x, c in enumerate(row):
                if c == "*":
                    g[y + y_off][x0 + i * (CW + 1) + x] = True
    return g


def width(text):
    return len(text) * (CW + 1) - 1


# Row 1: the ring, 9 pixels across, centred on column 9, row 9 (rows 5-13).
RING_CX, RING_CY, R_OUT, R_IN = 9.0, 9.0, 4.5, 3.4


def polar(x, y):
    dx, dy = x - RING_CX, y - RING_CY
    return (math.hypot(dx, dy),
            (math.degrees(math.atan2(dx, -dy)) + 360.) % 360.)


def draw(g, fn):
    for y in range(16):
        for x in range(16):
            if fn(x, y):
                g[y][x] = True
    return g


ARROW = ["................", "................", "................",
         ".........*......", ".........*......", ".........*......",
         ".........*......", "......*..*..*...", ".......*.*.*....",
         "........***.....", ".........*......", "................",
         "......*******...", "................", "................",
         "................"]
POS = ["................", "................", "................",
       ".......***......", "......*...*.....", ".....*.***.*....",
       ".....*.*.*.*....", ".....*.***.*....", "......*...*.....",
       ".......***......", "................", "................",
       "................", "................", "................",
       "................"]
BANG = [".*", ".*", ".*", ".*", "..", ".*"]
DROP = [".......**.......", ".......**.......", "......****......",
        "......****......", ".....******.....", ".....******.....",
        "....********....", "....********....", "...**********...",
        "...*******.**...", "...*******.**...", "...******..**...",
        "....****..**....", ".....******.....", "......****......",
        "................"]
DRYER = ["................", "....*...*...*...", "...*...*...*....",
         "...*...*...*....", "....*...*...*...", ".....*...*...*..",
         ".....*...*...*..", "....*...*...*...", "................",
         "..************..", "..*..........*..", "..*.********.*..",
         "..*..........*..", "..************..", "................",
         "................"]


def arrow(kind, lower):
    rows = {"feed": ARROW, "back": ARROW[:13][::-1] + ARROW[13:],
            "pos": POS}[kind]
    return ["." * 16] * lower + rows[:16 - lower]


def ring(kind):
    """Row 1's left glyph, at full tile width (x 0-15 used)."""
    g = blank()
    if kind[:1] == "p" and kind[1:].isdigit():
        level = int(kind[1:])
        draw(g, lambda x, y: polar(x, y)[0] <= R_OUT and (
            polar(x, y)[0] >= R_IN or polar(x, y)[1] < level * 45. - 1e-6))
    elif kind == "empty":
        draw(g, lambda x, y: R_IN <= polar(x, y)[0] <= R_OUT
             and int(polar(x, y)[1] // 30) % 2 == 0)
    elif kind == "unknown":
        put_text(g, "?", 7, y_off=3)
    elif kind == "fault":
        def lit(x, y):
            on = polar(x, y)[0] <= R_OUT
            mx, my = x - 8, y - 6
            if 0 <= my < len(BANG) and 0 <= mx < 2 and BANG[my][mx] == "*":
                return not on
            return on
        draw(g, lit)
    else:
        rows = arrow(kind, 2)
        draw(g, lambda x, y: rows[y][x] == "*")
    return g


def looked(wide, row, look):
    g = [r[:] for r in wide]
    for x, y in footprint(row, look):
        g[y][x] = (not g[y][x]) if look == "i" else True
    return g


def left(g):
    return [r[:16] for r in g]


def right(g):
    return [r[16:] for r in g]


MATERIALS = ["PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PC", "PVA", "PET",
             "HIPS", "PCTG", "PP", "PPS", "PEEK"]
TOOLS = 16                         # T0-T9; 10-15 drop the T to fit
RINGS = ["p%d" % i for i in range(9)] + ["empty", "unknown", "fault", "feed",
                                         "back", "pos"]


def glyphs():
    """Every glyph of the screen, name -> 16 rows of 16 booleans."""
    out = {}
    for kind in RINGS:
        wide = ring(kind)
        for look in LOOKS:
            out["oams_%s_%s" % (kind, look)] = left(looked(wide, 1, look))
    for t in list(range(TOOLS)) + [None]:
        # A bay in no group (or in one not named T<n>) shows no tool label;
        # its frame stays whole.
        wide = blank() if t is None else put_text(
            blank(), "T%d" % t if t < 10 else "%d" % t, 16, y_off=3)
        for look in LOOKS:
            out["oams_t%s_%s" % ("none" if t is None else t, look)] = right(
                looked(wide, 1, look))
    for m in MATERIALS + ["?", ""]:
        key = {"?": "q", "": "none"}.get(m, m.lower())
        wide = put_text(blank(), m, (32 - width(m)) // 2) if m else blank()
        for look in LOOKS:
            g = looked(wide, 2, look)
            out["oams_m_%s_l_%s" % (key, look)] = left(g)
            out["oams_m_%s_r_%s" % (key, look)] = right(g)
    # The percentage: the left glyph holds the tens (or the "10" of 100),
    # the right one the units and "%", so 24 glyphs per look cover 0-100.
    for key, txt in [("b", ""), ("dash", "-"), ("10", "10")] + \
            [(str(d), str(d)) for d in range(10)]:
        wide = put_text(blank(), txt, 15 - width(txt)) if txt else blank()
        for look in LOOKS:
            out["oams_nl_%s_%s" % (key, look)] = left(looked(wide, 3, look))
    for key, txt in [("dash", "-")] + [(str(d), "%d%%" % d)
                                       for d in range(10)]:
        wide = put_text(blank(), txt, 16)
        for look in LOOKS:
            out["oams_nr_%s_%s" % (key, look)] = right(looked(wide, 3, look))
    # The header's own glyphs, at their own rows.
    for kind in ("feed", "back"):
        rows = arrow(kind, 0)
        out["oams_%s" % kind] = [[c == "*" for c in r] for r in rows]
    out["oams_rh"] = [[c == "*" for c in r] for r in DROP]
    out["oams_dry"] = [[c == "*" for c in r] for r in DRYER]
    return out

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

# Klipper strips '#' to the end of a line and ' ;' starts a comment, so the
# templates use neither. A glyph name that does not exist would draw nothing
# and shift the rest of the row, so every name a template can build is
# generated above.
TEMPLATES = r"""
# The material glyph a bay's material maps to, '' for none (then text).
[display_template _oams_mat]
param_name: ""
text:
  {% set mk = param_name|upper|replace('+', '-')|replace(' ', '-') %}
  {% set mk = mk.split('-')[0] %}
  { mk|lower if mk in @MATERIALS@ else '' }

# A bay's look: x named by a fault, i loaded (and a bay loading, in the
# odd seconds), f outlined.
[display_template _oams_look]
param_bay: 0
text:
  {% set name = printer.openams_screen.active %}
  {% set u = printer['openams_ui ' ~ name] %}
  {% set b = u.bays[param_bay] %}
  {% set busy = u.lane.op in ['LOADING', 'UNLOADING'] and u.lane.bay == name ~ '-' ~ param_bay %}
  {% if b.s == 'x' %}x
  {% elif b.s == 'l' or (busy and printer.toolhead.estimated_print_time|int % 2) %}i
  {% else %}f
  {% endif %}

[display_template _oams_tool]
param_bay: 0
text:
  {% set name = printer.openams_screen.active %}
  {% set u = printer['openams_ui ' ~ name] %}
  {% set key = name ~ '-' ~ param_bay %}
  {% set ns = namespace(tool='') %}
  {% for g in u.groups %}
    {% if not ns.tool and key in g.b|default([]) %}{% set ns.tool = g.n %}{% endif %}
  {% endfor %}
  { ns.tool }

[display_template _oams_tile]
param_bay: 0
param_row: 1
text:
  {% set scr = printer.openams_screen %}
  {% set name = scr.active %}
  {% set u = printer['openams_ui ' ~ name] %}
  {% set b = u.bays[param_bay] %}
  {% set sb = scr.bays[name][param_bay] %}
  {% set busy = u.lane.op in ['LOADING', 'UNLOADING'] and u.lane.bay == name ~ '-' ~ param_bay %}
  {% set s = '_' ~ render('_oams_look', param_bay=param_bay).strip() %}
  {% set known = sb.r is number and b.s != 'e' %}
  {% if param_row == 1 %}
    {% if b.s == 'x' %}{% set icon = 'fault' %}
    {% elif busy %}{% set icon = 'feed' if u.lane.op == 'LOADING' else 'back' %}
    {% elif b.s == 'e' %}{% set icon = 'empty' %}
    {% elif b.s == 'p' %}{% set icon = 'pos' %}
    {% elif known %}{% set icon = 'p%d' % (([sb.r, 100]|min * 8 + 50) // 100) %}
    {% else %}{% set icon = 'unknown' %}
    {% endif %}
    {% set tool = render('_oams_tool', param_bay=param_bay).strip() %}
    {% set tn = tool[1:] if tool[:1] == 'T' else '' %}
    ~oams_{ icon }{ s }~~oams_t{ (tn|int) if (tn.isdigit() and tn|int < @TOOLS@) else 'none' }{ s }~
  {% elif param_row == 2 %}
    {% set mk = render('_oams_mat', param_name=sb.m or '').strip() %}
    {% if b.s == 'e' %}{% set mk = 'none' %}
    {% elif not sb.m %}{% set mk = 'q' %}
    {% endif %}
    {% if mk %}~oams_m_{ mk }_l{ s }~~oams_m_{ mk }_r{ s }~
    {% else %}{ ' %-3s' % sb.m[:3] }
    {% endif %}
  {% elif known %}
    {% set v = [(sb.r + 0.5)|int, 100]|min %}
    ~oams_nl_{ 'b' if v < 10 else ('10' if v >= 100 else v // 10) }{ s }~~oams_nr_{ v % 10 }{ s }~
  {% else %}~oams_nl_dash{ s }~~oams_nr_dash{ s }~
  {% endif %}

[display_template _oams_step]
text:
  {% set lane = printer['openams_ui ' ~ printer.openams_screen.active].lane %}
  {% set st = lane.stage %}
  {% if st and st[1] is not none %}
    {% set step = st[0][st[1]] %}
    {% set words = {'home': 'homing', 'heat': 'heating', 'cut': 'cutting', 'feed': 'feeding', 'purge': 'purging', 'wipe': 'wiping', 'load': 'loading', 'unload': 'unloading'} %}
    { '%s %d/%d' % ((st[4] or {}).get(step) or words.get(step, step), st[1] + 1, st[0]|length) }
  {% elif lane.op == 'LOADING' %}load
  {% else %}unload
  {% endif %}

[display_data @GROUP@ head]
position: 0, 0
text:
  {% set scr = printer.openams_screen if 'openams_screen' in printer else {} %}
  {% set name = scr.active %}
  {% if name %}
    {% set u = printer['openams_ui ' ~ name] %}
    {% set lane = u.lane %}
    {% set e = u.env or {'t': 0, 'rh': 0} %}
    {% set letter = 'ABCDEFGH'[scr.units.index(name)] if scr.units.index(name) < 8 else '?' %}
    {% set left = (u.dryer.min or 0) if u.dryer else 0 %}
    {% set st = lane.stage %}
    {% set mine = (lane.bay or '').startswith(name ~ '-') %}
    {% set planned = st and st[1] is not none and not st[2] and (mine or not lane.bay) %}
    {% if u.fault and u.fault.sev in ['stop', 'pause'] %}
      { ('!' ~ ((lane.group ~ ' ') if lane.group else '') ~ u.fault.text)[:16] }
    {% elif (lane.op in ['LOADING', 'UNLOADING'] and mine) or planned %}
      ~oams_{ 'feed' if lane.op in ['LOADING', 'UNLOADED'] else 'back' }~{ ((scr.tool or lane.group or '') ~ ' ' ~ render('_oams_step').strip())[:14] }
    {% elif u.dryer and u.dryer.state in ['preheat', 'hold', 'timed_dry'] %}
      { letter } ~oams_dry~{ ('%3dh' % (left // 60)) if left >= 600 else ('%d:%02d' % (left // 60, left % 60)) } { '%2d%%' % [e.rh or 0, 99]|min } { '%2d' % (e.t or 0) }~degrees~
    {% else %}
      { letter } { '%-4s' % name[:4] } ~oams_rh~{ '%2d%%' % [e.rh or 0, 99]|min } { '%2d' % (e.t or 0) }~degrees~
    {% endif %}
  {% else %}
    No OpenAMS unit
  {% endif %}

[display_data @GROUP@ row1]
position: 1, 0
text:
  {% set name = printer.openams_screen.active if 'openams_screen' in printer else none %}
  {% if name %}
    {% for i in range([printer['openams_ui ' ~ name].bays|length, printer.openams_screen.bays[name]|length, 4]|min) %}{ render('_oams_tile', param_bay=i, param_row=1) }{% endfor %}
  {% endif %}

[display_data @GROUP@ row2]
position: 2, 0
text:
  {% set name = printer.openams_screen.active if 'openams_screen' in printer else none %}
  {% if name %}
    {% for i in range([printer['openams_ui ' ~ name].bays|length, printer.openams_screen.bays[name]|length, 4]|min) %}{ render('_oams_tile', param_bay=i, param_row=2) }{% endfor %}
  {% endif %}

[display_data @GROUP@ row3]
position: 3, 0
text:
  {% set name = printer.openams_screen.active if 'openams_screen' in printer else none %}
  {% if name %}
    {% for i in range([printer['openams_ui ' ~ name].bays|length, printer.openams_screen.bays[name]|length, 4]|min) %}{ render('_oams_tile', param_bay=i, param_row=3) }{% endfor %}
  {% endif %}

# Drawn last (row 3, column 15): the header's letter badge, or the whole
# header bar for an alert, inverted with Klipper's progress bar. A material
# with no glyph is text, and its row gets the tile's look from bars whose
# top and bottom rows cancel (a UC1701; rows 1-14 of the cell only).
[display_data @GROUP@ zdraw]
position: 3, 15
text:
  {% set name = printer.openams_screen.active if 'openams_screen' in printer else none %}
  {% if name %}
    {% set u = printer['openams_ui ' ~ name] %}
    {% set lane = u.lane %}
    {% set mine = (lane.bay or '').startswith(name ~ '-') %}
    {% set st = lane.stage %}
    {% set busy = (lane.op in ['LOADING', 'UNLOADING'] and mine) or (st and st[1] is not none and not st[2] and (mine or not lane.bay)) %}
    {% if u.fault and u.fault.sev in ['stop', 'pause'] %}{ draw_progress_bar(0, 0, 16, 1.0) }
    {% elif not busy %}{ draw_progress_bar(0, 0, 1, 1.0) }
    {% endif %}
    {% for sb in printer.openams_screen.bays[name][:4] %}
      {% if sb.m and u.bays[loop.index0].s != 'e' and not render('_oams_mat', param_name=sb.m).strip() %}
        {% set look = render('_oams_look', param_bay=loop.index0).strip() %}
        {% for n in {'f': [29, 30, 2, 3], 'x': [29, 31, 1, 3], 'i': [2, 30]}[look] %}{ draw_progress_bar(2, loop.index0 * 4, 4, 1.0 - n / 32.0) }{% endfor %}
      {% endif %}
    {% endfor %}
  {% endif %}
"""


def render_cfg():
    out = [
        "# OpenAMS \"AMS status\" screen for a 128x64 printer display.",
        "#",
        "# GENERATED by tools/lcd_screen/gen_oams_screen.py: do not edit;",
        "# install.sh links it next to oams_menu.cfg, which includes it, so",
        "# it loads only where a [display] exists. OpenAMS > AMS status in",
        "# the LCD menu shows it (SET_DISPLAY_GROUP GROUP=%s) and" % GROUP,
        "# Printer status goes back. It reads printer.openams_screen and the",
        "# shown unit's openams_ui object (docs/guide/lcd-menu.md).",
        "",
    ]
    for name, g in glyphs().items():
        assert len(g) == 16 and all(len(r) == 16 for r in g), name
        out.append("[display_glyph %s]" % name)
        out.append("data:")
        out.extend("  " + "".join("*" if c else "." for c in r) for r in g)
    body = (TEMPLATES.replace("@MATERIALS@", repr(MATERIALS))
            .replace("@TOOLS@", str(TOOLS)).replace("@GROUP@", GROUP))
    return "\n".join(out) + "\n" + body


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="exit 1 when oams_screen.cfg is not up to date")
    args = parser.parse_args(argv)
    text = render_cfg()
    if args.check:
        with open(OUTPUT) as f:
            if f.read() != text:
                print("oams_screen.cfg is stale: run "
                      "tools/lcd_screen/gen_oams_screen.py")
                return 1
        return 0
    with open(OUTPUT, "w") as f:
        f.write(text)
    print("%s: %d glyphs, %d lines" % (OUTPUT, len(glyphs()),
                                       text.count("\n")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
