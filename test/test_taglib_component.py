"""The openams_taglib Moonraker component against a fake Moonraker and a small
fake library tree, plus the mock Moonraker's --taglib routes."""

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "moonraker"))

from openams_taglib import component as taglib_component  # noqa: E402
from openams_taglib import library  # noqa: E402


def make_image(uid, fill=0x11):
    """A 1 KB dump whose block 0 starts with ``uid`` (4 bytes) and its BCC."""
    head = bytes.fromhex(uid)
    bcc = head[0] ^ head[1] ^ head[2] ^ head[3]
    return head + bytes([bcc]) + bytes([fill]) * (1024 - 5)


def bad_bcc_image(uid):
    image = bytearray(make_image(uid))
    image[4] ^= 0xFF
    return bytes(image)


LONG_NAME = "Very Long Color Name ééé That Overflows"


def build_library(root):
    """Category / Material / Color / UID / file, with every case to skip."""
    def dump(*parts, data):
        path = root.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    dump("Bambu Lab", "PLA Basic", "Black", "A", "A-dump.bin",
         data=make_image("0A000001"))
    dump("Bambu Lab", "PLA Basic", "Black", "B", "B-dump.bin",
         data=make_image("0A000002"))
    dump("Bambu Lab", "PLA Basic", "Red", "C", "C-dump.bin",
         data=make_image("0A000003"))
    dump("Bambu Lab", "ABS", "Black", "D", "D-dump.bin",
         data=make_image("0A000004"))
    # the same UID again, later in sorted order: skipped
    dump("Bambu Lab", "PLA Basic", "Red", "Z", "Z-dump.bin",
         data=make_image("0A000003"))
    dump("Bambu Lab", "ABS", "Black", "E", "E-dump.bin",
         data=bad_bcc_image("0A000005"))
    dump("Bambu Lab", "ABS", "Black", "F", "F-dump.bin", data=b"\x00" * 100)
    # too shallow: not a tag folder at all
    dump("Bambu Lab", "PLA Basic", "stray-dump.bin", data=make_image("0A000006"))
    dump("Bambu Lab", "PLA Basic", "stray-dump.bin.txt", data=b"x")
    dump("Bambu Lab", "Ärger", LONG_NAME, "G", "G-dump.bin",
         data=make_image("0A000007"))


def fake_clock():
    """Replace the component's ``time`` only: patching ``time.monotonic``
    itself would stop the event loop's clock."""
    clock = mock.Mock(return_value=0.0)
    patcher = mock.patch.object(
        taglib_component, "time", types.SimpleNamespace(monotonic=clock))

    class Context(object):
        def __enter__(self):
            patcher.start()
            return clock

        def __exit__(self, *exc):
            patcher.stop()

    return Context()


