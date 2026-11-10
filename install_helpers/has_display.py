#!/usr/bin/env python3
"""Whether a printer's Klipper config has a [display] section.

    python3 install_helpers/has_display.py <config dir> [printer.cfg]

install.sh includes oams_menu.cfg, the OpenAMS entries of the printer's LCD
menu, only on such a printer. Klipper reads [menu] sections only through its
primary display: klippy/extras/display/display.py creates the menu for the
section named exactly [display], never for a [display <name>]. With no
[display], nothing reads the [menu] sections, and Klipper's startup check of
unused sections refuses the config ("Section 'menu __main __openams' is not a
valid config section").

The config is read the way Klipper reads it (klippy/configfile.py): everything
after a hash is dropped, an [include <path>] is resolved next to the file that
names it, and a pattern includes every match in sorted order.

Exits 0 with a [display] section, 1 without, and 2 when printer.cfg or a file
it includes cannot be read.
"""

import glob
import os
import re
import sys

# configparser.RawConfigParser.SECTCRE, which Klipper matches includes with.
SECTION = re.compile(r"\[(?P<header>.+)\]")


class Unreadable(Exception):
    pass


def section_names(path, visited):
    """Every section header of `path` and of the files it includes, in the
    order Klipper reads them."""
    path = os.path.abspath(path)
    if path in visited:
        raise Unreadable("recursive include of %s" % path)
    visited.add(path)
    try:
        with open(path, "r", errors="replace") as handle:
            lines = handle.read().split("\n")
    except OSError as error:
        raise Unreadable("cannot read %s: %s" % (path, error))
    names = []
    for line in lines:
        match = SECTION.match(line.split("#", 1)[0])
        if match is None:
            continue
        header = match.group("header")
        if header.startswith("include "):
            pattern = os.path.join(os.path.dirname(path), header[8:].strip())
            found = sorted(glob.glob(pattern))
            if not found and not glob.has_magic(pattern):
                raise Unreadable("include file %s does not exist" % pattern)
            for name in found:
                names.extend(section_names(name, visited))
        else:
            names.append(header.strip())
    visited.discard(path)
    return names


def main(argv):
    if len(argv) not in (2, 3):
        print(__doc__.strip().split("\n\n")[1].strip(), file=sys.stderr)
        return 2
    config_dir = os.path.expanduser(argv[1])
    printer_cfg = argv[2] if len(argv) == 3 else "printer.cfg"
    try:
        names = section_names(os.path.join(config_dir, printer_cfg), set())
    except Unreadable as error:
        print("has_display: %s" % error, file=sys.stderr)
        return 2
    return 0 if "display" in names else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
