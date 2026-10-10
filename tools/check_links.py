#!/usr/bin/env python3
"""Check every relative Markdown link and anchor in this repository.

Standard library only.  Every inline link in every tracked Markdown file is
resolved: a relative path must exist, and an ``#anchor`` must match a heading in
the file it points at.  Links with a URL scheme (``http``, ``https``,
``mailto``, ``file``, protocol-relative) are skipped, as is anything inside a
fenced code block, which renders as code rather than as a link.

Exits 0 when every relative link resolves, 1 otherwise.  Paths given on the
command line limit the walk; with none, the whole repository is checked.
"""

import os
import re
import sys
import urllib.parse

# "build" holds generated output (the public export, klipper_mmu check runs),
# never tracked sources.
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
             "build"}
SKIP_SCHEMES = ("http://", "https://", "mailto:", "tel:", "ftp://", "file://", "//")

LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\n]*)\)")
FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")
ATX_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
SETEXT_RE = re.compile(r"^\s{0,3}(=+|-+)\s*$")
EXPLICIT_ID_RE = re.compile(r"\{#([^}]+)\}\s*$")
HTML_ANCHOR_RE = re.compile(r"<a\s[^>]*\b(?:id|name)=[\"']([^\"']+)[\"']", re.I)


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def markdown_files(root, paths):
    if not paths:
        paths = ["."]
    for rel in paths:
        target = os.path.normpath(os.path.join(root, rel))
        if os.path.isfile(target):
            yield target
            continue
        for dirpath, dirnames, filenames in os.walk(target):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            for name in sorted(filenames):
                if name.endswith(".md"):
                    yield os.path.join(dirpath, name)


def split_target(raw):
    raw = raw.strip()
    if raw.startswith("<"):
        end = raw.find(">")
        return raw[1:end] if end != -1 else raw[1:]
    for i, ch in enumerate(raw):
        if ch.isspace():
            return raw[:i]
    return raw


def iter_lines(text):
    """Yield (lineno, line) for lines outside fenced code blocks."""
    fence = None
    for lineno, line in enumerate(text.splitlines(), 1):
        match = FENCE_RE.match(line)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = marker
                continue
            if line.strip().startswith(fence):
                fence = None
                continue
        if fence is None:
            yield lineno, line


def iter_links(text):
    for lineno, line in iter_lines(text):
        for match in LINK_RE.finditer(line):
            yield lineno, split_target(match.group(1))


def slugify(text):
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = EXPLICIT_ID_RE.sub("", text)
    text = re.sub(r"[^\w\s-]", "", text.strip().lower(), flags=re.UNICODE)
    return re.sub(r"\s", "-", text.strip())


def anchors_of(text):
    anchors = set()
    counts = {}
    lines = list(iter_lines(text))
    headings = []
    for index, (_, line) in enumerate(lines):
        atx = ATX_RE.match(line)
        if atx:
            headings.append(atx.group(1))
            continue
        if index + 1 < len(lines) and line.strip() and SETEXT_RE.match(lines[index + 1][1]):
            headings.append(line.strip())
    for heading in headings:
        explicit = EXPLICIT_ID_RE.search(heading)
        if explicit:
            anchors.add(explicit.group(1).lower())
            continue
        slug = slugify(heading)
        if not slug:
            continue
        seen = counts.get(slug, 0)
        anchors.add(slug if not seen else "%s-%d" % (slug, seen))
        counts[slug] = seen + 1
    for _, line in lines:
        anchors.update(a.lower() for a in HTML_ANCHOR_RE.findall(line))
    return anchors


def main(argv):
    root = repo_root()
    cache = {}

    def anchors_for(path):
        if path not in cache:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                cache[path] = anchors_of(handle.read())
        return cache[path]

    broken = []
    for path in markdown_files(root, argv):
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        for lineno, target in iter_links(text):
            if not target or target.startswith(SKIP_SCHEMES):
                continue
            rel = os.path.relpath(path, root)
            path_part, _, anchor = target.partition("#")
            path_part = urllib.parse.unquote(path_part.split("?")[0])
            resolved = os.path.normpath(os.path.join(os.path.dirname(path), path_part)) if path_part else path
            if not os.path.exists(resolved):
                broken.append("%s:%d: %s -> missing %s" % (rel, lineno, target, path_part or "file"))
            elif anchor and os.path.isfile(resolved) and resolved.endswith(".md"):
                if urllib.parse.unquote(anchor).lower() not in anchors_for(resolved):
                    broken.append("%s:%d: %s -> no anchor #%s" % (rel, lineno, target, anchor))
    for entry in broken:
        print(entry)
    if broken:
        print("%d broken link(s)" % len(broken))
    else:
        print("all relative Markdown links resolve")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
