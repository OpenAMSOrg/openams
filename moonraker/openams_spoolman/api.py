# The Spoolman request interface used by the openams_spoolman component (C4 §5).
#
# Everything goes through one call, ``request(method, path, query, body)``, so a
# real Moonraker proxy and the in-memory fake below are interchangeable: C4 §5
# says the component talks to Spoolman only via Moonraker's ``[spoolman]`` proxy
# (``POST /server/spoolman/proxy``); ``DirectSpoolmanAPI`` is that same interface
# over plain HTTP, for tests and the host-side gate.

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request


class SpoolmanUnavailable(Exception):
    """Spoolman could not be reached: a connection error or a timeout."""


class SpoolmanError(Exception):
    """Spoolman answered with an HTTP error status."""

    def __init__(self, status, message=None):
        self.status = status
        self.message = message
        super().__init__("Spoolman error %s: %s" % (status, message))


class SpoolmanAPI(object):
    """The interface the ops in ``ops.py`` use towards Spoolman."""

    async def request(self, method, path, query=None, body=None):
        """Return the decoded JSON for ``method`` on ``path`` (starts /v1/).

        ``query`` is a dict of URL parameters; ``body`` is a JSON object.
        """
        raise NotImplementedError


class DirectSpoolmanAPI(SpoolmanAPI):
    """Spoolman over HTTP, e.g. ``http://127.0.0.1:7912/api``."""

    def __init__(self, base_url, timeout=5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def request(self, method, path, query=None, body=None):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._request, method, path, query, body)

    def _request(self, method, path, query, body):
        url = self.base_url + path
        if query:
            url = url + "?" + urllib.parse.urlencode(query)
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            raise SpoolmanError(error.code, _error_message(error))
        except (urllib.error.URLError, OSError) as error:
            raise SpoolmanUnavailable(str(error))
        if not payload:
            return None
        return json.loads(payload)


def _error_message(error):
    """Spoolman's JSON ``message`` for an HTTP error, else the reason text."""
    try:
        body = json.loads(error.read().decode("utf-8"))
    except (ValueError, OSError):
        return str(error)
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        return body["message"]
    return str(error)