def set_head(root, commit, packed=False):
    """A fake checkout's .git: HEAD names main, whose commit is ``commit``."""
    git = root / ".git"
    (git / "refs/heads").mkdir(parents=True, exist_ok=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    loose = git / "refs/heads/main"
    if packed:
        loose.unlink(missing_ok=True)
        (git / "packed-refs").write_text(
            "# pack-refs with: peeled\n%s refs/heads/main\n"
            "^%s\n" % (commit, "f" * 40))
    else:
        loose.write_text(commit + "\n")


class ServerError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class StubDatabase(object):
    def __init__(self, store):
        self.store = store

    async def get(self, key, default=None):
        return self.store.get(key, default)

    def insert(self, key, value):
        self.store[key] = json.loads(json.dumps(value))


class FakeDatabaseComponent(object):
    def __init__(self, store):
        self.store = store

    def register_local_namespace(self, name):
        return StubDatabase(self.store.setdefault(name, {}))


class FakeServer(object):
    error = ServerError

    def __init__(self, store):
        self.endpoints = {}
        self.database = FakeDatabaseComponent(store)

    def register_endpoint(self, path, methods, handler):
        self.endpoints[path] = (methods, handler)

    def lookup_component(self, name, default=None):
        return self.database if name == "database" else default

    def get_event_loop(self):
        return MoonrakerEventLoop(asyncio.get_event_loop())


class MoonrakerEventLoop(object):
    """What Moonraker's get_event_loop() returns: its own EventLoop
    (moonraker/eventloop.py), not asyncio's. It runs blocking work with
    run_in_thread and has no run_in_executor, so a component calling that
    fails here as it does in Moonraker."""

    def __init__(self, aioloop):
        self.aioloop = aioloop

    def create_task(self, coro):
        return self.aioloop.create_task(coro)

    def delay_callback(self, delay, callback, *args):
        return self.aioloop.call_later(delay, callback, *args)

    def run_in_thread(self, callback, *args):
        return self.aioloop.run_in_executor(None, callback, *args)


class FakeConfig(object):
    def __init__(self, server, **options):
        self.server = server
        self.options = options

    def get_server(self):
        return self.server

    def get(self, key, default=None):
        return self.options.get(key, default)

    def getboolean(self, key, default=None):
        return self.options.get(key, default)


class Request(object):
    def __init__(self, **args):
        self.args = args

    def get_str(self, key, default=None):
        return self.args.get(key, default)


class FakeProcess(object):
    def __init__(self, code=0, output=b""):
        self.returncode = code
        self.output = output
        self.release = None

    async def communicate(self):
        if self.release is not None:
            await self.release.wait()
        return self.output, None


class TaglibTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "lib"
        self.store = {}
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.addCleanup(self.loop.close)

    def make(self, build=True, **options):
        if build:
            build_library(self.path)
        self.server = FakeServer(self.store)
        options.setdefault("path", str(self.path))
        self.component = taglib_component.OpenAmsTaglib(
            FakeConfig(self.server, **options))
        self.loop.run_until_complete(self.component.component_init())
        self.settle()
        return self.component

    def settle(self):
        """Wait for the component's running task."""
        while self.component._running():
            self.loop.run_until_complete(asyncio.sleep(0.01))

    def call(self, name, **args):
        _, handler = self.server.endpoints["/server/openams/taglib/" + name]
        return self.loop.run_until_complete(handler(Request(**args)))


class IndexTests(TaglibTestCase):
    def test_indexing_counts_and_skips(self):
        self.make()
        status = self.call("status")
        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["count"], 5)
        self.assertEqual(status["skipped"], 3)    # repeat, bad BCC, size
        self.assertEqual(status["materials"], 3)
        self.assertEqual(status["colors"], 4)
        self.assertEqual(status["path"], str(self.path))
        self.assertIsNone(status["message"])

    def test_materials_are_sorted_by_utf8_bytes(self):
        self.make()
        self.assertEqual(self.call("materials"),
                         {"state": "ready", "count": 5,
                          "materials": ["ABS", "PLA Basic", "Ärger"]})

    def test_names_are_cut_to_31_bytes_of_whole_characters(self):
        self.make()
        colors = self.call("colors", material="Ärger")["colors"]
        name = colors[0][0]
        self.assertLessEqual(len(name.encode()), 31)
        self.assertTrue(LONG_NAME.startswith(name))
        self.assertGreater(len(name.encode()), 28)
        # cut mid-character: the byte count is 31 minus a dangling half
        self.assertEqual(library.cut_name("é" * 20),
                         "é" * 15)
        self.assertEqual(library.cut_name("short"), "short")

    def test_the_first_of_a_repeated_uid_wins(self):
        self.make()
        picked = self.call("pick", material="PLA Basic", color="Red")
        self.assertEqual(picked["uid"], "0A000003")
        paths = [e.path for e in self.component.library._entries
                 if e.uid == "0A000003"]
        self.assertEqual(len(paths), 1)
        self.assertIn("/C/", paths[0])

    def test_colors_sorted_with_fresh_counts(self):
        self.make()
        self.call("cloned", uid="0a000001")
        self.assertEqual(self.call("colors", material="PLA Basic"),
                         {"material": "PLA Basic",
                          "colors": [["Black", 2, 1], ["Red", 1, 1]]})
        self.assertEqual(self.call("colors", material="Nope"),
                         {"material": "Nope", "colors": []})

    def test_indexing_runs_in_a_thread(self):
        called = []
        original = MoonrakerEventLoop.run_in_thread

        def spy(loop, func, *args):
            called.append(func)
            return original(loop, func, *args)

        with mock.patch.object(MoonrakerEventLoop, "run_in_thread", spy):
            self.make()
        self.assertIn(library.scan, called)


