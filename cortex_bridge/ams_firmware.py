# SPDX-License-Identifier: MIT
"""AMS firmware images: where Bambu Lab publishes them, and how this computer
gets and checks one before the bridge sends it to an AMS.

OpenAMS stores no Bambu firmware. ``ams_firmware.json`` lists, for each AMS
family, firmware version and board revision, where Bambu publishes the image:
the address on its public download server, or the printer offline package
(bambulab.com > Support > Firmware download) that carries it, with its MD5
and size. The image reaches a cache folder on this computer either by
download or by ``import`` from an offline package the user downloaded, and
is checked first:

- the size and MD5 the list gives;
- the BIMH header: magic, format 1, total size equal to the file's size, a
  416-byte header, data size equal to the rest of the file;
- the file name written inside the header, so a file for another AMS or board
  revision is never sent.

The image itself is Bambu's signed file, exactly as a Bambu printer streams it
to the AMS; the AMS's loader checks Bambu's signature, so only official images
can be installed this way.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import urllib.parse
import urllib.request

CATALOG = Path(__file__).resolve().parent / "ams_firmware.json"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or
                 Path.home() / ".cache") / "openams" / "ams-firmware"

# Only Bambu's own download servers, over HTTPS.
ALLOWED_HOSTS = ("public-cdn.bblmw.com", "public-cdn.bambulab.com")

FAMILIES = {
    "ams1": "AMS 1",
    "ams2": "AMS 2 Pro",
    "ams_ht": "AMS HT",
}
# The bridge snapshot's family names, mapped to OpenAMS's.
SNAPSHOT_FAMILIES = {"ams1": "ams1", "ams2pro": "ams2", "ams2": "ams2",
                     "ams_ht": "ams_ht"}

BIMH_MAGIC = b"BIMH"
BIMH_FORMAT = 1
BIMH_HEADER_SIZE = 416
BIMH_NAME_OFFSET = 0x30
BIMH_NAME_END = 0x80
LARGEST_IMAGE = 512 * 1024      # every known image is under 200 KiB
DOWNLOAD_TIMEOUT_S = 30.0


class AmsFirmwareError(RuntimeError):
    """Plain-language reason an image cannot be used."""


@dataclasses.dataclass(frozen=True)
class Image:
    family: str
    version: str
    hardware: str
    md5: str
    size: int
    file: str = ""
    url: str = ""
    package: str = ""
    inner_name: str = ""
    source: str = ""

    @property
    def label(self) -> str:
        return "%s %s (%s)" % (FAMILIES.get(self.family, self.family),
                               self.version, self.hardware)

    @property
    def file_name(self) -> str:
        """The file name as Bambu publishes it."""
        return self.file or Path(urllib.parse.urlparse(self.url).path).name


# --------------------------------------------------------------- versions

def parse_version(text: str) -> tuple:
    """"03.00.21.29" -> (3, 0, 21, 29); raises AmsFirmwareError."""
    parts = str(text).strip().split(".")
    if len(parts) != 4 or not all(p.isdigit() and len(p) <= 3 for p in parts):
        raise AmsFirmwareError("%r is not an AMS firmware version "
                               "(four numbers, like 03.00.21.29)" % (text,))
    return tuple(int(p) for p in parts)


def format_version(value) -> str:
    """A version as Bambu writes it, from a tuple or from the bridge's
    snapshot (``ams_version``: the four bytes as the AMS sends them, lowest
    first, so [29, 21, 0, 3] is 03.00.21.29)."""
    if isinstance(value, str):
        return "%02d.%02d.%02d.%02d" % parse_version(value)
    parts = list(value)
    if len(parts) != 4:
        raise AmsFirmwareError("an AMS version has four parts, not %d"
                               % len(parts))
    return "%02d.%02d.%02d.%02d" % tuple(reversed(parts))


# ---------------------------------------------------------------- catalog

def _check_entry(entry: dict) -> Image:
    try:
        image = Image(family=str(entry["family"]),
                      version=str(entry["version"]),
                      hardware=str(entry["hardware"]),
                      md5=str(entry["md5"]).lower(),
                      size=int(entry["size"]),
                      file=str(entry.get("file", "")),
                      url=str(entry.get("url", "")),
                      package=str(entry.get("package", "")),
                      inner_name=str(entry.get("inner_name", "")),
                      source=str(entry.get("source", "")))
    except (KeyError, TypeError, ValueError) as exc:
        raise AmsFirmwareError("bad entry in the AMS firmware list: %s"
                               % exc)
    if image.family not in FAMILIES:
        raise AmsFirmwareError("unknown AMS family %r in the AMS firmware "
                               "list" % image.family)
    parse_version(image.version)
    if image.url:
        check_url(image.url)
    elif not (image.package and image.file):
        raise AmsFirmwareError("%s has neither a download address nor an "
                               "offline package and file name" % image.label)
    name = image.file_name
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise AmsFirmwareError("bad file name for %s" % image.label)
    if len(image.md5) != 32 or any(c not in "0123456789abcdef"
                                   for c in image.md5):
        raise AmsFirmwareError("bad MD5 for %s" % image.label)
    if not 0 < image.size <= LARGEST_IMAGE:
        raise AmsFirmwareError("bad size for %s" % image.label)
    return image


def load_catalog(path: Path = CATALOG) -> list:
    """Every listed image, oldest version first."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except ValueError as exc:
        raise AmsFirmwareError("unreadable %s: %s" % (path, exc))
    if data.get("schema") != 1:
        raise AmsFirmwareError("%s has schema %r; this OpenAMS reads 1"
                               % (path, data.get("schema")))
    images = [_check_entry(e) for e in data.get("images", [])]
    seen = set()
    for image in images:
        key = (image.family, image.version, image.hardware)
        if key in seen:
            raise AmsFirmwareError("%s is listed twice" % image.label)
        seen.add(key)
    return sorted(images, key=lambda i: (i.family, parse_version(i.version),
                                         i.hardware))


