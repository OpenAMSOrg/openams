#!/usr/bin/env python3
"""Apply, check, remove and report the opt-in Klipper CAN patch series.

Standard library only (Python 3.7 or newer).  The series lives in
``klipper_patches/can/`` of this repository:

    BASE         the full upstream Klipper sha the series was made and tested on
    series       the apply order, one patch file name per line
    SHA256SUMS   checksums of BASE, series and every patch
    NNNN-*.patch the patches, ``git format-patch`` output

Actions (``klipper_patches.py [options] ACTION KLIPPER_PATH``):

    check    verify the series and say whether it would apply; change nothing
    apply    apply the series to the working tree; make no commit
    remove   reverse exactly what ``apply`` did
    status   say whether the series is applied

The helper edits only the working tree of the Klipper checkout.  It never
commits, never touches the index and never changes a branch.  What it applied is
recorded inside the checkout's git directory (``openams-klipper-patches/``),
together with a copy of the patches, so ``remove`` and ``status`` stay exact even
after this repository is updated, and applying twice is harmless.

Exit status: 0 success (for ``status``: the series is applied), 1 refused or
failed, 10 (``status`` only): the series is not applied.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SERIES_DIR = os.path.join(ROOT, "klipper_patches", "can")
STATE_DIRNAME = "openams-klipper-patches"
STATE_VERSION = 1
EXIT_NOT_APPLIED = 10

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SUM_RE = re.compile(r"^([0-9a-f]{64}) [ *](.+)$")


class Refused(Exception):
    """The helper will not proceed; the message says why and what to do."""


class NotACheckout(Refused):
    """The path is not a Git checkout, so no patches can have been applied to it."""


# --------------------------------------------------------------------------
# git and file helpers
# --------------------------------------------------------------------------

def run_git(args, cwd, check=True):
    """Run git; return the CompletedProcess.  With check, refuse on failure."""
    try:
        proc = subprocess.run(["git"] + list(args), cwd=cwd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True)
    except OSError as err:
        raise Refused("cannot run git: %s" % err)
    if check and proc.returncode != 0:
        raise Refused("git %s failed: %s" % (" ".join(args), proc.stderr.strip()))
    return proc


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def file_hash(repo, rel):
    """The sha256 of a working-tree file, or None when it does not exist."""
    path = os.path.join(repo, rel)
    if os.path.isfile(path):
        return sha256_file(path)
    return None


def safe_name(name):
    return bool(name) and name == os.path.basename(name) and name not in (".", "..")


# --------------------------------------------------------------------------
# the series
# --------------------------------------------------------------------------

class Series(object):
    def __init__(self, directory, base, names, sums):
        self.directory = directory
        self.base = base
        self.names = names
        self.sums = sums

    def path(self, name):
        return os.path.join(self.directory, name)

    def fingerprint(self):
        """One value that identifies this exact series: what SHA256SUMS pins."""
        return [[name, self.sums[name]] for name in self.names]


def read_text(path, what):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as err:
        raise Refused("cannot read %s (%s): %s" % (what, path, err))


def load_series(directory):
    """Load the series and verify every checksum.  Refuse on any mismatch."""
    directory = os.path.abspath(directory)
    sums = {}
    for number, line in enumerate(read_text(os.path.join(directory, "SHA256SUMS"),
                                            "SHA256SUMS").splitlines(), 1):
        if not line.strip():
            continue
        match = SUM_RE.match(line)
        if not match or not safe_name(match.group(2)):
            raise Refused("SHA256SUMS line %d is not 'hash  file name': %r" % (number, line))
        sums[match.group(2)] = match.group(1)

    names = []
    for line in read_text(os.path.join(directory, "series"), "series").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not safe_name(line) or not line.endswith(".patch"):
            raise Refused("series names %r, which is not a plain .patch file name" % line)
        names.append(line)
    if not names:
        raise Refused("the series file lists no patches")

    needed = ["BASE", "series"] + names
    for name in needed:
        if name not in sums:
            raise Refused("SHA256SUMS does not list %s" % name)
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            raise Refused("%s is listed but missing from %s" % (name, directory))
        if sha256_file(path) != sums[name]:
            raise Refused(
                "checksum mismatch for %s in %s: the series was modified or damaged.\n"
                "  Restore it with 'git -C %s checkout -- klipper_patches' or re-fetch "
                "OpenAMS; do not apply a series that fails its checksums."
                % (name, directory, ROOT))

    base = read_text(os.path.join(directory, "BASE"), "BASE").strip()
    if not SHA_RE.match(base):
        raise Refused("BASE must hold one full 40-character lowercase sha, got %r" % base)
    return Series(directory, base, names, sums)


def touched_files(series, patch_dir=None):
    """Every path the series changes, from git's own reading of the patches."""
    files = []
    for name in series.names:
        path = os.path.join(patch_dir or series.directory, name)
        proc = run_git(["apply", "--numstat", "-z", path], cwd=tempfile.gettempdir())
        parts = proc.stdout.split("\0")
        index = 0
        while index < len(parts):
            entry = parts[index]
            index += 1
            if not entry:
                continue
            fields = entry.split("\t", 2)
            if len(fields) == 3 and fields[2] == "":
                # A rename: the old and new paths follow as two more entries.
                candidates = parts[index:index + 2]
                index += 2
            elif len(fields) == 3:
                candidates = [fields[2]]
            else:
                continue
            for rel in candidates:
                if rel and rel not in files:
                    files.append(rel)
    return files


