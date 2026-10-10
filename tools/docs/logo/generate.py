#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""OpenAMS logo generator: four filaments spiralling inside a circle.

Each arm is one filament, one AMS bay: a stroke along a spiral, with a round
dot (the filament tip) at its leading end. The arms have n-fold rotational
symmetry, line and dot share one colour, and each arm has its own colour.
Everything is drawn from geometry: each arm's centre line is a polar curve
r(phi) fitted with cubic Beziers (Hermite end tangents, at most 15 degrees per
segment), so the files are small, exact and crisp at any size.

Standard library only. ImageMagick or rsvg-convert is needed only for --png.

Usage
-----
    python3 generate.py            SVGs + preview.html into this folder
    python3 generate.py --png      also the PNGs, apple-touch-icon and favicon.ico
    python3 generate.py --report   palette contrast and clearances, no files
    python3 generate.py --svg-only DIR   <KEY>-light.svg and <KEY>-dark.svg per
                                         direction into DIR (for check renders)

Parameters (keys of DEFAULTS; each direction in DIRECTIONS overrides some)
-------------------------------------------------------------------------
All lengths are in viewBox units: the mark's viewBox is 100 x 100, centred on
0,0, so at 16 px one unit is 0.16 px and 1 px is 6.25 units.

arms        Number of arms (filaments, AMS bays). 4.
spiral      Shape of the spiral part of each centre line, from r_inner to r_outer:
              'archimedean'  r grows linearly with angle (even spacing);
              'logarithmic'  r grows geometrically (constant pitch angle);
              'sine'         r = r_inner + (r_outer - r_inner) * sin(pi/2 * u),
                             an Archimedean-like spiral eased so that it meets the
                             circle r = r_outer tangentially (no kink into outer_arc).
turns       Sweep of the spiral part of each arm, in revolutions (0.5 = 180 deg).
outer_arc   Extra sweep, in revolutions, that each arm runs along the circle
            r = r_outer beyond the spiral, so the arms themselves draw the circle.
r_inner     Centre-line radius at the inner end of the arm.
r_outer     Centre-line radius at the outer end of the arm.
head        'outer' or 'inner': which end of the arm carries the dot (the tip).
            The line's other end is its tail.
hand        'ccw' or 'cw': the on-screen direction the dots travel (tail to head).
phase       Angle of the first arm's dot, degrees, 0 = 3 o'clock, counter-
            clockwise positive. The other arms follow counter-clockwise, every
            360/arms degrees, in palette order.
stroke      Line width.
dot_r       Dot radius. dot_r = stroke / 2 gives a dot as wide as the line.
gap         Clear space between the line's round cap and its dot. The line is
            trimmed at the head so the gap is exact (chord distance).
ring        True: draw a neutral ring around the arms (the 'subtle ring' option).
            False: the circle is implied by the arms' outer ends.
ring_r      Ring centre-line radius.
ring_w      Ring width.
palette     Name of the colour set in PALETTES used for the file's colours.
name, note  Labels for preview.html (directions only).

Colours (PALETTES)
------------------
'light'  Default set, for white, #f2f5f4 and also safe on #0d1315 (the favicon
         and PNGs use it, since a browser tab may be light or dark).