def versions(images: list, family: str) -> list:
    """The versions listed for ``family``, newest first."""
    found = {i.version for i in images if i.family == family}
    return sorted(found, key=parse_version, reverse=True)


def choose(images: list, family: str, version: str = "latest") -> list:
    """Every board revision's image of one version ("latest" = the newest
    listed). The right one is picked once the AMS's loader names its board."""
    if family not in FAMILIES:
        raise AmsFirmwareError("unknown AMS family %r" % family)
    listed = versions(images, family)
    if not listed:
        raise AmsFirmwareError("no %s firmware is listed"
                               % FAMILIES[family])
    if version in ("", "latest", None):
        version = listed[0]
    else:
        version = format_version(version)
        if version not in listed:
            raise AmsFirmwareError(
                "%s %s is not listed; listed: %s"
                % (FAMILIES[family], version, ", ".join(listed)))
    return [i for i in images if i.family == family and i.version == version]


# ------------------------------------------------------------- the image

def check_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise AmsFirmwareError("%s is not on Bambu Lab's download server"
                               % url)


def bimh_fields(data: bytes) -> dict:
    """The header fields of a BIMH image; raises AmsFirmwareError."""
    if len(data) < BIMH_HEADER_SIZE:
        raise AmsFirmwareError("the file is too short to be an AMS image")
    if data[:4] != BIMH_MAGIC:
        raise AmsFirmwareError("the file is not an AMS image (no BIMH header)")
    fmt, total = struct.unpack_from("<II", data, 4)
    header, = struct.unpack_from("<I", data, 0x20)
    body, = struct.unpack_from("<I", data, 0x28)
    name = data[BIMH_NAME_OFFSET:BIMH_NAME_END].split(b"\0", 1)[0]
    return {"format": fmt, "total": total, "header": header, "body": body,
            "name": name.decode("ascii", "replace")}


def check_image(data: bytes, image: Image) -> dict:
    """Check downloaded bytes against the list entry; return the header
    fields. Raises AmsFirmwareError with the reason."""
    if len(data) != image.size:
        raise AmsFirmwareError("%s: the download is %d bytes, the list says "
                               "%d" % (image.label, len(data), image.size))
    md5 = hashlib.md5(data).hexdigest()
    if md5 != image.md5:
        raise AmsFirmwareError("%s: the download's MD5 is %s, the list says "
                               "%s" % (image.label, md5, image.md5))
    fields = bimh_fields(data)
    if fields["format"] != BIMH_FORMAT:
        raise AmsFirmwareError("%s: BIMH format %d, expected %d"
                               % (image.label, fields["format"], BIMH_FORMAT))
    if fields["total"] != len(data):
        raise AmsFirmwareError("%s: the header gives %d bytes, the file has %d"
                               % (image.label, fields["total"], len(data)))
    if fields["header"] != BIMH_HEADER_SIZE:
        raise AmsFirmwareError("%s: a %d-byte header, expected %d"
                               % (image.label, fields["header"],
                                  BIMH_HEADER_SIZE))
    if fields["body"] != len(data) - BIMH_HEADER_SIZE:
        raise AmsFirmwareError("%s: the header gives %d data bytes, the file "
                               "has %d" % (image.label, fields["body"],
                                           len(data) - BIMH_HEADER_SIZE))
    if image.inner_name and fields["name"] != image.inner_name:
        raise AmsFirmwareError("%s: the image is named %r inside, expected %r"
                               % (image.label, fields["name"],
                                  image.inner_name))
    return fields


def cached_path(image: Image, cache_dir: Path = CACHE_DIR) -> Path:
    return Path(cache_dir) / ("%s-%s" % (image.md5, image.file_name))


def _download(url: str, limit: int, timeout: float) -> bytes:
    check_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "OpenAMS"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        final = response.geturl()
        check_url(final)      # a redirect may not leave Bambu's servers
        data = response.read(limit + 1)
    if len(data) > limit:
        raise AmsFirmwareError("%s is larger than any AMS image" % url)
    return data


