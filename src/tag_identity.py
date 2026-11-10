# OpenAMS spool identity of a tag: the spool key (pure, no Klipper imports).
#
# Copyright (C) 2026 JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# A Bambu spool carries two tags. Their UIDs differ; block 9 (the tray UUID)
# is the spool's serial and is the same on both. The spool key is derived from
# that, so the host, the display and the Spoolman component all compute the
# same value with no registry (C2 section 4.6):
#
#   bambu:<tray UUID, lower case>   a Bambu tag whose tray UUID is valid
#   uid:<UID, upper case>           any other tag with a UID
#   None                            no UID at all (a unit-decoded summary)
#
# The Moonraker component keeps a copy of these two functions with the same
# behaviour; both are tested against the same vectors.

_HEX = frozenset("0123456789abcdefABCDEF")


def valid_tray(tray_uid):
    """True for a tray UUID that identifies a spool: exactly 32 hex digits
    after strip(), not all '0' and not all 'f'. A blank block 9 (all zero, or
    an erased all-F block) would make every such tag the same spool."""
    if not isinstance(tray_uid, str):
        return False
    text = tray_uid.strip()
    if len(text) != 32 or not all(ch in _HEX for ch in text):
        return False
    lowered = text.lower()
    return lowered != "0" * 32 and lowered != "f" * 32


def spool_key(tag):
    """The spool key of a C2 tag object, or None."""
    if not isinstance(tag, dict):
        return None
    if tag.get("format") == "bambu":
        bambu = tag.get("bambu")
        tray = bambu.get("tray_uid") if isinstance(bambu, dict) else None
        if valid_tray(tray):
            return "bambu:" + tray.strip().lower()
    uid = tag.get("uid")
    if isinstance(uid, str) and uid.strip():
        return "uid:" + uid.strip().upper()
    return None