class HeadTests(TaglibTestCase):
    def test_read_head_forms(self):
        set_head(self.path, "a" * 40)
        self.assertEqual(library.read_head(self.path), "a" * 40)
        set_head(self.path, "b" * 40, packed=True)
        self.assertEqual(library.read_head(self.path), "b" * 40)
        (self.path / ".git/HEAD").write_text("c" * 40 + "\n")
        self.assertEqual(library.read_head(self.path), "c" * 40)
        self.assertIsNone(library.read_head(self.root / "nothing"))

    def pull(self, commit, count_new=True):
        """What Moonraker's update manager does: new files, a new HEAD."""
        new = self.path / "Bambu Lab" / "ABS" / "Red" / "H"
        new.mkdir(parents=True, exist_ok=True)
        (new / "H-dump.bin").write_bytes(make_image("0A000008"))
        set_head(self.path, commit)

    def test_a_new_head_triggers_a_background_reindex(self):
        build_library(self.path)
        set_head(self.path, "a" * 40)
        with fake_clock() as clock:
            clock.return_value = 1000.0
            self.make(build=False)
            self.assertEqual(self.call("status")["count"], 5)
            self.pull("b" * 40)
            clock.return_value = 1020.0
            status = self.call("status")
            # the old index keeps serving while the new one is built
            self.assertEqual((status["state"], status["count"]),
                             ("updating", 5))
            self.assertEqual(len(self.call("materials")["materials"]), 3)
            self.settle()
        status = self.call("status")
        self.assertEqual((status["state"], status["count"]), ("ready", 6))

    def test_an_unchanged_head_does_not_reindex(self):
        build_library(self.path)
        set_head(self.path, "a" * 40)
        with fake_clock() as clock:
            clock.return_value = 1000.0
            self.make(build=False)
            with mock.patch.object(self.component, "_start") as start:
                clock.return_value = 1050.0
                self.call("status")
                self.call("materials")
            start.assert_not_called()

    def test_the_head_is_looked_at_most_every_ten_seconds(self):
        build_library(self.path)
        set_head(self.path, "a" * 40)
        with fake_clock() as clock:
            clock.return_value = 1000.0
            self.make(build=False)
            clock.return_value = 1020.0
            self.call("status")             # looks: nothing new
            self.pull("b" * 40)
            clock.return_value = 1025.0
            self.assertEqual(self.call("status")["state"], "ready")
            clock.return_value = 1031.0
            self.assertEqual(self.call("status")["state"], "updating")
            self.settle()
        self.assertEqual(self.call("status")["count"], 6)

    def test_a_clone_by_the_component_notes_the_update_manager(self):
        self.make(build=False, url="https://example/x.git")

        async def spawn(*args, **kwargs):
            build_library(self.path)
            return FakeProcess()

        with mock.patch.object(asyncio, "create_subprocess_exec", spawn):
            self.call("update")
            self.settle()
        self.assertIn("install.sh", self.call("status")["message"])


class PickTests(TaglibTestCase):
    def test_pick_prefers_a_tag_never_cloned(self):
        self.make()
        first = self.call("pick", material="PLA Basic", color="Black")
        self.assertEqual(first["uid"], "0A000001")
        self.assertTrue(first["fresh"])
        self.assertEqual(first["image"], make_image("0A000001").hex().upper())
        self.call("cloned", uid="0A000001")
        second = self.call("pick", material="PLA Basic", color="Black")
        self.assertEqual((second["uid"], second["fresh"]), ("0A000002", True))
        self.call("cloned", uid="0A000002")
        last = self.call("pick", material="PLA Basic", color="Black")
        self.assertEqual((last["uid"], last["fresh"]), ("0A000001", False))

    def test_pick_with_no_match_is_404(self):
        self.make()
        with self.assertRaises(ServerError) as caught:
            self.call("pick", material="ABS", color="Red")
        self.assertEqual(caught.exception.status, 404)
        self.assertIn("ABS", str(caught.exception))

    def test_endpoints_are_registered_with_plain_methods(self):
        self.make()
        methods = dict((path, m) for path, (m, _) in
                       self.server.endpoints.items())
        self.assertEqual(methods["/server/openams/taglib/status"], ["GET"])
        self.assertEqual(methods["/server/openams/taglib/pick"], ["GET"])
        self.assertEqual(methods["/server/openams/taglib/cloned"], ["POST"])
        self.assertEqual(methods["/server/openams/taglib/add"], ["POST"])
        self.assertEqual(methods["/server/openams/taglib/update"], ["POST"])