'dark'   Lighter set for dark-only backgrounds (#0d1315, #141c1f).
'docs'   The untuned docs spool colours, kept for comparison.
Arm order is bay order: red, amber, blue, green.

Outputs (out/)
--------------
openams-logo-A.svg, -B.svg, -C.svg   the three explored directions
openams-logo.svg                     recommended mark, light set
openams-logo-dark.svg                recommended mark, dark set
openams-logo-mono.svg                one colour, currentColor
openams-lockup.svg, -dark.svg        mark + 'OpenAMS' (Barlow Condensed 700, as paths)
openams-logo-<N>.png                 N = 16 32 48 180 192 512 1024 (with --png)
apple-touch-icon.png                 180 px on white (iOS fills transparency black)
favicon.ico                          16/32/48 (with --png)
preview.html (beside this script)    review page; every SVG inlined
"""

import argparse
import base64
import math
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")

ZIP_NAME = "openams-logo-files.zip"
ZIP_FILES = ["openams-logo.svg", "openams-logo-dark.svg", "openams-logo-mono.svg",
             "openams-lockup.svg", "openams-lockup-dark.svg", "apple-touch-icon.png", "favicon.ico",
             "openams-discord-dark.png", "openams-discord-light.png", "openams-lockup-1200.png"]


def write_zip():
    """All files of the chosen mark in one zip, with fixed timestamps so a rebuild
    of unchanged files gives the same zip."""
    path = os.path.join(OUT, ZIP_NAME)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ZIP_FILES + ["openams-logo-%d.png" % n for n in PNG_SIZES]:
            src = os.path.join(OUT, name)
            if not os.path.exists(src):
                continue
            info = zipfile.ZipInfo("openams-logo/" + name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(src, "rb") as fh:
                z.writestr(info, fh.read())


# --------------------------------------------------------------------------- #
# Colours
# --------------------------------------------------------------------------- #

DOCS_SPOOLS = ["#d9534f", "#f0b429", "#3d8bd4", "#4fae6c"]  # wiring diagram sp1..sp4

PALETTES = {
    # amber deepened and green darkened a step so neither washes out on white;
    # red and blue are the docs values. All four also hold on #0d1315.
    "light": {"arms": ["#d9534f", "#e8a01a", "#3d8bd4", "#3f9f5f"],
              "ring": "#b4bebc", "ink": "#162027"},
    # one step lighter red, blue and green, docs amber: even weight on dark.
    "dark": {"arms": ["#e8625d", "#f0b429", "#5aa2e6", "#52b673"],
             "ring": "#3b4b52", "ink": "#e2e9ea"},
    "docs": {"arms": DOCS_SPOOLS, "ring": "#c6cfcd", "ink": "#162027"},
}

BACKGROUNDS = {"white": "#ffffff", "docs light": "#f2f5f4", "docs dark": "#0d1315"}

# --------------------------------------------------------------------------- #
# Parameters
# --------------------------------------------------------------------------- #

DEFAULTS = {  # = direction A, the recommended mark
    "arms": 4,
    "spiral": "archimedean",
    "turns": 0.41,
    "outer_arc": 0.0,
    "r_inner": 11.0,
    "r_outer": 41.5,
    "head": "outer",
    "hand": "cw",
    "phase": 120.0,
    "stroke": 9.0,
    "dot_r": 5.75,
    "gap": 5.5,
    "ring": False,
    "ring_r": 45.0,
    "ring_w": 5.0,
    "palette": "light",
}

# How the values were chosen: at 16 px one unit is 0.16 px, so a 5.5 gap is
# ~0.9 px and a 9 stroke ~1.4 px. With four arms, every extra 90 degrees of
# sweep stacks one more arm on each ray from the centre, so the sweep is the
# largest that keeps (a) at least ~0.75 px between different arms at 16 px
# and (b) each dot nearer its own line than any other arm (`--report`
# 'grouping' > 1.2), or the dots read as a separate square of four.
DIRECTIONS = {
    "A": {
        "name": "Sweep",
        "note": "Open mark. Medium arms sweep out clockwise; each dot leads its "
                "arm onto the implied circle.",
        "spiral": "archimedean", "turns": 0.41, "r_inner": 11.0, "r_outer": 41.5,
        "head": "outer", "hand": "cw", "phase": 120.0,
        "stroke": 9.0, "dot_r": 5.75, "gap": 5.5, "ring": False,
    },
    "B": {
        "name": "Orbit",
        "note": "The same sweep, thinner, inside a subtle neutral ring.",
        "spiral": "archimedean", "turns": 0.34, "r_inner": 10.5, "r_outer": 33.75,
        "head": "outer", "hand": "cw", "phase": 120.0,
        "stroke": 8.5, "dot_r": 5.25, "gap": 5.0,
        "ring": True, "ring_r": 45.5, "ring_w": 4.5,
    },
    "C": {
        "name": "Feed",
        "note": "Thick, short arms come in from the circle; the dots lead into "
                "the hub, like four filaments fed to one path.",
        "spiral": "archimedean", "turns": 0.29, "r_inner": 18.0, "r_outer": 42.5,
        "head": "inner", "hand": "cw", "phase": 120.0,
        "stroke": 10.0, "dot_r": 6.5, "gap": 5.5, "ring": False,
    },
}

# A with the owner's spool idea: the dot is the spool, larger and joined to
# its filament (a negative gap runs the line into the dot), the filament thinner.
DIRECTIONS["A2"] = dict(DIRECTIONS["A"], name="Spool",
    note="A, with each dot as a larger spool joined to its thinner filament.",
    r_outer=38.5, stroke=6.5, dot_r=8.5, gap=-4.0)

# A2 with the spool beside the filament: the filament leaves the spool at its
# rim (top or bottom), the way it comes off a real spool, not from its centre.
DIRECTIONS["A3"] = dict(DIRECTIONS["A2"], name="Rim",
    note="A2, with the filament leaving each spool at its rim instead of its centre.",
    attach="tangent", attach_side="outer", r_outer=33.0, turns=0.41)

RECOMMENDED = "A3"


def params(key):
    p = dict(DEFAULTS)
    p.update(DIRECTIONS[key])
    return p


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #

def radius_fn(p):
    """Return (R, S, kink): R(phi) for phi in [0, S] radians measured from the
    inner end outward, the total sweep S, and the phi where the spiral ends."""
    r0, r1 = float(p["r_inner"]), float(p["r_outer"])
    sp = 2 * math.pi * p["turns"]
    arc = 2 * math.pi * p["outer_arc"]
    kind = p["spiral"]

    def R(phi):
        if phi >= sp:
            return r1
        u = max(phi, 0.0) / sp
        if kind == "archimedean":
            return r0 + (r1 - r0) * u
        if kind == "logarithmic":
            return r0 * (r1 / r0) ** u
        if kind == "sine":
            return r0 + (r1 - r0) * math.sin(math.pi / 2 * u)
        raise ValueError("unknown spiral %r" % kind)

    return R, sp + arc, sp


def arm_curve(p, i):
    """Return (P, S, knots): P(psi) -> (x, y) in y-up coordinates, with psi
    running from 0 at the tail to S at the head; knots are psi values where
    the curve is only C1 or C0 (Bezier segments must break there)."""
    R, S, sp = radius_fn(p)
    hand = 1.0 if p["hand"] == "ccw" else -1.0
    head_outer = p["head"] == "outer"
    th_head = math.radians(p["phase"] + 360.0 * i / p["arms"])

    def P(psi):
        phi = psi if head_outer else S - psi
        th = th_head - hand * (S - psi)
        r = R(phi)
        return (r * math.cos(th), r * math.sin(th))

    knots = []
    if p["outer_arc"] > 0:
        knots.append(sp if head_outer else S - sp)
    return P, S, knots


def _deriv(P, a, side, h=1e-5):
    """One-sided derivative of P at a (side=+1: from the right)."""
    x0, y0 = P(a)
    if side > 0:
        x1, y1 = P(a + h)
        x2, y2 = P(a + 2 * h)
        return ((-3 * x0 + 4 * x1 - x2) / (2 * h), (-3 * y0 + 4 * y1 - y2) / (2 * h))
    x1, y1 = P(a - h)
    x2, y2 = P(a - 2 * h)
    return ((3 * x0 - 4 * x1 + x2) / (2 * h), (3 * y0 - 4 * y1 + y2) / (2 * h))


def bezier_segments(P, a, b, knots, max_step=math.radians(15)):
    """Cubic Bezier fit of P over [a, b]: Hermite end tangents per segment."""
    cuts = [a] + [k for k in sorted(knots) if a < k < b] + [b]
    segs = []
    for lo, hi in zip(cuts, cuts[1:]):
        n = max(1, math.ceil((hi - lo) / max_step - 1e-9))
        for j in range(n):
            t0 = lo + (hi - lo) * j / n
            t1 = lo + (hi - lo) * (j + 1) / n
            h = (t1 - t0) / 3.0
            p0, p3 = P(t0), P(t1)
            d0, d1 = _deriv(P, t0, +1), _deriv(P, t1, -1)
            p1 = (p0[0] + d0[0] * h, p0[1] + d0[1] * h)
            p2 = (p3[0] - d1[0] * h, p3[1] - d1[1] * h)
            segs.append((p0, p1, p2, p3))
    return segs


def trim_at_head(P, S, chord):
    """psi on the line where the straight distance to the head is `chord`."""
    hx, hy = P(S)
    step = 0.01
    lo = S
    while lo > 0:
        lo = max(0.0, lo - step)
        x, y = P(lo)
        if math.hypot(x - hx, y - hy) >= chord:
            break
    hi = min(S, lo + step)
    for _ in range(60):
        mid = (lo + hi) / 2
        x, y = P(mid)
        if math.hypot(x - hx, y - hy) >= chord:
            lo = mid
        else:
            hi = mid
    return lo


def arm_geometry(p, i):
    """Return dict(segs, dot, line_end, P, psi_end) for arm i (y-up coords).

    attach "center" (default): the dot sits on the curve's end, past a gap.
    attach "tangent": the dot is a spool beside the line's end, so the line
    leaves it at the rim, its outer edge flush with the spool; attach_side
    "outer" or "inner" picks the side away from or towards the mark's centre."""
    P, S, knots = arm_curve(p, i)
    if p.get("attach") == "tangent":
        tx, ty = _deriv(P, S, -1)
        n = math.hypot(tx, ty)
        nx, ny = -ty / n, tx / n
        off = p["dot_r"] - p["stroke"] / 2.0
        ex, ey = P(S)
        a_ = (ex + nx * off, ey + ny * off)
        b_ = (ex - nx * off, ey - ny * off)
        outer = a_ if math.hypot(*a_) > math.hypot(*b_) else b_
        inner = b_ if outer is a_ else a_
        return {
            "segs": bezier_segments(P, 0.0, S, knots),
            "dot": outer if p.get("attach_side", "outer") == "outer" else inner,
            "P": P,
            "psi_end": S,
        }
    chord = p["dot_r"] + p["gap"] + p["stroke"] / 2.0
    psi_end = trim_at_head(P, S, chord)
    return {
        "segs": bezier_segments(P, 0.0, psi_end, knots),
        "dot": P(S),
        "P": P,
        "psi_end": psi_end,
    }


# --------------------------------------------------------------------------- #
# SVG
# --------------------------------------------------------------------------- #

def fmt(v):
    s = "%.2f" % v
    s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def pt(x, y):
    # y-up geometry to SVG's y-down
    return "%s %s" % (fmt(x), fmt(-y))


def path_d(segs):
    d = ["M" + pt(*segs[0][0])]
    for _, c1, c2, e in segs:
        d.append("C" + " ".join(pt(*q) for q in (c1, c2, e)))
    return "".join(d)


def mark_elements(p, colour, ring_colour, indent="  "):
    """SVG elements of the mark. colour(i) and ring_colour are paint values:
    hex, 'currentColor', or 'var(--x)' (emitted as style for the preview)."""

    def paint(prop, value):
        if value.startswith("var("):
            return 'style="%s:%s"' % (prop, value)
        return '%s="%s"' % (prop, value)

    out = []
    if p["ring"]:
        out.append('%s<circle r="%s" fill="none" %s stroke-width="%s"/>'
                   % (indent, fmt(p["ring_r"]), paint("stroke", ring_colour),
                      fmt(p["ring_w"])))
    for i in range(p["arms"]):
        g = arm_geometry(p, i)
        c = colour(i)
        out.append('%s<path d="%s" fill="none" %s stroke-width="%s" '
                   'stroke-linecap="round" stroke-linejoin="round"/>'
                   % (indent, path_d(g["segs"]), paint("stroke", c), fmt(p["stroke"])))
        out.append('%s<circle cx="%s" cy="%s" r="%s" %s/>'
                   % (indent, fmt(g["dot"][0]), fmt(-g["dot"][1]), fmt(p["dot_r"]),
                      paint("fill", c)))
    return "\n".join(out)


def svg_file(body, viewbox, title):
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="%s">\n'
            '  <title>%s</title>\n%s\n</svg>\n' % (viewbox, title, body))


