#!/usr/bin/env python3
"""Check the license boundary between the MIT plugin and the GPL directories.

Standard library only.  Licensing here is per directory.  Two directories are
GPL: ``klipper_mmu/`` (the klipper-mmu add-on) and ``klipper_patches/`` (patches
to Klipper).  Every source file under either must carry an SPDX identifier of
``GPL-3.0-or-later`` or ``GPL-3.0-only`` (the patch files themselves are not
source files here: they are ``git format-patch`` output and carry no header),
and no file outside them may carry a GPL identifier at all, so GPL code cannot
leak into the MIT half.

The verbatim upstream Klipper fixtures under ``klipper_mmu/references/`` and
``klipper_mmu/tests/fixtures/shared_tmcuart/`` are exempt on the GPL side: they
are copied byte for byte, their hashes are pinned in
``klipper_mmu/upstream/source-hashes.json``, and they are not ours to relicense.

Exits 0 when the boundary holds, 1 otherwise, printing one line per offense.
Paths given on the command line limit the walk; with none, the whole repository
is checked.
"""

import os
import subprocess
import re
import sys

GPL_DIRS = ("klipper_mmu", "klipper_patches")
SOURCE_SUFFIXES = (".c", ".h", ".py", ".sh", ".mjs")
GPL_SOURCE_IDS = (b"GPL-3.0-or-later", b"GPL-3.0-only")
EXEMPT_PREFIXES = (
    "klipper_mmu/references",
    "klipper_mmu/tests/fixtures/shared_tmcuart",
)
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}
# An SPDX tag is a header, so the head of the file is the whole search space.
HEAD_BYTES = 65536
ID_RE = re.compile(rb"SPDX-License-Identifier:\s*([A-Za-z0-9.+-]+)")


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def under_gpl_dir(rel):
    return any(rel == d or rel.startswith(d + os.sep) for d in GPL_DIRS)


def is_exempt(rel):
    return any(rel == prefix or rel.startswith(prefix + os.sep) for prefix in EXEMPT_PREFIXES)


def spdx_ids(path):
    """Return the SPDX identifiers in the head of a text file, or None if binary."""
    with open(path, "rb") as handle:
        head = handle.read(HEAD_BYTES)
    if b"\x00" in head:
        return None
    return ID_RE.findall(head)


def tracked(root):
    """The files git tracks under root, or None where git cannot say (no git,
    or not a work tree). Build output and other untracked files are not part
    of the repository, so they are never checked."""
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-z"],
                             capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return sorted(p for p in out.stdout.decode("utf-8", "replace").split("\0") if p)


def walk(root, paths):
    if not paths:
        files = tracked(root)
        if files is not None:
            for rel in files:
                path = os.path.join(root, rel)
                if os.path.isfile(path):
                    yield rel, path
            return
    for rel in paths or ["."]:
        target = os.path.normpath(os.path.join(root, rel))
        if os.path.isfile(target):
            yield rel, target
            continue
        for dirpath, dirnames, filenames in os.walk(target):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            for name in sorted(filenames):
                path = os.path.join(dirpath, name)
                yield os.path.relpath(path, target), path


def check_file(rel, path):
    """Return the offenses for one file: an empty list means it is in bounds."""
    ids = spdx_ids(path)
    if ids is None:
        return []
    if under_gpl_dir(rel):
        if is_exempt(rel) or not rel.endswith(SOURCE_SUFFIXES):
            return []
        if any(good in ids for good in GPL_SOURCE_IDS):
            return []
        found = ", ".join(sorted(set(i.decode() for i in ids))) or "none"
        return ["%s: GPL-directory source is not GPL-3.0 (SPDX: %s)" % (rel, found)]
    leaked = sorted(set(i.decode() for i in ids if i.startswith(b"GPL")))
    if leaked:
        return ["%s: GPL SPDX identifier in the MIT tree (%s)" % (rel, ", ".join(leaked))]
    return []


def main(argv):
    root = repo_root()
    offenses = []
    for rel, path in walk(root, argv):
        offenses.extend(check_file(rel, path))
    for entry in offenses:
        print(entry)
    if offenses:
        print("%d license violation(s)" % len(offenses))
    else:
        print("the license boundary holds")
    return 1 if offenses else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
