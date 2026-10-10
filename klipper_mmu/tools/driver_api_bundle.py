#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Rebuild and verify the reviewed Klipper driver API bundle.

The MMU firmware extends Klipper with a small, reviewed set of driver
changes.  That extension ships as two files under ``upstream/``:

* ``drivers-local.patch`` -- unified diffs against one pinned Klipper
  revision, concatenated in path order.
* ``source-hashes.json`` -- the sha256 of every touched file before the
  patch and after it.

Both are pinned by sha256 in ``tools/install.py`` (``DRIVER_API_ASSETS``),
so editing the extension by hand used to mean building a patch and three
sets of hashes by hand too.  This tool rebuilds the whole bundle from a
Klipper tree you have already edited, and checks that the committed bundle
still applies and still matches its pins.

Commands::

    # Regenerate upstream/drivers-local.patch and upstream/source-hashes.json
    # from a pinned tarball plus your edited Klipper checkout.  --add appends
    # a new path (new file, or an upstream file this bundle now touches).
    python3 tools/driver_api_bundle.py rebuild \
        --pinned build/klipper-<rev>.tar.gz \
        --edited ~/klipper --add src/stepper.c

    # Apply the committed patch to the pinned sources and verify every
    # "after" hash and both pins in tools/install.py.
    python3 tools/driver_api_bundle.py check \
        --pinned build/klipper-<rev>.tar.gz

The pinned tarball's single top directory is the Klipper root.  ``rebuild``
keeps the path order of the existing ``source-hashes.json`` and appends any
``--add`` paths; pass ``--write-install`` to update the path list and both
hashes in ``tools/install.py`` as well.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "upstream"
DEFAULT_INSTALL_PY = REPO_ROOT / "tools" / "install.py"

PATCH_NAME = "drivers-local.patch"
HASHES_NAME = "source-hashes.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_text(path: Path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _load_hashes(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: expected a JSON object of paths")
    return data


def _check_relpath(relpath: str) -> None:
    if not relpath or relpath.startswith("/") or ".." in Path(relpath).parts:
        raise SystemExit(f"path must be relative to the Klipper root: {relpath!r}")


def _archive_root(archive: tarfile.TarFile) -> str:
    roots = {name.split("/", 1)[0] for name in archive.getnames() if name}
    if len(roots) != 1:
        raise SystemExit(f"pinned archive must have one top directory, saw {sorted(roots)}")
    return roots.pop()


def _extract(archive: tarfile.TarFile, names: list[str], dest: Path) -> None:
    members = [archive.getmember(name) for name in names]
    try:
        archive.extractall(path=dest, members=members, filter="data")
    except TypeError:  # Python < 3.12 has no extraction filter.
        archive.extractall(path=dest, members=members)


def _split_lines(path: Path) -> list[str]:
    return _read_text(path).splitlines(keepends=True)


def _render_patch(pinned_root: Path, edited_root: Path, paths: list[str],
                  before: dict[str, str | None]) -> bytes:
    chunks: list[str] = []
    for relpath in paths:
        new_lines = _split_lines(edited_root / relpath)
        if before[relpath] is None:
            old_lines: list[str] = []
            from_label = "/dev/null"
        else:
            old_lines = _split_lines(pinned_root / relpath)
            from_label = "a/" + relpath
        chunks.extend(difflib.unified_diff(old_lines, new_lines,
                                           fromfile=from_label, tofile="b/" + relpath))
    return "".join(chunks).encode("utf-8")


def _parse_targets(text: str) -> list[str]:
    match = re.search(r"DRIVER_API_TARGETS\s*=\s*\((.*?)\)", text, re.S)
    if not match:
        raise ValueError("no DRIVER_API_TARGETS tuple")
    return re.findall(r'"([^"]+)"', match.group(1))


def _parse_assets(text: str) -> dict[str, str]:
    match = re.search(r"DRIVER_API_ASSETS\s*=\s*\{(.*?)\}", text, re.S)
    if not match:
        raise ValueError("no DRIVER_API_ASSETS dict")
    return dict(re.findall(r'"([^"]+)"\s*:\s*"([0-9a-f]{64})"', match.group(1)))


def _write_install(install_py: Path, paths: list[str],
                   hashes_sha: str, patch_sha: str) -> None:
    text = install_py.read_text(encoding="utf-8")
    match = re.search(r"DRIVER_API_TARGETS\s*=\s*\((.*?)\)", text, re.S)
    if not match:
        raise SystemExit(f"{install_py}: no DRIVER_API_TARGETS tuple")
    block = "DRIVER_API_TARGETS = (\n" + "".join(f'    "{p}",\n' for p in paths) + ")"
    text = text[:match.start()] + block + text[match.end():]
    for key, digest in ((HASHES_NAME, hashes_sha), (PATCH_NAME, patch_sha)):
        pattern = re.compile(r'("' + re.escape(key) + r'"\s*:\s*")[0-9a-f]{64}(")')
        text, count = pattern.subn(lambda m: m.group(1) + digest + m.group(2), text, count=1)
        if count != 1:
            raise SystemExit(f"{install_py}: no DRIVER_API_ASSETS entry for {key}")
    install_py.write_text(text, encoding="utf-8")


