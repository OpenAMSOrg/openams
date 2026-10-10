"""Tests for tools/klipper_patches.py and the install.sh flags that call it.

Every test builds a fake Klipper git checkout in a temporary directory, at a tiny
synthetic base with a two-patch series.  The real series under
klipper_patches/can is never used here; test_real_series_is_well_formed only
reads its files.  install.sh runs with OPENAMS_NO_SERVICE=1, so no systemd unit
is touched and no printer is involved.
"""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tools" / "klipper_patches.py"
INSTALLER = ROOT / "install.sh"
REAL_SERIES = ROOT / "klipper_patches" / "can"

sys.path.insert(0, str(ROOT / "test"))
from test_install_sh import GCO_INSTALLER, GCO_URL  # noqa: E402

DEMO_PY = "line one\nline two\nline three\n"
DEMO_C = "int one;\nint two;\n"
STEP1_PY = "line one\nline two changed\nline three\n"
STEP2_PY = "line one\nline two changed again\nline three\n"
STEP2_C = "int one;\nint two changed;\n"
KCONFIG = "# upstream Kconfig\nmenu \"General\"\nendmenu\n"
MAKEFILE = "# upstream Makefile\nall:\n\techo hello\n"

GIT_ENV = dict(
    os.environ,
    GIT_AUTHOR_NAME="Test Author", GIT_AUTHOR_EMAIL="author@example.invalid",
    GIT_COMMITTER_NAME="Test Author", GIT_COMMITTER_EMAIL="author@example.invalid",
    GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
)


def git(repo, *args, check=True):
    proc = subprocess.run(["git", "-C", str(repo)] + list(args), env=GIT_ENV, text=True,
                          capture_output=True, timeout=60)
    if check and proc.returncode != 0:
        raise AssertionError("git %s failed: %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_klipper(path):
    """A fake Klipper checkout with one base commit, and the two-patch series made
    from it.  Returns (base sha, list of patch file names in apply order)."""
    path = Path(path)
    (path / "klippy/extras").mkdir(parents=True)
    (path / "scripts").mkdir()
    (path / "src").mkdir()
    (path / "Makefile").write_text(MAKEFILE)
    (path / "klippy/demo.py").write_text(DEMO_PY)
    (path / "klippy/extras/.gitkeep").write_text("")
    (path / "scripts/.gitkeep").write_text("")
    (path / "src/demo.c").write_text(DEMO_C)
    (path / "src/Kconfig").write_text(KCONFIG)
    (path / "src/Makefile").write_text(MAKEFILE)
    git(path, "init", "-q", "-b", "master")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "base")
    base = git(path, "rev-parse", "HEAD")
    # Both patches change klippy/demo.py, so the second only applies after the first.
    (path / "klippy/demo.py").write_text(STEP1_PY)
    git(path, "commit", "-q", "-am", "demo: change line two")
    (path / "klippy/demo.py").write_text(STEP2_PY)
    (path / "src/demo.c").write_text(STEP2_C)
    git(path, "commit", "-q", "-am", "demo: change line two again and the C file")
    return base


def write_series(series_dir, klipper, base):
    """Turn the two commits above the base into a checksummed series directory."""
    series_dir = Path(series_dir)
    series_dir.mkdir(parents=True)
    git(klipper, "format-patch", "-N", "--no-signature", "--zero-commit", "--no-stat",
        "-o", str(series_dir), base + "..HEAD")
    names = sorted(p.name for p in series_dir.glob("*.patch"))
    (series_dir / "series").write_text("".join(n + "\n" for n in names))
    (series_dir / "BASE").write_text(base + "\n")
    write_sums(series_dir)
    git(klipper, "reset", "-q", "--hard", base)
    return names


def write_sums(series_dir):
    series_dir = Path(series_dir)
    files = ["BASE", "series"] + sorted(p.name for p in series_dir.glob("*.patch"))
    (series_dir / "SHA256SUMS").write_text(
        "".join("%s  %s\n" % (sha256(series_dir / f), f) for f in files))


class HelperTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="openams-klipper-patches-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.klipper = self.root / "klipper"
        self.series = self.root / "series"
        self.base = build_klipper(self.klipper)
        self.names = write_series(self.series, self.klipper, self.base)
        self.assertEqual(len(self.names), 2)

    def helper(self, action, *extra, klipper=None):
        return subprocess.run(
            [sys.executable, str(HELPER), "--series", str(self.series)] + list(extra)
            + [action, str(klipper or self.klipper)],
            env=GIT_ENV, text=True, capture_output=True, timeout=120)

    def read(self, rel):
        return (self.klipper / rel).read_text()

    def assert_pristine(self):
        self.assertEqual(self.read("klippy/demo.py"), DEMO_PY)
        self.assertEqual(self.read("src/demo.c"), DEMO_C)

    def assert_patched(self):
        self.assertEqual(self.read("klippy/demo.py"), STEP2_PY)
        self.assertEqual(self.read("src/demo.c"), STEP2_C)


class HelperTests(HelperTestCase):
    def test_check_reports_a_clean_apply_and_changes_nothing(self):
        result = self.helper("check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("applies cleanly", result.stdout)
        self.assert_pristine()
        self.assertEqual(git(self.klipper, "status", "--porcelain"), "")

    def test_status_before_apply_is_not_applied(self):
        result = self.helper("status")
        self.assertEqual(result.returncode, 10, result.stdout + result.stderr)
        self.assertIn("not applied", result.stdout)

    def test_apply_edits_the_working_tree_only(self):
        head = git(self.klipper, "rev-parse", "HEAD")
        result = self.helper("apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_patched()
        self.assertEqual(git(self.klipper, "rev-parse", "HEAD"), head, "apply must not commit")
        self.assertEqual(git(self.klipper, "diff", "--cached", "--name-only"), "", "index untouched")
        self.assertEqual(git(self.klipper, "status", "--porcelain").count("\n") + 1, 2)
        self.assertIn("Moonraker", result.stdout)

    def test_status_after_apply_reports_the_series(self):
        self.assertEqual(self.helper("apply").returncode, 0)
        result = self.helper("status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("applied: 2 patches", result.stdout)
        self.assertIn("klippy/demo.py", result.stdout)
        self.assertIn(self.base[:12], result.stdout)

    def test_applying_twice_is_harmless(self):
        self.assertEqual(self.helper("apply").returncode, 0)
        result = self.helper("apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("already applied", result.stdout)
        self.assert_patched()
        result = self.helper("check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("already applied", result.stdout)

    def test_remove_restores_the_exact_original(self):
        self.assertEqual(self.helper("apply").returncode, 0)
        result = self.helper("remove")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_pristine()
        self.assertEqual(git(self.klipper, "status", "--porcelain"), "")
        self.assertEqual(self.helper("status").returncode, 10)
        self.assertFalse((self.klipper / ".git/openams-klipper-patches").exists())

    def test_removing_when_nothing_is_applied_is_harmless(self):
        result = self.helper("remove")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("nothing to remove", result.stdout)
        self.assert_pristine()

    def test_apply_remove_apply_cycle(self):
        for _ in range(2):
            self.assertEqual(self.helper("apply").returncode, 0)
            self.assert_patched()
            self.assertEqual(self.helper("remove").returncode, 0)
            self.assert_pristine()

    def test_refuses_local_changes_in_a_touched_file(self):
        edited = "line one\nmy own edit\nline three\n"
        (self.klipper / "klippy/demo.py").write_text(edited)
        for action in ("check", "apply"):
            result = self.helper(action)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("local changes", result.stderr)
            self.assertIn("klippy/demo.py", result.stderr)
        self.assertEqual(self.read("klippy/demo.py"), edited, "the user's edit must survive")
        self.assertEqual(self.read("src/demo.c"), DEMO_C)
        self.assertFalse((self.klipper / ".git/openams-klipper-patches").exists())

    def test_local_changes_in_other_files_do_not_block(self):
        (self.klipper / "README-local").write_text("notes\n")
        (self.klipper / "src/Kconfig").write_text(KCONFIG + "# my change\n")
        result = self.helper("apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_patched()
        self.assertEqual(self.helper("remove").returncode, 0)
        self.assertEqual(self.read("src/Kconfig"), KCONFIG + "# my change\n")

    def test_exact_base_refuses_a_newer_base_and_says_what_to_do(self):
        (self.klipper / "src/Kconfig").write_text(KCONFIG + "# newer upstream\n")
        git(self.klipper, "commit", "-q", "-am", "newer upstream")
        result = self.helper("apply", "--exact-base")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("newer than the tested base", result.stderr)
        self.assertIn("--exact-base", result.stderr)
        self.assertIn("checkout " + self.base, result.stderr)
        self.assert_pristine()

    def test_a_newer_base_is_allowed_only_when_touched_files_are_unchanged(self):
        (self.klipper / "src/Kconfig").write_text(KCONFIG + "# newer upstream\n")
        git(self.klipper, "commit", "-q", "-am", "newer upstream")
        result = self.helper("apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("applies to them unchanged", result.stdout)
        self.assert_patched()
        self.assertEqual(self.helper("remove").returncode, 0)
        (self.klipper / "src/demo.c").write_text("int one;\nint two upstream;\n")
        git(self.klipper, "commit", "-q", "-am", "upstream touches a patched file")
        result = self.helper("apply")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("src/demo.c", result.stderr)
        self.assertEqual(self.read("klippy/demo.py"), DEMO_PY)
        self.assertEqual(self.read("src/demo.c"), "int one;\nint two upstream;\n")

    def test_refuses_an_older_revision(self):
        (self.klipper / "src/Kconfig").write_text(KCONFIG + "# newer upstream\n")
        git(self.klipper, "commit", "-q", "-am", "newer upstream")
        git(self.klipper, "checkout", "-q", "HEAD~1")
        (self.series / "BASE").write_text(git(self.klipper, "rev-parse", "master") + "\n")
        write_sums(self.series)
        result = self.helper("apply")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("older than or unrelated", result.stderr)
        self.assert_pristine()

    def test_refuses_a_checkout_that_lacks_the_base_commit(self):
        other = self.root / "other-klipper"
        other.mkdir()
        (other / "klippy").mkdir()
        (other / "klippy/demo.py").write_text(DEMO_PY)
        git(other, "init", "-q", "-b", "master")
        git(other, "add", ".")
        git(other, "commit", "-q", "-m", "unrelated history")
        result = self.helper("apply", klipper=other)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("does not contain the tested base commit", result.stderr)
        self.assertEqual((other / "klippy/demo.py").read_text(), DEMO_PY)

    def test_refuses_a_directory_that_is_not_a_git_checkout(self):
        plain = self.root / "plain"
        plain.mkdir()
        result = self.helper("apply", klipper=plain)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("not a Git checkout", result.stderr)
        # status and remove are answers there, not errors: nothing can be applied.
        self.assertEqual(self.helper("status", klipper=plain).returncode, 10)
        self.assertEqual(self.helper("remove", klipper=plain).returncode, 0)

    def test_refuses_a_patch_whose_checksum_does_not_match(self):
        patch = self.series / self.names[1]
        patch.write_text(patch.read_text().replace("changed again", "changed twice"))
        for action in ("check", "apply"):
            result = self.helper(action)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("checksum mismatch", result.stderr)
            self.assertIn(self.names[1], result.stderr)
        self.assert_pristine()
        self.assertFalse((self.klipper / ".git/openams-klipper-patches").exists())

    def test_refuses_a_series_file_that_does_not_match(self):
        (self.series / "series").write_text("".join(n + "\n" for n in reversed(self.names)))
        result = self.helper("apply")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("checksum mismatch for series", result.stderr)
        self.assert_pristine()

    def test_refuses_a_patch_missing_from_the_checksums(self):
        lines = (self.series / "SHA256SUMS").read_text().splitlines()
        (self.series / "SHA256SUMS").write_text(
            "".join(l + "\n" for l in lines if not l.endswith(self.names[0])))
        result = self.helper("apply")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("does not list " + self.names[0], result.stderr)
        self.assert_pristine()

    def test_refuses_a_series_that_does_not_apply_and_changes_nothing(self):
        # A series that matches its checksums and names the current HEAD as its base,
        # but whose second patch no longer fits the C file: only the dry run can tell.
        (self.klipper / "src/demo.c").write_text("int one;\nint different;\n")
        git(self.klipper, "commit", "-q", "-am", "C file diverges")
        (self.series / "BASE").write_text(git(self.klipper, "rev-parse", "HEAD") + "\n")
        write_sums(self.series)
        result = self.helper("apply")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("does not apply cleanly", result.stderr)
        self.assertEqual(self.read("src/demo.c"), "int one;\nint different;\n")
        self.assertEqual(self.read("klippy/demo.py"), DEMO_PY, "the first patch must not stay applied")
        self.assertFalse((self.klipper / ".git/openams-klipper-patches").exists())

    def test_remove_refuses_when_a_patched_file_was_edited_afterwards(self):
        self.assertEqual(self.helper("apply").returncode, 0)
        edited = STEP2_PY + "my later edit\n"
        (self.klipper / "klippy/demo.py").write_text(edited)
        result = self.helper("remove")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("changed since", result.stderr)
        self.assertEqual(self.read("klippy/demo.py"), edited)
        self.assertEqual(self.helper("status").returncode, 1)

    def test_remove_uses_the_recorded_patches_not_the_shipped_ones(self):
        self.assertEqual(self.helper("apply").returncode, 0)
        (self.series / self.names[0]).write_text("tampered after apply\n")
        result = self.helper("remove")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_pristine()

    def test_remove_clears_a_stale_record_when_the_files_were_reset(self):
        self.assertEqual(self.helper("apply").returncode, 0)
        git(self.klipper, "checkout", "--", "klippy/demo.py", "src/demo.c")
        result = self.helper("status")
        self.assertEqual(result.returncode, 10, result.stdout + result.stderr)
        result = self.helper("remove")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.klipper / ".git/openams-klipper-patches").exists())
        self.assertEqual(self.helper("apply").returncode, 0)
        self.assert_patched()


class RealSeriesTests(unittest.TestCase):
    """Read-only checks on the shipped series; it is never applied here."""

    def test_real_series_is_well_formed(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import klipper_patches
        series = klipper_patches.load_series(str(REAL_SERIES))
        self.assertRegex(series.base, r"^[0-9a-f]{40}$")
        self.assertGreater(len(series.names), 1)
        self.assertEqual(series.names, sorted(series.names))
        self.assertEqual(sorted(p.name for p in REAL_SERIES.glob("*.patch")), sorted(series.names))
        for name in series.names:
            text = (REAL_SERIES / name).read_text(errors="replace")
            self.assertIn("From: JR Lomas <lomas.jr@gmail.com>", text.splitlines()[1], name)
            self.assertNotIn("Co-Authored-By", text, name)


class InstallShKlipperPatchTests(HelperTestCase):
    def setUp(self):
        if os.geteuid() == 0:
            self.skipTest("install.sh intentionally refuses to run as root")
        super().setUp()
        self.moonraker = self.root / "moonraker"
        (self.moonraker / "moonraker/components").mkdir(parents=True)
        (self.moonraker / "moonraker/__init__.py").write_text("# fixture\n")
        self.config = self.root / "printer_data/config"
        self.config.mkdir(parents=True)
        (self.config / "moonraker.conf").write_text("[server]\n")
        (self.config / "printer.cfg").write_text("[printer]\nkinematics: none\n")
        self.remote = self.root / "gco-remote"
        self.checkout = self.root / "gco-routines"
        self.gitconfig = self.root / "gitconfig"
        self.gitconfig.write_text(
            '[user]\nname = Installer Test\nemail = installer@example.invalid\n'
            '[url "file://%s"]\ninsteadOf = %s\n' % (self.remote, GCO_URL))
        self.env = dict(
            os.environ,
            OPENAMS_NO_SERVICE="1",
            OPENAMS_SPOOLMAN_INFO_URL="file:///nonexistent/openams-spoolman",
            OPENAMS_KLIPPER_PATCHES_DIR=str(self.series),
            MOONRAKER_PATH=str(self.moonraker),
            GIT_CONFIG_GLOBAL=str(self.gitconfig),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
            GIT_ALLOW_PROTOCOL="file",
        )
        (self.remote / "tools").mkdir(parents=True)
        (self.remote / "klippy_extra/gco_routines").mkdir(parents=True)
        (self.remote / "tools/install_gco_routines.py").write_text(GCO_INSTALLER)
        (self.remote / "klippy_extra/gco_routines/__init__.py").write_text("# fixture\n")
        for args in (("init", "-b", "main", str(self.remote)),
                     ("-C", str(self.remote), "add", "."),
                     ("-C", str(self.remote), "commit", "-m", "fixture")):
            subprocess.run(["git"] + list(args), env=self.env, check=True, capture_output=True)

    def install(self, *extra):
        return subprocess.run(
            ["bash", str(INSTALLER), "-k", str(self.klipper), "-c", str(self.config),
             "-s", "klipper-test", "-r", str(self.checkout), *extra],
            env=self.env, text=True, capture_output=True, timeout=120, input="n\n")

    def test_patches_are_not_applied_by_default(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_pristine()
        self.assertNotIn("patch helper", result.stdout)

    def test_with_klipper_patches_applies_the_series(self):
        result = self.install("--with-klipper-patches")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_patched()
        for expected in ("working tree", "--remove-klipper-patches", "install.sh -u",
                         "applied 2 patches"):
            self.assertIn(expected, result.stdout)
        self.assertIn("[skipped: OPENAMS_NO_SERVICE=1] systemctl stop klipper-test", result.stdout)
        self.assertIn("[skipped: OPENAMS_NO_SERVICE=1] systemctl start klipper-test", result.stdout)
        self.assertEqual(self.helper("status").returncode, 0)

    def test_with_klipper_patches_twice_is_harmless(self):
        self.assertEqual(self.install("--with-klipper-patches").returncode, 0)
        result = self.install("--with-klipper-patches")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("already applied", result.stdout)
        self.assert_patched()

    def test_with_klipper_patches_refuses_a_mismatched_base_and_restarts_klipper(self):
        (self.klipper / "src/Kconfig").write_text(KCONFIG + "# newer upstream\n")
        git(self.klipper, "commit", "-q", "-am", "newer upstream")
        env = dict(self.env, OPENAMS_KLIPPER_PATCHES_EXACT_BASE="1")
        exact = subprocess.run(
            ["bash", str(INSTALLER), "-k", str(self.klipper), "-c", str(self.config),
             "-s", "klipper-test", "-r", str(self.checkout), "--with-klipper-patches"],
            env=env, text=True, capture_output=True, timeout=120, input="n\n")
        self.assertNotEqual(exact.returncode, 0, exact.stdout + exact.stderr)
        self.assertIn("newer than the tested base", exact.stderr)
        self.assertIn("[FAILED] the Klipper patches were not applied", exact.stdout)
        self.assertIn("attempting to restore the Klipper service", exact.stdout)
        self.assert_pristine()
        result = self.install("--with-klipper-patches")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_patched()

    def test_with_klipper_patches_refuses_a_checksum_mismatch(self):
        patch = self.series / self.names[0]
        patch.write_text(patch.read_text() + "\n")
        result = self.install("--with-klipper-patches")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("checksum mismatch", result.stderr)
        self.assert_pristine()

    def test_remove_klipper_patches_reverses_them_and_does_nothing_else(self):
        self.assertEqual(self.install("--with-klipper-patches").returncode, 0)
        result = self.install("--remove-klipper-patches")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_pristine()
        self.assertIn("removed 2 patches", result.stdout)
        self.assertIn("[skipped: OPENAMS_NO_SERVICE=1] systemctl stop klipper-test", result.stdout)
        self.assertIn("[skipped: OPENAMS_NO_SERVICE=1] systemctl start klipper-test", result.stdout)
        # OpenAMS itself stays installed: --remove-klipper-patches is stand-alone.
        self.assertTrue((self.klipper / "klippy/extras/oams.py").is_symlink())
        self.assertEqual(self.helper("status").returncode, 10)

    def test_remove_klipper_patches_when_none_are_applied_succeeds(self):
        result = self.install("--remove-klipper-patches")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("nothing to remove", result.stdout)

    def test_remove_klipper_patches_refuses_an_edited_file_and_still_starts_klipper(self):
        self.assertEqual(self.install("--with-klipper-patches").returncode, 0)
        (self.klipper / "klippy/demo.py").write_text(STEP2_PY + "my later edit\n")
        result = self.install("--remove-klipper-patches")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("changed since", result.stderr)
        self.assertIn("systemctl start klipper-test", result.stdout)

    def test_uninstall_removes_the_patches_when_applied(self):
        self.assertEqual(self.install("--with-klipper-patches").returncode, 0)
        result = self.install("-u")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_pristine()
        self.assertFalse((self.klipper / "klippy/extras/oams.py").is_symlink())
        self.assertEqual(self.helper("status").returncode, 10)

    def test_uninstall_without_patches_says_so(self):
        self.assertEqual(self.install().returncode, 0)
        result = self.install("-u")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("No Klipper patches are applied", result.stdout)

    def test_conflicting_flags_are_refused(self):
        result = self.install("--with-klipper-patches", "--remove-klipper-patches")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot be used together", result.stderr)
        result = self.install("--with-klipper-patches", "-u")
        self.assertNotEqual(result.returncode, 0)
        self.assert_pristine()


if __name__ == "__main__":
    unittest.main()
