#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Flag test functions that were deleted or hollowed out between revisions.

An implementer can make a branch's tests pass by replacing bodies with
``{ printf("PASS: name\\n"); }`` and deleting hundreds of assertions. Reviewing
that by eye does not scale, so this tool compares the test sources between two
git revisions and says exactly what changed:

    python3 tools/test_integrity.py BASE [HEAD] [--repo DIR] [--allow NAME]...
                                                              [--json]

It covers the C tests (``tests/**/*.c``, ``tests/**/*.h``) and the Python
tests (``tests/**/test_*.py``). ``HEAD`` defaults to the working tree, so
``... main --repo DIR`` reviews an uncommitted checkout. Per file it reports
deleted test functions, stubbed ones (a C test whose head body holds no
assertion call while its base body did, or whose head body is only a
print/return; a Python test whose body is only pass/print/return or a skip),
the assertion count before and after with a flagged drop, and new test files
that assert nothing. ``--allow NAME`` excuses findings by test name, or by
file path for a whole file, which is what a test of a deliberately deleted
API needs. Exit status is 1 when something is flagged and not excused, 2 on a
usage or git error.

C bodies are matched by brace (comment- and string- stripped), so a
commented-out or quoted ``assert`` never counts as a test.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import subprocess
import sys

C_ASSERT = re.compile(
    r"\b(?:assert|CHECK|ASSERT|EXPECT|REQUIRE|failf|fail|check\w*)\s*\("
)
C_TEST_NAME = re.compile(r"^(?:test_|case_)\w*$")
PRINT_FUNCS = ("printf", "fprintf", "snprintf", "puts", "fputs", "putchar",
               "putc", "printk", "print")
_STMT_CALL = re.compile(r"(\w+)\s*\(")
_STMT_RETURN = re.compile(r"return\b")
_WHITESPACE = re.compile(r"\s+")


class GitError(RuntimeError):
    """A git invocation this tool depends on failed."""


def _git(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=False
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise GitError("git %s: %s" % (" ".join(args), detail or "failed"))
    return proc.stdout


def _strip_c(text: str) -> str:
    """Blank out comments and string/char literals, keeping newlines."""
    out = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "/" and text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif ch == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join(c if c == "\n" else " " for c in text[i:j]))
            i = j
        elif ch in "\"'":
            quote, j = ch, i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == quote:
                    j += 1
                    break
                if text[j] == "\n":
                    break
                j += 1
            out.append("".join(c if c == "\n" else " " for c in text[i:j]))
            i = j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _match_brace(text: str, start: int) -> tuple[str, int]:
    """Return (body, index after the '}' matching text[start] == '{')."""
    depth = 0
    for j in range(start, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:j], j + 1
    return text[start + 1:], len(text)


def _c_functions(text: str) -> list[tuple[str, str]]:
    """Return (name, body) for every C function defined in stripped text."""
    funcs = []
    depth, i, n = 0, 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "{":
            depth += 1
            i += 1
        elif ch == "}":
            depth = max(0, depth - 1)
            i += 1
        elif depth or not (ch.isalpha() or ch == "_"):
            i += 1
        else:
            match = _STMT_CALL.match(text, i)
            if not match:
                i += 1
                continue
            j = _skip_parens(text, match.end() - 1)
            k = j
            while k < n and text[k] in " \t\r\n":
                k += 1
            if k < n and text[k] == "{":
                body, i = _match_brace(text, k)
                funcs.append((match.group(1), body))
            else:
                i = match.end()
    return funcs