# --------------------------------------------------------------------------
# the Klipper checkout
# --------------------------------------------------------------------------

class Checkout(object):
    def __init__(self, path):
        self.path = os.path.abspath(path)
        if not os.path.isdir(self.path):
            raise Refused("\"%s\" is not a directory." % path)
        proc = run_git(["rev-parse", "--show-toplevel"], cwd=self.path, check=False)
        if proc.returncode != 0:
            raise NotACheckout("\"%s\" is not a Git checkout. The patches need a Klipper git "
                               "checkout (git clone of Klipper), not a copy of the files." % path)
        if os.path.realpath(proc.stdout.strip()) != os.path.realpath(self.path):
            raise Refused("\"%s\" is inside a Git checkout but is not its top level (%s); "
                          "point at the Klipper checkout itself." % (path, proc.stdout.strip()))
        self.head = run_git(["rev-parse", "HEAD"], cwd=self.path).stdout.strip()
        gitdir = run_git(["rev-parse", "--absolute-git-dir"], cwd=self.path).stdout.strip()
        self.state_dir = os.path.join(gitdir, STATE_DIRNAME)
        self.state_file = os.path.join(self.state_dir, "state.json")

    def dirty(self, files):
        """The subset of files with local changes (tracked changes or untracked)."""
        proc = run_git(["status", "--porcelain", "--untracked-files=all", "-z", "--"] + list(files),
                       cwd=self.path)
        dirty = []
        for entry in proc.stdout.split("\0"):
            if len(entry) > 3:
                dirty.append(entry[3:])
        return dirty

    def read_state(self):
        if not os.path.isfile(self.state_file):
            return None
        try:
            with open(self.state_file, "r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError) as err:
            raise Refused("the record of applied patches (%s) is unreadable: %s\n"
                          "  Nothing was changed." % (self.state_file, err))
        if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
            raise Refused("the record of applied patches (%s) has an unknown format." % self.state_file)
        return state

    def write_state(self, state):
        os.makedirs(self.state_dir, exist_ok=True)
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, self.state_file)

    def check_revision(self, series, files, exact_base):
        """Refuse unless HEAD is the tested base or a newer one that left the patched
        files alone (only the tested base, with ``exact_base``)."""
        base = series.base
        if self.head == base:
            return "Klipper is at the tested base %s." % base[:12]
        if run_git(["cat-file", "-e", base + "^{commit}"], cwd=self.path, check=False).returncode != 0:
            raise Refused(
                "this Klipper checkout does not contain the tested base commit %s, so it "
                "cannot be the upstream revision the patches were made for (a shallow clone, "
                "or a fork that diverged).\n  Fetch upstream history ('git -C %s fetch "
                "https://github.com/Klipper3d/klipper.git'), then check out the base: "
                "'git -C %s checkout %s'." % (base, self.path, self.path, base))
        ancestor = run_git(["merge-base", "--is-ancestor", base, self.head], cwd=self.path,
                           check=False).returncode
        if ancestor != 0:
            raise Refused(
                "Klipper is at %s, which is older than or unrelated to the tested base %s.\n"
                "  Check out the base ('git -C %s checkout %s') or update Klipper to it or later."
                % (self.head[:12], base[:12], self.path, base))
        changed = [line for line in run_git(["diff", "--name-only", base, self.head, "--"] + list(files),
                                            cwd=self.path).stdout.splitlines() if line]
        if exact_base:
            raise Refused(
                "Klipper is at %s, newer than the tested base %s, and --exact-base "
                "(install.sh: OPENAMS_KLIPPER_PATCHES_EXACT_BASE=1) accepts only the base.\n"
                "  Check out the base ('git -C %s checkout %s'), or drop --exact-base to "
                "accept a newer Klipper that left the patched files alone.%s"
                % (self.head[:12], base[:12], self.path, base,
                   ("\n  Files changed upstream since the base that the patches touch: "
                    + ", ".join(changed)) if changed else ""))
        if changed:
            raise Refused(
                "Klipper is newer than the tested base and these patched files changed "
                "upstream since it: %s.\n  The patches may not apply or may not be correct "
                "there. Check out the base ('git -C %s checkout %s') or wait for a series "
                "rebased onto your Klipper." % (", ".join(changed), self.path, base))
        return ("Klipper is at %s, newer than the tested base %s; none of the patched files "
                "changed since the base, so the series applies to them unchanged."
                % (self.head[:12], base[:12]))


