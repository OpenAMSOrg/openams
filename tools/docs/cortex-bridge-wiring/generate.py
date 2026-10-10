#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Generate the Cortex Bridge wiring page and its wiring diagram.

The diagram is drawn once (build_body) and written twice:

* site/cortex-bridge-wiring.svg  - standalone SVG with its own light background and
                                   embedded fixed-colour styles (for Markdown docs).
* site/index.html                - the customer page; the same drawing is inlined and
                                   styled from the page's light/dark colour tokens.

Colour tokens and diagram class rules are also defined once here, so the standalone
styles, the page's tokens and the page's diagram CSS cannot drift apart.

Usage:
    python3 generate.py          # write site/index.html and site/cortex-bridge-wiring.svg
    python3 generate.py --docs   # also write docs/images/cortex-bridge-wiring.svg in the repo

Python 3 standard library only.
"""
import argparse
import re
import sys
import xml.dom.minidom
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]                      # tools/docs/cortex-bridge-wiring -> repo root
TEMPLATE = HERE / "page.tpl.html"
SITE = HERE / "site"
OUT_HTML = SITE / "index.html"
OUT_SVG = SITE / "cortex-bridge-wiring.svg"
DOCS_SVG = REPO / "docs" / "images" / "cortex-bridge-wiring.svg"

W, H = 1100, 1344                           # diagram viewBox; the page shows it at >= 880 px (labels >= 12 px)

URL_SPLIT = "https://makerworld.com/en/models/816201-2-to-5-way-ptfe-splitter#profileId-2452271"
# The OpenAMS repository on GitHub: every file and guide link on the page starts here.
URL_GITHUB = "https://github.com/OpenAMSOrg/openams"
GITHUB_REF = "main"
GH_BLOB = "%s/blob/%s" % (URL_GITHUB, GITHUB_REF)
GH_TREE = "%s/tree/%s" % (URL_GITHUB, GITHUB_REF)
URL_COUPLER_STL = GH_BLOB + "/hardware/ptfe-coupler/ptfe_coupler_v5.stl"
# Bambu Lab's guide that removes and refits the buffer's slider and spring.
URL_BAMBU_BUFFER = ("https://wiki.bambulab.com/en/ams/filament-buffer-connection-board-replacement-guide"
                    "#step-1-remove-the-buffer-slider-and-spring")

# ---------------------------------------------------------------------------------------------
# Colour tokens: page tokens and diagram tokens, light and dark.
# ---------------------------------------------------------------------------------------------
PAGE_LIGHT = {"bg": "#f2f5f4", "surface": "#ffffff", "ink": "#162027", "muted": "#55616a", "rule": "#d2dad8",
              "accent": "#1d6a47", "accent-soft": "#e1eee7", "warn": "#9e4806", "warn-bg": "#fcf0e2",
              "warn-rule": "#e9c39a", "photo-bg": "#e8edec", "callout": "#1d6a47", "callout-ink": "#ffffff"}
PAGE_DARK = {"bg": "#0d1315", "surface": "#141c1f", "ink": "#e2e9ea", "muted": "#9ba9ae", "rule": "#2a373b",
             "accent": "#62cf9a", "accent-soft": "#17302a", "warn": "#ffac6a", "warn-bg": "#2a1c10",
             "warn-rule": "#5e3d1e", "photo-bg": "#c4cccd", "callout": "#1d6a47", "callout-ink": "#ffffff"}
DIAGRAM_TOKENS = [  # name, light, dark
    ("d-panel", "#f9fbfa", "#11191c"), ("d-frame", "#edf1f0", "#162024"), ("d-frame-edge", "#bac5c3", "#304046"),
    ("d-card", "#ffffff", "#1a2428"), ("d-line", "#8f9ca0", "#5f7076"), ("d-ink", "#162027", "#e2e9ea"),
    ("d-muted", "#4b575f", "#a7b4b9"), ("port", "#d9dfe1", "#33424a"),
    ("usb", "#1f62c4", "#6fa7ff"), ("pwr", "#c9381d", "#ff7a5c"), ("pwr-text", "#b4321a", "#ff8e74"),
    ("rs", "#7a45c8", "#b893ff"), ("ams", "#0b8676", "#3ccfb8"), ("fil", "#d98a00", "#ffb63a"),
    ("tube", "#f2d9a7", "#4f3d1c"), ("exist", "#86929a", "#7d8b92"),
    ("pcb", "#1f5b3e", "#1c4e36"), ("pcb-edge", "#123f2a", "#3a7f5c"), ("silk", "#f1f5ee", "#eef3ec"),
    ("silk-faint", "#86ae98", "#6f9a83"), ("silk-note", "#c2dccc", "#b3d2c0"),
    ("metal", "#cdd3d7", "#aeb6bc"), ("metal-edge", "#6f7a80", "#5e6970"), ("metal-in", "#59636a", "#3f474d"),
    ("metal-ink", "#172026", "#172026"),
    ("term", "#52c98e", "#3fb27b"), ("term-edge", "#23784f", "#1f6b47"),
    ("screw", "#dfe3e5", "#c9ced1"), ("screw-edge", "#5d686d", "#4a5458"),
    ("conn-white", "#f6f6f1", "#e6e6df"), ("conn-white-in", "#c9cbc2", "#a9aca2"),
    ("conn-dark", "#2b3034", "#16191c"), ("cavity", "#121517", "#050607"),
    ("fuse", "#efe9d8", "#d6cfbb"), ("pad", "#d9b44a", "#c9a540"), ("pad-edge", "#9c7c22", "#8a6c1c"),
    ("led-w", "#ffffff", "#ffffff"), ("led-g", "#3ddc74", "#3ddc74"), ("led-r", "#ff4632", "#ff4632"),
    ("dev", "#e6eaec", "#2a353b"), ("dev-edge", "#7d8a8f", "#71838a"), ("dev-ink", "#172026", "#e2e9ea"),
    ("pi-pcb", "#2f8a45", "#2a7039"), ("pi-edge", "#1c5e2c", "#56a565"),
    ("usb3", "#2f6fd6", "#4d8ef0"), ("usb2", "#3a4146", "#2c3237"),
    ("vent", "#98a3a9", "#6a777e"), ("lid", "#8fa7b4", "#7d97a5"), ("flange", "#cfd6da", "#8e999f"),
    ("sp1", "#d9534f", "#d9534f"), ("sp2", "#f0b429", "#f0b429"), ("sp3", "#3d8bd4", "#3d8bd4"),
    ("sp4", "#4fae6c", "#4fae6c"),
]
LIGHT = dict(PAGE_LIGHT)
LIGHT.update({n: light for n, light, _ in DIAGRAM_TOKENS})
DARK = dict(PAGE_DARK)
DARK.update({n: dark for n, _, dark in DIAGRAM_TOKENS})
# Print always uses the light palette on white paper.
PRINT = dict(LIGHT, bg="#ffffff", surface="#ffffff", **{"warn-bg": "#ffffff", "d-panel": "#ffffff"})

SANS_SVG = '"Helvetica Neue",Helvetica,Arial,"Liberation Sans","Nimbus Sans","DejaVu Sans",sans-serif'
MONO_SVG = '"DejaVu Sans Mono",Menlo,Consolas,"Liberation Mono",monospace'

# ---------------------------------------------------------------------------------------------
# Diagram class rules, written with var(--token). The standalone SVG resolves them to the light
# values; the page uses them as-is under .wd so they follow the theme.
# ---------------------------------------------------------------------------------------------
RULES = [
    (".bg", "fill:var(--d-panel);stroke:var(--rule);stroke-width:1.5"),
    (".frame", "fill:var(--d-frame);stroke:var(--d-frame-edge);stroke-width:1.5"),
    (".box", "fill:var(--d-card);stroke:var(--d-line);stroke-width:1.5"),
    (".opt-outline", "fill:none;stroke:var(--d-line);stroke-width:1.5;stroke-dasharray:7 5"),
    (".legend", "fill:var(--d-card);stroke:var(--rule);stroke-width:1.2"),
    (".th", "font-size:17.5px;font-weight:700"),
    (".tb", "font-size:16px;font-weight:700"),
    (".ts", "font-size:15px;fill:var(--d-muted)"),
    (".tw", "font-size:15px;font-weight:700;fill:var(--warn)"),
    (".tref", "font-size:15px;font-weight:700;fill:var(--d-ink)"),
    (".tpol", "font-size:15px;font-weight:700;fill:var(--pwr-text)"),
    (".tnum", "font-size:15px;font-weight:700;fill:var(--d-ink)"),
    (".tlink", "font-size:15px;font-weight:700;fill:var(--accent);text-decoration:underline"),
    (".tdots", "font-size:34px;font-weight:700;fill:var(--d-muted);letter-spacing:2px"),
    (".silk", "font-size:15px;font-weight:700;fill:var(--silk)"),
    (".silk-big", "font-size:19px;font-weight:700;fill:var(--silk)"),
    (".silk-note", "font-size:15px;font-style:italic;fill:var(--silk-note)"),
    (".ann", "font-size:15px;font-weight:700;fill:var(--silk)"),
    (".dev-t", "font-size:15px;font-weight:700;fill:var(--dev-ink)"),
    (".metal-t", "font-size:16px;font-weight:700;fill:var(--metal-ink)"),
    (".metal-ts", "font-size:15px;fill:var(--metal-ink)"),
    (".leader", "fill:none;stroke:var(--d-line);stroke-width:1.2;stroke-dasharray:2 3"),
    (".fitting", "fill:var(--port);stroke:var(--d-line);stroke-width:1"),
    (".pcb", "fill:var(--pcb);stroke:var(--pcb-edge);stroke-width:2"),
    (".silk-line", "fill:none;stroke:var(--silk);stroke-width:1.2"),
    (".silk-faint", "fill:none;stroke:var(--silk-faint);stroke-width:1.2;stroke-dasharray:4 3"),
    (".metal", "fill:var(--metal);stroke:var(--metal-edge);stroke-width:1.2"),
    (".metal-in", "fill:var(--metal-in)"),
    (".term", "fill:var(--term);stroke:var(--term-edge);stroke-width:1.2"),
    (".screw", "fill:var(--screw);stroke:var(--screw-edge);stroke-width:1.2"),
    (".slot", "stroke:var(--screw-edge);stroke-width:2;stroke-linecap:round"),
    (".conn-white", "fill:var(--conn-white);stroke:var(--metal-edge);stroke-width:1.2"),
    (".conn-white-in", "fill:var(--conn-white-in)"),
    (".pin", "stroke:var(--pad-edge);stroke-width:1.6"),
    (".conn-dark", "fill:var(--conn-dark);stroke:var(--cavity);stroke-width:1.2"),
    (".cavity", "fill:var(--cavity);stroke:var(--metal-edge);stroke-width:1"),
    (".fuse", "fill:var(--fuse);stroke:var(--metal-edge);stroke-width:1"),
    (".fuse-cap", "fill:var(--metal)"),
    (".pad", "fill:var(--pad);stroke:var(--pad-edge);stroke-width:1"),
    (".led-w", "fill:var(--led-w);stroke:var(--metal-edge);stroke-width:1"),
    (".led-g", "fill:var(--led-g);stroke:var(--pcb-edge);stroke-width:1"),
    (".led-r", "fill:var(--led-r);stroke:var(--pcb-edge);stroke-width:1"),
    (".halo-w", "fill:var(--led-w);fill-opacity:.42"),
    (".halo-g", "fill:var(--led-g);fill-opacity:.38"),
    (".halo-r", "fill:var(--led-r);fill-opacity:.4"),
    (".dev", "fill:var(--dev);stroke:var(--dev-edge);stroke-width:1.5"),
    (".pi", "fill:var(--pi-pcb);stroke:var(--pi-edge);stroke-width:1.5"),
    (".hole-ring", "fill:var(--pad);stroke:var(--pad-edge);stroke-width:.8"),
    (".hole", "fill:var(--d-panel)"),
    (".gpin", "fill:var(--pad)"),
    (".usb3t", "fill:var(--usb3)"),
    (".usb2t", "fill:var(--usb2)"),
    (".fin", "stroke:var(--metal-edge);stroke-width:1.4"),
    (".chip", "fill:var(--conn-dark)"),
    (".psu", "fill:var(--metal);stroke:var(--metal-edge);stroke-width:1.5"),
    (".vent", "fill:var(--vent)"),
    (".wire-stub", "stroke:var(--exist);stroke-width:3;stroke-linecap:round"),
    (".lid", "fill:var(--lid);fill-opacity:.32;stroke:var(--dev-edge);stroke-width:1.5"),
    (".flange", "fill:var(--flange);stroke:var(--dev-edge);stroke-width:1"),
    (".sp1", "fill:var(--sp1)"), (".sp2", "fill:var(--sp2)"), (".sp3", "fill:var(--sp3)"), (".sp4", "fill:var(--sp4)"),
    (".spring", "fill:none;stroke:var(--metal-edge);stroke-width:1.6;stroke-linejoin:round"),
    (".slider", "fill:var(--metal);stroke:var(--metal-edge);stroke-width:1.2"),
    (".gear", "fill:var(--metal);stroke:var(--metal-edge);stroke-width:1.2"),
    (".gear-teeth", "fill:none;stroke:var(--metal-edge);stroke-width:4;stroke-dasharray:3 3"),
    (".nozzle", "fill:var(--pad);stroke:var(--pad-edge);stroke-width:1"),
    (".cutter", "fill:var(--warn)"),
    (".faded", "opacity:.55"),
    (".w-usb", "fill:none;stroke:var(--usb);stroke-width:4;stroke-linejoin:round;stroke-linecap:round"),
    (".plug-usb", "fill:var(--usb)"),
    (".w-pwr", "fill:none;stroke:var(--pwr);stroke-width:3;stroke-linejoin:round"),
    (".w-4", "fill:none;stroke:var(--rs);stroke-width:7"),
    (".plug-4", "fill:var(--rs)"),
    (".w-6", "fill:none;stroke:var(--ams);stroke-width:7;stroke-linejoin:round"),
    (".plug-6", "fill:var(--ams)"),
    (".w-6opt", "fill:none;stroke:var(--ams);stroke-width:5;stroke-dasharray:10 7;stroke-linejoin:round"),
    (".slash", "stroke:var(--d-ink);stroke-width:2"),
    (".tube", "fill:none;stroke:var(--tube);stroke-width:15;stroke-linejoin:round"),
    (".fil", "fill:none;stroke:var(--fil);stroke-width:4;stroke-dasharray:9 6;stroke-linejoin:round"),
    (".tube-s", "fill:none;stroke:var(--tube);stroke-width:10;stroke-linejoin:round"),
    (".fil-s", "fill:none;stroke:var(--fil);stroke-width:3;stroke-dasharray:7 5;stroke-linejoin:round"),
    (".fil-head", "fill:var(--fil)"),
    (".rib", "stroke:var(--dev-edge);stroke-width:1.5"),
    (".w-exist", "fill:none;stroke:var(--exist);stroke-width:2.2;stroke-dasharray:6 5"),
]
MONO_CLASSES = [".tref", ".silk", ".silk-big"]


def resolve(decl, tokens=None):
    """Replace var(--token) with the token's value, light by default (for the standalone SVG)."""
    tokens = LIGHT if tokens is None else tokens
    return re.sub(r"var\(--([\w-]+)\)", lambda m: tokens[m.group(1)], decl)


