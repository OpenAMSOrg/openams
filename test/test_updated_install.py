"""An install that an update moved past install.sh (the 2026-11-10 incident).

install.sh links each OpenAMS module into klippy/extras when it runs. An update
through Moonraker's update manager is only a git pull of the checkout, so a
module a later release adds has no link of its own, and a sibling that imports
it (`from . import tag_identity`) stopped Klipper with "No module named ...".

Every module that imports a sibling must load it from the checkout instead.
Each case builds a links-only klippy tree whose extras package holds the links
an older install.sh made, imports the module the way Klipper does
(`extras.<name>`), and checks that every sibling it names was loaded from the
checkout's own file, once, under the package's name.

    python3 -m unittest discover -s test -p 'test_updated_install.py' -v
"""

import ast
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")

# Klipper's own modules that OpenAMS modules import, as stand-ins: the extras
# package's (`from . import bus`) and klippy's top level (`import mcu`).
KLIPPER_EXTRAS = {
    "bus": "def MCU_I2C_from_config(*a, **k):\n    return None\n",
    "tmc_uart": "",
}
KLIPPER_TOP = {"mcu": "class error(Exception):\n    pass\n"}

# A sibling a module reaches by name rather than by an import statement. Such
# a module is only ever imported by its loader (oams_ext_local imports
# oams_ext, which imports it back by name), so it is entered through it.
DYNAMIC = {"oams_ext": ("oams_ext_local",)}
LOADER = {target: loader for loader, targets in DYNAMIC.items() for target in targets}


def module_names():
    return sorted(name[:-3] for name in os.listdir(SRC)
                  if name.endswith(".py") and not name.startswith("klipper_mmu"))


def siblings(name):
    """The OpenAMS modules src/<name>.py imports with a relative import,
    lazy ones included."""
    with open(os.path.join(SRC, name + ".py")) as handle:
        tree = ast.parse(handle.read())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            found.update([node.module] if node.module
                         else [alias.name for alias in node.names])
    found.update(DYNAMIC.get(name, ()))
    return sorted(n for n in found if os.path.exists(os.path.join(SRC, n + ".py")))


def importers():
    """Every src module that imports a sibling, with the siblings."""
    return {name: siblings(name) for name in module_names() if siblings(name)}


CHECK = """
import importlib, os, sys
importlib.import_module("extras." + ENTRY)
importlib.import_module("extras." + MODULE)
for name in SIBLINGS:
    module = importlib.import_module("extras." + name)
    where = os.path.realpath(module.__file__)
    assert where == os.path.join(SRC, name + ".py"), (name, where)
    assert name not in sys.modules, name + " was loaded a second time, outside the package"
print("ok")
"""


def run_with_links(tmp, linked, code):
    """Run @p code in a klippy tree under @p tmp whose extras package links
    only the src modules in @p linked."""
    extras = os.path.join(tmp, "extras")
    os.mkdir(extras)
    open(os.path.join(extras, "__init__.py"), "w").close()
    for name, body in KLIPPER_EXTRAS.items():
        with open(os.path.join(extras, name + ".py"), "w") as handle:
            handle.write(body)
    for name, body in KLIPPER_TOP.items():
        with open(os.path.join(tmp, name + ".py"), "w") as handle:
            handle.write(body)
    for name in linked:
        os.symlink(os.path.join(SRC, name + ".py"),
                   os.path.join(extras, name + ".py"))
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return subprocess.run([sys.executable, "-c", code], cwd=tmp, env=env,
                          capture_output=True, text=True, timeout=60)


class UpdatedInstallTests(unittest.TestCase):

    def test_the_graph_is_found(self):
        # the walk itself: the modules that import siblings today
        graph = importers()
        self.assertIn("tag_identity", graph["oams_tags"])
        self.assertIn("oams_tags", graph["oams"])
        self.assertIn("helix_backend", graph["oams_manager"])   # a lazy import

    def test_every_sibling_loads_from_the_checkout_without_a_link(self):
        # The oldest install: only the module itself is linked (with its
        # loader, for one loaded by name), so every sibling it names, and every
        # sibling of those, has no link.
        src = os.path.realpath(SRC)
        for name, wanted in sorted(importers().items()):
            entry = LOADER.get(name, name)
            with self.subTest(module=name), tempfile.TemporaryDirectory() as tmp:
                code = ("ENTRY = %r\nMODULE = %r\nSIBLINGS = %r\nSRC = %r\n"
                        % (entry, name, [w for w in wanted if w != entry], src) + CHECK)
                run = run_with_links(tmp, sorted({name, entry}), code)
                self.assertEqual(run.stdout.strip(), "ok", run.stderr[-2000:])

    def test_a_sibling_new_to_the_release_beside_the_old_links(self):
        # The 2026-11-10 case: every module linked but the new one. A module
        # loaded through its link and one loaded from the checkout share the
        # one copy of tag_identity.
        if not os.path.exists(os.path.join(SRC, "tag_identity.py")):
            self.skipTest("this edition has no tag_identity")
        codec = os.path.exists(os.path.join(SRC, "tag_codec.py"))
        code = ("import extras.oams_tags as t, extras.mfrc522 as m, sys\n"
                "tag = {'format': 'bambu', 'uid': '04a1b2c3',"
                " 'bambu': {'tray_uid': '0123456789ABCDEF0123456789abcdef'}}\n"
                "assert t.tag_identity.spool_key(tag) == 'bambu:0123456789abcdef0123456789abcdef'\n"
                "assert m.tag_identity is t.tag_identity is sys.modules['extras.tag_identity']\n"
                + ("import extras.tag_codec as c\n"
                   "assert c.spool_key is t.tag_identity.spool_key\n" if codec else "")
                + "print('ok')\n")
        with tempfile.TemporaryDirectory() as tmp:
            run = run_with_links(
                tmp, [n for n in module_names() if n != "tag_identity"], code)
        self.assertEqual(run.stdout.strip(), "ok", run.stderr[-2000:])

    def test_a_linked_module_is_the_one_loaded(self):
        # The checkout is searched after klippy/extras, never before it: a
        # module that has its own link (or a Klipper module of the same name)
        # is the one Klipper gets.
        code = ("import extras.oams_tags, extras.bus as bus, os\n"
                "assert os.path.dirname(bus.__file__) == os.path.abspath('extras'), bus.__file__\n"
                "print('ok')\n")
        with tempfile.TemporaryDirectory() as tmp:
            run = run_with_links(tmp, ["oams_tags"], code)
        self.assertEqual(run.stdout.strip(), "ok", run.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
