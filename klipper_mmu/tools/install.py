#!/usr/bin/env python3
"""Install, inspect or remove Klipper-MMU source hooks, the host extra, and explicit driver APIs."""
# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

PACKAGE = Path(__file__).resolve().parents[1]
BEGIN = b"# BEGIN klipper-mmu add-on\n"
END = b"# END klipper-mmu add-on\n"
# Optional includes: a moved or deleted package cannot break upstream builds.
HOOKS = {
    "Kconfig": b'\n' + BEGIN + b'osource "src/klipper_mmu/mcu/Kconfig"\n' + END,
    "Makefile": b'\n' + BEGIN + b'-include src/klipper_mmu/mcu/Makefile\n' + END,
}
# Earlier packages wrote mandatory includes; install upgrades them in place.
OLD_HOOKS = {
    "Kconfig": b'\n' + BEGIN + b'source "src/klipper_mmu/mcu/Kconfig"\n' + END,
    "Makefile": b'\n' + BEGIN + b'include src/klipper_mmu/mcu/Makefile\n' + END,
}
REQUIRED_MCU_FILES = ("Kconfig", "Kconfig.driver_api", "Kconfig.devices", "Kconfig.control", "Kconfig.pins", "Kconfig.feeder", "Kconfig.tmc", "Kconfig.switch", "Kconfig.rewind", "Kconfig.adc", "Makefile")
# src/Kconfig is not a target: mcu/Kconfig.driver_api declares these options.
DRIVER_API_TARGETS = (
    "src/tmcuart.c",
    "src/tmcuart.h",
    "src/stm32/hard_pwm.c",
    "src/stm32/pwm.h",
    "src/stm32/stm32h7_adc.c",
    "src/stm32/adc_local.h",
    "src/stm32/stm32h7_gpio.c",
    "src/stm32/gpioperiph.c",
    "src/stm32/internal.h",
    "src/stm32/gpio_local.h",
    "src/stm32/gpio.c",
    "src/stepper.c",
    "src/stepper.h",
)
# These assets are one reviewed extension, not a general patch registry. Updating
# them requires accepting the new source/build regression evidence first.
DRIVER_API_ASSETS = {
    "source-hashes.json": "0fbd8fdb0b5c4df260c9a6ab0a2736f81575169c02efeaf23f7e3404e7130ec0",
    "drivers-local.patch": "141fa4f1c40d928a4032bc23c9a2e706846c7bfb7abffaff6cb9886ccc25fb51",
}
# The previous twelve-path bundle also added these declarations to src/Kconfig
# (pinned before/after hashes). Its other eleven files equal this bundle's, so
# reversing that reviewed hunk alone upgrades or removes it.
PREVIOUS_KCONFIG_PATCH = ("previous-kconfig.patch",
                          "7179b4b0c5d578cedd3c3dcdc18145cdd42bf1d93a61edd093dd05300ecdedc7")
PREVIOUS_KCONFIG_HASHES = ("b085a73a30c492ff0185b57b17e0ca9e7e52a45d4ab5d853fde8030486e6a072",
                           "f7fe938a44b12410fc875bc8f4547116457ed1050844e547fa6639e9a126b9de")
DRIVER_API_DECLARATION = re.compile(
    rb"^[ \t]*(?:menu)?config[ \t]+(?:WANT_ADC_LOCAL|WANT_HARD_PWM_QUERY|WANT_HARD_PWM_LOCAL_PAIR"
    rb"|HARD_PWM_LOCAL_MAX_PAIRS|WANT_TMCUART_LOCAL|TMCUART_LOCAL_MAX_CHANNELS|WANT_GPIO_LOCAL"
    rb"|WANT_STEPPER_LOCAL|STEPPER_LOCAL_COUNT|STEPPER_LOCAL_MOVES)[ \t]*$",
    re.MULTILINE)
REQUIRED_HOST_FILES = ("__init__.py", "protocol.py", "requests.py")