def colour_only(decl):
    """The colour declarations of a rule (fill, stroke, colour); sizes and dashes stay as they are."""
    keep = [d for d in decl.split(";") if "var(--" in d]
    return ";".join(keep)


# ---------------------------------------------------------------------------------------------
# SVG writer helpers. Output order and number formatting are part of the file format: keep them
# stable so regenerated files diff cleanly.
# ---------------------------------------------------------------------------------------------
out = []


def a(s):
    out.append(s)


def f(v):
    return ("%.2f" % v).rstrip("0").rstrip(".")


def R(x, y, w, h, c, rx=None):
    a('<rect class="%s" x="%s" y="%s" width="%s" height="%s"%s/>' % (
        c, f(x), f(y), f(w), f(h), (' rx="%s"' % f(rx)) if rx else ""))


def C(cx, cy, r, c):
    a('<circle class="%s" cx="%s" cy="%s" r="%s"/>' % (c, f(cx), f(cy), f(r)))


def L(x1, y1, x2, y2, c):
    a('<line class="%s" x1="%s" y1="%s" x2="%s" y2="%s"/>' % (c, f(x1), f(y1), f(x2), f(y2)))


def P(d, c):
    a('<path class="%s" d="%s"/>' % (c, d))


def PG(pts, c):
    a('<polygon class="%s" points="%s"/>' % (c, " ".join("%s,%s" % (f(x), f(y)) for x, y in pts)))