def fetch(image: Image, cache_dir: Path = CACHE_DIR, *, download=None,
          timeout: float = DOWNLOAD_TIMEOUT_S) -> Path:
    """The checked image file in the cache, downloading it if needed."""
    path = cached_path(image, cache_dir)
    try:
        check_image(path.read_bytes(), image)
        return path
    except FileNotFoundError:
        pass
    except AmsFirmwareError:
        path.unlink(missing_ok=True)      # a damaged copy: download again
    if not image.url:
        raise AmsFirmwareError(
            "%s comes in Bambu Lab's offline package %s, not as a download: "
            "get that package from bambulab.com (Support > Firmware "
            "download), then run: cortex-bridge.py ams-firmware import "
            "<the .zip>" % (image.label, image.package))
    download = download or _download
    try:
        data = download(image.url, LARGEST_IMAGE, timeout)
    except OSError as exc:
        raise AmsFirmwareError("could not download %s from Bambu Lab (%s)"
                               % (image.label,
                                  getattr(exc, "reason", None) or exc))
    check_image(data, image)
    return _store(path, data)


def _store(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".part-")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def import_package(package: Path, images: list,
                   cache_dir: Path = CACHE_DIR) -> list:
    """Copy every listed image found in a Bambu offline package (.zip) into
    the cache, each checked. Returns [(Image, Path or None, error text)].

    The package is only read: members are looked up by the exact file names
    the list gives, sizes are checked before anything is read, and nothing
    is extracted or run."""
    import zipfile
    results = []
    try:
        archive = zipfile.ZipFile(str(package))
    except (OSError, zipfile.BadZipFile) as exc:
        raise AmsFirmwareError("%s is not a readable offline package (%s)"
                               % (package, exc))
    with archive:
        members = {info.filename: info for info in archive.infolist()
                   if "/" not in info.filename and not info.is_dir()}
        for image in images:
            info = members.get(image.file_name)
            if info is None:
                continue
            if info.file_size != image.size:
                results.append((image, None, "%d bytes in the package, the "
                                "list says %d" % (info.file_size,
                                                  image.size)))
                continue
            try:
                with archive.open(info) as member:
                    data = member.read(image.size + 1)
                check_image(data, image)
            except (OSError, zipfile.BadZipFile, AmsFirmwareError) as exc:
                results.append((image, None, str(exc)))
                continue
            results.append((image, _store(cached_path(image, cache_dir),
                                          data), ""))
    return results


# ------------------------------------------------------------------ units

def units_from_snapshot(snapshot: dict) -> list:
    """The AMS units the bridge reports, with their firmware version."""
    units = []
    for unit in (snapshot or {}).get("units") or []:
        family = SNAPSHOT_FAMILIES.get(str(unit.get("family") or ""))
        raw = unit.get("ams_version")
        try:
            version = format_version(raw) if raw else ""
        except AmsFirmwareError:
            version = ""
        units.append({"uid": str(unit.get("uid") or ""),
                      "index": unit.get("index"),
                      "family": family,
                      "online": unit.get("link") == "online",
                      "version": version})
    return units


# Bambu Lab's AMS firmware versions have a small first part (single digits
# to date). A reading outside that range is unknown or garbled, and no update
# is offered for it.
MAX_KNOWN_MAJOR = 9


def version_unsupported(unit: dict) -> bool:
    """True when the unit reports a firmware version whose first part is
    outside the known range: an unknown or garbled reading, for which no
    update is offered. An absent or malformed version is not this case."""
    try:
        return parse_version(unit.get("version") or "")[0] > MAX_KNOWN_MAJOR
    except AmsFirmwareError:
        return False


def compare(installed: str, target: str) -> str:
    """"upgrade", "downgrade" or "same"."""
    a, b = parse_version(installed), parse_version(target)
    return "same" if a == b else ("upgrade" if b > a else "downgrade")


# ------------------------------------------------------- what was installed

INSTALLED_FILE = "installed.json"


def record_installed(uid: str, version: str, cache_dir: Path = CACHE_DIR,
                     now: str = "") -> None:
    """Remember the version an update installed on a unit: a bridge reads a
    unit's version when it admits it, so after an update its reading can be
    the old one until the bridge restarts."""
    import datetime
    path = Path(cache_dir) / INSTALLED_FILE
    try:
        data = json.loads(path.read_text("utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[uid] = {"version": version,
                 "at": now or datetime.datetime.now(datetime.timezone.utc)
                 .strftime("%Y-%m-%dT%H:%M:%SZ")}
    path.parent.mkdir(parents=True, exist_ok=True)
    _store(path, (json.dumps(data, indent=1, sort_keys=True) + "\n")
           .encode("utf-8"))


def installed_versions(cache_dir: Path = CACHE_DIR) -> dict:
    try:
        data = json.loads((Path(cache_dir) / INSTALLED_FILE)
                          .read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