def cmd_rebuild(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    hashes_path = out_dir / HASHES_NAME
    patch_path = out_dir / PATCH_NAME

    paths = list(_load_hashes(hashes_path)) if hashes_path.is_file() else []
    for extra in args.add:
        _check_relpath(extra)
        if extra in paths:
            raise SystemExit(f"{extra} is already in the bundle")
        paths.append(extra)
    if not paths:
        raise SystemExit("no paths: empty source-hashes.json and no --add")

    edited_root = Path(args.edited)
    before: dict[str, str | None] = {}
    with tarfile.open(args.pinned, "r:*") as archive:
        pinned_root = Path(tempfile.mkdtemp(prefix="driver-api-pinned-"))
        try:
            root = _archive_root(archive)
            for relpath in paths:
                try:
                    _extract(archive, [f"{root}/{relpath}"], pinned_root)
                except KeyError:
                    before[relpath] = None
                else:
                    before[relpath] = sha256_file(pinned_root / root / relpath)
            patch = _render_patch(pinned_root / root, edited_root, paths, before)
        finally:
            shutil.rmtree(pinned_root, ignore_errors=True)

    after: dict[str, str] = {}
    for relpath in paths:
        if not (edited_root / relpath).is_file():
            raise SystemExit(f"edited tree has no {relpath}")
        after[relpath] = sha256_file(edited_root / relpath)

    patch_path.write_bytes(patch)
    data = {relpath: {"before": before[relpath], "after": after[relpath]}
            for relpath in paths}
    hashes_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    patch_sha = sha256_file(patch_path)
    hashes_sha = sha256_file(hashes_path)
    print(f"{PATCH_NAME} sha256 {patch_sha}")
    print(f"{HASHES_NAME} sha256 {hashes_sha}")
    if args.write_install:
        _write_install(Path(args.install_py), paths, hashes_sha, patch_sha)
    return 0


def _run_patch(work: Path, patch_bytes: bytes, *, dry_run: bool) -> subprocess.CompletedProcess:
    argv = ["patch", "-p1", "--batch", "--no-backup-if-mismatch"]
    if dry_run:
        argv.append("--dry-run")
    return subprocess.run(argv, cwd=work, input=patch_bytes, capture_output=True)


def cmd_check(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    hashes_path = out_dir / HASHES_NAME
    patch_path = out_dir / PATCH_NAME
    problems: list[str] = []
    if not hashes_path.is_file():
        problems.append(f"{hashes_path}: missing")
    if not patch_path.is_file():
        problems.append(f"{patch_path}: missing")
    if problems:
        for line in problems:
            print(line)
        return 1

    hashes = _load_hashes(hashes_path)
    patch_bytes = patch_path.read_bytes()
    work_dir = Path(tempfile.mkdtemp(prefix="driver-api-check-"))
    try:
        with tarfile.open(args.pinned, "r:*") as archive:
            root = _archive_root(archive)
            for relpath, entry in hashes.items():
                if entry.get("before") is None:
                    continue
                try:
                    _extract(archive, [f"{root}/{relpath}"], work_dir)
                except KeyError:
                    problems.append(f"{relpath}: not in pinned archive")
        work = work_dir / root
        for dry_run in (True, False):
            proc = _run_patch(work, patch_bytes, dry_run=dry_run)
            if proc.returncode != 0:
                stage = "dry-run" if dry_run else "apply"
                detail = (proc.stderr or proc.stdout).decode(errors="replace").strip()
                problems.append(f"patch {stage} failed ({proc.returncode}): {detail}")
        for relpath, entry in hashes.items():
            target = work / relpath
            if not target.is_file():
                problems.append(f"{relpath}: missing after patch")
            elif sha256_file(target) != entry.get("after"):
                problems.append(f"{relpath}: after hash {sha256_file(target)} != {entry.get('after')}")
        try:
            assets = _parse_assets(_read_text(Path(args.install_py)))
        except (OSError, ValueError) as exc:
            problems.append(f"{args.install_py}: {exc}")
        else:
            for name, path in ((HASHES_NAME, hashes_path), (PATCH_NAME, patch_path)):
                digest = sha256_file(path)
                if assets.get(name) != digest:
                    problems.append(
                        f"{args.install_py} pins {name} {assets.get(name)} but {path} is {digest}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    for line in problems:
        print(line)
    return 1 if problems else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="driver_api_bundle.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    rebuild = sub.add_parser(
        "rebuild", help="regenerate the bundle from an edited Klipper tree",
        description=cmd_rebuild.__doc__)
    rebuild.add_argument("--pinned", required=True,
                         help="pinned Klipper .tar.gz (single top directory)")
    rebuild.add_argument("--edited", required=True,
                         help="Klipper root of the edited checkout")
    rebuild.add_argument("--add", action="append", default=[], metavar="PATH",
                         help="path relative to the Klipper root, appended after the "
                              "existing paths (repeatable)")
    rebuild.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR),
                         help="directory holding drivers-local.patch and source-hashes.json")
    rebuild.add_argument("--install-py", default=str(DEFAULT_INSTALL_PY),
                         help="tools/install.py to rewrite with --write-install")
    rebuild.add_argument("--write-install", action="store_true",
                         help="rewrite DRIVER_API_TARGETS and DRIVER_API_ASSETS in install.py")
    rebuild.set_defaults(func=cmd_rebuild)

    check = sub.add_parser(
        "check", help="apply the committed bundle to the pinned sources and verify it",
        description=cmd_check.__doc__)
    check.add_argument("--pinned", required=True)
    check.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    check.add_argument("--install-py", default=str(DEFAULT_INSTALL_PY))
    check.set_defaults(func=cmd_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