def _skip_parens(text: str, start: int) -> int:
    """Return the index just past the ')' matching text[start] == '('."""
    depth = 0
    for j in range(start, len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return j + 1
    return len(text)


def _c_assertions(text: str) -> int:
    return len(C_ASSERT.findall(text))


def _statements(body: str):
    depth, start = 0, 0
    for i, ch in enumerate(body):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == ";" and depth == 0:
            yield body[start:i]
            start = i + 1
    yield body[start:]


def _c_print_only(body: str) -> bool:
    """True when every statement is a print-family call or a return."""
    for stmt in _statements(body):
        stmt = stmt.strip()
        if not stmt:
            continue
        call = _STMT_CALL.match(stmt)
        if call and call.group(1) in PRINT_FUNCS:
            continue
        if _STMT_RETURN.match(stmt):
            continue
        return False
    return True


def _normalize(body: str) -> str:
    return _WHITESPACE.sub(" ", body).strip()


def _py_tree(text: str):
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


def _node_name(node) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _py_assertions(node) -> int:
    """Count bare ``assert`` statements and ``self.assert*(...)`` calls."""
    if node is None:
        return 0
    count = 0
    for child in ast.walk(node):
        if isinstance(child, ast.Assert):
            count += 1
        elif isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            if child.func.attr.startswith("assert"):
                count += 1
    return count


def _py_tests(tree) -> list[tuple[str, ast.AST]]:
    if tree is None:
        return []
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                found.append((node.name, node))
    return found


def _py_skipped(node) -> bool:
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if "skip" in _node_name(target).lower():
            return True
    return False


def _py_trivial(node) -> bool:
    """True when the body only passes, prints, skips, returns or is empty."""
    for stmt in node.body:
        if isinstance(stmt, (ast.Pass, ast.Return)):
            continue
        if isinstance(stmt, ast.Expr):
            value = stmt.value
            if isinstance(value, ast.Constant) and (
                isinstance(value.value, str) or value.value is Ellipsis
            ):
                continue
            if isinstance(value, ast.Call):
                name = _node_name(value.func)
                if name == "print" or name.startswith("skip"):
                    continue
        return False
    return True


def _py_stub_reason(node) -> str:
    if _py_skipped(node):
        return "skip decorator"
    for stmt in node.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            if _node_name(stmt.value.func).startswith("skip"):
                return "body only skips itself"
    return "body is only pass/print/return"


def _is_test_file(rel: str) -> bool:
    if not rel.startswith("tests/") or "/.git/" in rel or rel.startswith("tests/.git/"):
        return False
    if rel.endswith((".c", ".h")):
        return True
    name = rel.rsplit("/", 1)[-1]
    return name.startswith("test_") and name.endswith(".py")


def _toplevel(path: Path) -> Path:
    out = _git(path, "rev-parse", "--show-toplevel").decode("utf-8", "replace").strip()
    if not out:
        raise GitError("not inside a git working tree: %s" % path)
    return Path(out)


def _require_rev(root: Path, rev: str) -> str:
    try:
        return _git(root, "rev-parse", "--verify", rev + "^{commit}").decode().strip()
    except GitError:
        raise GitError("not a git revision in %s: %s" % (root, rev))


def _rev_files(root: Path, rev: str) -> list[str]:
    raw = _git(root, "ls-tree", "-r", "-z", "--name-only", rev)
    names = raw.decode("utf-8", "replace").split("\0")
    return sorted(name for name in names if name and _is_test_file(name))


def _rev_text(root: Path, rev: str, rel: str) -> str:
    return _git(root, "show", "%s:%s" % (rev, rel)).decode("utf-8", "replace")


def _worktree_files(root: Path) -> list[str]:
    tests = root / "tests"
    if not tests.is_dir():
        return []
    return sorted(
        path.relative_to(root).as_posix()
        for path in tests.rglob("*")
        if path.is_file() and _is_test_file(path.relative_to(root).as_posix())
    )


def _worktree_text(root: Path, rel: str):
    try:
        return (root / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _allowed(allow, path, name):
    for entry in allow:
        if entry == path:
            return True
        if name and entry in (name, "%s:%s" % (path, name), "%s::%s" % (path, name)):
            return True
    return False


def _finding(path, kind, name, detail, allow):
    return {
        "file": path,
        "kind": kind,
        "name": name,
        "detail": detail,
        "allowed": _allowed(allow, path, name),
    }


def _add_drop(record, deleted, allow):
    """Report a falling assertion count, excusing what --allow explains."""
    base_count = record["base_assertions"]
    head_count = record["head_assertions"]
    if base_count is None or head_count is None or head_count >= base_count:
        return
    excused = sum(
        item["base_assertions"]
        for item in deleted
        if _allowed(allow, record["file"], item["name"])
    )
    finding = _finding(record["file"], "assertion-drop", "",
                       "%d -> %d assertion(s)" % (base_count, head_count), allow)
    if excused and head_count >= base_count - excused:
        finding["allowed"] = True
    record["findings"].append(finding)


def _analyse_c(record, base_text, head_text, allow):
    path = record["file"]
    base = _strip_c(base_text) if base_text is not None else None
    head = _strip_c(head_text) if head_text is not None else None
    base_fns = dict(_c_functions(base)) if base is not None else {}
    head_fns = dict(_c_functions(head)) if head is not None else {}
    record["base_assertions"] = _c_assertions(base) if base is not None else None
    record["head_assertions"] = _c_assertions(head) if head is not None else None
    deleted = []
    for name, body in base_fns.items():
        if not C_TEST_NAME.match(name) or name in head_fns:
            continue
        removed = _c_assertions(body)
        deleted.append({"name": name, "base_assertions": removed})
        record["findings"].append(_finding(
            path, "deleted", name, "base body had %d assertion(s)" % removed, allow))
    for name, body in head_fns.items():
        if not C_TEST_NAME.match(name) or C_ASSERT.search(body):
            continue
        base_body = base_fns.get(name)
        if base_body is not None and _normalize(base_body) == _normalize(body):
            continue
        if base_body is not None and C_ASSERT.search(base_body):
            record["findings"].append(_finding(
                path, "stub", name,
                "no assertion call left (base had %d)" % _c_assertions(base_body), allow))
        elif _c_print_only(body):
            record["findings"].append(_finding(
                path, "stub", name, "body is only a print/return", allow))
    _add_drop(record, deleted, allow)


def _analyse_py(record, base_text, head_text, allow):
    path = record["file"]
    base_tree = _py_tree(base_text) if base_text is not None else None
    head_tree = _py_tree(head_text) if head_text is not None else None
    unparsable = (base_text is not None and base_tree is None) or (
        head_text is not None and head_tree is None)
    if unparsable:
        record["findings"].append(_finding(
            path, "unparsable", "", "module does not parse", allow))
        return
    record["base_assertions"] = _py_assertions(base_tree) if base_tree is not None else 0
    record["head_assertions"] = _py_assertions(head_tree) if head_tree is not None else 0
    base_tests = dict(_py_tests(base_tree))
    head_tests = dict(_py_tests(head_tree))
    deleted = []
    for name in base_tests:
        if name in head_tests:
            continue
        removed = _py_assertions(base_tests[name])
        deleted.append({"name": name, "base_assertions": removed})
        record["findings"].append(_finding(
            path, "deleted", name, "base body had %d assertion(s)" % removed, allow))
    for name, node in head_tests.items():
        if not (_py_trivial(node) or _py_skipped(node)):
            continue
        base_node = base_tests.get(name)
        if base_node is not None and ast.dump(base_node) == ast.dump(node):
            continue
        record["findings"].append(_finding(
            path, "stub", name, _py_stub_reason(node), allow))
    _add_drop(record, deleted, allow)


def _analyse(path, base_text, head_text, allow):
    record = {
        "file": path,
        "status": "modified",
        "base_assertions": None,
        "head_assertions": None,
        "findings": [],
    }
    if base_text is None:
        record["status"] = "added"
    elif head_text is None:
        record["status"] = "deleted"
        record["findings"].append(_finding(
            path, "deleted-file", path, "whole test source removed", allow))
    if path.endswith(".py"):
        _analyse_py(record, base_text, head_text, allow)
    else:
        _analyse_c(record, base_text, head_text, allow)
    if record["status"] == "added" and record["head_assertions"] == 0:
        record["findings"].append(_finding(
            path, "new-file", path, "new test source asserts nothing", allow))
    return record


def _count(value):
    return "-" if value is None else str(value)


def render(report) -> str:
    lines = ["test-integrity: %s -> %s in %s"
             % (report["base"], report["head"], report["repo"])]
    by_file = {}
    for finding in report["findings"]:
        by_file.setdefault(finding["file"], []).append(finding)
    for record in report["files"]:
        findings = by_file.get(record["file"], [])
        if not findings:
            continue
        lines.append("%s [%s] assertions %s -> %s" % (
            record["file"], record["status"],
            _count(record["base_assertions"]), _count(record["head_assertions"])))
        for finding in findings:
            mark = "allowed" if finding["allowed"] else "FLAG"
            name = " " + finding["name"] if finding["name"] else ""
            lines.append("  %-7s %s%s: %s" % (
                mark, finding["kind"], name, finding["detail"]))
    if report["flagged"] == 0:
        lines.append("nothing flagged")
    lines.append("%d flagged, %d allowed, %d test files compared" % (
        report["flagged"], report["allowed"], report["files_compared"]))
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="test-integrity",
        description="Flag deleted, stubbed or assertion-free test changes "
                    "between two git revisions.")
    parser.add_argument("base", metavar="BASE", help="base git revision")
    parser.add_argument("head", nargs="?", metavar="HEAD",
                        help="head git revision (default: the working tree)")
    parser.add_argument("--repo", default=".", metavar="DIR",
                        help="git checkout to inspect (default: current directory)")
    parser.add_argument("--allow", action="append", default=[], metavar="NAME",
                        help="excuse a finding by test name or file path (repeatable)")
    parser.add_argument("--json", action="store_true", help="emit JSON for tools")
    args = parser.parse_args(argv)

    allow = list(args.allow)
    try:
        root = _toplevel(Path(args.repo))
        base_sha = _require_rev(root, args.base)
        base_files = set(_rev_files(root, base_sha))
        head_is_worktree = args.head is None
        if head_is_worktree:
            head_sha, head_label = None, "working tree"
            head_files = set(_worktree_files(root))
        else:
            head_sha, head_label = _require_rev(root, args.head), args.head
            head_files = set(_rev_files(root, head_sha))

        def text_at(rev, rel):
            return _worktree_text(root, rel) if rev is None else _rev_text(root, rev, rel)

        records = [
            _analyse(
                rel,
                _rev_text(root, base_sha, rel) if rel in base_files else None,
                text_at(head_sha, rel) if rel in head_files else None,
                allow,
            )
            for rel in sorted(base_files | head_files)
        ]
    except GitError as exc:
        print("test-integrity: %s" % exc, file=sys.stderr)
        return 2

    findings = []
    for record in records:
        findings.extend(record.pop("findings"))
    flagged = sum(1 for finding in findings if not finding["allowed"])
    report = {
        "schema": "test-integrity/1",
        "repo": str(root),
        "base": args.base,
        "head": head_label,
        "head_is_worktree": head_is_worktree,
        "files_compared": len(records),
        "flagged": flagged,
        "allowed": len(findings) - flagged,
        "findings": findings,
        "files": records,
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render(report))
    return 1 if flagged else 0


if __name__ == "__main__":
    raise SystemExit(main())