def T(x, y, s, c=None, anchor=None):
    a('<text%s x="%s" y="%s"%s>%s</text>' % ((' class="%s"' % c) if c else "", f(x), f(y),
                                              (' text-anchor="%s"' % anchor) if anchor else "", s))


def LINK(x, y, s, href, anchor=None):
    """A text label that is a clickable link in the page (opens in a new tab)."""
    a('<a href="%s" target="_blank" rel="noopener">' % href)
    T(x, y, s, "tlink", anchor)
    a("</a>")


def G(attrs=""):
    a("<g%s>" % ((" " + attrs) if attrs else ""))


def GE():
    a("</g>")


def comment(s):
    a("<!-- %s -->" % s)


def screw(cx, cy, r=10):
    C(cx, cy, r, "screw")
    L(cx - r * .65, cy + r * .65, cx + r * .65, cy - r * .65, "slot")


# ---------------------------------------------------------------------------------------------
# Parts
# ---------------------------------------------------------------------------------------------
def printer():
    """Printer frame: its own 24 V supply (spare terminals), mainboard and toolhead with cutter."""
    comment("Printer: 24 V supply, mainboard, toolhead (existing, unchanged)")
    G('id="printer"')
    R(16, 16, 1068, 210, "frame", 14)
    T(34, 44, "Printer", "th")
    R(34, 56, 180, 104, "psu", 4)
    for r in range(2):
        for c in range(9):
            R(44 + c * 18.4, 64 + r * 11, 12, 5, "vent", 2.5)
    T(124, 112, "24 V power supply", "metal-t", "middle")
    T(124, 132, "(the printer&#8217;s own)", "metal-ts", "middle")
    T(52, 154, "+", "metal-t", "middle")
    T(76, 154, "&#8722;", "metal-t", "middle")
    R(34, 160, 180, 26, "conn-dark", 2)
    for i in range(7):
        screw(52 + i * 24, 173, 8)
    for i in range(2, 7):                    # terminals already used by the printer
        L(52 + i * 24, 187, 52 + i * 24, 197, "wire-stub")
    R(380, 56, 180, 130, "box", 10)
    T(470, 116, "Mainboard", "tb", "middle")
    T(470, 138, "unchanged", "ts", "middle")
    R(862, 61, 16, 11, "fitting", 1.5)
    R(825, 72, 90, 68, "dev", 8)
    C(870, 106, 19, "gear-teeth")
    C(870, 106, 16, "gear")
    C(870, 106, 5, "metal-in")
    R(848, 140, 44, 24, "metal", 2)
    PG([(860, 164), (880, 164), (870, 181)], "nozzle")
    R(798, 93, 30, 11, "cutter", 3)
    PG([(826, 91), (841, 98.5), (826, 106)], "cutter")
    L(790, 117, 800, 104, "leader")
    T(788, 86, "Toolhead", "tb", "end")
    T(788, 106, "extruder", "ts", "end")
    T(788, 128, "filament cutter", "tw", "end")
    T(788, 147, "(required)", "tw", "end")
    GE()