def replace_file(path, content, mode):
    """Replace one regular file; callers handle rollback across the small set."""
    fd, temporary = tempfile.mkstemp(prefix=".klipper-mmu-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), mode)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if path.exists():
            for name in os.listxattr(path):
                os.setxattr(temporary, name, os.getxattr(path, name))
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def owned_link(link, package):
    if link.is_symlink():
        if link.resolve() != package:
            raise RuntimeError(f"{link} points to another installation; left unchanged")
        return True
    if link.exists():
        raise RuntimeError(f"{link} already exists and is not our symlink; left unchanged")
    return False


def read_hook(path, name):
    """Snapshot one hook file: bytes, mode, and its current or old block (or None)."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RuntimeError(f"{path} must be a regular source file with a single hard link")
    original = path.read_bytes()
    # Detect damaged markers too, including a missing final newline.
    blocks = [block for block in (HOOKS[name], OLD_HOOKS[name]) if block in original]
    has_marker = BEGIN.rstrip() in original or END.rstrip() in original
    if has_marker and (original.count(BEGIN.rstrip()) != 1
                       or original.count(END.rstrip()) != 1 or len(blocks) != 1):
        raise RuntimeError(f"Altered or duplicate add-on block in {path}; left unchanged")
    if b"# BEGIN klipper-mmu integration experiment" in original:
        raise RuntimeError(f"Remove the experimental integration from {path} first")
    block = blocks[0] if blocks else None
    if b"src/klipper_mmu/" in (original.replace(block, b"") if block else original):
        raise RuntimeError(f"Unmanaged add-on reference in {path}; left unchanged")
    return original, stat.S_IMODE(info.st_mode), block


def integrate(checkout, action, package=PACKAGE):
    """Only mutate two marked hooks and an owned symlink in an explicit checkout."""
    if action not in {"install", "remove", "check"}:
        raise ValueError("Unknown action")
    root, package = Path(checkout).resolve(), Path(package).resolve()
    src = root / "src"
    if not (root / "Makefile").is_file() or not (root / "klippy").is_dir():
        raise RuntimeError(f"{root} does not look like a Klipper source checkout")
    if src.is_symlink() or not src.is_dir():
        raise RuntimeError("Klipper src must be a real directory")
    if action == "install":
        for name in REQUIRED_MCU_FILES:
            if not (package / "mcu" / name).is_file():
                raise RuntimeError(f"Incomplete add-on package: missing mcu/{name}")
    # A directory lock coordinates installer invocations without leaving a lock
    # file in upstream. Other editors must not change these files concurrently.
    descriptor = os.open(src, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        link = src / "klipper_mmu"
        linked = owned_link(link, package)
        snapshots = {src / name: read_hook(src / name, name) for name in HOOKS}
        present = sum(item[2] is not None for item in snapshots.values())
        up_to_date = sum(item[2] == HOOKS[path.name] for path, item in snapshots.items())
        if action == "check":
            if linked and up_to_date == len(HOOKS):
                if not all((package / "mcu" / name).is_file() for name in REQUIRED_MCU_FILES):
                    raise RuntimeError("Installed add-on package is incomplete")
                return "installed"
            if linked and present == len(HOOKS):
                raise RuntimeError("Earlier mandatory add-on hooks found; run install to upgrade them")
            if not linked and present == 0:
                return "absent"
            raise RuntimeError("Incomplete installation: inspect the source hooks and symlink")

        changed, created_link = [], False
        try:
            if action == "install" and not linked:
                link.symlink_to(package, target_is_directory=True)
                created_link = True
            for path, (original, mode, block) in snapshots.items():
                replacement = HOOKS[path.name] if action == "install" else b""
                if block is not None:
                    updated = original.replace(block, replacement)
                else:
                    updated = original + replacement
                if original == updated:
                    continue
                # Refuse obvious concurrent changes instead of overwriting them.
                if read_hook(path, path.name) != (original, mode, block):
                    raise RuntimeError(f"{path} changed during installation")
                replace_file(path, updated, mode)
                changed.append((path, original, updated, mode))
            if action == "remove" and linked:
                if not owned_link(link, package):
                    raise RuntimeError("Add-on symlink disappeared during removal")
                link.unlink()
        except Exception as error:
            failures = []
            for path, original, updated, mode in reversed(changed):
                try:
                    current, current_mode, _ = read_hook(path, path.name)
                    if current != updated or current_mode != mode:
                        raise RuntimeError("changed concurrently; preserved")
                    replace_file(path, original, mode)
                except Exception as rollback_error:
                    failures.append(f"{path}: {rollback_error}")
            if created_link:
                try:
                    if owned_link(link, package):
                        link.unlink()
                except Exception as rollback_error:
                    failures.append(f"{link}: {rollback_error}")
            if failures:
                raise RuntimeError(f"{error}; rollback incomplete: " + "; ".join(failures)) from error
            raise
        return "installed" if action == "install" else "absent"
    finally:
        os.close(descriptor)


def load_driver_api_assets(package):
    assets = {}
    for name, digest in DRIVER_API_ASSETS.items():
        path = Path(package) / "upstream" / name
        content, _ = read_driver_file(path)
        if content is None or hashlib.sha256(content).hexdigest() != digest:
            raise RuntimeError(f"Missing or altered reviewed driver API asset: {path}")
        assets[name] = content
    # Parse exactly the bytes verified above; do not reopen mutable patch assets.
    return json.loads(assets["source-hashes.json"]), assets["drivers-local.patch"]


def read_driver_file(path):
    """Snapshot bytes/mode; an absent header is represented by (None, None)."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RuntimeError(f"{path} must be a regular source file with a single hard link")
    return path.read_bytes(), stat.S_IMODE(info.st_mode)


def run_driver_patch(stage, patch, reverse):
    command = ["patch", "-p1", "--batch", "--fuzz=0"] + (["--reverse"] if reverse else [])
    try:
        result = subprocess.run(command, cwd=stage, input=patch,
                                capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"Driver API staging failed: {error}") from error
    if result.returncode:
        raise RuntimeError("Driver API patch failed: " +
                           (result.stderr or result.stdout).decode(errors="replace"))


def stage_driver_api(contents, action, manifest, patch):
    """Apply the reviewed diff off-checkout and verify every resulting byte."""
    target = "after" if action == "install" else "before"
    with tempfile.TemporaryDirectory(prefix=".klipper-mmu-stage-") as directory:
        stage = Path(directory)
        for name in DRIVER_API_TARGETS:
            (stage / name).parent.mkdir(parents=True, exist_ok=True)
        for name, content in contents.items():
            if content is not None:
                (stage / name).write_bytes(content)
        run_driver_patch(stage, patch, action == "remove")
        updated = {}
        for name in DRIVER_API_TARGETS:
            content, _ = read_driver_file(stage / name)
            digest = hashlib.sha256(content).hexdigest() if content is not None else None
            if digest != manifest[name][target]:
                raise RuntimeError(f"Staged {name} did not match expected {target} hash")
            updated[name] = content
        return updated


def stage_previous_kconfig(content, package):
    """Reverse the previous bundle's reviewed src/Kconfig hunk off-checkout."""
    name, digest = PREVIOUS_KCONFIG_PATCH
    path = Path(package) / "upstream" / name
    patch, _ = read_driver_file(path)
    if patch is None or hashlib.sha256(patch).hexdigest() != digest:
        raise RuntimeError(f"Missing or altered reviewed driver API asset: {path}")
    with tempfile.TemporaryDirectory(prefix=".klipper-mmu-stage-") as directory:
        stage = Path(directory)
        (stage / "src").mkdir()
        (stage / "src/Kconfig").write_bytes(content)
        run_driver_patch(stage, patch, True)
        restored = (stage / "src/Kconfig").read_bytes()
    if hashlib.sha256(restored).hexdigest() != PREVIOUS_KCONFIG_HASHES[0]:
        raise RuntimeError("Staged src/Kconfig did not match expected before hash")
    return restored


def write_driver_file(path, snapshot):
    content, mode = snapshot
    if content is None:
        path.unlink()
    else:
        replace_file(path, content, mode)


def check_driver_parent(root, path):
    # Targets are the fixed src files above, at most one directory deeper.
    # Recheck src too: a real stm32 leaf can sit beneath a swapped src symlink.
    for parent in (root / "src", path.parent):
        if parent.is_symlink() or not parent.is_dir():
            raise RuntimeError(f"{parent} must be a real directory")


def integrate_driver_api(checkout, action, package=PACKAGE):
    """Explicitly install/remove the pinned shared driver APIs, preserving hooks."""
    if action not in {"install", "remove", "check"}:
        raise ValueError("Unknown action")
    root, package = Path(checkout).resolve(), Path(package).resolve()
    src = root / "src"
    if not (root / "Makefile").is_file() or not (root / "klippy").is_dir():
        raise RuntimeError(f"{root} does not look like a Klipper source checkout")
    if src.is_symlink() or not src.is_dir():
        raise RuntimeError("Klipper src must be a real directory")
    for name in DRIVER_API_TARGETS:
        check_driver_parent(root, root / name)
    manifest, patch = load_driver_api_assets(package)
    descriptor = os.open(src, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        snapshots = {name: read_driver_file(root / name) for name in DRIVER_API_TARGETS}
        contents = {name: value[0] for name, value in snapshots.items()}
        hashes = {name: hashlib.sha256(content).hexdigest() if content is not None else None
                  for name, content in contents.items()}
        states = {key for key in ("before", "after")
                  if all(hashes[name] == manifest[name][key] for name in DRIVER_API_TARGETS)}
        if len(states) != 1:
            raise RuntimeError("Unsupported, modified or mixed driver API sources; left unchanged")
        state = states.pop()
        # src/Kconfig belongs to the ordinary hook, so any upstream version is
        # accepted. Only the previous bundle's exact declarations are changed.
        raw, mode, block = read_hook(src / "Kconfig", "Kconfig")
        body = raw.replace(block, b"") if block else raw
        previous = (hashlib.sha256(body).hexdigest() == PREVIOUS_KCONFIG_HASHES[1]
                    and raw == body + (block or b""))
        if not previous and DRIVER_API_DECLARATION.search(body):
            raise RuntimeError("src/Kconfig declares driver API options that this package now "
                               "owns; remove that earlier bundle first; left unchanged")
        if previous and state != "after":
            raise RuntimeError("Unsupported, modified or mixed driver API sources; left unchanged")
        if previous and action == "check":
            raise RuntimeError("Previous driver API bundle with src/Kconfig declarations; run "
                               "install-driver-api to upgrade or remove-driver-api to remove it")
        if not previous and (action == "check" or (action == "install") == (state == "after")):
            return "installed" if state == "after" else "absent"

        updated = {}
        if (action == "install") != (state == "after"):
            updated = stage_driver_api(contents, action, manifest, patch)
        if previous:
            # Keep an ordinary hook (current or old) at the end, where it was.
            updated["src/Kconfig"] = stage_previous_kconfig(body, package) + (block or b"")
            snapshots["src/Kconfig"] = (raw, mode)
        changed = []
        try:
            for name, content in updated.items():
                path = root / name
                check_driver_parent(root, path)
                original = snapshots[name]
                mode = 0o644 if original[1] is None else original[1]
                replacement = (content, mode if content is not None else None)
                if read_driver_file(path) != original:
                    raise RuntimeError(f"{path} changed during installation; preserved")
                write_driver_file(path, replacement)
                changed.append((path, original, replacement))
        except Exception as error:
            failures = []
            for path, original, replacement in reversed(changed):
                try:
                    check_driver_parent(root, path)
                    if read_driver_file(path) != replacement:
                        raise RuntimeError("changed concurrently; preserved")
                    write_driver_file(path, original)
                except Exception as rollback_error:
                    failures.append(f"{path}: {rollback_error}")
            if failures:
                raise RuntimeError(f"{error}; rollback incomplete: " + "; ".join(failures)) from error
            raise
        return "installed" if action == "install" else "absent"
    finally:
        os.close(descriptor)


def integrate_host(checkout, action, package=PACKAGE):
    """Link or remove only klippy/extras/klipper_mmu for the read-only host extra."""
    if action not in {"install", "remove", "check"}:
        raise ValueError("Unknown action")
    root = Path(checkout).resolve()
    host = (Path(package).resolve() / "host").resolve()
    if not (root / "Makefile").is_file() or not (root / "klippy").is_dir():
        raise RuntimeError(f"{root} does not look like a Klipper source checkout")
    klippy = root / "klippy"
    extras = klippy / "extras"
    if klippy.is_symlink() or not klippy.is_dir():
        raise RuntimeError("Klipper klippy must be a real directory")
    if extras.is_symlink() or not extras.is_dir():
        raise RuntimeError("Klipper klippy/extras must be a real directory")

    def complete():
        return all((host / name).is_file() for name in REQUIRED_HOST_FILES)

    def require_complete():
        for name in REQUIRED_HOST_FILES:
            if not (host / name).is_file():
                raise RuntimeError(f"Incomplete add-on package: missing host/{name}")

    if action == "install":
        require_complete()
    descriptor = os.open(extras, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        link = extras / "klipper_mmu"
        shadow = extras / "klipper_mmu.py"
        if os.path.lexists(shadow):
            raise RuntimeError(
                f"{shadow} already exists and would compete with the extra; left unchanged")
        linked = owned_link(link, host)
        if action == "check":
            if not linked:
                return "absent"
            if not complete():
                raise RuntimeError("Installed add-on package is incomplete")
            return "installed"
        if action == "install":
            require_complete()
            if not linked:
                link.symlink_to(host, target_is_directory=True)
            return "installed"
        if linked:
            if not owned_link(link, host):
                raise RuntimeError("Host extra symlink disappeared during removal")
            link.unlink()
        return "absent"
    finally:
        os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "remove", "check",
                        "install-driver-api", "remove-driver-api", "check-driver-api",
                        "install-host", "remove-host", "check-host"))
    parser.add_argument("checkout", type=Path, help="Explicit matching source checkout")
    args = parser.parse_args()
    if args.action.endswith("-host"):
        action = args.action[:-len("-host")]
        handler = integrate_host
    else:
        action, suffix, _ = args.action.partition("-driver-api")
        handler = integrate_driver_api if suffix else integrate
    try:
        result = handler(args.checkout, action)
    except (OSError, RuntimeError) as error:
        print(f"Klipper-MMU: {error}", file=sys.stderr)
        return 2
    print(f"Klipper-MMU: {result} in {args.checkout.resolve()}")
    return 1 if action == "check" and result == "absent" else 0


if __name__ == "__main__":
    raise SystemExit(main())
