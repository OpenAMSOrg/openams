"""The mock's in-memory Spoolman (C4 §5): the vendors, filaments and spools
that `server.spoolman.proxy` answers and that a spool edit writes.

It is the component's own `api.FakeSpoolman` loaded with the fixture's data,
so a UI sees the same JSON shapes the real component reads and an edit runs
through the real `edit.apply`. Seeded from the fixture's `spoolman_responses`
collections (`GET /v1/spool`, `/v1/vendor`, `/v1/filament`, what the component
pre-warms with, C4 §6) and from the bays the fixture's component status links.
"""

import os
import sys

# The component lives beside `tools/`, so the mock reaches the real `edit`,
# `ops` and `api` the same way the component's own tests do.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MOONRAKER = os.path.join(_ROOT, "moonraker")
if _MOONRAKER not in sys.path:
    sys.path.insert(0, _MOONRAKER)

from openams_spoolman import api, edit, ops  # noqa: E402  (path set above)


class Spoolman(api.FakeSpoolman):
    """`api.FakeSpoolman` with the fixture's Spoolman loaded into it."""

    def seed_responses(self, responses):
        """Import the collections a fixture already answers over the proxy.

        Only the `GET` list responses are imported: a per-id response
        (`GET /v1/spool/1`) stays the proxy's fallback, unchanged.
        """
        for key in sorted(responses or {}):
            method, _, path = key.partition(" ")
            value = responses[key]
            if method.upper() != "GET" or not isinstance(value, list):
                continue
            parts = [part for part in path.split("/") if part]
            if parts[:1] != ["v1"]:
                continue
            for row in value:
                if parts[1:] == ["spool"]:
                    self._import_spool(row)
                elif parts[1:] == ["vendor"]:
                    self._import_vendor(row)
                elif parts[1:] == ["filament"]:
                    self._import_filament(row)

    def seed_bays(self, component_status, units):
        """Import one spool per bay the fixture's component status links."""
        bays = component_status.get("bays") or {}
        for unit, status in (units or {}).items():
            for bay in status.get("bays") or []:
                link = bay.get("sp") or {}
                if link.get("id") is None:
                    continue
                spool_id = int(link["id"])
                if self._has(self.spools, spool_id):
                    continue
                entry = bays.get("%s-%s" % (unit, bay.get("id"))) or {}
                summary = entry.get("spool")
                if isinstance(summary, dict):
                    self.spools.append(self._spool_from_summary(
                        spool_id, summary, "%s-%s" % (unit, bay.get("id"))))
        self._raise_ids()

    # -- views the proxy serves -----------------------------------------

    def spool_view(self, spool_id):
        return self._spool_view(self._find(self.spools, spool_id))

    def spools_view(self):
        return [self._spool_view(spool) for spool in self.spools
                if not spool.get("archived")]

    def vendors_view(self, name=None):
        return [self._vendor_view(vendor) for vendor in self.vendors
                if name is None or vendor.get("name") == name]

    # -- seeding ---------------------------------------------------------

    def _import_vendor(self, row):
        row = row or {}
        if self._has(self.vendors, row.get("id")):
            return self._match(self.vendors, row["id"])
        vendor = {"id": int(row["id"]) if row.get("id") is not None
                  else self._next_id("vendor"),
                  "name": row.get("name"), "extra": dict(row.get("extra") or {})}
        self.vendors.append(vendor)
        return vendor

    def _import_filament(self, row):
        row = row or {}
        if self._has(self.filaments, row.get("id")):
            return self._match(self.filaments, row["id"])
        filament = {
            "id": int(row["id"]) if row.get("id") is not None
            else self._next_id("filament"),
            "name": row.get("name"),
            "vendor": self._import_vendor(row.get("vendor") or {}),
            "material": row.get("material"), "density": row.get("density"),
            "diameter": row.get("diameter"), "weight": row.get("weight"),
            "color_hex": row.get("color_hex"),
            "settings_extruder_temp": row.get("settings_extruder_temp"),
            "settings_bed_temp": row.get("settings_bed_temp"),
            "extra": dict(row.get("extra") or {})}
        self.filaments.append(filament)
        return filament

    def _import_spool(self, row):
        if not isinstance(row, dict) or self._has(self.spools, row.get("id")):
            return
        if row.get("id") is None:
            return
        initial = row.get("initial_weight")
        remaining = row.get("remaining_weight")
        self.spools.append({
            "id": int(row["id"]), "filament": self._import_filament(
                row.get("filament") or {}),
            "initial_weight": initial, "remaining_weight": remaining,
            "used_weight": row.get("used_weight")
            if initial is None or remaining is None else initial - remaining,
            "location": row.get("location") or "",
            "lot_nr": row.get("lot_nr") or "",
            "archived": bool(row.get("archived")),
            "extra": dict(row.get("extra") or {})})

    def _spool_from_summary(self, spool_id, summary, location):
        """The spool behind a C4 §8 bay summary, with a filament to match."""
        material = summary.get("material")
        key = str(material or "").strip().upper().replace(" ", "")
        filament = {
            "id": self._next_id("filament"), "name": material,
            "vendor": self._vendor_row(summary.get("vendor")),
            "material": material,
            "density": ops.MATERIAL_DENSITY.get(key, ops.DEFAULT_DENSITY),
            "diameter": 1.75, "weight": summary.get("initial_g"),
            "color_hex": summary.get("color_hex"),
            "settings_extruder_temp": None,
            "settings_bed_temp": ops.MATERIAL_BED_TEMP.get(
                key, ops.DEFAULT_BED_TEMP),
            "extra": {}}
        self.filaments.append(filament)
        initial = summary.get("initial_g")
        remaining = summary.get("remaining_g")
        extra = {}
        if summary.get("flow_k") is not None:
            extra["flow_k"] = ops.encode_extra(summary["flow_k"])
        return {"id": spool_id, "filament": filament, "initial_weight": initial,
                "remaining_weight": remaining,
                "used_weight": None if initial is None or remaining is None
                else initial - remaining,
                "location": location, "lot_nr": "", "archived": False,
                "extra": extra}

    def _vendor_row(self, name):
        for vendor in self.vendors:
            if vendor["name"] == name:
                return vendor
        vendor = {"id": self._next_id("vendor"), "name": name, "extra": {}}
        self.vendors.append(vendor)
        return vendor

    def _raise_ids(self):
        """A seeded id must never be handed out again to a new row."""
        for kind, items in (("spool", self.spools), ("filament", self.filaments),
                            ("vendor", self.vendors)):
            highest = max([int(item["id"]) for item in items] or [0])
            self._ids[kind] = max(self._ids[kind], highest)

    @staticmethod
    def _has(items, raw_id):
        return Spoolman._match(items, raw_id) is not None

    @staticmethod
    def _match(items, raw_id):
        if raw_id is None:
            return None
        for item in items:
            if str(item["id"]) == str(raw_id):
                return item
        return None
