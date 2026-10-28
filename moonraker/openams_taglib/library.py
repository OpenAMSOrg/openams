# SPDX-License-Identifier: MIT
"""The tag library of the openams_taglib component: no asyncio, no Moonraker.

The library is a checkout of the Bambu Lab RFID library (Category / Material /
Color / UID folder / ``*-dump.bin``). ``scan`` indexes it (blocking: run it in
an executor); ``TagLibrary`` holds the index, the cloned set and the tags the
display captured, and answers the component's endpoints. The mock Moonraker
uses the same classes, so both serve identical shapes.

The indexing rules match the display's PC tool (``tagtool.py``'s
``library_index``, ``lib_name``): sorted path order, at least four path parts,
1024-byte images with a valid block-0 BCC, the first of a repeated UID wins,
names cut to 31 UTF-8 bytes.
"""

import binascii
import subprocess

IMAGE_SIZE = 1024
NAME_BYTES = 31


class LibraryError(Exception):
    """A request the library cannot serve; ``status`` is the HTTP code."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Entry(object):
    """One tag: its UID, names, and where the 1 KB image is (a file for the
    library, the hex text for a captured tag)."""
    __slots__ = ("uid", "material", "color", "path", "image")

    def __init__(self, uid, material, color, path=None, image=None):
        self.uid = uid
        self.material = material
        self.color = color
        self.path = path
        self.image = image


def cut_name(text):
    """At most 31 UTF-8 bytes, whole characters only (``lib_name``)."""
    while len(text.encode()) > NAME_BYTES:
        text = text[:-1]
    return text


def bcc_ok(image):
    return image[0] ^ image[1] ^ image[2] ^ image[3] == image[4]


def _byte_key(text):
    return text.encode()


def scan(root):
    """Index the checkout at ``root`` (a ``Path``). Returns ``(entries,
    skipped)``; skipped counts every dump left out (wrong size, bad BCC, a
    repeated UID, unreadable)."""
    entries = []
    seen = set()
    skipped = 0
    for binpath in sorted(root.rglob("*-dump.bin")):
        parts = binpath.relative_to(root).parts
        if len(parts) < 4:
            continue
        try:
            image = binpath.read_bytes()
        except OSError:
            skipped += 1
            continue
        if len(image) != IMAGE_SIZE or not bcc_ok(image):
            skipped += 1
            continue
        uid = image[:4].hex().upper()
        if uid in seen:
            skipped += 1
            continue
        seen.add(uid)
        entries.append(Entry(uid, cut_name(parts[1]), cut_name(parts[2]),
                             path=str(binpath)))
    return entries, skipped


def revision(root):
    """The checkout's short git hash, or None."""
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    text = done.stdout.strip()
    return text if done.returncode == 0 and text else None


def read_head(root):
    """The checkout's HEAD commit (40 hex digits) read from the ``.git``
    files, with no subprocess: HEAD itself when detached, else the branch ref
    it names, loose or in ``packed-refs``. None when unreadable."""
    git = root / ".git"
    try:
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref:"):
            return head or None
        ref = head[4:].strip()
        try:
            return (git / ref).read_text().strip() or None
        except OSError:
            pass
        for line in (git / "packed-refs").read_text().splitlines():
            if line.endswith(" " + ref) and not line.startswith(("#", "^")):
                return line.split()[0]
    except OSError:
        pass
    return None


def parse_uid(value):
    text = str(value or "").strip().upper()
    if len(text) != 8 or any(c not in "0123456789ABCDEF" for c in text):
        raise LibraryError("uid must be 8 hex digits", 400)
    return text


def parse_name(field, value):
    text = str(value or "").strip()
    size = len(text.encode())
    if size < 1 or size > NAME_BYTES:
        raise LibraryError(
            "%s must be 1 to %d UTF-8 bytes" % (field, NAME_BYTES), 400)
    return text


def parse_image(value):
    text = str(value or "").strip()
    try:
        image = binascii.unhexlify(text)
    except (binascii.Error, ValueError):
        raise LibraryError("image must be %d hex digits" % (IMAGE_SIZE * 2),
                           400)
    if len(image) != IMAGE_SIZE:
        raise LibraryError("image must be %d hex digits" % (IMAGE_SIZE * 2),
                           400)
    if not bcc_ok(image):
        raise LibraryError("image block 0 has a bad BCC byte", 400)
    return image


