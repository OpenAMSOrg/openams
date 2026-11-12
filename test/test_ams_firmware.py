"""AMS firmware images: the list of Bambu's addresses, the download and its
checks, versions, and the guard that keeps Bambu firmware out of this repo.

Every image here is made up: a BIMH-shaped header over random bytes. Nothing
here touches the network or hardware.
"""

import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cortex_bridge import ams_firmware as fw  # noqa: E402

URL = "https://public-cdn.bblmw.com/upgrade/device/BL-A001/00.00.06.49/" \
      "product/0000000000/ams_rev8-firmware-v00.00.06.49-1_product.bin.sig"
NAME = "ams_rev8-firmware-v00.00.06.49-1.bin.sig"


def make_image(body_size=3000, name=NAME, *, total=None, header=416,
               body=None, magic=b"BIMH", fmt=1):
    payload = os.urandom(body_size)
    head = bytearray(416)
    head[0:4] = magic
    size = 416 + body_size
    struct.pack_into("<II", head, 4, fmt, size if total is None else total)
    struct.pack_into("<I", head, 0x20, header)
    struct.pack_into("<I", head, 0x24, 0x100)
    struct.pack_into("<I", head, 0x28, body_size if body is None else body)
    encoded = name.encode("ascii")
    head[0x30:0x30 + len(encoded)] = encoded
    return bytes(head) + payload


def entry_for(data, **over):
    item = dict(family="ams1", version="00.00.06.49", hardware="rev8",
                url=URL, md5=hashlib.md5(data).hexdigest(), size=len(data),
                inner_name=NAME, source="test")
    item.update(over)
    return fw.Image(**item)


class VersionTests(unittest.TestCase):
    def test_snapshot_bytes_read_lowest_first(self):
        self.assertEqual(fw.format_version([29, 21, 0, 3]), "03.00.21.29")
        self.assertEqual(fw.format_version([40, 6, 0, 0]), "00.00.06.40")
        self.assertEqual(fw.format_version("5.0.22.19"), "05.00.22.19")

    def test_bad_versions(self):
        for bad in ("3.0.21", "a.b.c.d", "", "1.2.3.4.5"):
            with self.assertRaises(fw.AmsFirmwareError):
                fw.parse_version(bad)
        with self.assertRaises(fw.AmsFirmwareError):
            fw.format_version([1, 2, 3])

    def test_compare(self):
        self.assertEqual(fw.compare("03.00.21.29", "05.00.22.19"), "upgrade")
        self.assertEqual(fw.compare("05.00.22.19", "03.00.21.29"),
                         "downgrade")
        self.assertEqual(fw.compare("03.00.21.29", "03.00.21.29"), "same")


class CatalogTests(unittest.TestCase):
    def write(self, images, schema=1):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"schema": schema, "images": images}, tmp)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return Path(tmp.name)

    def entry(self, **over):
        item = dict(family="ams1", version="00.00.06.49", hardware="rev8",
                    url=URL, md5="0" * 32, size=1000)
        item.update(over)
        return item

    def test_shipped_list_loads_and_holds_only_addresses(self):
        images = fw.load_catalog()
        self.assertTrue(images)
        allowed = {"family", "version", "hardware", "file", "url",
                   "package", "md5", "size", "inner_name", "source"}
        raw = json.loads(fw.CATALOG.read_text("utf-8"))
        for item in raw["images"]:
            self.assertLessEqual(set(item), allowed, item)
        # A list of addresses stays small; an embedded image would not.
        self.assertLess(fw.CATALOG.stat().st_size, 64 * 1024)

    def test_latest_and_named_versions(self):
        path = self.write([
            self.entry(version="00.00.06.40", hardware="rev7"),
            self.entry(version="00.00.06.40", hardware="rev8"),
            self.entry(version="00.01.06.62", hardware="rev8"),
        ])
        images = fw.load_catalog(path)
        self.assertEqual(fw.versions(images, "ams1"),
                         ["00.01.06.62", "00.00.06.40"])
        self.assertEqual([i.version for i in fw.choose(images, "ams1")],
                         ["00.01.06.62"])
        self.assertEqual(
            sorted(i.hardware for i in fw.choose(images, "ams1", "0.0.6.40")),
            ["rev7", "rev8"])
        with self.assertRaises(fw.AmsFirmwareError):
            fw.choose(images, "ams1", "00.00.06.44")
        with self.assertRaises(fw.AmsFirmwareError):
            fw.choose(images, "ams_ht")

    def test_rejects_bad_entries(self):
        for over in ({"url": "http://public-cdn.bblmw.com/x.bin.sig"},
                     {"url": "", "package": "p.zip"},
                     {"url": "", "package": "p.zip", "file": "../x.bin"},
                     {"url": "https://example.com/x.bin.sig"},
                     {"family": "ams9"}, {"md5": "xyz"}, {"size": 0},
                     {"version": "1.2"}):
            with self.assertRaises(fw.AmsFirmwareError, msg=over):
                fw.load_catalog(self.write([self.entry(**over)]))
        with self.assertRaises(fw.AmsFirmwareError):
            fw.load_catalog(self.write([self.entry(), self.entry()]))
        with self.assertRaises(fw.AmsFirmwareError):
            fw.load_catalog(self.write([self.entry()], schema=2))


