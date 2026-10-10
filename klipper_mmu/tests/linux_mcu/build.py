#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
import os
import subprocess
import shutil
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_root import PINNED_ARCHIVE, BUILD_ROOT, skip_unless_available  # noqa: E402
from devices import DEVICES  # noqa: E402


def main():
    skip_unless_available('tests/linux_mcu/build.py')
    # Default (no args): exactly today's BoxTurtle behaviour, unchanged --
    # two ARM/sim fragment pairs, dictionary-gated against each other. A
    # device name argument instead builds only that device's sim-only
    # actuation-permitted image (no ARM build, no dictionary compare: those
    # exist to prove the release matrix's ARM and sim images agree, which
    # isn't the question for a device the release matrix doesn't build yet
    # -- see tests/linux_mcu/devices.py and README.md, "Proving a device").
    extra_devices = sys.argv[1:]
    if extra_devices:
        setup_klipper_tree()
        for name in extra_devices:
            if name not in DEVICES or name == 'boxturtle':
                raise SystemExit('unknown sim-only device %r (have %s)'
                                 % (name, ', '.join(d for d in DEVICES
                                                     if d != 'boxturtle')))
            device = DEVICES[name]
            build_sim_only(device.motion_config,
                           '%s-motion.config' % name, device.sim_out())
        print("Sim-only device image(s) built: %s" % ', '.join(extra_devices))
        return

    setup_klipper_tree()

    # Two release fragments, each built as an ARM/sim pair and gated against
    # its own twin: the "-safe" receive-only default AGENTS.md requires
    # (actuator START refused; the only fragment that compiles
    # klipper_mmu/mcu/diff_probe.c, which the phase-1 boot check's
    # `diff_probe: True` printer.cfg needs) and the actuation-permitted
    # motion image phase 2 boots through diagnostic startup. Building the
    # motion fragment here does not itself authorize flashing or hardware
    # use; it only proves the sim boots the same image the release matrix
    # ships, per docs/plan/linux-mcu-integration.md phase 2.
    fragments = [
        ('configs/boxturtle-h723-usb-safe.config', 'boxturtle.config', 'out-arm', 'out-sim'),
        ('configs/boxturtle-h723-usb.config', 'boxturtle-motion.config', 'out-arm-motion', 'out-sim-motion'),
    ]
    for src_config, cfg_name, arm_out, sim_out in fragments:
        build_pair(src_config, cfg_name, arm_out, sim_out)

    print("Dictionary gates passed (semantic) for both fragments.")


def setup_klipper_tree():
    """Extract the disposable Klipper tree and apply the same test-only
    patches (Linux target's shutdown -> sched_shutdown so process exit is
    clean, and the simboard symlink) regardless of which fragment gets
    built next. Idempotent: safe to call once per device in the same run."""
    os.makedirs('build/linux-mcu', exist_ok=True)

    if not os.path.exists('build/linux-mcu/klipper'):
        subprocess.run(['tar', '-xzf', str(BUILD_ROOT / PINNED_ARCHIVE), '-C', 'build/linux-mcu/'], check=True, timeout=120)
        extracted_dirs = [d for d in os.listdir('build/linux-mcu') if d.startswith('klipper-ce7002')]
        if extracted_dirs:
            os.rename(os.path.join('build/linux-mcu', extracted_dirs[0]), 'build/linux-mcu/klipper')

    subprocess.run(['python3', 'tools/install.py', 'install', 'build/linux-mcu/klipper'], check=True, timeout=60)
    subprocess.run(['python3', 'tools/install.py', 'install-driver-api', 'build/linux-mcu/klipper'], check=True, timeout=60)

    with open('build/linux-mcu/klipper/src/linux/console.c', 'r') as f:
        content = f.read()
    with open('build/linux-mcu/klipper/src/linux/console.c', 'w') as f:
        f.write(content.replace('shutdown("Force shutdown command");', 'sched_shutdown(0);'))

    with open('build/linux-mcu/klipper/src/linux/watchdog.c', 'r') as f:
        content = f.read()
    with open('build/linux-mcu/klipper/src/linux/watchdog.c', 'w') as f:
        f.write(content.replace('shutdown("Force shutdown command");', 'sched_shutdown(0);'))

    with open('build/linux-mcu/klipper/src/linux/main.c', 'r') as f:
        content = f.read()
    with open('build/linux-mcu/klipper/src/linux/main.c', 'w') as f:
        f.write(content.replace('DECL_COMMAND_FLAGS(command_config_reset, HF_IN_SHUTDOWN, "config_reset");', '').replace('DECL_CONSTANT_STR("MCU", "linux");', ''))

    simboard_link = 'build/linux-mcu/klipper/src/simboard'
    if os.path.islink(simboard_link) or os.path.exists(simboard_link):
        os.unlink(simboard_link)
    os.symlink(os.path.abspath('sim/board'), simboard_link)


