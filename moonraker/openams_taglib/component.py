# SPDX-License-Identifier: MIT
"""The genuine Bambu Lab tag library in Moonraker, for the display's tag writer.

The library is a checkout of github.com/queengooborg/Bambu-Lab-RFID-Library
(about 7,000 tag dumps, about 200 MB). The display is too small to hold it, so
this component indexes the checkout and serves it, one small answer at a time.

Config section ``[openams_taglib]``, all options optional:

    path        the library checkout (default ~/Bambu-Lab-RFID-Library)
    url         where it is cloned from
    auto_clone  clone on first use when ``path`` does not exist (default True)

Endpoints (each is also a JSON-RPC method, e.g. ``server.openams.taglib.status``):

GET  /server/openams/taglib/status
    ``{"state": "ready"|"missing"|"updating"|"error", "count", "materials",
    "colors", "cloned", "captured", "skipped", "path", "revision", "message"}``.
    ``revision`` is the checkout's short git hash; ``message`` is human text
    (the git error after a failed update). When Moonraker's update manager
    pulls the checkout, the next request notices the new HEAD commit (looked
    at most every 10 s) and re-indexes in the background. With ``auto_clone`` on, this and
    every endpoint below starts the clone when the library is missing.
GET  /server/openams/taglib/materials
    ``{"state", "count", "materials": ["ABS", "PLA Basic", ...]}``; the list
    is empty only when nothing is indexed; ``"message"`` is added unless the
    state is "ready" (as in status).
GET  /server/openams/taglib/colors?material=<name>
    ``{"material", "colors": [[color, count, fresh], ...]}``; ``fresh`` counts
    the tags of that color not yet cloned. An unknown material gives [].
GET  /server/openams/taglib/pick?material=<m>&color=<c>
    ``{"uid", "fresh", "image"}``: the first tag never cloned, else the first
    tag with ``fresh`` false; ``image`` is 2048 uppercase hex digits. 404 when
    there is none or the library is not ready.
POST /server/openams/taglib/cloned   uid=<8 hex digits>
    ``{"uid", "cloned": true}``; remembers that the tag was cloned (idempotent).
POST /server/openams/taglib/add   material=<m> color=<c> image=<2048 hex>
    ``{"uid", "replaced", "count"}``: add a tag the display read from a
    genuine spool. A captured UID that is also in the library replaces it.
POST /server/openams/taglib/update
    Clone the library, or pull it when it exists, then re-index. Answers the
    status at once (state "updating"); the outcome shows in ``state`` and
    ``message``. 409 while an update runs.

Indexing 7,000 files runs in an executor, never on the event loop. The cloned
set and the captured tags live in the ``openams_taglib`` database namespace.
Endpoints are registered with plain string request types so this imports
nothing from Moonraker itself; the indexing is in ``library.py``.
"""

import asyncio
import logging
import os
from pathlib import Path
import time

from . import library

_LOGGER = logging.getLogger(__name__)

DB_NAMESPACE = "openams_taglib"
CLONED_KEY = "cloned"
CAPTURED_KEY = "captured"
DEFAULT_PATH = "~/Bambu-Lab-RFID-Library"
HEAD_CHECK_SECONDS = 10.0
UPDATER_NOTE = ("Downloaded here; run ~/openams/install.sh to add it to "
                "Moonraker's update manager")
DEFAULT_URL = "https://github.com/queengooborg/Bambu-Lab-RFID-Library.git"