class ClonedTests(TaglibTestCase):
    def test_cloned_is_idempotent_validated_and_persisted(self):
        self.make()
        self.assertEqual(self.call("cloned", uid="0a000004"),
                         {"uid": "0A000004", "cloned": True})
        self.call("cloned", uid="0A000004")
        self.assertEqual(self.call("status")["cloned"], 1)
        self.assertEqual(self.store["openams_taglib"]["cloned"],
                         ["0A000004"])
        for bad in ("", "0A00", "0A00000G", "0A0000010"):
            with self.assertRaises(ServerError) as caught:
                self.call("cloned", uid=bad)
            self.assertEqual(caught.exception.status, 400)
            self.assertIn("uid", str(caught.exception))

    def test_cloned_survives_a_restart(self):
        self.make()
        self.call("cloned", uid="0A000001")
        self.make(build=False)
        self.assertEqual(self.call("status")["cloned"], 1)
        picked = self.call("pick", material="PLA Basic", color="Black")
        self.assertEqual(picked["uid"], "0A000002")


class AddTests(TaglibTestCase):
    def image(self, uid="0B000001"):
        return make_image(uid, 0x22).hex().upper()

    def test_add_joins_the_index_after_the_library(self):
        self.make()
        self.assertEqual(
            self.call("add", material="PLA Basic", color="Black",
                      image=self.image()),
            {"uid": "0B000001", "replaced": False, "count": 6})
        picks = [self.call("pick", material="PLA Basic", color="Black")]
        self.call("cloned", uid="0A000001")
        self.call("cloned", uid="0A000002")
        picks.append(self.call("pick", material="PLA Basic", color="Black"))
        self.assertEqual([p["uid"] for p in picks],
                         ["0A000001", "0B000001"])
        self.assertEqual(self.call("status")["captured"], 1)

    def test_add_replaces_a_library_entry_with_the_same_uid(self):
        self.make()
        result = self.call("add", material="PLA Basic", color="Orange",
                           image=self.image("0A000004"))
        self.assertEqual(result, {"uid": "0A000004", "replaced": True,
                                  "count": 5})
        self.assertEqual(self.call("colors", material="ABS")["colors"], [])
        picked = self.call("pick", material="PLA Basic", color="Orange")
        self.assertEqual(picked["image"], self.image("0A000004"))

    def test_captured_tags_are_persisted_and_restored(self):
        self.make()
        self.call("add", material="PETG", color="Teal", image=self.image())
        stored = self.store["openams_taglib"]["captured"]
        self.assertEqual(stored["0B000001"]["material"], "PETG")
        self.make(build=False)
        self.assertEqual(self.call("status")["captured"], 1)
        self.assertEqual(self.call("pick", material="PETG", color="Teal")
                         ["uid"], "0B000001")

    def test_add_validation_names_the_field(self):
        self.make()
        cases = [
            ({"material": "", "color": "Red", "image": self.image()},
             "material"),
            ({"material": "x" * 32, "color": "Red", "image": self.image()},
             "material"),
            ({"material": "PLA", "color": "", "image": self.image()},
             "color"),
            ({"material": "PLA", "color": "é" * 16,
              "image": self.image()}, "color"),
            ({"material": "PLA", "color": "Red", "image": "zz"}, "image"),
            ({"material": "PLA", "color": "Red", "image": "00" * 100},
             "image"),
            ({"material": "PLA", "color": "Red",
              "image": bad_bcc_image("0B000001").hex()}, "image"),
        ]
        for args, field in cases:
            with self.assertRaises(ServerError) as caught:
                self.call("add", **args)
            self.assertEqual(caught.exception.status, 400)
            self.assertIn(field, str(caught.exception))
        self.assertEqual(self.call("status")["captured"], 0)


