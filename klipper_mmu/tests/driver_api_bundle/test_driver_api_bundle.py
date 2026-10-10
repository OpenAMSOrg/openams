# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/driver_api_bundle.py.

The pinned Klipper archive is a build input, not a source file: the tests
find it by name under this package's ``build/`` directory (overridable with
``KLIPPER_MMU_BUILD_ROOT``, or a single file via
``DRIVER_API_PINNED_ARCHIVE``), and only accept an archive whose pinned
files reproduce ``upstream/source-hashes.json``.
"""

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import driver_api_bundle as dab  # noqa: E402

UPSTREAM = REPO_ROOT / "upstream"
COMMITTED_PATCH = UPSTREAM / "drivers-local.patch"
COMMITTED_HASHES = UPSTREAM / "source-hashes.json"
COMMITTED_INSTALL = REPO_ROOT / "tools" / "install.py"


def _extract(archive, names, dest):
    members = [archive.getmember(name) for name in names]
    try:
        archive.extractall(path=dest, members=members, filter="data")
    except TypeError:
        archive.extractall(path=dest, members=members)


def _single_root(archive):
    roots = {name.split("/", 1)[0] for name in archive.getnames() if name}
    if len(roots) != 1:
        raise AssertionError(f"archive has {sorted(roots)} top directories")
    return roots.pop()


def _hashes():
    return json.loads(COMMITTED_HASHES.read_text())


def _archive_matches(archive_path, hashes):
    try:
        with tarfile.open(archive_path, "r:*") as archive:
            root = _single_root(archive)
            for relpath, entry in hashes.items():
                if entry["before"] is None:
                    continue
                handle = archive.extractfile(f"{root}/{relpath}")
                if handle is None:
                    return False
                if hashlib.sha256(handle.read()).hexdigest() != entry["before"]:
                    return False
        return True
    except (tarfile.TarError, OSError):
        return False


def find_archive():
    candidates = []
    override = os.environ.get("DRIVER_API_PINNED_ARCHIVE")
    if override:
        candidates.append(Path(override))
    # The build root is this package's own build/, or wherever
    # KLIPPER_MMU_BUILD_ROOT points: the pinned archive is a build input,
    # not a source file, so it travels with the package rather than
    # sitting beside it (klipper-mmu may be nested in another repository).
    build = Path(os.environ.get("KLIPPER_MMU_BUILD_ROOT") or REPO_ROOT / "build")
    if build.is_dir():
        candidates.extend(sorted(build.glob("klipper-*.tar.gz"),
                                 key=lambda p: p.stat().st_mtime, reverse=True))
    if not candidates:
        raise unittest.SkipTest(
            "no pinned Klipper archive under %s; provide one there or set "
            "KLIPPER_MMU_BUILD_ROOT" % build)
    hashes = _hashes()
    for candidate in candidates:
        if candidate.is_file() and _archive_matches(candidate, hashes):
            return candidate
    raise AssertionError("no pinned archive reproduces the committed before hashes")


def build_patched_tree(archive_path, dest, extra=()):
    """Extract the pinned files under ``dest`` and apply the committed patch."""
    hashes = _hashes()
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:*") as archive:
        root = _single_root(archive)
        names = [f"{root}/{p}" for p, e in hashes.items() if e["before"] is not None]
        names += [f"{root}/{p}" for p in extra]
        _extract(archive, names, dest)
    work = dest / root
    proc = subprocess.run(["patch", "-p1", "--batch", "--no-backup-if-mismatch"],
                          cwd=work, input=COMMITTED_PATCH.read_bytes(),
                          capture_output=True)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr.decode(errors="replace"))
    return work


def archive_member_sha256(archive_path, relpath):
    with tarfile.open(archive_path, "r:*") as archive:
        root = _single_root(archive)
        return hashlib.sha256(archive.extractfile(f"{root}/{relpath}").read()).hexdigest()


def printed_pins(output):
    return dict(re.findall(
        r"^(drivers-local\.patch|source-hashes\.json) sha256 ([0-9a-f]{64})$",
        output, re.M))


class DriverApiBundleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="driver-api-test-"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        # Skip per test, not from setUpClass: tools/check.py treats a module
        # that ran zero tests as a failure, and a missing pinned archive is
        # an absent input, not a broken module.
        self.archive = find_archive()

    def test_rebuild_reproduces_committed_bundle(self):
        work = build_patched_tree(self.archive, self.tmp / "round")
        out_dir = self.tmp / "round-out" / "upstream"
        out_dir.mkdir(parents=True)
        shutil.copy(COMMITTED_HASHES, out_dir / "source-hashes.json")
        shutil.copy(COMMITTED_PATCH, out_dir / "drivers-local.patch")
        install_py = self.tmp / "round-install.py"
        shutil.copy(COMMITTED_INSTALL, install_py)

        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            rc = dab.main(["rebuild", "--pinned", str(self.archive),
                           "--edited", str(work), "--out-dir", str(out_dir),
                           "--install-py", str(install_py)])
        self.assertEqual(rc, 0)
        self.assertEqual((out_dir / "drivers-local.patch").read_bytes(),
                         COMMITTED_PATCH.read_bytes())
        self.assertEqual((out_dir / "source-hashes.json").read_bytes(),
                         COMMITTED_HASHES.read_bytes())
        self.assertEqual(printed_pins(capture.getvalue()),
                         dab._parse_assets(COMMITTED_INSTALL.read_text()))

    def test_add_new_and_existing_paths(self):
        new_rel = "src/zz_driver_api_bundle_probe.c"
        existing_rel = "src/sched.c"  # an upstream file outside the bundle
        work = build_patched_tree(self.archive, self.tmp / "add", extra=[existing_rel])
        (work / new_rel).write_text("// probe\n")
        self.assertTrue((work / existing_rel).is_file())
        (work / existing_rel).write_text((work / existing_rel).read_text() + "\n// probe\n")

        out_dir = self.tmp / "add-out" / "upstream"
        out_dir.mkdir(parents=True)
        shutil.copy(COMMITTED_HASHES, out_dir / "source-hashes.json")
        install_py = self.tmp / "add-install.py"
        shutil.copy(COMMITTED_INSTALL, install_py)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = dab.main(["rebuild", "--pinned", str(self.archive),
                           "--edited", str(work), "--add", new_rel,
                           "--add", existing_rel, "--out-dir", str(out_dir),
                           "--install-py", str(install_py), "--write-install"])
        self.assertEqual(rc, 0)

        rewritten = install_py.read_text()
        hashes = json.loads((out_dir / "source-hashes.json").read_text())
        self.assertEqual(dab._parse_targets(rewritten), list(hashes))
        assets = dab._parse_assets(rewritten)
        self.assertEqual(assets["source-hashes.json"],
                         hashlib.sha256((out_dir / "source-hashes.json").read_bytes()).hexdigest())
        self.assertEqual(assets["drivers-local.patch"],
                         hashlib.sha256((out_dir / "drivers-local.patch").read_bytes()).hexdigest())
        self.assertIn("REQUIRED_MCU_FILES", rewritten)
        self.assertEqual(list(hashes), list(_hashes()) + [new_rel, existing_rel])
        self.assertIsNone(hashes[new_rel]["before"])
        self.assertEqual(hashes[new_rel]["after"],
                         hashlib.sha256(b"// probe\n").hexdigest())
        self.assertEqual(hashes[existing_rel]["before"],
                         archive_member_sha256(self.archive, existing_rel))
        self.assertEqual(hashes[existing_rel]["after"],
                         hashlib.sha256((work / existing_rel).read_bytes()).hexdigest())

        patch = (out_dir / "drivers-local.patch").read_text()
        self.assertIn(f"--- /dev/null\n+++ b/{new_rel}", patch)
        self.assertIn(f"+++ b/{existing_rel}", patch)
        self.assertLess(patch.index(f"+++ b/{new_rel}"),
                        patch.index(f"+++ b/{existing_rel}"))

    def test_check_accepts_committed_and_rejects_flipped_byte(self):
        rc = dab.main(["check", "--pinned", str(self.archive),
                       "--out-dir", str(UPSTREAM),
                       "--install-py", str(COMMITTED_INSTALL)])
        self.assertEqual(rc, 0)

        bad_dir = self.tmp / "flip" / "upstream"
        bad_dir.mkdir(parents=True)
        patch_bytes = bytearray(COMMITTED_PATCH.read_bytes())
        match = re.search(rb"\n\+[^+]", bytes(patch_bytes))
        self.assertIsNotNone(match)
        pos = match.start() + 2
        patch_bytes[pos] = ord("X") if patch_bytes[pos] != ord("X") else ord("Y")
        (bad_dir / "drivers-local.patch").write_bytes(bytes(patch_bytes))
        shutil.copy(COMMITTED_HASHES, bad_dir / "source-hashes.json")

        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            rc = dab.main(["check", "--pinned", str(self.archive),
                           "--out-dir", str(bad_dir),
                           "--install-py", str(COMMITTED_INSTALL)])
        self.assertEqual(rc, 1)
        self.assertTrue(capture.getvalue().strip())


if __name__ == "__main__":
    unittest.main()