def build_sim_only(src_config, cfg_name, sim_out):
    """Build one device's sim image only -- no ARM build, no dictionary
    compare (see main()'s comment for why those don't apply to a device
    the release matrix doesn't build). Mirrors build_pair()'s sim half
    exactly, including the microstep-4 override, so a device's sim image
    is built the identical way BoxTurtle's already-proven one is."""
    cfg_path = 'build/linux-mcu/' + cfg_name
    shutil.copy(src_config, cfg_path)
    with open(cfg_path, 'a') as f:
        f.write('CONFIG_KLIPPER_MMU_TMC_MICROSTEPS=4\n')
    abscfg = os.path.abspath(cfg_path)

    print(f"Building sim image ({src_config})...")
    subprocess.run(['make', 'KCONFIG_CONFIG=' + abscfg, 'OUT=' + sim_out + '/', 'olddefconfig'], cwd='build/linux-mcu/klipper', check=True, timeout=60)
    subprocess.run(['make', 'KCONFIG_CONFIG=' + abscfg, 'OUT=' + sim_out + '/', 'clean'], cwd='build/linux-mcu/klipper', check=True, timeout=60)
    subprocess.run(['make', 'KCONFIG_CONFIG=' + abscfg, 'OUT=' + sim_out + '/', 'CONFIG_BOARD_DIRECTORY=simboard'], cwd='build/linux-mcu/klipper', check=True, timeout=600)
    print(f"Sim image built ({src_config}): build/linux-mcu/klipper/{sim_out}/klipper.elf")


def build_pair(src_config, cfg_name, arm_out, sim_out):
    cfg_path = 'build/linux-mcu/' + cfg_name
    shutil.copy(src_config, cfg_path)
    # The Linux sim cannot dispatch the ~145k steps/s a 200 mm/s cruise needs
    # at 16 microsteps in real time. Motion is configured in mm, so 4
    # microsteps runs the identical profile at a quarter of the step rate;
    # the real H723 step rate is a hardware-qualification item.
    with open(cfg_path, 'a') as f:
        f.write('CONFIG_KLIPPER_MMU_TMC_MICROSTEPS=4\n')
    abscfg = os.path.abspath(cfg_path)

    print(f"Building ARM release image ({src_config})...")
    subprocess.run(['make', 'KCONFIG_CONFIG=' + abscfg, 'OUT=' + arm_out + '/', 'olddefconfig'], cwd='build/linux-mcu/klipper', check=True, timeout=60)
    subprocess.run(['make', 'KCONFIG_CONFIG=' + abscfg, 'OUT=' + arm_out + '/'], cwd='build/linux-mcu/klipper', check=True, timeout=600)

    print(f"Building sim image ({src_config})...")
    subprocess.run(['make', 'KCONFIG_CONFIG=' + abscfg, 'OUT=' + sim_out + '/', 'clean'], cwd='build/linux-mcu/klipper', check=True, timeout=60)
    subprocess.run(['make', 'KCONFIG_CONFIG=' + abscfg, 'OUT=' + sim_out + '/', 'CONFIG_BOARD_DIRECTORY=simboard'], cwd='build/linux-mcu/klipper', check=True, timeout=600)

    dict_arm = f'build/linux-mcu/klipper/{arm_out}/klipper.dict'
    dict_sim = f'build/linux-mcu/klipper/{sim_out}/klipper.dict'

    with open(dict_arm) as f:
        d_arm = json.load(f)
    with open(dict_sim) as f:
        d_sim = json.load(f)

    diffs = semantic_dict_diff(d_arm, d_sim)
    if diffs:
        print(f"DICTIONARY MISMATCH ({src_config})!")
        for line in diffs:
            print(" -", line)
        sys.exit(1)

    print(f"Dictionary gate passed (semantic) for {src_config}.")