class UpdateTests(TaglibTestCase):
    def test_update_pulls_then_reindexes(self):
        self.make()
        spawned = []

        async def spawn(*args, **kwargs):
            spawned.append(args)
            new = self.path / "Bambu Lab" / "ABS" / "Red" / "H"
            new.mkdir(parents=True)
            (new / "H-dump.bin").write_bytes(make_image("0A000008"))
            return FakeProcess()

        with mock.patch.object(asyncio, "create_subprocess_exec", spawn):
            status = self.call("update")
            self.assertEqual((status["state"], status["count"]),
                             ("updating", 5))
            self.settle()
        self.assertEqual(spawned, [("git", "-C", str(self.path), "pull",
                                    "--ff-only")])
        status = self.call("status")
        self.assertEqual((status["state"], status["count"]), ("ready", 6))

    def test_update_clones_when_the_checkout_is_missing(self):
        self.make(build=False, auto_clone=False, url="https://example/x.git")
        self.assertEqual(self.call("status")["state"], "missing")
        spawned = []

        async def spawn(*args, **kwargs):
            spawned.append(args)
            build_library(self.path)
            return FakeProcess()

        with mock.patch.object(asyncio, "create_subprocess_exec", spawn):
            self.call("update")
            self.settle()
        self.assertEqual(spawned, [("git", "clone", "--depth", "1",
                                    "https://example/x.git",
                                    str(self.path))])
        self.assertEqual(self.call("status")["count"], 5)

    def test_the_old_index_is_served_while_an_update_runs(self):
        self.make()
        process = FakeProcess()
        process.release = asyncio.Event()

        async def spawn(*args, **kwargs):
            return process

        with mock.patch.object(asyncio, "create_subprocess_exec", spawn):
            self.call("update")
            self.loop.run_until_complete(asyncio.sleep(0.01))
            reply = self.call("materials")
            self.assertEqual((reply["state"], reply["count"]),
                             ("updating", 5))
            self.assertEqual(len(reply["materials"]), 3)
            self.assertEqual(
                self.call("pick", material="ABS", color="Black")["uid"],
                "0A000004")
            self.call("cloned", uid="0A000004")
            self.call("add", material="PETG", color="Teal",
                      image=make_image("0B000001").hex())
            self.assertEqual(self.call("status")["count"], 6)
            process.release.set()
            self.settle()
        self.assertEqual(self.call("status")["state"], "ready")

    def test_a_second_update_while_one_runs_is_409(self):
        self.make()
        process = FakeProcess()
        process.release = asyncio.Event()

        async def spawn(*args, **kwargs):
            return process

        with mock.patch.object(asyncio, "create_subprocess_exec", spawn):
            self.call("update")
            self.loop.run_until_complete(asyncio.sleep(0.01))
            with self.assertRaises(ServerError) as caught:
                self.call("update")
            self.assertEqual(caught.exception.status, 409)
            process.release.set()
            self.settle()
        self.assertEqual(self.call("status")["state"], "ready")

    def test_a_failed_update_keeps_the_old_index(self):
        self.make()

        async def spawn(*args, **kwargs):
            return FakeProcess(1, b"fatal: Not possible to fast-forward\n")

        with mock.patch.object(asyncio, "create_subprocess_exec", spawn):
            self.call("update")
            self.settle()
        status = self.call("status")
        self.assertEqual((status["state"], status["count"]), ("ready", 5))
        self.assertIn("fast-forward", status["message"])
        self.assertEqual(
            self.call("pick", material="ABS", color="Black")["uid"],
            "0A000004")

    def test_a_failed_first_clone_is_an_error_state(self):
        self.make(build=False)

        async def spawn(*args, **kwargs):
            return FakeProcess(1, b"fatal: unable to access\n")

        with mock.patch.object(asyncio, "create_subprocess_exec", spawn):
            self.call("update")
            self.settle()
        status = self.call("status")
        self.assertEqual(status["state"], "error")
        self.assertIn("unable to access", status["message"])
        with self.assertRaises(ServerError) as caught:
            self.call("pick", material="ABS", color="Black")
        self.assertEqual(caught.exception.status, 404)
        self.assertIn("unable to access", str(caught.exception))

    def test_git_that_cannot_run_is_reported(self):
        self.make()

        async def spawn(*args, **kwargs):
            raise FileNotFoundError(2, "No such file", "git")

        with mock.patch.object(asyncio, "create_subprocess_exec", spawn):
            self.call("update")
            self.settle()
        self.assertEqual(self.call("status")["state"], "ready")
        self.assertIn("git", self.call("status")["message"])


