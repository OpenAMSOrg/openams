# SPDX-License-Identifier: MIT
"""Whether the bridge relay still runs the code it started with.

The relay keeps the OpenAMS code it started with until its service restarts.
At start it records in its status file which files that code is and their
fingerprint (`snapshot`); `outdated` hashes the same files of the checkout
again. Only the relay's own files count, so an OpenAMS update that leaves
them alone needs no relay restart (which would cost Klipper its AMS ports).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "cortex_bridge"
# Imported by the relay only when it runs an update; listed from the start so
# a change to it counts too.
LAZY = ("cortex_bridge/updater.py",)


def loaded_files(root: Path = ROOT, extra=LAZY) -> list:
    """The package's files this process has loaded, relative to `root`
    (the relay runs as `python -m cortex_bridge.relay`, so its own module is
    `__main__`)."""
    files = set(extra)
    package = (Path(root) / PACKAGE).resolve()
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if not path:
            continue
        path = Path(path).resolve()
        if path.suffix == ".py" and package in path.parents:
            files.add(path.relative_to(Path(root).resolve()).as_posix())
    return sorted(files)


def fingerprint(files, root: Path = ROOT) -> str:
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8") + b"\0")
        try:
            digest.update((Path(root) / name).read_bytes())
        except OSError:
            digest.update(b"\0missing")
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def snapshot(root: Path = ROOT) -> dict:
    """What the relay writes as `code` in its status file."""
    files = loaded_files(root)
    return {"root": str(root), "files": files,
            "fingerprint": fingerprint(files, root)}


def outdated(status):
    """True when a running relay's code differs from its checkout on disk,
    or it is from before this record (it writes no `code`); False when it
    is current; None when no relay runs."""
    if not status or status.get("state") in (None, "stopped"):
        return None
    code = status.get("code")
    if not isinstance(code, dict) or not code.get("files") or \
            not code.get("root"):
        return True
    return fingerprint(code["files"], Path(code["root"])) != \
        code.get("fingerprint")
