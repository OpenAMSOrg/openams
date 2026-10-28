"""The mock's tag library: the `openams_taglib` component's five GET/POST
routes over HTTP, served from the real `library.py` against a local checkout.

Results are wrapped as Moonraker does: HTTP 200 with `{"result": <object>}`,
an error as its HTTP status with `{"error": {"code": n, "message": "..."}}`.
The database is in memory and nothing is cloned: the checkout must exist.
"""

import os
import sys
from pathlib import Path

from aiohttp import web

# The component lives beside `tools/`, so the mock reaches the real library
# logic the same way the component's own tests do.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MOONRAKER = os.path.join(_ROOT, "moonraker")
if _MOONRAKER not in sys.path:
    sys.path.insert(0, _MOONRAKER)

from openams_taglib import library  # noqa: E402  (path set above)

PREFIX = "/server/openams/taglib/"


class Taglib(object):
    """`library.TagLibrary` indexed from `path`, with the HTTP routes."""

    def __init__(self, path):
        self.path = Path(os.path.expanduser(str(path)))
        self.library = library.TagLibrary(self.path)

    def index(self):
        """Scan the checkout (blocking); a missing one is state "missing"."""
        if not self.path.is_dir():
            self.library.state = "missing"
            return
        entries, skipped = library.scan(self.path)
        self.library.load(entries, skipped, library.revision(self.path))
        self.library.state = "ready"
        self.library.message = None

    def add_routes(self, router):
        for name in ("status", "materials", "colors", "pick"):
            router.add_get(PREFIX + name, self._route(name))
        for name in ("cloned", "add", "update"):
            router.add_post(PREFIX + name, self._route(name))

    def _route(self, name):
        async def handle(request):
            args = dict(request.query)
            if request.method == "POST":
                args.update(await self._body(request))
            try:
                result = getattr(self, "_" + name)(args)
            except library.LibraryError as error:
                return web.json_response(
                    {"error": {"code": error.status, "message": str(error)}},
                    status=error.status)
            return web.json_response({"result": result})
        return handle

    @staticmethod
    async def _body(request):
        """The POST parameters: a JSON object body, else the form fields."""
        if request.can_read_body and "json" in (request.content_type or ""):
            try:
                body = await request.json()
            except ValueError:
                return {}
            return body if isinstance(body, dict) else {}
        return dict(await request.post())

    def _status(self, args):
        return self.library.status()

    def _materials(self, args):
        return self.library.materials()

    def _colors(self, args):
        return self.library.colors(args.get("material", ""))

    def _pick(self, args):
        return self.library.pick(args.get("material", ""),
                                 args.get("color", ""))

    def _cloned(self, args):
        return self.library.mark_cloned(args.get("uid", ""))

    def _add(self, args):
        return self.library.add(args.get("material", ""),
                                args.get("color", ""), args.get("image", ""))

    def _update(self, args):
        """The mock never clones: an update re-reads the checkout."""
        self.index()
        return self.library.status()