def pi5(x0, y0):
    """Raspberry Pi 5, 85 x 56 mm at 3 px/mm, rotated 180 degrees: GPIO along the bottom edge,
    USB-A and Ethernet on the left edge, USB-C and micro-HDMI on the top edge."""
    comment("Klipper computer: Raspberry Pi 5 (conceptual)")
    G('id="pi5"')
    s = 3.0
    w, h = 85 * s, 56 * s
    R(x0, y0, w, h, "pi", 9)
    for hx in (x0 + w - 3.5 * s, x0 + w - 61.5 * s):
        for hy in (y0 + 3.5 * s, y0 + h - 3.5 * s):
            C(hx, hy, 8, "hole-ring")
            C(hx, hy, 4.2, "hole")
    gx0 = x0 + w - 57.9 * s                 # 40-pin GPIO header
    R(gx0, y0 + h - 18.5, 50.8 * s, 16, "chip", 1.5)
    for i in range(20):
        for r in range(2):
            R(gx0 + 3.81 + i * 7.62 - 1.6, y0 + h - 14.3 + r * 7.62 - 1.6, 3.2, 3.2, "gpin")
    R(x0 + 128, y0 + 43, 48, 48, "metal", 3)   # SoC heatsink
    for i in range(7):
        L(x0 + 134 + i * 6, y0 + 48, x0 + 134 + i * 6, y0 + 86, "fin")
    R(x0 + 184, y0 + 46, 36, 22, "chip", 2)    # RAM
    R(x0 + 72, y0 + 66, 28, 28, "chip", 2)     # I/O chip
    R(x0 + 186, y0 + 104, 28, 10, "conn-white", 1.5)   # fan header
    for i in range(4):
        C(x0 + 191 + i * 6, y0 + 109, 1.6, "gpin")
    cx = x0 + w - 11.2 * s                  # USB-C power
    R(cx - 13.5, y0 - 4, 27, 18, "metal", 5)
    R(cx - 9, y0 - 2, 18, 5, "metal-in", 2.5)
    for c in (x0 + w - 26 * s, x0 + w - 39.5 * s):     # two micro-HDMI
        R(c - 11, y0 - 3, 22, 16, "metal", 2)
        R(c - 8, y0 - 1, 16, 4, "metal-in", 1)
    ex = x0 - 8                             # Ethernet, USB 3 (blue), USB 2 (black)
    R(ex, y0 + 6, 63, 48, "metal", 2)
    R(ex + 2, y0 + 12, 12, 36, "cavity", 1.5)
    R(ex + 14, y0 + 26, 5, 8, "cavity")
    for (yy, tongue) in ((y0 + 61, "usb3t"), (y0 + 107, "usb2t")):
        R(ex, yy, 51, 39, "metal", 2)
        for k in range(2):
            R(ex + 2, yy + 4 + k * 17, 20, 14, "cavity", 1.5)
            R(ex + 5, yy + 8 + k * 17, 15, 5, tongue, 1)
    GE()


