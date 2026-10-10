#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Enforce the merge checklist before a verified branch reaches local main.

The maintainer merges verified branches into the local main checkout and never
pushes. Twice a gate result was lost inside a hand-typed command chain and a
broken change reached main. This tool runs the checklist in one place and
refuses to merge unless every step passes, so a gate cannot be dropped by a
missing `&&`.

Usage:

  python3 tools/premerge.py BRANCH -m "merge message" [--allow NAME]... \\
      [--repo DIR] [--dry-run] [--release]

The steps, each stopping at the first failure with a clear message and a
nonzero exit, and never leaving the repository mid-merge:

  1. The main checkout (--repo, default the parent of the git common dir) is on
     branch main with a clean tree; untracked files under build/ are ignored.
  2. BRANCH contains main, or it must be rebased onto main first.
  3. Prints `git diff --stat main...BRANCH`.
  4. Runs tools/test_integrity.py main BRANCH --repo REPO (--allow passed on).
  5. Runs tools/check.py, or tools/check.py --release when BRANCH's changed
     paths (against main) are firmware-affecting or --release was passed, in a
     temporary worktree of BRANCH, under the build directory beside the
     checkout and removed afterwards even on failure, with a timeout (default
     3600 s). --release adds the pinned release matrix: real firmware builds of
     the installed add-on, which the native and Klipper-built suites cannot
     stand in for (they pass CONFIG_* with -D, so a missing autoconf.h include
     or a source left out of mcu/Makefile only fails there); it takes tens of
     minutes and tens of GB of temporary disk, so a test-only or docs change
     runs the much cheaper default gate instead. See firmware_affecting() for
     the exact rule; premerge always prints its decision and, when firmware
     paths triggered it, the paths that did. On a failed gate, only the
     failing run's logs/ and summary.json are kept (as logs-check-*), never
     its build trees (e.g. the release matrix's images).
  6. Unless --dry-run, merges with `git merge --no-ff` using the -m message
     exactly as given (no trailers are added).

No step ever pushes. Tests: tests/premerge/test_premerge.py. The environment
variable PREMERGE_CHECK_CMD replaces the check command (the tests point it at a
fake gate), and PREMERGE_BUILD_DIR replaces the build directory.
"""

import argparse
import os
import shlex
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
GIT_TIMEOUT = 120.0
INTEGRITY_TIMEOUT = 600.0
CHECK_TIMEOUT = 3600.0

# Paths that make a branch "firmware-affecting": the pinned release matrix
# (real firmware builds) is the only thing that catches a regression here, so
# these force `check.py --release` even for a branch that only touches one
# file. Kept intentionally broad per AGENTS.md ("when unsure, treat a path as
# firmware-affecting"); tests/release_matrix/, tests/host_h723_command_join/
# and tests/klipper_board_startup/ are named explicitly as a fallback in case
# _release_only_suite_dirs() below cannot be computed.
# host/ runs against the real H723 image in the release matrix's host-join
# tests, which caught host bugs the host unit tests did not.
FIRMWARE_PREFIXES = (
    "mcu/", "src/", "include/", "host/", "upstream/", "configs/", "tests/config/",
    "tests/release_matrix/", "tests/host_h723_command_join/",
    "tests/klipper_board_startup/",
)
FIRMWARE_FILES = frozenset({
    "Makefile", "tools/install.py", "tools/driver_api_bundle.py",
    "tools/check.py", "tools/premerge.py",
})


def _release_only_suite_dirs():
    """tests/<suite>/ directories that check.py builds only under --upstream,
    --openams, --slow or a saved/pinned-artifact gate -- never by a plain
    `python3 tools/check.py` run. Derived from check.py's own registries
    (DEFAULT_* vs UPSTREAM_*/SAVED_*/OPTIONAL_MAKEFILES) instead of copied by
    hand, so this tracks check.py automatically when its suites change."""
    sys.path.insert(0, str(HERE))
    import check as check_module

    def topdir(path):
        parts = path.split("/")
        return "/".join(parts[:2]) + "/" if len(parts) >= 2 else path

    default = {"tests/%s/" % name for name in check_module.DEFAULT_MAKEFILES}
    for registry in (check_module.DEFAULT_RUNNERS, check_module.MAKEFILE_RUNNERS,
                     check_module.DEFAULT_MODULES):
        default |= {topdir(p) for p in registry}
    gated = set()
    for registry in (check_module.UPSTREAM_RUNNERS, check_module.UPSTREAM_OTHER_RUNNERS,
                     check_module.SAVED_RUNNERS, check_module.SAVED_MODULES):
        gated |= {topdir(p) for p in registry}
    gated |= {"tests/%s/" % name for name in check_module.OPTIONAL_MAKEFILES}
    return {d for d in gated if d.startswith("tests/") and d not in default}


def firmware_affecting(paths):
    """The subset of changed repo-relative paths that require --release."""
    try:
        prefixes = FIRMWARE_PREFIXES + tuple(sorted(_release_only_suite_dirs()))
    except Exception as exc:
        print("premerge: could not read check.py's suite registries (%s); "
              "falling back to the static firmware-affecting list" % exc)
        prefixes = FIRMWARE_PREFIXES
    hits = []
    for path in paths:
        name = path.rsplit("/", 1)[-1]
        if path in FIRMWARE_FILES or path.startswith(prefixes) or name.startswith("Kconfig"):
            hits.append(path)
    return hits


class Refusal(Exception):
    """A checklist step failed; the caller reports it and exits nonzero."""


def git(repo, *args, timeout=GIT_TIMEOUT):
    """Run one git command in repo and return the completed process."""
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, timeout=timeout)


def git_out(repo, *args, timeout=GIT_TIMEOUT):
    """Run one git command in repo, raising Refusal when it fails."""
    proc = git(repo, *args, timeout=timeout)
    if proc.returncode != 0:
        detail = (proc.stderr.strip() or proc.stdout.strip() or "exit %d" % proc.returncode)
        raise Refusal("git %s failed: %s" % (" ".join(args), detail))
    return proc.stdout


def git_common_dir(repo=None):
    """The absolute common git directory of repo (default: this checkout)."""
    argv = ["git"] + (["-C", str(repo)] if repo is not None else [])
    proc = subprocess.run(argv + ["rev-parse", "--path-format=absolute", "--git-common-dir"],
                          cwd=HERE, capture_output=True, text=True, timeout=GIT_TIMEOUT)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or "not a git checkout: %s" % (repo or HERE)
        raise Refusal(detail)
    return Path(proc.stdout.strip()).resolve()


def build_dir(repo=None):
    """The build directory beside the main checkout (PREMERGE_BUILD_DIR wins)."""
    if os.environ.get("PREMERGE_BUILD_DIR"):
        return Path(os.environ["PREMERGE_BUILD_DIR"])
    return git_common_dir(repo).parent.parent / "build"


def is_ignored_status(line):
    """True for an untracked build/ entry, which does not dirty the checkout."""
    if not line.startswith("?? "):
        return False
    path = line[3:].strip().strip('"').rstrip("/")
    return path == "build" or path.startswith("build/")


def run_child(argv, cwd, timeout, env=None):
    """Run argv with a timeout; return (exit code or None on timeout, output)."""
    process = subprocess.Popen(argv, cwd=str(cwd), stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True,
                               start_new_session=True, env=env)
    try:
        output, _ = process.communicate(timeout=timeout)
        return process.returncode, output
    except subprocess.TimeoutExpired:
        for signal_name in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, signal_name)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                output, _ = process.communicate(timeout=5)
                return None, output
            except subprocess.TimeoutExpired:
                continue
        return None, ""


def failed_lines(output):
    """The status lines of a failing check.py run, for a short report."""
    prefixes = ("failed:", "timed_out:", "cleanup_failed:", "unavailable:")
    return [line for line in output.splitlines() if line.startswith(prefixes)]


def summary_path(output):
    """The check.py summary.json path it prints last, or None."""
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if stripped.endswith("summary.json"):
            return stripped.rsplit(": ", 1)[-1]
    return None


def check_command(release, all_targets=False):
    """The check command for the worktree (PREMERGE_CHECK_CMD replaces the
    base command; either way --release is appended only when the branch's
    changed paths, or --release on the command line, require the full gate)."""
    override = os.environ.get("PREMERGE_CHECK_CMD")
    base = shlex.split(override) if override else [sys.executable, "tools/check.py"]
    return base + (["--release"] if release else []) + (["--all-targets"] if all_targets else [])


def premerge(repo, branch, allows, message, dry_run, force_release=False,
             all_targets=False):
    """Run the checklist in order and (unless dry_run) merge, or raise Refusal."""
    repo = Path(repo).resolve()

    # 1. The main checkout is on main with a clean tree.
    head = git_out(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if head != "main":
        raise Refusal("%s is on branch %r, not main" % (repo, head))
    porcelain = git_out(repo, "status", "--porcelain")
    dirty = [line for line in porcelain.splitlines()
             if line.strip() and not is_ignored_status(line)]
    if dirty:
        raise Refusal("%s has uncommitted changes:\n  %s" % (repo, "\n  ".join(dirty)))

    # 2. BRANCH must contain main.
    ancestor = git(repo, "merge-base", "--is-ancestor", "main", branch)
    if ancestor.returncode == 1:
        raise Refusal("%s does not contain main; rebase %s onto main first" % (branch, branch))
    if ancestor.returncode != 0:
        detail = ancestor.stderr.strip() or ancestor.stdout.strip()
        raise Refusal("cannot compare %s with main: %s" % (branch, detail))

    # 3. Show what would be merged.
    print(git_out(repo, "diff", "--stat", "main...%s" % branch).rstrip())

    # 4. The branch must not weaken its own tests.
    integrity = [sys.executable, str(HERE / "test_integrity.py"), "main", branch,
                 "--repo", str(repo)]
    for name in allows:
        integrity += ["--allow", name]
    code, output = run_child(integrity, repo, INTEGRITY_TIMEOUT)
    if code is None:
        print(output.rstrip())
        raise Refusal("test integrity timed out after %g s" % INTEGRITY_TIMEOUT)
    if code != 0:
        print(output.rstrip())
        raise Refusal("test integrity reported findings; name deliberate ones with --allow")

    # 5. Scope the gate by what changed, then run it in a disposable worktree.
    changed = [p for p in git_out(repo, "diff", "--name-only",
                                  "main...%s" % branch).splitlines() if p.strip()]
    triggers = firmware_affecting(changed)
    release = force_release or all_targets or bool(triggers)
    if force_release:
        why = "--release was passed"
    elif triggers:
        why = "firmware-affecting paths changed"
    else:
        why = "no firmware-affecting paths changed"
    print("premerge: gate = %s (%s)" %
          ("tools/check.py --release" if release else "tools/check.py", why))
    if triggers:
        print("  firmware-affecting paths:")
        for path in triggers:
            print("    " + path)

    parent = build_dir(repo) / "premerge"
    parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    worktree = parent / ("check-%s-%d" % (stamp, os.getpid()))
    added = git(repo, "worktree", "add", "--detach", str(worktree), branch)
    if added.returncode != 0:
        detail = added.stderr.strip() or added.stdout.strip()
        raise Refusal("git worktree add failed: %s" % detail)
    try:
        code, output = run_child(check_command(release, all_targets), worktree, CHECK_TIMEOUT)
        if code != 0:
            if code is None:
                reason = "check timed out after %g s" % CHECK_TIMEOUT
                print(output.rstrip())
            else:
                reason = "check failed with exit code %d" % code
                findings = failed_lines(output)
                if findings:
                    print("failed checks:")
                    for line in findings:
                        print("  " + line)
                else:
                    print(output.rstrip()[-4000:])
            summary = summary_path(output)
            if summary and Path(summary).is_file():
                # The worktree is removed below; keep only the check's logs
                # and summary.json, never its build trees (a release-matrix
                # run alone can be ~19 GB) -- copy those two, not the whole
                # run directory.
                run_dir = Path(summary).parent
                kept = worktree.parent / ("logs-" + worktree.name)
                kept.mkdir(parents=True, exist_ok=True)
                logs_dir = run_dir / "logs"
                if logs_dir.is_dir():
                    shutil.copytree(logs_dir, kept / "logs", dirs_exist_ok=True)
                shutil.copy2(Path(summary), kept / Path(summary).name)
                summary = str(kept / Path(summary).name)
            print("summary: %s" % (summary or "(none written)"))
            raise Refusal(reason)
    finally:
        git(repo, "worktree", "remove", "--force", str(worktree))
        git(repo, "worktree", "prune")

    # 6. Merge, and never leave the checkout mid-merge.
    if dry_run:
        print("dry run: every gate passed; leaving %s unmerged" % branch)
        return
    merged = git(repo, "merge", "--no-ff", branch, "-m", message)
    if merged.returncode != 0:
        git(repo, "merge", "--abort")
        detail = merged.stderr.strip() or merged.stdout.strip()
        raise Refusal("git merge failed and was aborted: %s" % detail)
    print("merged %s; HEAD is now %s" % (branch, git_out(repo, "rev-parse", "HEAD").strip()))


def build_parser():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("branch", metavar="BRANCH", help="branch to merge into main")
    parser.add_argument("-m", "--message", required=True, help="merge commit message")
    parser.add_argument("--allow", action="append", default=[], metavar="NAME",
                        help="excuse a test-integrity finding by name; repeatable")
    parser.add_argument("--repo", default=None, metavar="DIR",
                        help="main checkout (default: the git common dir's parent)")
    parser.add_argument("--dry-run", action="store_true",
                        help="run every gate but do not merge")
    parser.add_argument("--release", action="store_true",
                        help="force the full gate (check.py --release) even if "
                             "no changed path looks firmware-affecting")
    parser.add_argument("--all-targets", action="store_true",
                        help="release matrix for every STM32 family, not only the "
                        "H723 (implies --release; use before tagging a release)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        repo = Path(args.repo).resolve() if args.repo else git_common_dir().parent
        premerge(repo, args.branch, args.allow, args.message, args.dry_run, args.release,
                 args.all_targets)
    except Refusal as refusal:
        print("premerge: refusing to merge: %s" % refusal, file=sys.stderr)
        return 1
    except (subprocess.TimeoutExpired, OSError) as failure:
        print("premerge: refusing to merge: %s" % failure, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
