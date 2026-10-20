#!/usr/bin/env python3
"""Starting values for [gcode_macro _oams_macro_variables] from the toolhead
database (toolheads/cut_defaults.json).

Standard library only. Each entry is a toolhead + extruder + cutter + hotend
measured in its published CAD: the gear nip, the blade and the melt zone along
the filament path, and the OpenAMS settings that follow from them.

    python3 tools/toolhead_defaults.py list
    python3 tools/toolhead_defaults.py show --toolhead Stealthburner --extruder "Clockwork 2" \\
        --cutter FilamATrix --hotend "Dragon HF"
    python3 tools/toolhead_defaults.py show sb-cw2-filamatrix-dragonhf
    python3 tools/toolhead_defaults.py community Stealthburner

``show`` prints the distances and a ready-to-paste block of settings. Leave out
any of --toolhead/--extruder/--cutter/--hotend to list what is available for
it. Matching is case-insensitive and accepts part of a name. ``community``
prints what other MMU systems ship for a toolhead, as references only: their
parameters measure different things. Exit status is 0 on success, 1 when
nothing matches and 2 for a usage or I/O problem.
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "toolheads", "cut_defaults.json")
PARTS = ("toolhead", "extruder", "cutter", "hotend")


def load(path=DB_PATH):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def name(combo):
    return " + ".join(combo[p] for p in PARTS)


def select(combos, wanted):
    """Combos matching every given part: exact names first, then substrings."""
    wanted = {k: v.lower() for k, v in wanted.items() if v}
    exact = [c for c in combos if all(c[k].lower() == v for k, v in wanted.items())]
    if exact:
        return exact
    return [c for c in combos if all(v in c[k].lower() for k, v in wanted.items())]


def print_combo(combo, out):
    d, h, o = combo["distances"], combo["heights"], combo["openams"]
    out.write("%s  (%s)\n" % (name(combo), combo["id"]))
    out.write("  status: %s\n" % combo["status"])
    out.write("  gear nip -> blade      %6.2f mm\n" % d["nip_to_blade"])
    out.write("  blade -> nozzle tip    %6.2f mm\n" % d["blade_to_tip"])
    out.write("  gear nip -> nozzle tip %6.2f mm\n" % d["nip_to_tip"])
    if d.get("meltzone_top") is not None:
        out.write("  melt zone top          %6.2f mm above the tip\n" % d["meltzone_top"])
    for p in combo.get("narrow_passages", []):
        out.write("  narrow passage: %.2f mm wide, %.1f-%.1f mm above the tip\n"
                  % (p["width"], p["z_bottom"], p["z_top"]))
    for line in combo.get("checks", []):
        out.write("  note: %s\n" % line)
    if not o:
        out.write("\n  no starting values: the CAD gives no melt zone for this hotend (it is one solid)\n"
                  "  and no drawing gives its heater position. The distances above still hold.\n")
    else:
        out.write("\n  # Starting values: tune them on the printer.\n")
        out.write("  [gcode_macro _oams_macro_variables]\n")
        for key in ("retract_length", "extrusion_unload_length",
                    "extrusion_reload_length", "hotend_meltzone_compensation"):
            out.write("  variable_%s: %g\n" % (key, o[key]))
    for src in combo.get("cad", []):
        out.write("\n  CAD: %s%s\n" % (src["file"], (" (%s)" % src["url"]) if src.get("url") else ""))


def cmd_list(db, out):
    for combo in db["combos"]:
        out.write("%-40s %s\n" % (combo["id"], name(combo)))
    return 0


def cmd_show(db, args, out):
    combos = db["combos"]
    if args.id:
        found = [c for c in combos if c["id"] == args.id]
    else:
        found = select(combos, {p: getattr(args, p) for p in PARTS})
    if len(found) == 1:
        print_combo(found[0], out)
        return 0
    out.write("%d entries match. Narrow it down with:\n" % len(found) if found
              else "No entry matches.\nAvailable:\n")
    for p in PARTS:
        out.write("  --%-9s %s\n" % (p, " | ".join(sorted({c[p] for c in (found or combos)}))))
    return 1 if not found else 0


def cmd_community(db, args, out):
    want = (args.toolhead or "").lower()
    rows = [c for c in db["community"]["combos"]
            if not want or want in (c.get("toolhead") or c["name"]).lower() or want in c["name"].lower()]
    if not rows:
        out.write("No community values for %r.\n" % args.toolhead)
        return 1
    out.write(db["community"]["about"] + "\n\n")
    for c in rows:
        out.write("%s: %s\n" % (c["system"], c["name"]))
        for key, v in sorted(c["values"].items()):
            out.write("  %-34s %8s  %s\n" % (key, v["mm"], v["meaning"][:90]))
    return 0


def main(argv=None, out=sys.stdout):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=DB_PATH, help="the database (default: toolheads/cut_defaults.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="every measured toolhead combination")
    show = sub.add_parser("show", help="distances and starting values for one combination")
    show.add_argument("id", nargs="?")
    for p in PARTS:
        show.add_argument("--" + p)
    com = sub.add_parser("community", help="values other MMU systems ship, as references")
    com.add_argument("toolhead", nargs="?")
    args = ap.parse_args(argv)
    try:
        db = load(args.db)
    except (OSError, ValueError) as exc:
        sys.stderr.write("cannot read %s: %s\n" % (args.db, exc))
        return 2
    if args.cmd == "list":
        return cmd_list(db, out)
    if args.cmd == "show":
        return cmd_show(db, args, out)
    return cmd_community(db, args, out)


if __name__ == "__main__":
    sys.exit(main())