def board(ox, oy):
    """Cortex Bridge Rev A4, 55 x 38 mm at 6 px/mm, component side seen from above.
    Positions follow the A4 PCB: J1 left, P1 lower left (- upper screw, + lower), J4 top,
    CN1 right, J7 bottom right, F1 bottom, D9/D10/D8 LEDs."""
    def X(v):
        return ox + v

    def Y(v):
        return oy + v
    comment("Cortex Bridge Rev A4 (55 x 38 mm, 6 px/mm)")
    G('id="bridge"')
    R(X(0), Y(0), 330, 228, "pcb", 18)
    R(X(209), Y(17), 113, 92, "silk-faint", 2)          # unfitted 10BASE-T option
    T(X(266), Y(14), "10BASE-T", "silk", "middle")
    T(X(266), Y(69), "not fitted", "silk-note", "middle")
    R(X(95), Y(-5), 78, 31, "conn-white", 3)            # J4 PRGRM
    R(X(103), Y(-2), 62, 12, "conn-white-in", 1.5)
    for i in range(6):
        L(X(115.3 + i * 7.5), Y(13), X(115.3 + i * 7.5), Y(23), "pin")
    T(X(90), Y(22), "PRGRM", "silk", "end")
    R(X(-8), Y(57), 44, 54, "metal", 6)                 # J1 USB-C
    R(X(-4), Y(64), 7, 40, "metal-in", 3)
    T(X(43), Y(51), "USBC", "silk", "middle")
    T(X(4), Y(125), "PWR IN", "silk")                   # P1
    R(X(-9), Y(128), 61, 62, "term", 3)
    screw(X(26), Y(144))
    screw(X(26), Y(174))
    T(X(27), Y(213), "+", "silk-big", "middle")
    R(X(38), Y(204), 38, 15, "fuse", 2)                 # F1 5 A
    R(X(38), Y(204), 7, 15, "fuse-cap")
    R(X(69), Y(204), 7, 15, "fuse-cap")
    T(X(80), Y(217), "5A", "silk")
    T(X(286), Y(126), "BUFFER", "silk", "middle")       # CN1
    R(X(278), Y(131), 61, 58, "conn-dark", 3)
    for (cx, cy) in ((300, 143), (318, 143), (300, 164), (318, 164)):
        R(X(cx), Y(cy), 13, 13, "cavity", 1.5)
    R(X(212), Y(207), 76, 15, "silk-line", 1)           # J7
    R(X(215.2), Y(210), 9, 9, "pad")
    for cx in (235, 250.2, 265.4, 280.6):
        C(X(cx), Y(214.5), 4.5, "pad")
    C(X(75), Y(110), 11, "halo-w")                       # LEDs, all lit when wired
    R(X(70.5), Y(107), 9, 6, "led-w", 1)
    T(X(91), Y(116), "D9 5 V", "ann")
    C(X(171), Y(131), 11, "halo-g")
    R(X(166.5), Y(128), 9, 6, "led-g", 1)
    T(X(171), Y(158), "D10 3.3 V", "ann", "middle")
    C(X(185), Y(219), 10, "halo-r")
    R(X(180.5), Y(216), 9, 6, "led-r", 1)
    T(X(185), Y(201), "D8 24 V", "ann", "middle")
    T(X(-14), Y(52), "J1", "tref", "end")
    T(X(-15), Y(212), "P1", "tref", "end")
    T(X(345), Y(212), "CN1", "tref")
    a('<text x="%s" y="%s" text-anchor="middle"><tspan class="tref">J4 PRGRM</tspan>'
      '<tspan class="tw" dx="8">leave empty</tspan></text>' % (f(X(134)), f(Y(-13))))
    L(X(57), Y(221), X(57), Y(236), "leader")
    T(X(57), Y(253), "5 A fuse", "ts", "middle")
    L(X(250), Y(224), X(250), Y(236), "leader")
    a('<text x="%s" y="%s" text-anchor="middle"><tspan class="tref">J7</tspan>'
      '<tspan class="tw" dx="7">leave empty</tspan></text>' % (f(X(250)), f(Y(253))))
    a('<text class="th" x="%s" y="%s">Cortex Bridge <tspan class="ts" font-weight="400">Rev A4</tspan></text>'
      % (f(X(0)), f(Y(282))))
    T(X(0), Y(302), "seen from above", "ts")
    GE()


def buffer_unit(x0, y0):
    """Original Bambu Lab AMS filament buffer, generic. 120 x 230: PTFE out (top, local x 95),
    PTFE in (bottom, local x 30), slider slot, 4-pin connector (left side), 6-pin connector (right side)."""
    comment("AMS filament buffer (conceptual)")
    G('id="buffer"')
    R(x0, y0, 120, 230, "dev", 12)
    R(x0 + 87, y0 - 10, 16, 10, "fitting", 1.5)
    R(x0 + 22, y0 + 230, 16, 10, "fitting", 1.5)
    R(x0 + 54, y0 + 30, 14, 170, "cavity", 7)
    pts = [(x0 + (56 if k % 2 == 0 else 66), y0 + 36 + k * 7) for k in range(9)]
    a('<polyline class="spring" points="%s"/>' % " ".join("%s,%s" % (f(x), f(y)) for x, y in pts))
    R(x0 + 50, y0 + 98, 22, 26, "slider", 4)
    R(x0 - 8, y0 + 100, 8, 20, "conn-dark", 1.5)
    T(x0 + 6, y0 + 116, "4-pin", "dev-t")
    R(x0 + 120, y0 + 180, 8, 20, "conn-dark", 1.5)
    T(x0 + 114, y0 + 196, "6-pin", "dev-t", "end")
    GE()


def ams_art(x0, y0, s=1.0, ident="ams1", extra=""):
    """Conceptual 4-bay AMS, front view: hinged smoked lid, 4 spools edge-on.
    PTFE outlet at the top right; 6-pin ports on both sides (daisy chain in/out)."""
    G('id="%s" transform="translate(%s %s)%s"%s' % (ident, f(x0), f(y0), (" scale(%s)" % f(s)) if s != 1 else "", extra))
    R(188, -12, 16, 16, "fitting", 1.5)
    for i, cx in enumerate((30, 80, 130, 180)):
        R(cx - 21, 14, 5, 92, "flange", 2)
        R(cx + 16, 14, 5, 92, "flange", 2)
        R(cx - 16, 24, 32, 72, "sp%d" % (i + 1), 3)
    P("M4 84 V28 Q4 4 28 4 H182 Q206 4 206 28 V84 Z", "lid")
    R(0, 76, 210, 104, "dev", 12)
    R(88, 70, 34, 10, "dev", 3)
    for cx in (30, 80, 130, 180):
        R(cx - 13, 100, 26, 12, "cavity", 4)
        C(cx, 132, 4, "fitting")
    L(10, 154, 200, 154, "leader")
    R(-8, 130, 8, 20, "conn-dark", 1.5)
    R(210, 130, 8, 20, "conn-dark", 1.5)
    R(14, 180, 26, 6, "conn-dark", 2)
    R(170, 180, 26, 6, "conn-dark", 2)
    GE()