class OpenAmsTaglib(object):
    def __init__(self, config):
        self.server = config.get_server()
        self.eventloop = self.server.get_event_loop()
        self.path = Path(os.path.expanduser(
            config.get("path", DEFAULT_PATH)))
        self.url = config.get("url", DEFAULT_URL)
        self.auto_clone = config.getboolean("auto_clone", True)
        self.library = library.TagLibrary(self.path)
        self._db = None
        self._task = None
        self._head = None           # the HEAD commit the index was built from
        self._head_checked = float("-inf")
        self._note = None           # what status says once the index is ready

        database = self.server.lookup_component("database", None)
        if database is not None:
            self._db = database.register_local_namespace(DB_NAMESPACE)

        self.server.register_endpoint(
            "/server/openams/taglib/status", ["GET"], self._handle_status)
        self.server.register_endpoint(
            "/server/openams/taglib/materials", ["GET"],
            self._handle_materials)
        self.server.register_endpoint(
            "/server/openams/taglib/colors", ["GET"], self._handle_colors)
        self.server.register_endpoint(
            "/server/openams/taglib/pick", ["GET"], self._handle_pick)
        self.server.register_endpoint(
            "/server/openams/taglib/cloned", ["POST"], self._handle_cloned)
        self.server.register_endpoint(
            "/server/openams/taglib/add", ["POST"], self._handle_add)
        self.server.register_endpoint(
            "/server/openams/taglib/update", ["POST"], self._handle_update)

    async def component_init(self):
        """Moonraker's post-load hook: restore the database, then index the
        checkout when there is one."""
        if self._db is not None:
            cloned = await self._db.get(CLONED_KEY, None)
            captured = await self._db.get(CAPTURED_KEY, None)
            self.library.load_state(cloned, captured)
        if self.path.is_dir():
            self._start(self._index_only)

    # -- the work ---------------------------------------------------------

    def _start(self, work):
        self.library.state = "updating"
        self.library.message = None
        self._task = self.eventloop.create_task(work())

    def _running(self):
        return self._task is not None and not self._task.done()

    async def _index(self):
        # Moonraker's EventLoop runs blocking work with run_in_thread; it has
        # no run_in_executor of its own.
        head = await self.eventloop.run_in_thread(library.read_head, self.path)
        entries, skipped = await self.eventloop.run_in_thread(library.scan, self.path)
        revision = await self.eventloop.run_in_thread(library.revision, self.path)
        self.library.load(entries, skipped, revision)
        self._head = head
        self.library.state = "ready"
        self.library.message = self._note

    def _failed(self, message):
        """A failed update keeps the old index, if there is one."""
        self.library.state = "ready" if self.library.indexed else "error"
        self.library.message = message

    async def _index_only(self):
        self.library.message = "Indexing the tag library"
        try:
            await self._index()
        except Exception as error:
            _LOGGER.exception("openams_taglib: indexing failed")
            self._failed("Indexing failed: %s" % error)

    async def _git(self, *args):
        """Run git; raises ``RuntimeError`` with git's own text on failure."""
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        try:
            process = await asyncio.create_subprocess_exec(
                "git", *args, env=env, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
        except OSError as error:
            raise RuntimeError("Cannot run git: %s" % error)
        output, _ = await process.communicate()
        if process.returncode != 0:
            text = output.decode("utf-8", "replace").strip()
            raise RuntimeError(text or "git exited with %s"
                               % process.returncode)

    async def _run_update(self):
        cloning = not self.path.is_dir()
        self.library.message = "Downloading the tag library" if cloning \
            else "Updating the tag library"
        try:
            if cloning:
                await self._git("clone", "--depth", "1", self.url,
                                str(self.path))
            else:
                await self._git("-C", str(self.path), "pull", "--ff-only")
            self.library.message = "Indexing the tag library"
            if cloning:
                self._note = UPDATER_NOTE
            await self._index()
        except Exception as error:
            _LOGGER.warning("openams_taglib: update failed: %s", error)
            self._failed(str(error))

    def _use(self):
        """Every endpoint's first step: a missing library is downloaded on
        first use when ``auto_clone`` is on."""
        if self.library.state == "missing" and self.auto_clone \
                and not self._running():
            self._start(self._run_update)
        else:
            self._check_head()

    def _check_head(self):
        """Moonraker's update manager pulls the checkout behind our back: when
        its HEAD is no longer the commit indexed, re-index in the background
        (the old index keeps serving). Looked at most every 10 s."""
        now = time.monotonic()
        if self._running() or not self.library.indexed \
                or now - self._head_checked < HEAD_CHECK_SECONDS:
            return
        self._head_checked = now
        head = library.read_head(self.path)
        if head is not None and head != self._head:
            self._start(self._index_only)

    def _save(self, key, value):
        if self._db is not None:
            self._db.insert(key, value)     # fire and forget

    def _fail(self, error):
        return self.server.error(str(error), error.status)

    # -- endpoints --------------------------------------------------------

    async def _handle_status(self, web_request):
        self._use()
        return self.library.status()

    async def _handle_materials(self, web_request):
        self._use()
        return self.library.materials()

    async def _handle_colors(self, web_request):
        self._use()
        return self.library.colors(web_request.get_str("material", ""))

    async def _handle_pick(self, web_request):
        self._use()
        try:
            return self.library.pick(web_request.get_str("material", ""),
                                     web_request.get_str("color", ""))
        except library.LibraryError as error:
            raise self._fail(error)

    async def _handle_cloned(self, web_request):
        self._use()
        try:
            result = self.library.mark_cloned(web_request.get_str("uid", ""))
        except library.LibraryError as error:
            raise self._fail(error)
        self._save(CLONED_KEY, self.library.cloned_list())
        return result

    async def _handle_add(self, web_request):
        self._use()
        try:
            result = self.library.add(web_request.get_str("material", ""),
                                      web_request.get_str("color", ""),
                                      web_request.get_str("image", ""))
        except library.LibraryError as error:
            raise self._fail(error)
        self._save(CAPTURED_KEY, self.library.captured_dict())
        return result

    async def _handle_update(self, web_request):
        if self._running():
            raise self.server.error(
                "The tag library is already updating", 409)
        self._start(self._run_update)
        return self.library.status()