class FakeSpoolman(SpoolmanAPI):
    """In memory Spoolman: the routes and shapes ``ops.py`` uses (C4 §5).

    Values follow Spoolman's REST shapes, including the JSON-encoded extra
    strings; ``online = False`` makes every call raise ``SpoolmanUnavailable``.
    """

    def __init__(self):
        self.online = True
        self.vendors = []
        self.filaments = []
        self.spools = []
        self.fields = {"spool": [], "filament": []}
        self.calls = []
        self._ids = {"vendor": 0, "filament": 0, "spool": 0}

    async def request(self, method, path, query=None, body=None):
        if not self.online:
            raise SpoolmanUnavailable("Spoolman is offline")
        self.calls.append((method, path, dict(query or {}), body))
        parts = [part for part in path.split("/") if part]
        if parts and parts[0] == "v1":
            parts = parts[1:]
        if not parts:
            raise SpoolmanError(404, "unknown path %s" % path)
        head, rest = parts[0], parts[1:]
        if head == "info":
            return {"version": "0.27.0", "debug_mode": False}
        if head == "field":
            return self._field(method, rest, body)
        if head == "vendor":
            return self._vendor(method, rest, query, body)
        if head == "filament":
            return self._filament(method, rest, query, body)
        if head == "spool":
            return self._spool(method, rest, query, body)
        raise SpoolmanError(404, "unknown path %s" % path)

    def _field(self, method, rest, body):
        entity = rest[0] if rest else None
        if entity not in self.fields:
            raise SpoolmanError(404, "unknown entity %s" % entity)
        if method == "GET" and len(rest) == 1:
            return [dict(field) for field in self.fields[entity]]
        if method == "POST" and len(rest) == 2:
            body = body or {}
            entry = {"key": rest[1], "name": body.get("name", rest[1]),
                     "field_type": body.get("field_type", "text"),
                     "entity_type": entity}
            existing = [f for f in self.fields[entity] if f["key"] == entry["key"]]
            if existing:
                existing[0].update(entry)
            else:
                self.fields[entity].append(entry)
            return [dict(field) for field in self.fields[entity]]
        raise SpoolmanError(405, "field %s %s" % (method, rest))

    def _vendor(self, method, rest, query, body):
        if method == "GET" and not rest:
            name = (query or {}).get("name")
            return [self._vendor_view(v) for v in self.vendors
                    if name is None or v["name"] == name]
        if method == "POST" and not rest:
            vendor = {"id": self._next_id("vendor"),
                      "name": (body or {}).get("name"), "extra": {}}
            self.vendors.append(vendor)
            return self._vendor_view(vendor)
        raise SpoolmanError(404, "unknown vendor path")

    def _filament(self, method, rest, query, body):
        if method == "GET" and not rest:
            vendor_name = (query or {}).get("vendor.name")
            material = (query or {}).get("material")
            return [self._filament_view(f) for f in self.filaments
                    if (material is None or f["material"] == material)
                    and (vendor_name is None
                         or (f["vendor"] or {}).get("name") == vendor_name)]
        if method == "POST" and not rest:
            body = body or {}
            vendor = self._find(self.vendors, body.get("vendor_id"))
            filament = {
                "id": self._next_id("filament"), "name": body.get("name"),
                "vendor": vendor, "material": body.get("material"),
                "density": body.get("density"), "diameter": body.get("diameter"),
                "weight": body.get("weight"), "color_hex": body.get("color_hex"),
                "settings_extruder_temp": body.get("settings_extruder_temp"),
                "settings_bed_temp": body.get("settings_bed_temp"), "extra": {}}
            self.filaments.append(filament)
            return self._filament_view(filament)
        if method == "PATCH" and len(rest) == 1:
            filament = self._find(self.filaments, rest[0])
            self._apply_patch(filament, body)
            return self._filament_view(filament)
        raise SpoolmanError(404, "unknown filament path")

    def _spool(self, method, rest, query, body):
        if method == "GET" and not rest:
            return [self._spool_view(s) for s in self.spools if not s["archived"]]
        if method == "GET" and len(rest) == 1:
            return self._spool_view(self._find(self.spools, rest[0]))
        if method == "POST" and not rest:
            body = body or {}
            initial = body.get("initial_weight")
            used = None if initial is None or body.get("remaining_weight") is None \
                else initial - body["remaining_weight"]
            spool = {
                "id": self._next_id("spool"),
                "filament": self._find(self.filaments, body.get("filament_id")),
                "initial_weight": initial,
                "remaining_weight": body.get("remaining_weight"),
                "used_weight": used, "location": body.get("location", ""),
                "lot_nr": body.get("lot_nr", ""), "archived": False,
                "extra": dict(body.get("extra") or {})}
            self.spools.append(spool)
            return self._spool_view(spool)
        if method == "PATCH" and len(rest) == 1:
            spool = self._find(self.spools, rest[0])
            body = dict(body or {})
            if "filament_id" in body:
                spool["filament"] = self._find(
                    self.filaments, body.pop("filament_id"))
            self._apply_patch(spool, body)
            self._settle_weights(spool, body)
            return self._spool_view(spool)
        if method == "DELETE" and len(rest) == 1:
            self.spools.remove(self._find(self.spools, rest[0]))
            return None
        raise SpoolmanError(404, "unknown spool path")

    @staticmethod
    def _settle_weights(spool, body):
        """Keep ``used_weight``/``remaining_weight`` consistent as Spoolman
        does: a written ``used_weight`` wins, else a written
        ``remaining_weight``, each against the spool's initial weight."""
        initial = spool.get("initial_weight")
        if initial is None:
            return
        if "used_weight" in body:
            spool["remaining_weight"] = initial - body["used_weight"]
        elif "remaining_weight" in body:
            spool["used_weight"] = initial - body["remaining_weight"]
        elif "initial_weight" in body and spool.get("used_weight") is not None:
            spool["remaining_weight"] = initial - spool["used_weight"]

    def _next_id(self, kind):
        self._ids[kind] += 1
        return self._ids[kind]

    @staticmethod
    def _find(items, raw_id):
        for item in items:
            if str(item["id"]) == str(raw_id):
                return item
        raise SpoolmanError(404, "no object with ID %s found." % raw_id)

    @staticmethod
    def _apply_patch(obj, body):
        for key, value in (body or {}).items():
            obj[key] = value

    @staticmethod
    def _vendor_view(vendor):
        return dict(vendor, extra=dict(vendor["extra"]))

    def _filament_view(self, filament):
        vendor = filament["vendor"]
        return dict(filament, vendor=None if vendor is None
                    else self._vendor_view(vendor), extra=dict(filament["extra"]))

    def _spool_view(self, spool):
        return dict(spool, filament=self._filament_view(spool["filament"]),
                    extra=dict(spool["extra"]))