def splitter_inset(x0, y0):
    """Detail: up to 4 tube inputs merging into 1 output."""
    comment("Inset: PTFE splitter detail")
    G('id="splitter-detail"')
    R(x0, y0, 188, 250, "legend", 10)
    T(x0 + 16, y0 + 26, "Splitter detail", "tb")
    cx = x0 + 94
    T(cx, y0 + 50, "1 output, to buffer", "ts", "middle")
    P("M%s %s V%s" % (f(cx), f(y0 + 60), f(y0 + 76)), "tube-s")
    P("M%s %s V%s" % (f(cx), f(y0 + 60), f(y0 + 76)), "fil-s")
    R(cx - 8, y0 + 76, 16, 8, "fitting", 1.5)
    R(x0 + 26, y0 + 84, 136, 56, "dev", 8)
    ins = [x0 + 46, x0 + 78, x0 + 110, x0 + 142]
    for xi in ins:
        d = "M%s %s C%s %s %s %s %s %s" % (f(xi), f(y0 + 140), f(xi), f(y0 + 110), f(cx), f(y0 + 112), f(cx), f(y0 + 84))
        P(d, "tube-s")
        P(d, "fil-s")
    for xi in ins:
        R(xi - 5, y0 + 140, 10, 7, "fitting", 1.5)
        P("M%s %s V%s" % (f(xi), f(y0 + 147), f(y0 + 166)), "tube-s")
        P("M%s %s V%s" % (f(xi), f(y0 + 147), f(y0 + 166)), "fil-s")
    T(cx, y0 + 190, "up to 4 inputs,", "ts", "middle")
    T(cx, y0 + 210, "one per AMS", "ts", "middle")
    T(cx, y0 + 234, "(printed)", "ts", "middle")
    GE()


def legend(x0, y0):
    comment("Legend")
    G('id="legend"')
    R(x0, y0, 1052, 120, "legend", 10)
    T(x0 + 18, y0 + 26, "Legend", "tb")
    cols = [
        (x0 + 18, [("usb", "USB data cable (USB-A to USB-C)"), ("pwr", "24 V from the printer&#8217;s supply"),
                   ("w4", "4-pin buffer cable (24 V + RS-485)")]),
        (x0 + 396, [("w6", "6-pin AMS cable"), ("fil", "Filament, PTFE 3 mm ID / 4 mm OD"),
                    ("exist", "Existing link, unchanged")]),
        (x0 + 756, [("opt", "Optional: dashed or faded"), ("txt", "One AMS: its tube goes straight"),
                    ("txt", "to the buffer, no splitter")]),
    ]
    for sx0, rows in cols:
        for i, (k, label) in enumerate(rows):
            y = y0 + 52 + i * 26
            sx1 = sx0 + 58
            tx = sx0 + 72
            if k == "usb":
                L(sx0 + 2, y, sx1 - 2, y, "w-usb")
            elif k == "pwr":
                L(sx0, y - 4, sx1, y - 4, "w-pwr")
                L(sx0, y + 4, sx1, y + 4, "w-pwr")
            elif k in ("w4", "w6"):
                L(sx0, y, sx1, y, "w-4" if k == "w4" else "w-6")
                L(sx0 + 24, y + 9, sx0 + 32, y - 9, "slash")
            elif k == "fil":
                L(sx0, y, sx1, y, "tube")
                L(sx0 + 2, y, sx1 - 2, y, "fil")
            elif k == "exist":
                L(sx0, y, sx1, y, "w-exist")
            elif k == "opt":
                L(sx0, y, sx1, y, "w-6opt")
            elif k == "txt":                 # note without a line sample
                tx = sx0
            T(tx, y + 5, label, "ts")
    GE()