class ImageCheckTests(unittest.TestCase):
    def test_a_good_image_passes(self):
        data = make_image()
        fields = fw.check_image(data, entry_for(data))
        self.assertEqual(fields["header"], 416)
        self.assertEqual(fields["name"], NAME)

    def test_each_defect_is_named(self):
        good = make_image()
        cases = {
            "MD5": (good[:-1] + bytes([good[-1] ^ 1]), None),
            "bytes, the list says": (good, {"size": len(good) + 1}),
            "no BIMH header": (make_image(magic=b"XXXX"), None),
            "BIMH format": (make_image(fmt=2), None),
            "the header gives": (make_image(total=10), None),
            "byte header": (make_image(header=512), None),
            "data bytes": (make_image(body=5), None),
            "named": (make_image(name="ams_rev7-firmware-v00.00.06.49-1"),
                      None),
        }
        for words, (data, over) in cases.items():
            image = entry_for(good if words == "MD5" else data, **(over or {}))
            with self.assertRaises(fw.AmsFirmwareError, msg=words) as ctx:
                fw.check_image(data, image)
            self.assertIn(words, str(ctx.exception))


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.cache = Path(tempfile.mkdtemp())
        self.data = make_image()
        self.image = entry_for(self.data)
        self.calls = []

    def tearDown(self):
        for path in self.cache.glob("*"):
            path.unlink()
        self.cache.rmdir()

    def download(self, data):
        def fake(url, limit, timeout):
            self.calls.append(url)
            return data
        return fake

    def test_downloads_once_then_uses_the_cache(self):
        path = fw.fetch(self.image, self.cache,
                        download=self.download(self.data))
        self.assertEqual(path.read_bytes(), self.data)
        fw.fetch(self.image, self.cache, download=self.download(b""))
        self.assertEqual(len(self.calls), 1)

    def test_a_damaged_cached_copy_is_downloaded_again(self):
        path = fw.cached_path(self.image, self.cache)
        path.write_bytes(self.data[:-10])
        fw.fetch(self.image, self.cache, download=self.download(self.data))
        self.assertEqual(path.read_bytes(), self.data)
        self.assertEqual(len(self.calls), 1)

    def test_a_bad_download_is_not_kept(self):
        with self.assertRaises(fw.AmsFirmwareError):
            fw.fetch(self.image, self.cache,
                     download=self.download(make_image()))
        self.assertEqual(list(self.cache.glob("*")), [])

    def test_network_errors_read_plainly(self):
        def broken(url, limit, timeout):
            raise OSError("no route")
        with self.assertRaises(fw.AmsFirmwareError) as ctx:
            fw.fetch(self.image, self.cache, download=broken)
        self.assertIn("could not download", str(ctx.exception))

    def test_only_bambu_servers(self):
        with self.assertRaises(fw.AmsFirmwareError):
            fw.check_url("https://evil.example/ams.bin.sig")
        fw.check_url(URL)