def mark_svg(p, palette=None, mono=False, title="OpenAMS"):
    pal = PALETTES[palette or p["palette"]]
    if mono:
        body = mark_elements(p, lambda i: "currentColor", "currentColor")
    else:
        body = mark_elements(p, lambda i: pal["arms"][i % len(pal["arms"])], pal["ring"])
    return svg_file(body, "-50 -50 100 100", title)


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #

def _lin(c):
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hexcol):
    h = hexcol.lstrip("#")
    r, g, b = (_lin(int(h[k:k + 2], 16)) for k in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def clearances(p, samples=600):
    """Smallest clear distance between the ink of different arms, between an
    arm and the ring, the outer extent, and the gap between the tails."""
    shapes = []  # (arm, feature, x, y, half-width)
    for i in range(p["arms"]):
        g = arm_geometry(p, i)
        P = g["P"]
        for k in range(samples + 1):
            x, y = P(g["psi_end"] * k / samples)
            feat = "tail" if k == 0 else "line"
            shapes.append((i, feat, x, y, p["stroke"] / 2.0))
        shapes.append((i, "dot", g["dot"][0], g["dot"][1], p["dot_r"]))
    best, which, dot_other = float("inf"), "", float("inf")
    for a in range(len(shapes)):
        ia, fa, xa, ya, ra = shapes[a]
        for b in range(a + 1, len(shapes)):
            ib, fb, xb, yb, rb = shapes[b]
            if ia == ib:
                continue
            d = math.hypot(xa - xb, ya - yb) - ra - rb
            if d < best:
                best, which = d, "-".join(sorted((fa, fb)))
            if (fa == "dot" or fb == "dot") and d < dot_other:
                dot_other = d
    extent = max(math.hypot(x, y) + r for _, _, x, y, r in shapes)
    ring_clear = None
    if p["ring"]:
        ring_clear = (p["ring_r"] - p["ring_w"] / 2.0) - extent
        extent = p["ring_r"] + p["ring_w"] / 2.0
    # dot_other / gap > 1: each dot sits closer to its own line than to any
    # other arm, so it groups with its own line (proximity).
    return {"arm_to_arm": best, "limit": which, "dot_other": dot_other,
            "grouping": dot_other / p["gap"], "extent": extent,
            "ring_clear": ring_clear}


def report():
    print("Contrast against the three test backgrounds (WCAG ratio):")
    for name, pal in PALETTES.items():
        print("  palette %-5s" % name)
        for c in pal["arms"]:
            print("    %s  " % c + "  ".join("%s %.2f" % (bg, contrast(c, h))
                                            for bg, h in BACKGROUNDS.items()))
    px16 = 16 / 100.0
    print("\nGeometry (viewBox units; px at 16 px in brackets):")
    for key in DIRECTIONS:
        p = params(key)
        c = clearances(p)
        line = ("  %s %-6s stroke %s [%.2f]  dot %s [%.2f]  gap %s [%.2f]  "
                "arm-arm %.1f [%.2f] (%s)  grouping %.2f  extent %.1f"
                % (key, DIRECTIONS[key]["name"], fmt(p["stroke"]), p["stroke"] * px16,
                   fmt(2 * p["dot_r"]), 2 * p["dot_r"] * px16, fmt(p["gap"]),
                   p["gap"] * px16, c["arm_to_arm"], c["arm_to_arm"] * px16,
                   c["limit"], c["grouping"], c["extent"]))
        if c["ring_clear"] is not None:
            line += "  ring clear %.1f [%.2f]" % (c["ring_clear"], c["ring_clear"] * px16)
        print(line)


# --------------------------------------------------------------------------- #
# Wordmark: "OpenAMS" in Barlow Condensed Bold (700), as outlines
# --------------------------------------------------------------------------- #
# Glyph outlines from BarlowCondensed-Bold.ttf v1.4 (Copyright 2017 The Barlow
# Project Authors, https://github.com/jpt/barlow; SIL Open Font License 1.1),
# read once with fontTools: font units, y up, 1000 units per em, cap height 700.
# The font has no kerning pairs within "OpenAMS". No font is needed to rebuild.

UPM, CAP = 1000, 700
GLYPHS = {  # char: (advance, xMin, xMax, path)
    "O": (473, 34, 439, "M34 190V510Q34 600 89.0 654.0Q144 708 236 708Q328 708 383.5 654.0Q439 600 439 510V190Q439 100 383.5 46.0Q328 -8 236 -8Q144 -8 89.0 46.0Q34 100 34 190ZM298 184V516Q298 548 281.0 567.5Q264 587 236 587Q208 587 191.5 567.5Q175 548 175 516V184Q175 152 191.5 132.5Q208 113 236 113Q264 113 281.0 132.5Q298 152 298 184Z"),
    "p": (448, 41, 418, "M418 355V160Q418 79 382.0 35.5Q346 -8 278 -8Q256 -8 232.0 0.5Q208 9 188 31Q186 34 184.0 33.0Q182 32 182 29V-174Q182 -179 178.5 -182.5Q175 -186 170 -186H53Q48 -186 44.5 -182.5Q41 -179 41 -174V502Q41 507 44.5 510.5Q48 514 53 514H170Q175 514 178.5 510.5Q182 507 182 502V486Q182 484 184 484Q188 484 192 489Q226 522 278 522Q347 522 382.5 477.5Q418 433 418 355ZM229 402Q208 402 195.0 386.0Q182 370 182 344V171Q182 145 195.0 129.0Q208 113 229 113Q251 113 264.0 129.0Q277 145 277 171V344Q277 370 264.0 386.0Q251 402 229 402Z"),
    "e": (436, 31, 409, "M397 213H177Q172 213 172 208V171Q172 146 185.5 129.5Q199 113 220 113Q239 113 251.0 125.5Q263 138 267 157Q270 167 280 167L395 161Q400 161 403.5 157.5Q407 154 406 148Q399 71 352.5 31.5Q306 -8 220 -8Q131 -8 81.0 36.5Q31 81 31 160V354Q31 430 81.5 476.0Q132 522 220 522Q308 522 358.5 476.0Q409 430 409 354V225Q409 220 405.5 216.5Q402 213 397 213ZM172 344V305Q172 300 177 300H263Q268 300 268 305V344Q268 369 254.5 385.0Q241 401 220 401Q199 401 185.5 385.0Q172 369 172 344Z"),
    "n": (447, 38, 413, "M413 351V12Q413 7 409.5 3.5Q406 0 401 0H284Q279 0 275.5 3.5Q272 7 272 12V340Q272 369 260.5 385.0Q249 401 227 401Q204 401 191.5 384.5Q179 368 179 338V12Q179 7 175.5 3.5Q172 0 167 0H50Q45 0 41.5 3.5Q38 7 38 12V502Q38 507 41.5 510.5Q45 514 50 514H167Q172 514 175.5 510.5Q179 507 179 502V483Q179 480 181.5 480.0Q184 480 188 484Q220 522 277 522Q345 522 379.0 477.0Q413 432 413 351Z"),
    "A": (482, 11, 469, "M325 11 309 107Q309 112 303 112H176Q170 112 170 107L154 11Q153 0 141 0H24Q11 0 14 13L161 689Q163 700 174 700H309Q320 700 322 689L468 13L469 9Q469 0 458 0H338Q326 0 325 11ZM194 221H284Q289 221 288 226L241 499Q240 502 238.0 502.0Q236 502 235 499L190 226Q190 221 194 221Z"),
    "M": (548, 41, 507, "M379 700H495Q500 700 503.5 696.5Q507 693 507 688V12Q507 7 503.5 3.5Q500 0 495 0H378Q373 0 369.5 3.5Q366 7 366 12V461Q366 465 364.0 465.0Q362 465 360 462L282 324Q277 316 274 316Q271 316 266 324L188 462Q186 465 184.0 465.0Q182 465 182 461V12Q182 7 178.5 3.5Q175 0 170 0H53Q48 0 44.5 3.5Q41 7 41 12V688Q41 693 44.5 696.5Q48 700 53 700H168Q178 700 183 692L272 533Q275 529 278 533L364 692Q368 700 379 700Z"),
    "S": (444, 27, 420, "M30 175V204Q30 209 33.5 212.5Q37 216 42 216H157Q162 216 165.5 212.5Q169 209 169 204V182Q169 151 185.0 132.0Q201 113 225 113Q249 113 265.0 131.0Q281 149 281 178Q281 208 261.0 229.5Q241 251 183 293Q133 329 102.0 357.0Q71 385 49.0 426.0Q27 467 27 521Q27 606 80.0 657.0Q133 708 220 708Q308 708 361.5 655.0Q415 602 415 516V492Q415 487 411.5 483.5Q408 480 403 480H288Q283 480 279.5 483.5Q276 487 276 492V519Q276 550 260.5 568.5Q245 587 220 587Q196 587 181.0 569.5Q166 552 166 521Q166 491 185.0 468.0Q204 445 260 404Q322 358 353.5 329.5Q385 301 402.5 266.5Q420 232 420 184Q420 96 366.0 44.0Q312 -8 224 -8Q166 -8 122.0 15.0Q78 38 54.0 79.5Q30 121 30 175Z"),
}
WORD = "OpenAMS"
TRACKING = 10  # font units between letters: the docs' letter-spacing .01em

# Lockup proportions, relative to the mark's visible diameter.
LOCKUP_CAP = 0.40   # cap height; Barlow's ~141-unit stems then land near the mark's stroke
LOCKUP_GAP = 0.25   # space between the mark and the "O"

_ARITY = {"M": 2, "L": 2, "H": 1, "V": 1, "Q": 4, "C": 6, "Z": 0}


def transform_path(d, sx, tx, ty):
    """Absolute-command path (M L H V Q C Z) through x' = tx + sx*x,
    y' = ty - sx*y (scale and flip font units into SVG space)."""
    import re
    toks = re.findall(r"[MLHVQCZ]|-?\d*\.?\d+", d)
    out, cmd, k = [], None, 0
    while k < len(toks):
        t = toks[k]
        if t in _ARITY:
            cmd = t
            k += 1
            if cmd == "Z":
                out.append("Z")
                continue
        elif cmd == "M":
            cmd = "L"  # implicit lineto after moveto
        n = _ARITY[cmd]
        vals = [float(v) for v in toks[k:k + n]]
        k += n
        if cmd == "H":
            out.append("H" + fmt(tx + sx * vals[0]))
        elif cmd == "V":
            out.append("V" + fmt(ty - sx * vals[0]))
        else:
            pts = [fmt(tx + sx * vals[j]) + " " + fmt(ty - sx * vals[j + 1])
                   for j in range(0, n, 2)]
            out.append(cmd + " ".join(pts))
    return "".join(out)


def wordmark(x_left, baseline, cap):
    """Path data for WORD with its first glyph's ink starting at x_left.
    Returns (d, right edge of the last glyph's ink)."""
    s = cap / float(CAP)
    pen = -GLYPHS[WORD[0]][1]
    parts = []
    for ch in WORD:
        adv, _, xmax, d = GLYPHS[ch]
        parts.append(transform_path(d, s, x_left + s * pen, baseline))
        right = x_left + s * (pen + xmax)
        pen += adv + TRACKING
    return "".join(parts), right


def lockup_svg(p, palette):
    pal = PALETTES[palette]
    ext = clearances(p)["extent"]
    dia = 2 * ext
    cap = LOCKUP_CAP * dia
    x_text = ext + LOCKUP_GAP * dia
    d, right = wordmark(x_text, cap / 2.0, cap)
    body = ('  <g>\n%s\n  </g>\n  <path d="%s" fill="%s"/>'
            % (mark_elements(p, lambda i: pal["arms"][i % 4], pal["ring"], "    "),
               d, pal["ink"]))
    vb = "%s %s %s %s" % (fmt(-ext), fmt(-ext), fmt(right + ext), fmt(dia))
    return svg_file(body, vb, "OpenAMS")


# --------------------------------------------------------------------------- #
# PNG and ICO (external renderer; optional)
# --------------------------------------------------------------------------- #

PNG_SIZES = (16, 32, 48, 180, 192, 512, 1024)


def renderer():
    if shutil.which("rsvg-convert"):
        return "rsvg"
    if shutil.which("magick") or shutil.which("convert"):
        return "magick"
    return None


def magick():
    return shutil.which("magick") or shutil.which("convert")


def render_png(svg_text, png_path, size):
    """Render SVG text (from stdin, no temp files) to a size x size PNG."""
    data = svg_text.encode("utf-8")
    if renderer() == "rsvg":
        cmd = ["rsvg-convert", "-w", str(size), "-h", str(size), "-o", png_path]
    else:
        cmd = [magick(), "-background", "none", "-density", str(max(72, size * 72 // 100 * 2)),
               "svg:-", "-resize", "%dx%d" % (size, size), png_path]
    subprocess.run(cmd, input=data, check=True)


def render_png_width(svg_text, png_path, width):
    """Render SVG text to a PNG `width` px wide, keeping the aspect ratio."""
    data = svg_text.encode("utf-8")
    if renderer() == "rsvg":
        cmd = ["rsvg-convert", "-w", str(width), "-o", png_path]
    else:
        cmd = [magick(), "-background", "none", "-density", "600", "svg:-",
               "-resize", "%dx" % width, png_path]
    subprocess.run(cmd, input=data, check=True)


def touch_icon_svg(p):
    """Opaque white square with the mark at 80 %: iOS fills transparency black."""
    pal = PALETTES["light"]
    body = ('  <rect x="-50" y="-50" width="100" height="100" fill="#ffffff"/>\n'
            '  <g transform="scale(0.8)">\n%s\n  </g>'
            % mark_elements(p, lambda i: pal["arms"][i % 4], pal["ring"], "    "))
    return svg_file(body, "-50 -50 100 100", "OpenAMS")


DISCORD = (("dark", "#141c1f"), ("light", "#ffffff"))


def discord_icon_svg(p, palette, bg):
    """Discord server icon: square, solid background, the mark at 78 % so it
    sits well inside the circle Discord crops icons to (512 px recommended)."""
    pal = PALETTES[palette]
    body = ('  <rect x="-50" y="-50" width="100" height="100" fill="%s"/>\n'
            '  <g transform="scale(0.78)">\n%s\n  </g>'
            % (bg, mark_elements(p, lambda i: pal["arms"][i % 4], pal["ring"], "    ")))
    return svg_file(body, "-50 -50 100 100", "OpenAMS")


def write_pngs(p):
    if renderer() is None:
        print("no rsvg-convert or ImageMagick: PNGs skipped", file=sys.stderr)
        return
    light = mark_svg(p, "light")
    for n in PNG_SIZES:
        render_png(light, os.path.join(OUT, "openams-logo-%d.png" % n), n)
    render_png(touch_icon_svg(p), os.path.join(OUT, "apple-touch-icon.png"), 180)
    # The lockup as a raster, for logo pickers that take no SVG (the store header).
    render_png_width(lockup_svg(p, "light"), os.path.join(OUT, "openams-lockup-1200.png"), 1200)
    for palette, bg in DISCORD:
        render_png(discord_icon_svg(p, palette, bg), os.path.join(OUT, "openams-discord-%s.png" % palette), 512)
    if magick():
        ico = [os.path.join(OUT, "openams-logo-%d.png" % n) for n in (16, 32, 48)]
        subprocess.run([magick()] + ico + [os.path.join(OUT, "favicon.ico")], check=True)
    else:
        print("no ImageMagick: favicon.ico skipped", file=sys.stderr)


# --------------------------------------------------------------------------- #
# preview.html
# --------------------------------------------------------------------------- #

PAGE_LIGHT = {"bg": "#f2f5f4", "surface": "#ffffff", "ink": "#162027",
              "muted": "#55616a", "rule": "#d2dad8", "accent": "#1d6a47"}
PAGE_DARK = {"bg": "#0d1315", "surface": "#141c1f", "ink": "#e2e9ea",
             "muted": "#9ba9ae", "rule": "#2a373b", "accent": "#62cf9a"}


def css_vars(page, palette):
    pal = PALETTES[palette]
    v = ["--%s: %s;" % kv for kv in page.items()]
    v += ["--a%d: %s;" % (i + 1, c) for i, c in enumerate(pal["arms"])]
    v.append("--ring: %s;" % pal["ring"])
    return " ".join(v)


def symbol(key, p, mono=False):
    if mono:
        body = mark_elements(p, lambda i: "currentColor", "currentColor", "    ")
    else:
        body = mark_elements(p, lambda i: "var(--a%d)" % (i % 4 + 1), "var(--ring)", "    ")
    return ('  <symbol id="%s" viewBox="-50 -50 100 100">\n%s\n  </symbol>'
            % (key, body))


def use(sym, size, label=None, cls="m"):
    aria = 'role="img" aria-label="%s"' % label if label else 'aria-hidden="true"'
    return ('<svg class="%s" width="%d" height="%d" %s><use href="#%s"/></svg>'
            % (cls, size, size, aria, sym))


def strip_title(svg_text, cls):
    s = svg_text.replace("<title>OpenAMS</title>\n", "")
    return s.replace("<svg ", '<svg class="%s" role="img" aria-label="OpenAMS" ' % cls, 1)


def data_uri(path):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")


def params_rows(key):
    p = params(key)
    c = clearances(p)
    rows = [
        ("spiral", "%s, %s turn" % (p["spiral"], fmt(p["turns"]))),
        ("radii", "%s to %s" % (fmt(p["r_inner"]), fmt(p["r_outer"]))),
        ("stroke", "%s (%.1f px at 16)" % (fmt(p["stroke"]), p["stroke"] * .16)),
        ("dot", "r %s, %.2gx stroke" % (fmt(p["dot_r"]), 2 * p["dot_r"] / p["stroke"])),
        ("gap", "%s (%.1f px at 16)" % (fmt(p["gap"]), p["gap"] * .16)),
        ("dot at", "%s end, %s" % (p["head"], "clockwise" if p["hand"] == "cw" else "counter-clockwise")),
        ("circle", "ring %s / %s" % (fmt(p["ring_r"]), fmt(p["ring_w"])) if p["ring"] else "implied"),
        ("arm gap", "%.2f px at 16" % (c["arm_to_arm"] * .16)),
    ]
    return "".join("<dt>%s</dt><dd>%s</dd>" % r for r in rows)


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenAMS Logo Review</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:ital,wght@0,400;0,700;1,400&family=Barlow+Condensed:wght@600;700&family=JetBrains+Mono:wght@500&display=swap">
<style>
:root {
  @@LIGHT@@
  --font-display: "Barlow Condensed", "Arial Narrow", "Roboto Condensed", sans-serif;
  --font-body: "Atkinson Hyperlegible", "Segoe UI", Helvetica, Arial, sans-serif;
  --font-mono: "JetBrains Mono", "DejaVu Sans Mono", Menlo, Consolas, monospace;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) { @@DARK@@ color-scheme: dark; }
}
:root[data-theme="dark"] { @@DARK@@ color-scheme: dark; }

/* fixed test backgrounds: these keep their colours in either page theme */
.on-white { background: #ffffff; color: #162027; @@PAL_LIGHT@@ }
.on-docs  { background: #f2f5f4; color: #162027; @@PAL_LIGHT@@ }
.on-dark  { background: #0d1315; color: #e2e9ea; @@PAL_DARK@@ }

* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 16px/1.55 var(--font-body);
}
main { max-width: 1080px; margin: 0 auto; padding: 40px 16px 72px; }
@media (min-width: 720px) { main { padding: 56px 24px 88px; } }
h1, h2, h3 {
  font-family: var(--font-display); font-weight: 700; letter-spacing: .01em;
  line-height: 1.1; margin: 0;
}
h1 { font-size: 2.5rem; }
h2 { font-size: 1.65rem; margin: 56px 0 4px; }
h3 { font-size: 1.25rem; }
p { margin: 0; }
.lede, .sub { color: var(--muted); max-width: 64ch; }
.lede { margin-top: 10px; font-size: 1.05rem; }
.sub { margin-bottom: 18px; }
code, .mono { font-family: var(--font-mono); font-size: .82em; }
svg.m { display: block; flex: none; }

/* directions */
.grid3 {
  display: grid; gap: 16px;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 280px), 1fr));
}
.card {
  background: var(--surface); border: 1px solid var(--rule); border-radius: 12px;
  padding: 20px; min-width: 0;
}
.card.rec { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }
.card .hero { width: 100%; max-width: 240px; height: auto; aspect-ratio: 1; margin: 6px auto 18px; }
.card h3 { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.card p { color: var(--muted); margin-top: 6px; font-size: .95rem; }
.tag {
  font-family: var(--font-mono); font-size: .68rem; font-weight: 500;
  text-transform: uppercase; letter-spacing: .06em; color: var(--accent);
  border: 1px solid currentColor; border-radius: 4px; padding: 1px 6px;
}
dl.params {
  display: grid; grid-template-columns: max-content 1fr; gap: 2px 14px;
  margin: 14px 0 0; padding-top: 12px; border-top: 1px solid var(--rule);
  font-family: var(--font-mono); font-size: .76rem;
}
dl.params dt { color: var(--muted); }
dl.params dd { margin: 0; min-width: 0; }

/* small sizes */
.sizes { display: grid; gap: 14px; }
.size-row { display: grid; gap: 8px; grid-template-columns: 7.5rem repeat(3, minmax(0, 1fr)); align-items: stretch; }
.size-row .label { align-self: center; font-family: var(--font-display); font-weight: 700; font-size: 1.15rem; }
.swatch {
  display: flex; align-items: center; justify-content: center; gap: 18px;
  padding: 14px 10px; border-radius: 10px; border: 1px solid var(--rule); min-width: 0;
}
.size-head { font-family: var(--font-mono); font-size: .72rem; color: var(--muted); text-align: center; }
@media (max-width: 640px) {
  .size-row { grid-template-columns: 1fr; }
  .size-row.head { display: none; }
  .swatch { justify-content: flex-start; padding-left: 16px; }
  .swatch::after { content: attr(data-bg); margin-left: auto; font-family: var(--font-mono); font-size: .7rem; opacity: .7; }
}

/* top bar mock */
.bars { display: grid; gap: 12px; }
.bar {
  display: flex; align-items: center; gap: 12px; height: 64px; padding: 0 16px;
  border-radius: 10px; min-width: 0; overflow: hidden;
}
.bar.light { background: #ffffff; color: #162027; box-shadow: 0 0 0 1px #d2dad8; @@PAL_LIGHT@@ }
.bar.dark { background: #141c1f; color: #e2e9ea; box-shadow: 0 0 0 1px #2a373b; @@PAL_DARK@@ }
.bar .title {
  font-family: var(--font-display); font-weight: 700; font-size: 1.5rem; letter-spacing: .01em;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0;
}
.bar .search {
  margin-left: auto; flex: 0 1 260px; min-width: 0; height: 42px; display: flex; align-items: center;
  padding: 0 14px; border-radius: 8px; font-size: .95rem; white-space: nowrap; overflow: hidden;
}
.bar.light .search { background: #f2f5f4; border: 1px solid #d2dad8; color: #55616a; }
.bar.dark .search { background: #0d1315; border: 1px solid #2a373b; color: #9ba9ae; }
@media (max-width: 600px) { .bar .search { display: none; } .bar .title { font-size: 1.3rem; } }

/* browser tab mock */
.tabs-grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(min(100%, 340px), 1fr)); margin-top: 12px; }
.browser { border-radius: 10px; overflow: hidden; font: 12.5px/1 system-ui, "Segoe UI", Roboto, sans-serif; }
.browser .strip { display: flex; align-items: flex-end; gap: 2px; padding: 8px 8px 0; }
.browser .tab {
  display: flex; align-items: center; gap: 8px; width: 220px; max-width: 70%; height: 34px;
  padding: 0 10px 0 12px; border-radius: 8px 8px 0 0; min-width: 0;
}
.browser .tab.off { width: 140px; max-width: 28%; background: transparent !important; opacity: .75; }
.browser .tab span.t { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.browser .tab .x { opacity: .6; }
.browser .tab img, .browser .tab .blank { width: 16px; height: 16px; flex: none; }
.browser .tab .blank { border-radius: 50%; }
.browser .url { padding: 9px 12px 11px; }
.browser .url span { display: block; padding: 7px 12px; border-radius: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.browser.light .strip { background: #dfe3e8; color: #1f1f1f; }
.browser.light .tab, .browser.light .url { background: #ffffff; color: #1f1f1f; }
.browser.light .url span { background: #eef1f4; color: #474747; }
.browser.light .blank { background: #b9c0c7; }
.browser.dark .strip { background: #1f2023; color: #e3e3e3; }
.browser.dark .tab, .browser.dark .url { background: #35363a; color: #e3e3e3; }
.browser.dark .url span { background: #202124; color: #c4c7c5; }
.browser.dark .blank { background: #5f6368; }

/* favicon pixels */
.pixels { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 12px; margin-top: 12px; }
.pixels figure { margin: 0; padding: 12px; border-radius: 10px; border: 1px solid var(--rule); }
.pixels img { display: block; image-rendering: pixelated; image-rendering: crisp-edges; }
.pixels figcaption, .file figcaption { font-family: var(--font-mono); font-size: .72rem; color: var(--muted); margin-top: 8px; }
.on-dark figcaption { color: #9ba9ae; }

/* files */
.files { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr)); }
.file { margin: 0; padding: 18px; border-radius: 10px; border: 1px solid var(--rule); min-width: 0; }
.file.surface { background: var(--surface); color: var(--ink); }
.file .art { display: block; width: 112px; height: 112px; margin: 4px auto 6px; }
.file.wide { grid-column: 1 / -1; }
.file .lock { display: block; width: 100%; max-width: 420px; height: auto; margin: 8px auto; }
.file strong { display: block; font-family: var(--font-mono); font-size: .78rem; font-weight: 500; margin-top: 10px; overflow-wrap: anywhere; }
.file span { display: block; font-size: .88rem; opacity: .75; margin-top: 2px; }

/* colours */
.table-wrap { overflow-x: auto; border: 1px solid var(--rule); border-radius: 10px; background: var(--surface); }
table { border-collapse: collapse; width: 100%; font-size: .9rem; }
th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--rule); white-space: nowrap; }
th { font-family: var(--font-mono); font-size: .7rem; font-weight: 500; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }
tr:last-child td { border-bottom: 0; }
td.num { font-family: var(--font-mono); font-size: .8rem; }
.chip { display: inline-block; width: 14px; height: 14px; border-radius: 50%; vertical-align: -2px; margin-right: 8px; }
.note { color: var(--muted); font-size: .9rem; margin-top: 10px; max-width: 72ch; }
footer { margin-top: 56px; padding-top: 16px; border-top: 1px solid var(--rule); color: var(--muted); font-size: .88rem; }
</style>
</head>
<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false">
@@SYMBOLS@@
</svg>
<main>
<header>
  <h1>OpenAMS logo</h1>
  <p class="lede">Four filaments, one for each AMS bay, spiral inside a circle, each led by its tip.
  Line and dot share a colour; the four colours are the docs' spool colours.
  Every direction is drawn from one generator. <strong>A3, "Rim"</strong> is the chosen mark: each spool lets its filament go at the rim.</p>
</header>

<section>
  <h2>Directions</h2>
  <p class="sub">Shown in the page's light or dark theme. Parameters are in viewBox units: the mark is 100 wide, so 1 unit is 0.16 px at 16 px.</p>
  <div class="grid3">
@@CARDS@@
  </div>
</section>

<section>
  <h2>Small sizes</h2>
  <p class="sub">64, 32 and 16 px on white, the docs' light background and the docs' dark background (dark colour set). Vector rendering here; the true 16 px favicon pixels are further down.</p>
  <div class="sizes">
    <div class="size-row head"><span class="size-head"></span><span class="size-head">#ffffff</span><span class="size-head">#f2f5f4</span><span class="size-head">#0d1315</span></div>
@@SIZES@@
  </div>
</section>

<section>
  <h2>In context</h2>
  <p class="sub">The chosen mark (A3) in a mock of the docs' top bar, light and dark.</p>
  <div class="bars">
    <div class="bar light">@@BAR_LOGO@@<span class="title">OpenAMS Documentation</span><span class="search">Search...</span></div>
    <div class="bar dark">@@BAR_LOGO@@<span class="title">OpenAMS Documentation</span><span class="search">Search...</span></div>
  </div>
  <h3 style="margin-top:28px">Browser tab</h3>
  <p class="sub">The favicon is the PNG/ICO in the light colour set, which also holds on a dark tab strip.</p>
  <div class="tabs-grid">
    <div class="browser light"><div class="strip"><div class="tab">@@FAV@@<span class="t">OpenAMS Documentation</span><span class="x">&times;</span></div><div class="tab off"><span class="blank"></span><span class="t">New Tab</span></div></div><div class="url"><span>openams.si-forge.com</span></div></div>
    <div class="browser dark"><div class="strip"><div class="tab">@@FAV@@<span class="t">OpenAMS Documentation</span><span class="x">&times;</span></div><div class="tab off"><span class="blank"></span><span class="t">New Tab</span></div></div><div class="url"><span>openams.si-forge.com</span></div></div>
  </div>
  @@PIXELS@@
</section>

<section>
  <h2>Files for the chosen mark</h2>
  <p class="sub">In <code>out/</code>. Everything here is regenerated by <code>python3 generate.py --png</code>.</p>
  <div class="files">
@@FILES@@
  </div>
</section>

<section>
  <h2>Colours</h2>
  <p class="sub">Arm order is bay order. WCAG contrast ratios against each background.</p>
  <div class="table-wrap"><table>
    <thead><tr><th>Bay</th><th>Light set</th><th>on #ffffff</th><th>on #f2f5f4</th><th>on #0d1315</th><th>Dark set</th><th>on #0d1315</th><th>Docs value</th></tr></thead>
    <tbody>
@@COLOURS@@
    </tbody>
  </table></div>
  <p class="note">Amber cannot reach 3:1 on white and stay amber; it is deepened as far as it still reads as the docs' amber, and the other three arms carry the shape. The dark set lifts red, blue and green one step so the four weigh evenly on #0d1315.</p>
</section>

<footer>Generated by <code>generate.py</code> (standard-library Python). Edit <code>DIRECTIONS</code> or <code>PALETTES</code> there and rerun; this page and every file in <code>out/</code> are rebuilt.</footer>
</main>
<script>
/* On claude.ai, file links go through the viewer's save prompt (the
   "downloads" capability); opened locally they stay plain links. */
(function () {
  var c = window.claude;
  if (!c || typeof c.use !== "function") return;
  c.use("downloads").then(function (downloads) {
    if (!downloads) return;
    document.addEventListener("click", function (e) {
      var a = e.target.closest ? e.target.closest("a[download]") : null;
      if (!a) return;
      e.preventDefault();
      var href = a.getAttribute("href");
      fetch(href).then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.blob();
      }).then(function (blob) {
        return downloads.save({ filename: href.split("/").pop(), data: blob });
      }).catch(function () {});
    });
  });
})();
</script>
</body>
</html>
"""


def preview_html():
    pl, pd = css_vars(PAGE_LIGHT, "light"), css_vars(PAGE_DARK, "dark")
    pal_l = " ".join("--a%d: %s;" % (i + 1, c) for i, c in enumerate(PALETTES["light"]["arms"])) \
        + " --ring: %s;" % PALETTES["light"]["ring"]
    pal_d = " ".join("--a%d: %s;" % (i + 1, c) for i, c in enumerate(PALETTES["dark"]["arms"])) \
        + " --ring: %s;" % PALETTES["dark"]["ring"]

    rec = params(RECOMMENDED)
    symbols = [symbol("mark-%s" % k, params(k)) for k in DIRECTIONS]
    symbols.append(symbol("mark-mono", rec, mono=True))

    cards = []
    for k, d in DIRECTIONS.items():
        tag = ' <span class="tag">Chosen</span>' if k == RECOMMENDED else ""
        cards.append(
            '    <article class="card%s">\n      %s\n      <h3>%s &middot; %s%s</h3>\n'
            '      <p>%s</p>\n      <dl class="params">%s</dl>\n    </article>'
            % (" rec" if k == RECOMMENDED else "",
               '<svg class="m hero" viewBox="0 0 240 240" role="img" aria-label="Direction %s">'
               '<use href="#mark-%s" width="240" height="240"/></svg>' % (k, k),
               k, d["name"], tag, d["note"], params_rows(k)))

    sizes = []
    for k, d in DIRECTIONS.items():
        cells = []
        for cls, bg in (("on-white", "#ffffff"), ("on-docs", "#f2f5f4"), ("on-dark", "#0d1315")):
            cells.append('<div class="swatch %s" data-bg="%s">%s%s%s</div>'
                         % (cls, bg, use("mark-" + k, 64), use("mark-" + k, 32), use("mark-" + k, 16)))
        sizes.append('    <div class="size-row"><span class="label">%s &middot; %s</span>%s</div>'
                     % (k, d["name"], "".join(cells)))

    bar_logo = use("mark-" + RECOMMENDED, 32, "OpenAMS")
    p16 = data_uri(os.path.join(OUT, "openams-logo-16.png"))
    p32 = data_uri(os.path.join(OUT, "openams-logo-32.png"))
    if p16 and p32:
        fav = '<img src="%s" srcset="%s 2x" width="16" height="16" alt="">' % (p16, p32)
        pixels = ('<h3 style="margin-top:28px">Favicon pixels</h3>\n'
                  '  <p class="sub">The 16 and 32 px PNGs (light set), magnified 4x without smoothing.</p>\n'
                  '  <div class="pixels">'
                  '<figure class="on-white"><img src="%s" width="64" height="64" alt="16 px favicon on white"><figcaption>16 px</figcaption></figure>'
                  '<figure class="on-dark"><img src="%s" width="64" height="64" alt="16 px favicon on dark"><figcaption>16 px</figcaption></figure>'
                  '<figure class="on-white"><img src="%s" width="128" height="128" alt="32 px favicon on white"><figcaption>32 px</figcaption></figure>'
                  '<figure class="on-dark"><img src="%s" width="128" height="128" alt="32 px favicon on dark"><figcaption>32 px</figcaption></figure>'
                  '</div>' % (p16, p16, p32, p32))
    else:
        fav = '<span class="on-white" style="display:contents">%s</span>' % use("mark-" + RECOMMENDED, 16)
        pixels = ""

    files = [
        ('on-white', strip_title(mark_svg(rec, "light"), "art"), "openams-logo.svg",
         "Default. Light backgrounds; also fine on dark."),
        ('on-dark', strip_title(mark_svg(rec, "dark"), "art"), "openams-logo-dark.svg",
         "Dark backgrounds only."),
        ('surface', use("mark-mono", 112, "OpenAMS", "m art"), "openams-logo-mono.svg",
         "One colour; takes the text colour (currentColor)."),
        ('on-white', '<img class="art" alt="" src="%s">' % (data_uri(os.path.join(OUT, "apple-touch-icon.png")) or ""),
         "apple-touch-icon.png", "180 px on white, for iOS home screens.")
        if os.path.exists(os.path.join(OUT, "apple-touch-icon.png")) else None,
        ('on-white wide', strip_title(lockup_svg(rec, "light"), "lock"), "openams-lockup.svg",
         "Mark and wordmark (Barlow Condensed 700, outlined). Light backgrounds."),
        ('on-dark wide', strip_title(lockup_svg(rec, "dark"), "lock"), "openams-lockup-dark.svg",
         "Mark and wordmark for dark backgrounds."),
    ]
    file_html = []
    for f in files:
        if f is None:
            continue
        cls, art, name, what = f
        file_html.append('    <figure class="file %s">%s<strong><a href="out/%s" download>%s</a></strong><span>%s</span></figure>'
                         % (cls, art, name, name, what))
    discord = " &middot; ".join(
        '<a href="out/openams-discord-%s.png" download><img src="%s" width="64" height="64" alt="" '
        'style="display:block;border-radius:50%%;margin:0 0 6px">%s</a>'
        % (pal, data_uri(os.path.join(OUT, "openams-discord-%s.png" % pal)) or "", pal)
        for pal, _ in DISCORD if os.path.exists(os.path.join(OUT, "openams-discord-%s.png" % pal)))
    if discord:
        file_html.append('    <figure class="file surface wide"><strong>Discord server icon (512 px)</strong>'
                         '<span style="display:flex;gap:24px;flex-wrap:wrap;margin-top:8px">%s</span>'
                         '<span>Square, solid background, the mark sized for Discord&rsquo;s circular crop. '
                         'Server Settings &rarr; Overview &rarr; Server icon.</span></figure>' % discord)
    pngs = " &middot; ".join('<a href="out/openams-logo-%d.png" download>%d</a>' % (n, n)
                             for n in (16, 32, 48, 180, 192, 512, 1024))
    file_html.append('    <figure class="file surface wide"><strong>openams-logo-*.png: %s px &middot; '
                     'favicon.ico (16/32/48) &middot; <a href="out/%s" download>all files (zip)</a></strong>'
                     '<span>Light colour set, transparent. 16/32/48 and the .ico for favicons; 180 and 192 for app icons; 512 and 1024 for everything else. The zip holds every file of the chosen mark, the .ico included.</span></figure>' % (pngs, ZIP_NAME))

    colours = []
    for i in range(4):
        cl, cd, c0 = PALETTES["light"]["arms"][i], PALETTES["dark"]["arms"][i], DOCS_SPOOLS[i]
        colours.append(
            '      <tr><td>%d</td><td class="num"><span class="chip" style="background:%s"></span>%s</td>'
            '<td class="num">%.2f</td><td class="num">%.2f</td><td class="num">%.2f</td>'
            '<td class="num"><span class="chip" style="background:%s"></span>%s</td><td class="num">%.2f</td>'
            '<td class="num"><span class="chip" style="background:%s"></span>%s</td></tr>'
            % (i + 1, cl, cl, contrast(cl, "#ffffff"), contrast(cl, "#f2f5f4"), contrast(cl, "#0d1315"),
               cd, cd, contrast(cd, "#0d1315"), c0, c0))

    page = PAGE
    for k, v in (("@@LIGHT@@", pl), ("@@DARK@@", pd), ("@@PAL_LIGHT@@", pal_l),
                 ("@@PAL_DARK@@", pal_d), ("@@SYMBOLS@@", "\n".join(symbols)),
                 ("@@CARDS@@", "\n".join(cards)), ("@@SIZES@@", "\n".join(sizes)),
                 ("@@BAR_LOGO@@", bar_logo), ("@@FAV@@", fav), ("@@PIXELS@@", pixels),
                 ("@@FILES@@", "\n".join(file_html)), ("@@COLOURS@@", "\n".join(colours))):
        page = page.replace(k, v)
    return page


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def write_svgs():
    for key in DIRECTIONS:
        write(os.path.join(OUT, "openams-logo-%s.svg" % key), mark_svg(params(key), "light"))
    rec = params(RECOMMENDED)
    write(os.path.join(OUT, "openams-logo.svg"), mark_svg(rec, "light"))
    write(os.path.join(OUT, "openams-logo-dark.svg"), mark_svg(rec, "dark"))
    write(os.path.join(OUT, "openams-logo-mono.svg"), mark_svg(rec, mono=True))
    write(os.path.join(OUT, "openams-lockup.svg"), lockup_svg(rec, "light"))
    write(os.path.join(OUT, "openams-lockup-dark.svg"), lockup_svg(rec, "dark"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--png", action="store_true", help="also render PNGs and favicon.ico")
    ap.add_argument("--report", action="store_true", help="print checks only")
    ap.add_argument("--svg-only", metavar="DIR",
                    help="write <KEY>-light.svg and <KEY>-dark.svg per direction to DIR")
    a = ap.parse_args(argv)
    if a.report:
        report()
        return
    if a.svg_only:
        for key in DIRECTIONS:
            for pal in ("light", "dark"):
                write(os.path.join(a.svg_only, "%s-%s.svg" % (key, pal)),
                      mark_svg(params(key), pal))
        return
    write_svgs()
    if a.png:
        write_pngs(params(RECOMMENDED))
        write_zip()
    write(os.path.join(HERE, "preview.html"), preview_html())
    print("wrote %s and preview.html" % os.path.relpath(OUT, HERE))


if __name__ == "__main__":
    main()