# ---------------------------------------------------------------------------------------------
# The whole drawing
# ---------------------------------------------------------------------------------------------
def build_body():
    out.clear()
    R(1, 1, W - 2, H - 2, "bg", 18)
    printer()

    comment("Existing, unchanged link")
    L(470, 290, 470, 186, "w-exist")
    T(458, 250, "existing USB or CAN link", "ts", "end")
    T(458, 268, "to mainboard and toolhead,", "ts", "end")
    T(458, 286, "unchanged", "tw", "end")

    pi5(440, 290)
    T(708, 330, "Klipper computer", "tb")
    T(708, 350, "(e.g. Raspberry Pi 5)", "ts")

    comment("24 V from the printer's power supply to P1")
    P("M76 186 V654 H411", "w-pwr")          # - to the upper screw
    P("M52 186 V684 H411", "w-pwr")          # + to the lower screw
    T(88, 246, "spare terminals", "ts")
    T(405, 648, "&#8722; 0 V", "tpol", "end")
    T(405, 678, "+24 V", "tpol", "end")
    T(92, 722, "24 V from the printer&#8217;s", "tb")
    T(92, 742, "power supply", "tb")
    T(92, 764, "+ to +, &#8722; to &#8722;", "ts")
    T(92, 786, "needs spare capacity for the AMS", "ts")
    T(92, 806, "units, which draw most while drying", "ts")

    comment("USB data cable, USB-A to USB-C")
    P("M414 426 H380 V594 H392", "w-usb")
    R(414, 419, 18, 14, "plug-usb", 2)
    R(392, 587, 22, 14, "plug-usb", 2)
    T(368, 480, "USB data cable", "tb", "end")
    T(368, 500, "(USB-A to USB-C)", "ts", "end")
    T(368, 522, "not charge-only", "ts", "end")

    board(420, 510)

    comment("4-pin buffer cable")
    L(770, 670, 925, 670, "w-4")
    R(759, 660, 16, 20, "plug-4", 2)
    R(921, 660, 16, 20, "plug-4", 2)
    L(898, 682, 908, 658, "slash")
    T(912, 702, "4", "tnum")
    T(846, 650, "4-pin buffer cable", "tb", "middle")
    T(842, 700, "(24 V + RS-485)", "ts", "middle")

    buffer_unit(945, 560)
    T(1025, 478, "AMS filament buffer", "tb", "end")
    T(1025, 498, "original Bambu Lab part", "ts", "end")
    T(1025, 520, "fit the supplied", "tw", "end")
    T(1025, 540, "stronger spring", "tw", "end")

    comment("Filament path: buffer -> PTFE tube coupler v5 -> toolhead")
    P("M1040 550 V62 Q1040 40 1018 40 H892 Q870 40 870 61", "tube")
    P("M1040 546 V62 Q1040 40 1018 40 H892 Q870 40 870 58", "fil")
    PG([(1032, 345), (1040, 330), (1048, 345)], "fil-head")
    R(1029, 234, 22, 34, "dev", 4)           # coupler v5, just before the printer frame
    L(1029, 245, 1051, 245, "rib")
    L(1029, 257, 1051, 257, "rib")
    LINK(1018, 256, "PTFE tube coupler v5 (printed)", URL_COUPLER_STL, "end")
    T(1018, 276, "3 mm ID in &#8594; 3 mm or 2 mm ID out", "ts", "end")
    # tube from the coupler to the toolhead (inside the printer frame, right of the toolhead)
    T(1024, 160, "PTFE 3 mm ID", "tw", "end")
    T(1024, 178, "(or 2 mm ID if", "ts", "end")
    T(1024, 196, "the extruder", "ts", "end")
    T(1024, 214, "needs it)", "ts", "end")
    T(1025, 392, "Filament (PTFE tube)", "tb", "end")
    T(1025, 412, "PTFE 3 mm ID / 4 mm OD", "tw", "end")
    T(1025, 432, "from the buffer to the coupler", "ts", "end")

    comment("6-pin AMS cable, buffer to unit 1 only")
    P("M1079 750 H1086 V1041 H923", "w-6")   # down the right edge, so it crosses no tube
    R(1073, 742, 10, 16, "plug-6", 2)
    R(913, 1033, 12, 16, "plug-6", 2)
    L(1076, 966, 1096, 954, "slash")
    T(1070, 972, "6", "tnum", "end")
    T(1074, 994, "6-pin AMS cable", "tb", "end")
    T(1074, 1014, "buffer to unit 1", "ts", "end")

    comment("PTFE splitter (printed, optional: only with more than one AMS)")
    P("M975 822 V800", "tube")               # splitter output into the buffer's PTFE inlet (x 975)
    P("M975 820 V802", "fil")
    R(967, 822, 16, 8, "fitting", 1.5)
    R(940, 830, 70, 34, "dev", 7)
    for xi in (947.5, 962.5, 977.5, 992.5):
        R(xi - 5, 864, 10, 7, "fitting", 1.5)
    T(925, 790, "PTFE splitter", "tb", "end")
    T(925, 810, "(printed, optional: only with", "ts", "end")
    T(925, 830, "more than one AMS)", "ts", "end")
    LINK(925, 850, "2 to 5 way PTFE splitter (MakerWorld)", URL_SPLIT, "end")

    # AMS row: unit 1 under the buffer, the daisy chain runs left.
    s = 0.72
    U = {1: 755, 2: 540, 4: 230}
    TOP = 940
    comment("AMS units in a row: unit 1, unit 2, ..., unit 4, daisy-chained on 6-pin cables")
    ams_art(U[1], TOP, s, "ams1")
    ams_art(U[2], TOP, s, "ams2", ' class="faded"')
    ams_art(U[4], TOP, s, "ams4", ' class="faded"')
    T(465, 1050, "&#8230;", "tdots", "middle")
    ychain = TOP + 140 * s
    P("M%s %s H%s" % (f(U[1] - 6), f(ychain), f(U[2] + 210 * s + 6)), "w-6opt")
    P("M%s %s H%s" % (f(U[2] - 6), f(ychain), "498"), "w-6opt")
    P("M432 %s H%s" % (f(ychain), f(U[4] + 210 * s + 6)), "w-6opt")
    # Tubes from each outlet into the splitter; the levels keep them from crossing.
    ytop = TOP - 12 * s

    def outlet(x):
        return x + 196 * s
    tubes = [(outlet(U[1]), 921, 992.5, False), (outlet(U[2]), 906, 977.5, True),
             (465, 891, 962.5, True), (outlet(U[4]), 876, 947.5, True)]
    for xo, lvl, xi, optional in tubes:
        y_start = 1012 if xo == 465 else ytop    # unit 3's tube starts at the ellipsis
        d = "M%s %s V%s H%s V871" % (f(xo), f(y_start), f(lvl), f(xi))
        G(' class="faded"' if optional else "")   # (the leading space is kept for byte-stable output)
        P(d, "tube-s")
        P(d, "fil-s")
        GE()
    T(380, 864, "Filament, PTFE 3 mm ID / 4 mm OD", "ts")
    T(U[1] + 105 * s, 1100, "Unit 1", "tb", "middle")
    T(U[2] + 105 * s, 1100, "Unit 2, optional", "ts", "middle")
    T(465, 1100, "Unit 3", "ts", "middle")
    T(U[4] + 105 * s, 1100, "Unit 4, optional", "ts", "middle")
    T(568, 1132, "Up to 4 AMS units, daisy-chained on their 6-pin cables", "tb", "middle")
    T(568, 1154, "Each unit: AMS 1, AMS 2 Pro or AMS HT", "ts", "middle")
    T(568, 1174, "on stock Bambu Lab firmware, not opened or flashed", "ts", "middle")
    T(568, 1194, "add units 2 to 4 after the first one works", "ts", "middle")

    splitter_inset(24, 836)
    legend(24, 1212)
    return "\n".join(out)