def dry_run(checkout, files, patch_paths, reverse):
    """Replay the patches, one after another, on a scratch copy of the files.

    ``git apply --check`` of several patches that change the same file does not
    chain, so each patch is checked and then applied in turn on a copy: the real
    tree is touched only when the whole sequence is known to work.
    """
    scratch = tempfile.mkdtemp(prefix="openams-klipper-patches-")
    try:
        run_git(["init", "-q", scratch], cwd=scratch)
        for rel in files:
            source = os.path.join(checkout.path, rel)
            if os.path.isfile(source):
                target = os.path.join(scratch, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copyfile(source, target)
        sequence = list(reversed(patch_paths)) if reverse else list(patch_paths)
        flags = ["apply", "--whitespace=nowarn"] + (["-R"] if reverse else [])
        for path in sequence:
            for extra in (["--check"], []):
                proc = run_git(flags + extra + [path], cwd=scratch, check=False)
                if proc.returncode != 0:
                    raise Refused("%s does not %s cleanly on this checkout:\n    %s\n"
                                  "  Nothing was changed."
                                  % (os.path.basename(path), "reverse" if reverse else "apply",
                                     proc.stderr.strip().replace("\n", "\n    ")))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------

def state_matches(checkout, state):
    """The files whose content differs from what was recorded at apply time."""
    return [rel for rel, digest in sorted(state["files"].items())
            if file_hash(checkout.path, rel) != digest]


def describe_hint():
    return ("Klipper's working tree is now modified, so Moonraker's update manager will report "
            "Klipper as dirty. Before updating Klipper, run 'bash install.sh --remove-klipper-patches' "
            "(or 'python3 tools/klipper_patches.py remove <klipper path>'), update, then apply again.")


def load_applied_state(checkout, series):
    """Return (state, note) when the series is recorded as applied, else (None, None).

    Refuses when the record exists but disagrees with the files or the series.
    """
    state = checkout.read_state()
    if state is None:
        return None, None
    changed = state_matches(checkout, state)
    if changed:
        if not checkout.dirty(list(state["files"])):
            return "stale", "the patched files are back to Klipper's own content"
        raise Refused(
            "the OpenAMS Klipper patches are recorded as applied, but these files changed "
            "since: %s.\n  Applying or removing would not be exact. Back up your edits, "
            "restore the files ('git -C %s checkout -- %s'), then run remove."
            % (", ".join(changed), checkout.path, " ".join(changed)))
    return state, None


def action_status(series, checkout, options):
    state, note = load_applied_state(checkout, series)
    if state == "stale":
        print("not applied (a stale record exists: %s); run remove to clear it." % note)
        return EXIT_NOT_APPLIED
    if state is None:
        print("not applied")
        return EXIT_NOT_APPLIED
    print("applied: %d patches from base %s, applied when Klipper was at %s"
          % (len(state["patches"]), state["base"][:12], state["head"][:12]))
    if state["head"] != checkout.head:
        print("  note: Klipper's HEAD is now %s; update Klipper only after 'remove'."
              % checkout.head[:12])
    if series is not None and state["patches"] != series.fingerprint():
        print("  note: this OpenAMS checkout ships a different series than the one applied; "
              "'remove' uses the recorded copy and stays exact.")
    print("  files: %s" % ", ".join(sorted(state["files"])))
    return 0


def action_check(series, checkout, options):
    state, note = load_applied_state(checkout, series)
    if state not in (None, "stale"):
        print("already applied: %d patches (nothing to do)." % len(state["patches"]))
        return 0
    files = touched_files(series)
    print(checkout.check_revision(series, files, options.exact_base))
    dirty = checkout.dirty(files)
    if dirty:
        raise Refused("these files the patches change have local changes: %s.\n  Commit, stash or "
                      "discard them first ('git -C %s stash' saves them); the patches are "
                      "applied to a clean copy only." % (", ".join(dirty), checkout.path))
    dry_run(checkout, files, [series.path(n) for n in series.names], reverse=False)
    print("the series (%d patches) applies cleanly to %s." % (len(series.names), checkout.path))
    return 0


def action_apply(series, checkout, options):
    state, note = load_applied_state(checkout, series)
    if state not in (None, "stale"):
        if state["patches"] != series.fingerprint():
            raise Refused("a different OpenAMS Klipper patch series is already applied "
                          "(base %s, %d patches). Remove it first: 'klipper_patches.py remove %s'."
                          % (state["base"][:12], len(state["patches"]), checkout.path))
        print("already applied: %d patches (nothing to do)." % len(state["patches"]))
        return 0
    if state == "stale":
        shutil.rmtree(checkout.state_dir, ignore_errors=True)
        print("cleared a stale record of a previous apply.")

    action_check(series, checkout, options)

    files = touched_files(series)
    paths = [series.path(n) for n in series.names]
    # One git apply for the whole series: it is all-or-nothing on the working tree.
    run_git(["apply", "--whitespace=nowarn"] + paths, cwd=checkout.path)
    record = {
        "version": STATE_VERSION,
        "base": series.base,
        "head": checkout.head,
        "patches": series.fingerprint(),
        "files": dict((rel, file_hash(checkout.path, rel)) for rel in files),
    }
    try:
        patch_copy = os.path.join(checkout.state_dir, "patches")
        os.makedirs(patch_copy, exist_ok=True)
        for name in series.names:
            shutil.copyfile(series.path(name), os.path.join(patch_copy, name))
        checkout.write_state(record)
    except OSError as err:
        run_git(["apply", "--whitespace=nowarn", "-R"] + list(reversed(paths)), cwd=checkout.path,
                check=False)
        shutil.rmtree(checkout.state_dir, ignore_errors=True)
        raise Refused("could not record the applied patches (%s); the patches were reversed." % err)
    print("applied %d patches to %s (no commit was made)." % (len(series.names), checkout.path))
    print("Restart Klipper to load the host changes. MCU firmware changes take effect only "
          "after you rebuild and reflash each MCU.")
    print(describe_hint())
    return 0


def action_remove(series, checkout, options):
    state, note = load_applied_state(checkout, series)
    if state is None:
        print("not applied: nothing to remove.")
        return 0
    if state == "stale":
        shutil.rmtree(checkout.state_dir, ignore_errors=True)
        print("the patched files are already back to Klipper's own content; cleared the record.")
        return 0
    patch_copy = os.path.join(checkout.state_dir, "patches")
    recorded = state["patches"]
    paths = []
    for name, digest in recorded:
        path = os.path.join(patch_copy, name)
        if not safe_name(name) or not os.path.isfile(path) or sha256_file(path) != digest:
            raise Refused("the saved copy of %s in %s is missing or damaged, so the removal "
                          "cannot be exact.\n  Restore the files by hand: 'git -C %s checkout -- "
                          "%s', then delete %s."
                          % (name, patch_copy, checkout.path, " ".join(sorted(state["files"])),
                             checkout.state_dir))
        paths.append(path)
    files = sorted(state["files"])
    dry_run(checkout, files, paths, reverse=True)
    run_git(["apply", "--whitespace=nowarn", "-R"] + list(reversed(paths)), cwd=checkout.path)
    leftover = checkout.dirty(files)
    shutil.rmtree(checkout.state_dir, ignore_errors=True)
    if leftover:
        print("warning: these files still differ from Klipper's own content: %s" % ", ".join(leftover))
    print("removed %d patches from %s." % (len(paths), checkout.path))
    print("Restart Klipper; rebuild and reflash each MCU whose firmware was built with the patches.")
    return 0


ACTIONS = {
    "check": action_check,
    "apply": action_apply,
    "remove": action_remove,
    "status": action_status,
}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Apply, check, remove or report the opt-in Klipper CAN patch series.")
    parser.add_argument("action", choices=sorted(ACTIONS))
    parser.add_argument("klipper", help="path of the Klipper git checkout")
    parser.add_argument("--series", default=DEFAULT_SERIES_DIR,
                        help="series directory (default: klipper_patches/can of this repository)")
    parser.add_argument("--exact-base", action="store_true",
                        help="accept only the tested base, not a newer Klipper that left the "
                             "patched files alone")
    options = parser.parse_args(argv)
    try:
        # check and apply need a verified series.  status and remove work from
        # the record (and the saved patch copies) alone, so a damaged or missing
        # shipped series never stops a removal.
        try:
            series = load_series(options.series)
        except Refused:
            if options.action in ("check", "apply"):
                raise
            series = None
        try:
            checkout = Checkout(options.klipper)
        except NotACheckout as err:
            # Nothing can have been applied there: status and remove are answers, not errors.
            if options.action == "status":
                print("not applied (%s)" % err)
                return EXIT_NOT_APPLIED
            if options.action == "remove":
                print("not applied: nothing to remove (%s)" % err)
                return 0
            raise
        return ACTIONS[options.action](series, checkout, options)
    except Refused as err:
        sys.stderr.write("refused: %s\n" % err)
        return 1


if __name__ == "__main__":
    sys.exit(main())