def semantic_dict_diff(d_arm, d_sim):
    """Compare two klipper.dict structures the way klippy actually cares
    about them: ids are link-order accidents, not protocol. Everything a
    printer-facing behavior depends on must match exactly; everything that's
    just a numbering scheme (message ids, static_string_id ids) must match
    as a set. Returns a list of human-readable difference lines; empty means
    equal.
    """
    diffs = []

    # version/build_versions are expected to differ (MCU identity, compiler
    # timestamp) and are never compared.
    ignored = {'version', 'build_versions'}
    a_keys = set(d_arm) - ignored
    s_keys = set(d_sim) - ignored
    if a_keys != s_keys:
        diffs.append(f"top-level sections differ: ARM-only={a_keys - s_keys} "
                      f"SIM-only={s_keys - a_keys}")

    # Scalar/exact sections: app, license, kconfig, config all describe the
    # build itself and must match byte for byte.
    for key in ('app', 'license', 'kconfig', 'config'):
        if key not in a_keys or key not in s_keys:
            continue
        if d_arm[key] != d_sim[key]:
            if isinstance(d_arm[key], dict):
                a, s = d_arm[key], d_sim[key]
                changed = {k: (a.get(k), s.get(k))
                           for k in set(a) | set(s) if a.get(k) != s.get(k)}
                diffs.append(f"{key} differs: {changed}")
            else:
                diffs.append(f"{key} differs: ARM={d_arm[key]!r} "
                              f"SIM={d_sim[key]!r}")

    # commands/responses: the wire format strings are the protocol; the
    # numeric message ids are link-order and must not be compared.
    for key in ('commands', 'responses'):
        a_fmt = set(d_arm.get(key, {}))
        s_fmt = set(d_sim.get(key, {}))
        if a_fmt != s_fmt:
            diffs.append(f"{key} format strings differ: "
                          f"ARM-only={a_fmt - s_fmt} SIM-only={s_fmt - a_fmt}")

    # enumerations: every enum except static_string_id has host-meaningful
    # numeric values (pin ranges, error codes, ...) and must match exactly.
    # static_string_id numbers follow object link order and are compared
    # only as a set of known shutdown-reason strings.
    a_enums = d_arm.get('enumerations', {})
    s_enums = d_sim.get('enumerations', {})
    if set(a_enums) != set(s_enums):
        diffs.append(f"enumeration names differ: "
                      f"ARM-only={set(a_enums) - set(s_enums)} "
                      f"SIM-only={set(s_enums) - set(a_enums)}")

    for name in sorted(set(a_enums) & set(s_enums)):
        if name == 'static_string_id':
            a_names = set(a_enums[name])
            s_names = set(s_enums[name])
            if a_names != s_names:
                diffs.append(
                    f"static_string_id strings differ: "
                    f"ARM-only={a_names - s_names} SIM-only={s_names - a_names}")
            continue
        if a_enums[name] != s_enums[name]:
            diffs.append(f"enumeration {name} differs: "
                          f"ARM={a_enums[name]} SIM={s_enums[name]}")

    return diffs


if __name__ == '__main__':
    main()
