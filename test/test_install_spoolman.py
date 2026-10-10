"""Spoolman as part of the default install.sh run.

Everything runs against fake trees with OPENAMS_NO_SERVICE=1 and no network:
the "localhost:7912" probe is pointed at a file:// URL, and Spoolman's own
installer is replaced by a fake script (OPENAMS_SPOOLMAN_INSTALLER).

ensure_pg_config, the libpq-dev fallback that runs just before Spoolman's own
installer, is exercised on its own against fake sudo/apt/dpkg commands (see
EnsurePgConfigTests).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_install_sh as _base  # noqa: E402

ROOT_INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"
LOCAL = "http://localhost:7912"


class SpoolmanInstallTests(unittest.TestCase):
    setUp = _base.InstallShTests.setUp
    _build_klipper = _base.InstallShTests._build_klipper
    _build_moonraker = _base.InstallShTests._build_moonraker
    _build_config = _base.InstallShTests._build_config
    _build_gco_remote = _base.InstallShTests._build_gco_remote
    run_cmd = _base.InstallShTests.run_cmd

    def install(self, *extra, answer="n", uninstall=False):
        args = ["bash", str(ROOT_INSTALLER),
                "-k", str(self.klipper), "-c", str(self.config),
                "-s", "klipper-test", "-r", str(self.checkout)]
        args += ["-u"] if uninstall else ["--skip-gco-routines"]
        return self.run_cmd(*args, *extra, check=False, input=answer + "\n")

    @property
    def conf(self):
        return (self.config / "moonraker.conf").read_text()

    def fake_installer(self, body="", *, answering=True):
        """A stand-in for Spoolman's release installer; records that it ran."""
        self.ran = self.root / "spoolman-installer.ran"
        probe = self.root / "spoolman-info.json"
        script = self.root / "fake-spoolman-installer.sh"
        script.write_text("echo ran >> '%s'\n%s\n%s\n" % (
            self.ran, "echo '{}' > '%s'" % probe if answering else "", body))
        self.env["OPENAMS_SPOOLMAN_INSTALLER"] = str(script)
        self.env["OPENAMS_SPOOLMAN_INFO_URL"] = "file://" + str(probe)
        self.env["OPENAMS_SPOOLMAN_WAIT"] = "0"
        self.probe = probe

    def test_existing_spoolman_section_is_reused_and_nothing_installed(self):
        (self.config / "moonraker.conf").write_text(
            "[server]\n\n[spoolman]\nserver: http://printhost:7912\n")
        self.fake_installer()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.ran.exists())
        conf = self.conf
        self.assertEqual(conf.count("[spoolman]"), 1)
        self.assertNotIn(LOCAL, conf)
        self.assertEqual(len(re.findall(r"^\[openams_spoolman\]$", conf, re.M)), 1)

    def test_this_hosts_spoolman_that_does_not_answer_is_installed_again(self):
        (self.config / "moonraker.conf").write_text(
            "[server]\n\n[spoolman]\nserver: http://localhost:7912/\n")
        self.fake_installer()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.ran.exists())
        self.assertIn("installing it again", result.stdout)
        self.assertEqual(self.conf.count("[spoolman]"), 1)

    def test_this_hosts_spoolman_that_answers_is_left_alone(self):
        (self.config / "moonraker.conf").write_text(
            "[server]\n\n[spoolman]\nserver: http://localhost:7912/\n")
        self.fake_installer()
        self.probe.write_text("{}")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.ran.exists())

    def test_server_answering_the_probe_is_configured_without_install(self):
        self.fake_installer()
        self.probe.write_text("{}")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.ran.exists())
        self.assertIn("[spoolman]\nserver: %s\n" % LOCAL, self.conf)
        self.assertIn("[openams_spoolman]", self.conf)

    def test_nothing_present_runs_the_installer_and_writes_the_sections(self):
        self.fake_installer()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.ran.read_text().count("ran"), 1)
        self.assertIn("[spoolman]\nserver: %s\n" % LOCAL, self.conf)
        self.assertIn("[openams_spoolman]", self.conf)
        self.assertIn("[skipped: OPENAMS_NO_SERVICE=1] systemctl restart moonraker", result.stdout)

    def test_installer_failure_warns_and_changes_no_config(self):
        self.fake_installer("exit 1", answering=False)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[WARNING] Spoolman was not installed", result.stdout)
        self.assertNotIn("spoolman]", self.conf)

    def test_spoolman_section_without_server_gets_the_option(self):
        (self.config / "moonraker.conf").write_text("[server]\n\n[spoolman]\n\n[other]\na: 1\n")
        self.fake_installer()
        self.probe.write_text("{}")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.conf.count("[spoolman]"), 1)
        self.assertIn("[spoolman]\nserver: %s\n" % LOCAL, self.conf)

    def test_spoolman_url_flag_writes_it_and_installs_nothing(self):
        self.fake_installer()
        self.probe.write_text("{}")
        result = self.install("--spoolman-url", "http://10.0.0.9:7912/")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.ran.exists())
        self.assertIn("[spoolman]\nserver: http://10.0.0.9:7912\n", self.conf)
        self.assertNotIn(LOCAL, self.conf)
        self.assertIn("[openams_spoolman]", self.conf)

    def test_spoolman_url_flag_needs_a_value_and_excludes_no_spoolman(self):
        self.assertNotEqual(self.install("--spoolman-url").returncode, 0)
        both = self.install("--spoolman-url", "http://x:1", "--no-spoolman")
        self.assertNotEqual(both.returncode, 0)
        self.assertNotIn("spoolman]", self.conf)

    def test_no_spoolman_skips_everything(self):
        self.fake_installer()
        self.probe.write_text("{}")
        result = self.install("--no-spoolman")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.ran.exists())
        self.assertNotIn("spoolman]", self.conf)
        self.assertIn("--spoolman-url", result.stdout)

    def test_second_run_leaves_moonraker_conf_identical(self):
        self.fake_installer()
        self.assertEqual(self.install().returncode, 0)
        first = self.conf
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.conf, first)
        self.assertEqual(self.ran.read_text().count("ran"), 1)

    def test_uninstall_removes_only_openams_spoolman(self):
        (self.config / "moonraker.conf").write_text(
            "[server]\n\n[spoolman]\nserver: http://printhost:7912\n")
        self.fake_installer()
        self.assertEqual(self.install().returncode, 0)
        with_section = self.conf
        self.assertIn("[openams_spoolman]", with_section)
        result = self.install(uninstall=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        conf = self.conf
        self.assertNotIn("[openams_spoolman]", conf)
        self.assertIn("[spoolman]\nserver: http://printhost:7912\n", conf)
        self.assertIn("[server]", conf)
        self.assertIn("left in place", result.stdout)

    def test_failed_write_restores_moonraker_conf(self):
        self.fake_installer()
        self.env["OPENAMS_SPOOLMAN_FAIL_WRITE"] = "1"
        before = "[server]\n"
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restored from its backup", result.stderr)
        # The [update_manager openams] section was written before the Spoolman
        # step; the Spoolman sections must be gone again.
        self.assertNotIn("spoolman]", self.conf)
        self.assertTrue(self.conf.startswith(before))

    def test_no_component_link_means_no_openams_spoolman_section(self):
        self.fake_installer()
        self.probe.write_text("{}")
        self.env["MOONRAKER_PATH"] = str(self.root / "no-moonraker")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[WARNING] the openams_spoolman component is not linked", result.stdout)
        self.assertNotIn("[openams_spoolman]", self.conf)


# ---------------------------------------------------------------------------
# ensure_pg_config: Spoolman's installer needs pg_config, and on a Debian
# release whose security pocket was retired apt cannot supply libpq-dev. The
# exact version matching libpq5 comes from snapshot.debian.org instead.
# ---------------------------------------------------------------------------

# apt fails for the package (its pool file is gone) but takes a local .deb.
APT_STUB = '''case " $* " in
    *" ./"*) exit 0;;
esac
if [ "${FAKE_APT_FAIL:-0}" = "1" ]; then exit 100; fi
exit 0
'''
# dpkg-query -W libpq5 fails, as it does for a package that is not installed.
DPKG_QUERY_STUB = """if [ -n "${FAKE_LIBPQ5:-}" ]; then printf '%s\\n' "$FAKE_LIBPQ5"; exit 0; fi
exit 1
"""
DPKG_STUB = """printf '%s\\n' "${FAKE_ARCH:-arm64}"
"""
LIBPQ_VERSION = "13.23-0+deb11u4"
LIBPQ_ARCH = "arm64"
DEB_BYTES = b"not really a libpq-dev .deb\n"


class EnsurePgConfigTests(unittest.TestCase):
    """ensure_pg_config on a fake Debian host.

    The function is cut out of install.sh and sourced on its own. It runs only
    on the real Spoolman install path, behind a systemd host, Spoolman's own
    release installer and a download, so driving the whole installer to reach
    it would mean faking all of that; a switch that runs only this function
    would be one more thing install.sh has to carry for the tests' sake.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="openams-pg-config-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bindir = self.root / "fakebin"
        self.bindir.mkdir()
        self.calls = self.root / "calls.log"
        self.calls.write_text("")
        self.tmpdir = self.root / "tmp"
        self.tmpdir.mkdir()
        self.snapshot = self.root / "snapshot"
        self.bash = shutil.which("bash")
        self.env = dict(
            os.environ,
            PATH=str(self.bindir) + os.pathsep + os.environ["PATH"],
            TMPDIR=str(self.tmpdir),
            FAKE_CALLS=str(self.calls),
            OPENAMS_SNAPSHOT_BASE="file://" + str(self.snapshot),
        )

    # -- the fake host ----------------------------------------------------

    def stub(self, name, body=""):
        """Put a command on PATH that records its call and then runs its body."""
        path = self.bindir / name
        path.write_text('#!/bin/bash\necho "%s $*" >> "$FAKE_CALLS"\n%s\n' % (name, body))
        path.chmod(0o755)

    def fake_host(self, *, debian=True, pg_config=False, apt_fails=True,
                  libpq5=LIBPQ_VERSION, arch=LIBPQ_ARCH):
        if debian:
            self.stub("apt-get", APT_STUB)
            self.stub("dpkg-query", DPKG_QUERY_STUB)
            self.stub("dpkg", DPKG_STUB)
        if pg_config:
            self.stub("pg_config")
        self.stub("sudo", 'exec "$@"')
        self.stub("curl", 'exec %s "$@"' % shutil.which("curl"))
        self.env["FAKE_APT_FAIL"] = "1" if apt_fails else "0"
        self.env["FAKE_LIBPQ5"] = libpq5 or ""
        self.env["FAKE_ARCH"] = arch

    def snapshot_tree(self, version=LIBPQ_VERSION, arch=LIBPQ_ARCH,
                      content=DEB_BYTES, listed_sha1=None):
        """The part of snapshot.debian.org the fallback reads, as files.

        curl percent-decodes the URL before it opens a file:// path, so the
        directory carries the version's literal "+".
        """
        listed = listed_sha1 or hashlib.sha1(content).hexdigest()
        api = self.snapshot / "mr/binary/libpq-dev" / version
        api.mkdir(parents=True)
        (api / "binfiles").write_text(json.dumps({"result": [
            {"architecture": "amd64", "hash": "0" * 40},
            {"architecture": arch, "hash": listed},
        ]}))
        files = self.snapshot / "file"
        files.mkdir(parents=True)
        (files / listed).write_bytes(content)
        return listed

    # -- running it -------------------------------------------------------

    def ensure_pg_config(self):
        source = ROOT_INSTALLER.read_text()
        start = source.index("\nensure_pg_config()\n")
        script = source[start:source.index("\n}\n", start) + 3] + "\nensure_pg_config\n"
        return subprocess.run([self.bash, "-c", script], env=self.env, text=True,
                              capture_output=True, timeout=60)

    @property
    def log(self):
        return self.calls.read_text()

    # -- the fallback -----------------------------------------------------

    def test_pg_config_already_there_runs_nothing(self):
        self.fake_host(pg_config=True)
        result = self.ensure_pg_config()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.log, "")
        self.assertNotIn("[WARNING]", result.stdout)

    def test_a_host_that_is_not_debian_runs_nothing(self):
        self.fake_host(debian=False)
        # Only the fakes are on PATH, so apt-get, dpkg-query and pg_config are
        # all absent the way they are on a host with no dpkg at all.
        self.env["PATH"] = str(self.bindir)
        result = self.ensure_pg_config()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.log, "")
        self.assertNotIn("[WARNING]", result.stdout)

    def test_apt_answers_and_the_archive_is_never_asked(self):
        self.fake_host(apt_fails=False)
        self.snapshot_tree()
        result = self.ensure_pg_config()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[OK]", result.stdout)
        self.assertIn("sudo apt-get install -y libpq-dev", self.log)
        self.assertNotIn("curl", self.log)
        self.assertNotIn("dpkg-query", self.log)

    def test_a_failed_apt_installs_the_matching_deb_from_the_archive(self):
        self.fake_host()
        sha1 = self.snapshot_tree()
        result = self.ensure_pg_config()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[OK]", result.stdout)
        # The version of the installed libpq5, and of nothing else.
        self.assertIn("dpkg-query -W -f=${Version} libpq5", self.log)
        self.assertIn("dpkg --print-architecture", self.log)
        # curl percent-encodes the "+" of the version in the archive URL.
        self.assertIn("mr/binary/libpq-dev/13.23-0%2Bdeb11u4/binfiles", self.log)
        self.assertIn("file/" + sha1, self.log)
        self.assertIn("apt-get install -y ./libpq-dev_13.23-0+deb11u4_arm64.deb", self.log)
        self.assertEqual(list(self.tmpdir.iterdir()), [], "the .deb is left behind")

    def test_a_deb_that_does_not_match_its_hash_is_not_installed(self):
        self.fake_host()
        real = self.snapshot_tree(listed_sha1="de1de1de" * 5)
        result = self.ensure_pg_config()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[WARNING]", result.stdout)
        self.assertIn(real, result.stdout)
        self.assertNotIn("apt-get install -y ./", self.log)
        self.assertEqual(list(self.tmpdir.iterdir()), [], "the .deb is left behind")

    def test_without_libpq5_nothing_is_fetched(self):
        self.fake_host(libpq5="")
        self.snapshot_tree()
        result = self.ensure_pg_config()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[WARNING]", result.stdout)
        self.assertIn("libpq5 is not installed", result.stdout)
        self.assertNotIn("mr/binary", self.log)
        self.assertNotIn("apt-get install -y ./", self.log)

    def test_the_fallback_runs_only_on_the_real_install_path(self):
        """ensure_pg_config is called once, after both of the early returns."""
        source = ROOT_INSTALLER.read_text()
        start = source.index("\ninstall_spoolman_server()\n")
        body = source[start:source.index("\n}\n", start) + 3]
        self.assertEqual(body.count("ensure_pg_config"), 1)
        self.assertLess(body.index("OPENAMS_SPOOLMAN_INSTALLER"), body.index("ensure_pg_config"))
        self.assertLess(body.index('NO_SERVICE" = "1"'), body.index("ensure_pg_config"))
        self.assertLess(body.index("ensure_pg_config"), body.index("./scripts/install.sh"))


if __name__ == "__main__":
    unittest.main()