class NotReadyTests(TaglibTestCase):
    def test_missing_without_auto_clone_is_empty(self):
        self.make(build=False, auto_clone=False)
        status = self.call("status")
        self.assertEqual(status["state"], "missing")
        self.assertEqual(status["count"], 0)
        self.assertIsNotNone(status["message"])
        self.assertEqual(self.call("materials"),
                         {"state": "missing", "count": 0, "materials": [],
                          "message": "The tag library is not downloaded"})
        self.assertEqual(self.call("colors", material="ABS")["colors"], [])
        with self.assertRaises(ServerError) as caught:
            self.call("pick", material="ABS", color="Black")
        self.assertEqual(caught.exception.status, 404)
        self.assertIn("missing", str(caught.exception))

    def test_auto_clone_starts_on_first_use_of_any_endpoint(self):
        self.make(build=False)
        spawned = []
        process = FakeProcess()
        process.release = asyncio.Event()

        async def spawn(*args, **kwargs):
            spawned.append(args)
            return process

        with mock.patch.object(asyncio, "create_subprocess_exec", spawn):
            reply = self.call("materials")
            self.assertEqual((reply["state"], reply["materials"]),
                             ("updating", []))
            self.assertIn("message", reply)
            self.loop.run_until_complete(asyncio.sleep(0.01))
            self.assertEqual(self.call("status")["state"], "updating")
            with self.assertRaises(ServerError) as caught:
                self.call("pick", material="ABS", color="Black")
            self.assertIn("downloaded", str(caught.exception))
            build_library(self.path)
            process.release.set()
            self.settle()
        self.assertEqual(len(spawned), 1)
        self.assertEqual(self.call("status")["state"], "ready")

    def test_status_with_auto_clone_reports_updating(self):
        self.make(build=False)
        process = FakeProcess()
        process.release = asyncio.Event()

        async def spawn(*args, **kwargs):
            return process

        with mock.patch.object(asyncio, "create_subprocess_exec", spawn):
            self.assertEqual(self.call("status")["state"], "updating")
            process.release.set()
            self.settle()


try:
    import aiohttp
except ImportError:  # pragma: no cover - environment dependent
    aiohttp = None

if aiohttp is not None:
    sys.path.insert(0, str(ROOT / "tools"))
    from mock_moonraker.taglib import Taglib  # noqa: E402


@unittest.skipIf(aiohttp is None, "aiohttp is not installed")
class MockTaglibTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        build_library(Path(self.tmp.name))
        taglib = Taglib(self.tmp.name)
        taglib.index()
        app = aiohttp.web.Application()
        taglib.add_routes(app.router)
        self.runner = aiohttp.web.AppRunner(app)
        await self.runner.setup()
        site = aiohttp.web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = self.runner.addresses[0][1]
        self.base = "http://127.0.0.1:%d/server/openams/taglib/" % port
        self.session = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.session.close()
        await self.runner.cleanup()

    async def test_results_and_errors_are_wrapped_like_moonraker(self):
        async with self.session.get(self.base + "status") as reply:
            self.assertEqual(reply.status, 200)
            body = await reply.json()
        self.assertEqual(body["result"]["count"], 5)
        async with self.session.get(
                self.base + "colors", params={"material": "ABS"}) as reply:
            self.assertEqual((await reply.json())["result"]["colors"],
                             [["Black", 1, 1]])
        async with self.session.get(
                self.base + "pick",
                params={"material": "ABS", "color": "Red"}) as reply:
            self.assertEqual(reply.status, 404)
            error = (await reply.json())["error"]
        self.assertEqual(error["code"], 404)
        self.assertIn("ABS", error["message"])

    async def test_posts_take_query_form_or_json(self):
        async with self.session.post(
                self.base + "cloned", params={"uid": "0A000004"}) as reply:
            self.assertEqual((await reply.json())["result"],
                             {"uid": "0A000004", "cloned": True})
        async with self.session.post(
                self.base + "cloned", json={"uid": "nope"}) as reply:
            self.assertEqual(reply.status, 400)
        async with self.session.get(
                self.base + "pick",
                params={"material": "ABS", "color": "Black"}) as reply:
            self.assertFalse((await reply.json())["result"]["fresh"])


if __name__ == "__main__":
    unittest.main()