DESC = ("A Raspberry Pi 5 running Klipper connects from one of its USB-A ports to the Cortex Bridge USB-C port J1 "
        "with a USB-A to USB-C data cable. Two wires run from spare + and &#8722; terminals on the printer&#8217;s own "
        "24 V power supply to the PWR IN screw terminal P1, + to + and &#8722; to &#8722;. The BUFFER connector CN1 "
        "connects to the 4-pin connector of the original Bambu Lab AMS filament buffer with the 4-pin buffer cable "
        "(24 V and RS-485). The buffer&#8217;s 6-pin connector goes to the first AMS unit only; up to four AMS units "
        "are daisy-chained on their 6-pin cables. Up to the coupler, filament runs in PTFE tube, 3 mm inside and 4 mm outside diameter: "
        "from each AMS outlet into an optional printed PTFE splitter (only with more than one AMS), into the buffer, "
        "into the 3 mm side of a printed PTFE tube coupler v5; from its other side, 3 mm or 2 mm ID tube runs to the "
        "toolhead extruder, where a filament cutter is required. J4 PRGRM and J7 stay empty. The Klipper computer keeps its existing USB or CAN link to the printer.")


# ---------------------------------------------------------------------------------------------
# Output documents
# ---------------------------------------------------------------------------------------------
def standalone_svg(body):
    """Self-contained SVG: its own background and fixed colours, system fonts, no external refs.

    Light by default; a prefers-color-scheme block switches it to the dark palette, so the
    image follows the reader's light/dark setting wherever it is shown as an <img> (the wiki,
    GitHub). Each palette has its own background, so either reads on any page colour."""
    css = ["text{font-family:%s;font-size:15px;fill:%s}" % (SANS_SVG, LIGHT["d-ink"]),
           "%s{font-family:%s}" % (",".join(MONO_CLASSES), MONO_SVG)]
    css += ["%s{%s}" % (sel, resolve(decl)) for sel, decl in RULES]
    dark = ["text{fill:%s}" % DARK["d-ink"]]
    dark += ["%s{%s}" % (sel, resolve(colour_only(decl), DARK)) for sel, decl in RULES if colour_only(decl)]
    css += ["@media (prefers-color-scheme: dark){"] + dark + ["}"]
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" role="img" aria-labelledby="wd-title wd-desc">\n'
            '<title id="wd-title">Cortex Bridge Rev A4 wiring diagram</title>\n'
            '<desc id="wd-desc">%s</desc>\n<style>\n%s\n</style>\n%s\n</svg>\n') % (W, H, W, H, DESC, "\n".join(css), body)


def inline_svg(body):
    """The same drawing for the page: no <style>, coloured by the page CSS (.wd rules)."""
    return ('<svg class="wd" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" role="img" aria-labelledby="wd-title wd-desc">\n'
            '<title id="wd-title">Cortex Bridge Rev A4 wiring diagram</title>\n'
            '<desc id="wd-desc">%s</desc>\n%s\n</svg>') % (W, H, DESC, body)


def token_block(d, indent="  "):
    return "\n".join("%s--%s:%s;" % (indent, k, v) for k, v in d.items())


def wd_css():
    lines = [".wd text{font-family:var(--font-body);font-size:15px;fill:var(--d-ink)}",
             "%s{font-family:var(--font-mono)}" % ",".join(".wd " + c for c in MONO_CLASSES),
             ".wd a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}",
             ".wd a:hover .tlink{text-decoration-thickness:2px}"]
    lines += [".wd %s{%s}" % (sel, decl) for sel, decl in RULES]
    return "\n".join(lines)


# Placeholders in page.tpl.html and what fills them.
def placeholders(body):
    return [
        ("/*TOKENS-LIGHT*/", token_block(LIGHT)),
        ("/*TOKENS-DARK*/", token_block(DARK, "    ")),
        ("/*TOKENS-DARK2*/", token_block(DARK)),
        ("/*TOKENS-PRINT*/", token_block(PRINT, "    ")),
        ("/*WD-RULES*/", wd_css()),
        ("<!--INLINE-SVG-->", inline_svg(body)),
    ]


# Link placeholders; each may appear any number of times.
LINKS = [
    ("{{GH_BLOB}}", GH_BLOB),
    ("{{GH_TREE}}", GH_TREE),
    ("{{BAMBU_BUFFER}}", URL_BAMBU_BUFFER),
]


def build_page(template, body):
    html = template
    for key, value in placeholders(body):
        if html.count(key) != 1:
            sys.exit("page.tpl.html: expected exactly one %s placeholder" % key)
        html = html.replace(key, value)
    for key, value in LINKS:
        html = html.replace(key, value)
    if "{{" in html:
        sys.exit("page.tpl.html: unknown placeholder near %r" % html[html.index("{{"):][:40])
    return html


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    print("wrote", path.relative_to(REPO) if REPO in path.parents else path)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--docs", action="store_true",
                    help="also write the standalone SVG to docs/images/cortex-bridge-wiring.svg")
    args = ap.parse_args()

    body = build_body()
    svg = standalone_svg(body)
    xml.dom.minidom.parseString(svg.encode("utf-8"))          # must be well-formed
    html = build_page(TEMPLATE.read_text(encoding="utf-8"), body)
    xml.dom.minidom.parseString(inline_svg(body).encode("utf-8"))

    write(OUT_SVG, svg)
    write(OUT_HTML, html)
    if args.docs:
        write(DOCS_SVG, svg)


if __name__ == "__main__":
    main()