class ImportTests(unittest.TestCase):
    """Images taken out of a Bambu offline package (a made-up one here)."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.cache = self.dir / "cache"
        self.ht = make_image(4000, name="n3s_rev5-firmware-v05.00.22.19-1.bin.sig")
        self.image = fw.Image(
            family="ams_ht", version="05.00.22.19", hardware="rev5",
            md5=hashlib.md5(self.ht).hexdigest(), size=len(self.ht),
            file="n3s_rev5-firmware-v05.00.22.19-1_product.bin.sig",
            package="offline-ota-test.zip",
            inner_name="n3s_rev5-firmware-v05.00.22.19-1.bin.sig")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir)

    def package(self, members):
        import zipfile
        path = self.dir / "offline-ota-test.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        return path

    def test_a_listed_image_is_imported_and_checked(self):
        path = self.package({self.image.file: self.ht,
                             "mc_rev7-firmware-x.bin.sig": b"other"})
        results = fw.import_package(path, [self.image], self.cache)
        self.assertEqual(len(results), 1)
        image, cached, error = results[0]
        self.assertEqual(error, "")
        self.assertEqual(cached.read_bytes(), self.ht)
        # fetch now finds it without a download address
        self.assertEqual(fw.fetch(self.image, self.cache), cached)

    def test_a_wrong_or_damaged_member_is_refused(self):
        other = make_image(4000, name="n3s_rev5-firmware-v05.00.22.19-1.bin.sig")
        path = self.package({self.image.file: other})
        (image, cached, error), = fw.import_package(path, [self.image],
                                                    self.cache)
        self.assertIsNone(cached)
        self.assertIn("MD5", error)
        path = self.package({self.image.file: self.ht + b"x"})
        (image, cached, error), = fw.import_package(path, [self.image],
                                                    self.cache)
        self.assertIn("bytes in the package", error)

    def test_members_in_folders_are_ignored(self):
        path = self.package({"sub/" + self.image.file: self.ht})
        self.assertEqual(fw.import_package(path, [self.image], self.cache), [])

    def test_not_a_zip(self):
        path = self.dir / "x.zip"
        path.write_bytes(b"not a zip")
        with self.assertRaises(fw.AmsFirmwareError):
            fw.import_package(path, [self.image], self.cache)

    def test_without_an_address_fetch_names_the_package(self):
        with self.assertRaises(fw.AmsFirmwareError) as ctx:
            fw.fetch(self.image, self.cache)
        self.assertIn("offline-ota-test.zip", str(ctx.exception))
        self.assertIn("ams-firmware import", str(ctx.exception))


class SnapshotTests(unittest.TestCase):
    def test_units_and_versions(self):
        units = fw.units_from_snapshot({"units": [
            {"uid": "a1", "index": 0, "link": "online", "family": "ams1",
             "ams_version": [40, 6, 0, 0]},
            {"uid": "b2", "index": 128, "link": "offline",
             "family": "ams_ht", "ams_version": [29, 21, 0, 3]},
            {"uid": "c3", "index": 1, "link": "online", "family": "ams2pro",
             "ams_version": None},
        ]})
        self.assertEqual([(u["family"], u["version"], u["online"])
                          for u in units],
                         [("ams1", "00.00.06.40", True),
                          ("ams_ht", "03.00.21.29", False),
                          ("ams2", "", True)])


class InstalledTests(unittest.TestCase):
    def test_record_and_read(self):
        cache = Path(tempfile.mkdtemp())
        self.assertEqual(fw.installed_versions(cache), {})
        fw.record_installed("a1f20265d767", "01.00.07.00", cache, now="t1")
        fw.record_installed("e051b837a343", "05.00.22.19", cache, now="t2")
        fw.record_installed("a1f20265d767", "01.00.06.83", cache, now="t3")
        self.assertEqual(fw.installed_versions(cache), {
            "a1f20265d767": {"version": "01.00.06.83", "at": "t3"},
            "e051b837a343": {"version": "05.00.22.19", "at": "t2"}})
        for path in cache.glob("*"):
            path.unlink()
        cache.rmdir()


class NoBambuFirmwareInTheRepo(unittest.TestCase):
    """OpenAMS stores addresses of Bambu firmware, never the firmware. Fail
    if any tracked file is a BIMH image."""

    def test_no_tracked_file_is_an_ams_image(self):
        try:
            names = subprocess.run(
                ["git", "ls-files", "-z"], cwd=ROOT, check=True,
                capture_output=True).stdout.split(b"\0")
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("not a git checkout")
        found = []
        for name in filter(None, names):
            path = ROOT / os.fsdecode(name)
            try:
                with open(path, "rb") as handle:
                    if handle.read(8) == b"BIMH\x01\x00\x00\x00":
                        found.append(str(path))
            except OSError:
                continue
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