class TagLibrary(object):
    def __init__(self, path):
        self.path = str(path)
        self.state = "missing"
        self.message = None
        self.indexed = False    # an index has been loaded at least once
        self.revision = None
        self.skipped = 0
        self.cloned = set()
        self.captured = {}      # uid -> Entry
        self._library = []
        self._entries = []
        self._by_material = {}

    # -- loading ----------------------------------------------------------

    def load(self, entries, skipped, revision=None):
        """Replace the scanned part of the index."""
        self._library = entries
        self.indexed = True
        self.skipped = skipped
        self.revision = revision
        self._rebuild()

    def load_state(self, cloned, captured):
        """Restore what the database held; anything malformed is dropped."""
        self.cloned = set()
        for uid in cloned or ():
            try:
                self.cloned.add(parse_uid(uid))
            except LibraryError:
                pass
        self.captured = {}
        for uid, row in (captured or {}).items():
            try:
                uid = parse_uid(uid)
                image = parse_image(row.get("image"))
                self.captured[uid] = Entry(
                    uid, parse_name("material", row.get("material")),
                    parse_name("color", row.get("color")),
                    image=image.hex().upper())
            except (LibraryError, AttributeError):
                pass
        self._rebuild()

    def _rebuild(self):
        entries = [e for e in self._library if e.uid not in self.captured]
        entries.extend(self.captured.values())
        self._entries = entries
        self._by_material = {}
        for entry in entries:
            colors = self._by_material.setdefault(entry.material, {})
            colors.setdefault(entry.color, []).append(entry)

    # -- what is persisted ------------------------------------------------

    def cloned_list(self):
        return sorted(self.cloned)

    def captured_dict(self):
        return dict((uid, {"material": e.material, "color": e.color,
                           "image": e.image})
                    for uid, e in self.captured.items())

    # -- answers ----------------------------------------------------------

    def _message(self):
        if self.message is None and self.state == "missing":
            return "The tag library is not downloaded"
        return self.message

    def status(self):
        message = self._message()
        # Colors count per material: "Black" in ABS and in PLA are two.
        colors = sum(len(by_color) for by_color in self._by_material.values())
        return {"state": self.state, "count": len(self._entries),
                "materials": len(self._by_material), "colors": colors,
                "cloned": len(self.cloned), "captured": len(self.captured),
                "skipped": self.skipped, "path": self.path,
                "revision": self.revision, "message": message}

    def _ready(self):
        # An update of an indexed library keeps serving the old index.
        return self.state == "ready" or (
            self.state == "updating" and self.indexed)

    def materials(self):
        names = sorted(self._by_material, key=_byte_key) \
            if self._ready() else []
        reply = {"state": self.state, "count": len(self._entries),
                 "materials": names}
        if self.state != "ready":
            reply["message"] = self._message()
        return reply

    def colors(self, material):
        rows = []
        by_color = self._by_material.get(material) if self._ready() else None
        for color in sorted(by_color or (), key=_byte_key):
            tags = by_color[color]
            rows.append([color, len(tags), sum(
                1 for e in tags if e.uid not in self.cloned)])
        return {"material": material, "colors": rows}

    def image_hex(self, entry):
        if entry.image is not None:
            return entry.image
        try:
            with open(entry.path, "rb") as handle:
                image = handle.read()
        except OSError as error:
            raise LibraryError("Cannot read the tag %s: %s"
                               % (entry.uid, error), 500)
        if len(image) != IMAGE_SIZE:
            raise LibraryError("The tag file for %s changed" % entry.uid, 500)
        return image.hex().upper()

    def not_ready_message(self):
        if self.state == "updating":
            return "The tag library is being downloaded; try again shortly"
        if self.state == "error":
            return "The tag library is unavailable: %s" % (
                self.message or "the last update failed")
        return "The tag library is missing; update it to download it"

    def pick(self, material, color):
        if not self._ready():
            raise LibraryError(self.not_ready_message(), 404)
        tags = self._by_material.get(material, {}).get(color)
        if not tags:
            raise LibraryError("No tag for %s / %s" % (material, color), 404)
        chosen = next((e for e in tags if e.uid not in self.cloned), tags[0])
        return {"uid": chosen.uid, "fresh": chosen.uid not in self.cloned,
                "image": self.image_hex(chosen)}

    def mark_cloned(self, uid):
        uid = parse_uid(uid)
        self.cloned.add(uid)
        return {"uid": uid, "cloned": True}

    def add(self, material, color, image):
        material = parse_name("material", material)
        color = parse_name("color", color)
        data = parse_image(image)
        uid = data[:4].hex().upper()
        replaced = uid in self.captured or any(
            e.uid == uid for e in self._library)
        self.captured[uid] = Entry(uid, material, color,
                                   image=data.hex().upper())
        self._rebuild()
        return {"uid": uid, "replaced": replaced, "count": len(self._entries)}
