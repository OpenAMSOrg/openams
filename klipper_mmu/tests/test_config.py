# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual upstream Kconfig -> C profile -> common-core initialization checks."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MOTION_SYMBOLS = ('FEED_SPEED_MM_S', 'RETRACT_SPEED_MM_S', 'ACCELERATION_MM_S2',
                  'DECELERATION_MM_S2', 'UNLOAD_HUB_CLEAR_MM')


def motion_defaults(k):
    """The five per-device motion defaults, as Kconfig resolved them."""
    return [k.syms['KLIPPER_MMU_' + name].str_value for name in MOTION_SYMBOLS]


class ConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='km-config-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.work = Path(cls.temp.name)
        upstream = Path(os.environ['KLIPPER_SOURCE'])
        shutil.copytree(upstream / 'src', cls.work / 'src', symlinks=True,
                        ignore=shutil.ignore_patterns('klipper_mmu'))
        cls.product = cls.work / 'src/klipper_mmu'
        for name in ('mcu', 'include', 'src', 'tests/config'):
            shutil.copytree(ROOT / name, cls.product / name,
                            ignore=shutil.ignore_patterns('__pycache__'))
        # Kconfig uses source-relative paths. No upstream or installed file changes.
        cls.old_cwd = Path.cwd()
        cls.old_srctree = os.environ.get('srctree')
        os.environ['srctree'] = str(cls.work)
        os.chdir(cls.work)
        cls.addClassCleanup(cls.restore_environment)
        with (cls.work / 'src/Kconfig').open('a') as out:
            out.write('\nsource "src/klipper_mmu/mcu/Kconfig"\n')
        spec = importlib.util.spec_from_file_location('config_kconfiglib', upstream / 'lib/kconfiglib/kconfiglib.py')
        cls.kconfig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.kconfig)
        cls.base = (upstream / 'test/configs/stm32h723.config').read_text()

    @classmethod
    def restore_environment(cls):
        os.chdir(cls.old_cwd)
        if cls.old_srctree is None:
            os.environ.pop('srctree', None)
        else:
            os.environ['srctree'] = cls.old_srctree

    def configure(self, values='', seed=None):
        config = self.work / '.config'
        config.write_text((self.base if seed is None else seed)
                         + '\nCONFIG_KLIPPER_MMU=y\n' + values)
        k = self.kconfig.Kconfig('src/Kconfig', warn_to_stderr=False)
        k.load_config(str(config))
        k.write_config(str(config))
        saved = config.read_text()
        k.load_config(str(config)); k.write_config(str(config))
        self.assertEqual(config.read_text(), saved)
        k.write_autoconf(str(self.work / 'autoconf.h'))
        return k, saved

    def compile(self, expect_success=True):
        """A wiring mistake the build-time checks in feeder_mechanics.h catch
        (e.g. an arrival window that cannot fit the calibrated travel) fails
        here, not at km_profile_validate; expect_success=False checks that."""
        sources = ('rate', 'trajectory', 'step_segments', 'selector_home', 'buffer_sensor', 'follower', 'operation_core',
                   'event_queue', 'device_profile', 'protocol_wire', 'current_regulator')
        command = ['gcc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-pedantic',
                   '-Wconversion', '-Wsign-conversion', '-fsanitize=address,undefined',
                   '-fno-sanitize-recover=all', '-fno-pie', '-no-pie', '-g',
                   '-I' + str(self.work), '-I' + str(self.product / 'tests/config'),
                   '-I' + str(self.product / 'mcu'), '-I' + str(self.product / 'include'),
                   str(self.product / 'mcu/config.c'), str(self.product / 'tests/config/profile.c'),
                   *[str(self.product / f'src/{name}.c') for name in sources],
                   '-lm', '-o', str(self.work / 'profile')]
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if expect_success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, 'expected a build-time rejection')
        return result

    def profile(self, mode='profile', *bounds):
        result = subprocess.run([str(self.work / 'profile'), mode, *map(str, bounds)], capture_output=True,
                                text=True, timeout=15, env={**os.environ, 'ASAN_OPTIONS':'detect_leaks=0:halt_on_error=1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_mmu_stack_reservation_uses_upstream_stack_and_preserves_off_build(self):
        k, _ = self.configure()
        self.assertEqual(k.syms['STACK_SIZE'].str_value, '4096')
        for requested, expected in ((512, 4096), (8192, 8192)):
            with self.subTest(requested=requested):
                k, _ = self.configure(f'CONFIG_STACK_SIZE={requested}\n')
                self.assertEqual(k.syms['STACK_SIZE'].str_value, str(expected))
        k, saved = self.configure('# CONFIG_KLIPPER_MMU is not set\n')
        self.assertEqual(k.syms['KLIPPER_MMU'].str_value, 'n')
        self.assertEqual(k.syms['STACK_SIZE'].str_value, '512')
        self.assertEqual(k.syms['STACK_SIZE'].visibility, 0)

    def test_stale_arm_stack_reservation_is_rejected(self):
        self.configure()
        header = self.work / 'autoconf.h'
        original = header.read_text()
        command = ['gcc', '-std=c11', '-D__arm__', '-I' + str(self.work),
                   '-I' + str(self.product / 'tests/config'),
                   '-I' + str(self.product / 'mcu'),
                   '-I' + str(self.product / 'include'), '-c',
                   str(self.product / 'mcu/config.c'), '-o', str(self.work / 'config.o')]
        for size in (512, 4096):
            with self.subTest(size=size):
                header.write_text(original.replace('#define CONFIG_STACK_SIZE 4096',
                                                   f'#define CONFIG_STACK_SIZE {size}'))
                result = subprocess.run(command, capture_output=True, text=True, timeout=30)
                if size == 512:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('at least 4096 stack bytes', result.stderr)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
        header.write_text(original)

    def test_menu_requires_enough_flash_for_core_image(self):
        for target in ('STM32F103', 'STM32L412'):
            with self.subTest(target=target):
                k, _ = self.configure(seed=f'CONFIG_MACH_STM32=y\nCONFIG_MACH_{target}=y\n')
                self.assertEqual(k.syms['FLASH_SIZE'].str_value, '0x10000')
                self.assertEqual(k.syms['KLIPPER_MMU'].visibility, 0)
                self.assertEqual(k.syms['KLIPPER_MMU'].str_value, 'n')
        k, _ = self.configure(seed='CONFIG_MACH_STM32=y\nCONFIG_MACH_STM32F072=y\n')
        self.assertEqual(k.syms['FLASH_SIZE'].str_value, '0x20000')
        self.assertGreater(k.syms['KLIPPER_MMU'].visibility, 0)
        self.assertEqual(k.syms['KLIPPER_MMU'].str_value, 'y')

    def test_named_baselines_and_unexpected_calibration(self):
        for device, expected in [('BOXTURTLE', (1,18909,18909,363636,363636,800,200,600)),
                                 # NightOwl's hold current is 100 mA, not the generic
                                 # 150 mA fallback (installer/mmu_types/Kconfig.night_owl:89-90).
                                 ('NIGHTOWL', (0,36364,58182,72727,290909,700,100,0))]:
            with self.subTest(device=device):
                self.configure(f'CONFIG_KLIPPER_MMU_DEVICE_{device}=y\n')
                self.compile(); p = self.profile()
                self.assertEqual(tuple(p[k] for k in ('powered','feed','retract','acceleration','braking','run_ma','hold_ma','duty')), expected)
                self.assertEqual([p[k] for k in ('lanes','encoder','analog','measured','gain','deadband','current_target','current_limit')], [4,0,0,0,0,0,0,0])
                self.assertEqual(p['load'], 0)
                # BoxTurtle keeps its own qualified deadlines, unchanged. NightOwl
                # names none of its own, so mcu/config.c derives every one of them
                # (except cal_probe, which stays flat -- no dossier evidence bounds
                # the encoder probe) from KM_STAGE_DEADLINE_MS: 1.25x this stage's
                # distance/speed plus one ramp at ACCELERATION_MM_S2=100. hub: 300 mm
                # gate_homing_max at feed 50 mm/s (6000+500)*5/4=8125. arrival: the
                # generic 2000 mm bowden at feed 50 mm/s (no FPS arrival, so no
                # crawl) (40000+500)*5/4=50625. unload (retract): 2000+100 mm clear
                # at retract 80 mm/s (26250+800)*5/4=33812 (truncated). rewind: the
                # 100 mm clear alone at retract 80 mm/s (1250+800)*5/4=2562
                # (truncated). cal_retract: 2000 mm at retract 80 mm/s
                # (25000+800)*5/4=32250.
                deadlines = ([200,2000,2000,20000,118750,90000,15000,15000,90000]
                             if device == 'BOXTURTLE' else
                             [200,2000,2000,8125,50625,33812,2562,15000,32250])
                self.assertEqual([p[k] for k in ('sensor_age','lease','progress','hub','arrival','unload','rewind','cal_probe','cal_retract')], deadlines)
                self.profile('unexpected')

    def test_stage_timeouts_derive_per_device_geometry(self):
        # A selector machine (ERCF 2.0) and EMU (a virtual-HUB passive
        # feeder) both name no timeout of their own, so every one derives
        # from KM_STAGE_DEADLINE_MS at their own speeds -- none of which are
        # BoxTurtle's or NightOwl's -- proving the rule generalizes rather
        # than special-casing one device shape.
        cases = {
            # ERCF 2.0: now in KLIPPER_MMU_HH_MOTION_DEFAULTS (the
            # motion-defaults pass), so feed/retract/accel are its own
            # dossier's 50, 80, 100 mm/s(^2), not the generic 5/3/11
            # fallbacks. hub: gate_homing_max 100 mm at feed 50 mm/s
            # (2000+500)*5/4=3125 (truncated throughout). arrival: the
            # generic 2000 mm bowden at feed 50 mm/s (no FPS arrival)
            # (40000+500)*5/4=50625. unload: 2000 + its own 13 mm
            # unload_hub_clear at retract 80 mm/s (25162+800)*5/4=32452.
            # rewind: the 13 mm clear alone at retract 80 mm/s
            # (162+800)*5/4=1202. cal_retract: 2000 mm at retract 80 mm/s
            # (25000+800)*5/4=32250. A real LOAD/UNLOAD path is now seconds,
            # not the old generic-fallback ~14 minutes.
            'ERCF_2_0': [200,2000,2000,3125,50625,32452,1202,15000,32250],
            # EMU: also joins KLIPPER_MMU_HH_MOTION_DEFAULTS, but its own
            # dossier diverges from every other device's shared bucket: feed
            # 80, retract 250, accel 120 mm/s(^2). No dossier unload_hub_clear
            # (0), so rewind keeps the old flat 15000 ms guess instead of
            # dividing by a zero clear distance. hub: its own 600 mm
            # gate_homing_max at feed 80 mm/s (7500+666)*5/4=10207. arrival:
            # the generic 2000 mm bowden at feed 80 mm/s (25000+666)*5/4=
            # 32082. unload: 2000 + 0 mm clear at retract 250 mm/s
            # (8000+2083)*5/4=12603, the same distance as cal_retract, so the
            # same 12603.
            'EMU': [200,2000,2000,10207,32082,12603,15000,15000,12603],
        }
        for device, deadlines in cases.items():
            with self.subTest(device=device):
                self.configure(f'CONFIG_KLIPPER_MMU_DEVICE_{device}=y\n')
                self.compile(); p = self.profile()
                self.assertEqual([p[k] for k in ('sensor_age','lease','progress','hub',
                                                  'arrival','unload','rewind','cal_probe','cal_retract')],
                                 deadlines)
        # A device with no dossier evidence at all (Custom) keeps every one
        # of the old flat guesses, unchanged: FPS_ARRIVAL_MAX_MM and
        # GATE_HOMING_MAX_MM both stay 0 for it (mcu/Kconfig.control), so
        # every KM_STAGE_DEADLINE_MS call is skipped.
        self.configure('CONFIG_KLIPPER_MMU_DEVICE_CUSTOM=y\n')
        self.compile(); p = self.profile()
        self.assertEqual([p[k] for k in ('hub','arrival','unload','rewind','cal_probe','cal_retract')],
                         [8000,12000,8000,15000,15000,15000])
        # A user override still works and still wins over the derived value.
        self.configure('CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL=y\n'
                       'CONFIG_KLIPPER_MMU_HUB_TIMEOUT=4242\n'
                       'CONFIG_KLIPPER_MMU_REWIND_TIMEOUT=4343\n')
        self.compile(); p = self.profile()
        self.assertEqual((p['hub'], p['rewind']), (4242, 4343))
        self.assertEqual(p['arrival'], 50625) # Untouched fields still derive.

    def test_gear_currents_match_happy_hare_and_afc_sources(self):
        # (run mA, hold mA) for every KLIPPER_MMU_PROFILE_IMPLEMENTED device,
        # each cited in mcu/Kconfig.control's KLIPPER_MMU_RUN_CURRENT/
        # KLIPPER_MMU_HOLD_CURRENT help text. BoxTurtle and BTT ViViD are the
        # two the owner decided to keep as they already were; the rest come
        # from Happy Hare's installer/mmu_types/Kconfig.<device> [tmc2209
        # ...gear] PARAM_GEAR_RUN_CURRENT/PARAM_GEAR_HOLD_CURRENT defaults
        # (A -> mA), or, for Claymore, AFC's own template (no Happy Hare
        # dossier). Custom has neither and keeps the plain generic fallback.
        currents = (
            ('BOXTURTLE', 800, 200),
            ('NIGHTOWL', 700, 100),
            ('CUSTOM', 700, 150),
            ('KMS', 700, 100),
            ('ANGRY_BEAVER', 700, 100),
            ('THREE_MS', 700, 100),
            ('QUATTROBOX_1_0', 1270, 200),
            ('QUATTROBOX_1_1', 1270, 200),
            ('QUATTROBOX_2', 600, 200),
            ('ERCF_1_1', 500, 100),
            ('ERCF_2_0', 500, 100),
            ('ERCF_3_0', 800, 200),
            ('TRADRACK', 1270, 200),
            ('MMX', 700, 100),
            ('MMX6', 700, 100),
            ('CHAMELEON_3D', 700, 100),
            ('HTLF', 800, 100),
            ('LOW_RIDER', 700, 100),
            ('PICO_MMU', 700, 100),
            ('BTT_VIVID', 700, 100),
            ('CLAYMORE', 800, 150),
        )
        for device, run_ma, hold_ma in currents:
            with self.subTest(device=device):
                k, _ = self.configure(f'CONFIG_KLIPPER_MMU_DEVICE_{device}=y\n')
                self.assertEqual(k.syms['KLIPPER_MMU_RUN_CURRENT'].str_value, str(run_ma))
                self.assertEqual(k.syms['KLIPPER_MMU_HOLD_CURRENT'].str_value, str(hold_ma))

    def test_custom_overrides_survive_save_reload_and_device_change(self):
        # The menu is millimetres: 17 mm/s and 75 mm/s^2 become 12364 and
        # 54545 microsteps at the reference mechanics.
        k, saved = self.configure('CONFIG_KLIPPER_MMU_DEVICE_CUSTOM=y\nCONFIG_KLIPPER_MMU_LANE_COUNT=7\nCONFIG_KLIPPER_MMU_FEED_SPEED_MM_S=17\nCONFIG_KLIPPER_MMU_ACCELERATION_MM_S2=75\nCONFIG_KLIPPER_MMU_HOST_LEASE=4321\nCONFIG_KLIPPER_MMU_POWERED_REWIND=y\n')
        self.compile(); p = self.profile()
        self.assertEqual((p['name'],p['lanes'],p['feed'],p['acceleration'],p['lease'],p['powered']), ('Custom',7,12364,54545,4321,1))
        self.configure('CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL=y\n', saved)
        self.compile(); p = self.profile()
        self.assertEqual((p['name'],p['lanes'],p['feed'],p['acceleration'],p['lease'],p['powered']), ('NightOwl',7,12364,54545,4321,1))

    def test_enhancements_require_explicit_valid_calibration(self):
        self.configure('CONFIG_KLIPPER_MMU_DIFF_BUFFER=y\nCONFIG_KLIPPER_MMU_ENCODER=y\nCONFIG_KLIPPER_MMU_MEASURED_CURRENT=y\n')
        self.compile(); self.profile('missing'); self.profile('bad-cal')
        p = self.profile()
        self.assertEqual([p[k] for k in ('powered','encoder','analog','measured','gain','deadband','current_target','current_limit')], [1,1,1,1,8000,10,500,800])

    def test_optional_load_capability_reaches_runtime_profile(self):
        k, _ = self.configure('CONFIG_KLIPPER_MMU_LOAD_SENSORS=y\n')
        self.assertEqual(k.syms['KLIPPER_MMU_LOAD_SENSORS'].str_value, 'y')
        self.compile(); self.assertEqual(self.profile()['load'], 1)
        self.configure('CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL=y\n')
        self.compile(); self.assertEqual(self.profile()['load'], 0)

    def test_no_pin_configuration_defines_disabled_load_capability(self):
        k, saved = self.configure()
        self.assertEqual(k.syms['KLIPPER_MMU_PIN_CONFIG'].str_value, 'n')
        self.assertEqual(k.syms['KLIPPER_MMU_LOAD_SENSORS'].str_value, 'n')
        self.assertIn('# CONFIG_KLIPPER_MMU_LOAD_SENSORS is not set', saved)
        self.compile(); self.assertEqual(self.profile()['load'], 0)

    def test_unimplemented_device_cannot_enable_load_capability(self):
        # QUATTROBOX_1_0 was this test's example until it became implemented,
        # then ERCF_1_1 (selector work was separate scope), then LOW_RIDER,
        # then BTT_VIVID (INDEXED selector work was separate scope), then
        # CLAYMORE (AFC's own forward-only INDEXED variant was separate
        # scope too); OPENAMS (existing OpenAMS host/transport integration
        # is separate from a local MCU device driver -- Kconfig.devices --
        # so it carries no local composition here) is the current example.
        k, _ = self.configure('CONFIG_KLIPPER_MMU_PIN_CONFIG=y\n'
                              'CONFIG_KLIPPER_MMU_DEVICE_OPENAMS=y\n'
                              'CONFIG_KLIPPER_MMU_LOAD_SENSORS=y\n')
        self.assertEqual(k.syms['KLIPPER_MMU_PROFILE_IMPLEMENTED'].str_value, 'n')
        self.assertEqual(k.syms['KLIPPER_MMU_LOAD_SENSORS'].str_value, 'n')

    def test_bay_sensors_optional_reaches_runtime_profile(self):
        k, _ = self.configure()
        self.assertEqual(k.syms['KLIPPER_MMU_BAY_SENSORS'].str_value, 'y')
        self.compile(); self.assertEqual(self.profile()['bay'], 1)
        for device in ('ANGRY_BEAVER', 'THREE_MS', 'QUATTROBOX_1_0'):
            k, _ = self.configure(f'CONFIG_KLIPPER_MMU_DEVICE_{device}=y\n')
            self.assertEqual(k.syms['KLIPPER_MMU_BAY_SENSORS'].str_value, 'n')
            self.compile(); self.assertEqual(self.profile()['bay'], 0)
        # KMS keeps the baseline per-lane BAY switch (only its shared HUB is virtual).
        k, _ = self.configure('CONFIG_KLIPPER_MMU_DEVICE_KMS=y\n')
        self.assertEqual(k.syms['KLIPPER_MMU_BAY_SENSORS'].str_value, 'y')
        self.compile(); self.assertEqual(self.profile()['bay'], 1)

    def test_virtual_hub_selected_by_emu_and_kms_only(self):
        k, _ = self.configure()
        self.assertEqual(k.syms['KLIPPER_MMU_VIRTUAL_HUB'].str_value, 'n')
        self.compile(); self.assertEqual(self.profile()['virtual_hub'], 0)
        for device, load in (('KMS', 1), ('EMU', 1)):
            k, _ = self.configure(f'CONFIG_KLIPPER_MMU_DEVICE_{device}=y\n')
            self.assertEqual(k.syms['KLIPPER_MMU_VIRTUAL_HUB'].str_value, 'y')
            self.compile(); p = self.profile()
            self.assertEqual([p['virtual_hub'], p['load'], p['bay']], [1, load, 1])
        k, _ = self.configure('CONFIG_KLIPPER_MMU_DEVICE_ANGRY_BEAVER=y\n')
        self.assertEqual(k.syms['KLIPPER_MMU_VIRTUAL_HUB'].str_value, 'n')
        self.compile(); self.assertEqual(self.profile()['virtual_hub'], 0)

    def test_impossible_current_target_rejected_by_common_validator(self):
        self.configure('CONFIG_KLIPPER_MMU_MEASURED_CURRENT=y\nCONFIG_KLIPPER_MMU_CURRENT_TARGET=800\nCONFIG_KLIPPER_MMU_CURRENT_LIMIT=800\n')
        self.compile(); self.profile('invalid')

    def test_passive_rewind_removes_current_dependency(self):
        k, saved = self.configure('CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL=y\nCONFIG_KLIPPER_MMU_MEASURED_CURRENT=y\nCONFIG_KLIPPER_MMU_REWIND_DUTY=900\n')
        self.assertEqual(k.syms['KLIPPER_MMU_MEASURED_CURRENT'].str_value, 'n')
        self.compile(); p = self.profile()
        self.assertEqual([p[k] for k in ('powered','measured','duty','current_target','current_limit')], [0,0,0,0,0])

    FPS = 'CONFIG_KLIPPER_MMU_DIFF_BUFFER=y\nCONFIG_KLIPPER_MMU_FPS_ARRIVAL=y\n'
    # The smallest exercising travel pair: 1 mm and 4 mm after HUB, which the
    # reference mechanics turn into 727 and 2909 microsteps. A 1 mm window
    # fits below half that 3 mm spread; 15 mm of buffer travel is wide enough
    # for the calibrated LOAD's build-time stop-distance proof at either this
    # class's BoxTurtle base (cruise 200, crawl 20, braking 500 mm/s^2) or a
    # CONFIG_KLIPPER_MMU_DEVICE_CUSTOM base (the gentler generic fallbacks).
    FPS_VALUES = ('CONFIG_KLIPPER_MMU_FPS_ARRIVAL_PERMILLE=700\n'
                  'CONFIG_KLIPPER_MMU_FPS_ARRIVAL_SAMPLES=3\n'
                  'CONFIG_KLIPPER_MMU_FPS_ARRIVAL_HOLD_MS=20\n'
                  'CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MIN_MM=1\n'
                  'CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MAX_MM=4\n'
                  'CONFIG_KLIPPER_MMU_LOAD_WINDOW_MM=1\n'
                  'CONFIG_KLIPPER_MMU_BUFFER_TRAVEL_MM=15\n')

    def test_fps_arrival_default_off_and_toolhead_required(self):
        # FPS needs DiffA/DiffB; without FPS the toolhead switch cannot be removed.
        for values in ('CONFIG_KLIPPER_MMU_FPS_ARRIVAL=y\n', 'CONFIG_KLIPPER_MMU_DIFF_BUFFER=y\n'):
            with self.subTest(values=values):
                k, _ = self.configure(values + 'CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR=n\n' + self.FPS_VALUES)
                self.assertEqual(k.syms['KLIPPER_MMU_FPS_ARRIVAL'].str_value, 'n')
                self.assertEqual(k.syms['KLIPPER_MMU_TOOLHEAD_SENSOR'].str_value, 'y')
                self.assertEqual(k.syms['KLIPPER_MMU_TOOLHEAD_SENSOR'].visibility, 0)
                self.assertEqual(k.syms['KLIPPER_MMU_FPS_ARRIVAL_PERMILLE'].visibility, 0)
                self.compile(); p = self.profile()
                self.assertEqual((p['toolhead'], p['fps']), (1, [0] * 6))

    def test_fps_arrival_defaults_validate_only_with_known_geometry(self):
        # BoxTurtle: FPS-intrinsic defaults, travel limits from its AFC geometry.
        k, _ = self.configure(self.FPS + 'CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR=n\n')
        self.assertEqual([k.syms['KLIPPER_MMU_FPS_ARRIVAL_' + name].str_value for name in
                          ('PERMILLE', 'SAMPLES', 'HOLD_MS', 'MIN_MM', 'MAX_MM')],
                         ['700', '3', '20', '100', '1900'])
        self.compile(); p = self.profile()
        self.assertEqual((p['toolhead'], p['fps']), (0, [1, 700, 3, 20, 72727, 1381817]))
        # Without known step geometry the travel limits stay unset (0).
        custom = 'CONFIG_KLIPPER_MMU_DEVICE_CUSTOM=y\n' + self.FPS
        k, _ = self.configure(custom)
        self.assertEqual(k.syms['KLIPPER_MMU_FPS_ARRIVAL_MAX_MM'].str_value, '0')
        self.compile(); self.profile('invalid')
        self.configure(custom + self.FPS_VALUES)
        self.compile(); self.assertEqual(self.profile()['fps'], [1, 700, 3, 20, 727, 2909])
        # PERMILLE only affects km_profile_validate's runtime deadband rule.
        for name, value in (('PERMILLE', 510),):
            with self.subTest(name=name, value=value):
                self.configure(self.FPS + self.FPS_VALUES
                               + f'CONFIG_KLIPPER_MMU_FPS_ARRIVAL_{name}={value}\n')
                self.compile(); self.profile('invalid')
        # HOLD_MS=90000 (any value near the calibrated-approach timeout) and
        # MIN_MM=3000 (past MAX_MM) both reach one of feeder_mechanics.h's
        # calibrated-LOAD checks (arrival latency versus the buffer travel,
        # or the window versus the outer spread): a build-time rejection now,
        # not a runtime one -- exactly what "a wiring mistake is a build
        # error" means for these.
        for name, value in (('HOLD_MS', 90000), ('MIN_MM', 3000)):
            with self.subTest(name=name, value=value):
                self.configure(self.FPS + self.FPS_VALUES
                               + f'CONFIG_KLIPPER_MMU_FPS_ARRIVAL_{name}={value}\n')
                self.compile(expect_success=False)

    def test_fps_steps_command_accepts_zero_or_the_outer_range(self):
        self.configure(self.FPS + 'CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR=n\n')
        self.compile()
        low, high = 72727, 1381817
        # The outer travel limits (fps[4:]) never move; only the restored,
        # calibrated distance (load_profile[0]) does. 0 means uncalibrated.
        for steps in (0, low, low + 1, high - 1, high):
            with self.subTest(steps=steps):
                p = self.profile('profile', steps)
                self.assertEqual(p['fps'][4:], [low, high])
                self.assertEqual(p['load_profile'][0], steps)
        for steps in (low - 1, high + 1):
            with self.subTest(steps=steps):
                self.assertEqual(self.profile('profile', steps),
                                 {'shutdown': 'Invalid FPS calibrated travel'})

    def test_fps_arrival_values_and_optional_toolhead_reach_profile(self):
        for toolhead in ('y', 'n'):
            k, saved = self.configure(self.FPS + self.FPS_VALUES
                                      + f'CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR={toolhead}\n')
            self.assertGreater(k.syms['KLIPPER_MMU_TOOLHEAD_SENSOR'].visibility, 0)
            self.compile(); p = self.profile()
            self.assertEqual(p['fps'], [1, 700, 3, 20, 727, 2909])
            self.assertEqual(p['toolhead'], int(toolhead == 'y'))
        # Deselecting FPS restores the required switch and clears its values.
        k, _ = self.configure('CONFIG_KLIPPER_MMU_FPS_ARRIVAL=n\n', seed=saved)
        self.assertEqual(k.syms['KLIPPER_MMU_TOOLHEAD_SENSOR'].str_value, 'y')
        self.compile(); p = self.profile()
        self.assertEqual((p['toolhead'], p['fps']), (1, [0] * 6))

    def test_retained_header_without_fps_symbols_keeps_switch_arrival(self):
        self.configure()
        header = self.work / 'autoconf.h'
        lines = header.read_text().splitlines(keepends=True)
        header.write_text(''.join(line for line in lines
                                  if 'FPS_ARRIVAL' not in line and 'TOOLHEAD_SENSOR' not in line))
        self.compile(); p = self.profile()
        self.assertEqual((p['toolhead'], p['fps']), (1, [0] * 6))
        # A hand-made header cannot drop the switch without FPS arrival.
        header.write_text(''.join(line.replace('TOOLHEAD_SENSOR 1', 'TOOLHEAD_SENSOR 0')
                                  for line in lines))
        self.compile(); self.profile('invalid')

    def test_menu_ranges_reject_out_of_range_values(self):
        k, _ = self.configure('CONFIG_KLIPPER_MMU_FEED_SPEED_MM_S=276\nCONFIG_KLIPPER_MMU_HOST_LEASE=0\nCONFIG_KLIPPER_MMU_HOLD_CURRENT=900\n')
        self.assertTrue(any('outside the active range' in w for w in k.warnings))
        self.compile(); p = self.profile()
        self.assertEqual((p['feed'],p['lease'],p['run_ma'],p['hold_ma']), (18909,2000,800,200))

    def test_hh_device_motion_defaults(self):
        # BoxTurtle keeps the motion values it qualified and the generic
        # fallbacks still serve Custom and the planned catalogue entries;
        # each Happy Hare device takes its own dossier numbers instead.
        dossiers = (('KLIPPER_MMU_DEVICE_NIGHTOWL', 'nightowl'),
                    ('KLIPPER_MMU_DEVICE_KMS', 'kms'),
                    ('KLIPPER_MMU_DEVICE_ANGRY_BEAVER', 'angry_beaver'),
                    ('KLIPPER_MMU_DEVICE_THREE_MS', '3ms'),
                    ('KLIPPER_MMU_DEVICE_QUATTROBOX_1_0', 'quattrobox_1_0'),
                    ('KLIPPER_MMU_DEVICE_QUATTROBOX_1_1', 'quattrobox_1_1'),
                    ('KLIPPER_MMU_DEVICE_QUATTROBOX_2', 'quattrobox_2_0'),
                    ('KLIPPER_MMU_DEVICE_QIDI_BOX', 'qidi_box'))
        k, _ = self.configure()
        self.assertEqual(motion_defaults(k), ['26', '26', '500', '500', '65'])
        k, _ = self.configure('CONFIG_KLIPPER_MMU_DEVICE_CUSTOM=y\n')
        self.assertEqual(motion_defaults(k), ['5', '3', '11', '17', '0'])
        for symbol, name in dossiers:
            with self.subTest(device=name):
                path = ROOT / 'docs' / 'devices' / f'{name}.json'
                p = json.loads(path.read_text())['parameters']
                k, _ = self.configure(f'CONFIG_{symbol}=y\n')
                self.assertEqual(motion_defaults(k),
                                 [p['gear_homing_speed'], p['gear_unload_speed'],
                                  p['gear_load_accel'], p['gear_short_move_accel'],
                                  str(abs(int(p['gate_parking_distance'])))])
        # With the FPS arrival buffer in place of the switch arrival these
        # devices take the values BoxTurtle qualified against that buffer, and
        # the calibrated-LOAD stop check in mcu/feeder_mechanics.h still has
        # to accept them at the wider travel this class calibrates.
        for symbol, name in (dossiers[0], dossiers[4]):
            with self.subTest(device=name, fps=True):
                k, _ = self.configure(f'CONFIG_{symbol}=y\n' + self.FPS
                                      + 'CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR=n\n')
                self.assertEqual([k.syms['KLIPPER_MMU_' + n].str_value for n in
                                  ('FEED_SPEED_MM_S', 'DECELERATION_MM_S2',
                                   'LOAD_CRAWL_SPEED_MM_S', 'LOAD_CRUISE_SPEED_MM_S',
                                   'LOAD_WINDOW_MM', 'BUFFER_TRAVEL_MM')],
                                 ['26', '500', '20', '100', '50', '15'])
                self.configure(f'CONFIG_{symbol}=y\n' + self.FPS + self.FPS_VALUES
                               + 'CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR=n\n')
                self.compile(); self.assertEqual(self.profile()['toolhead'], 0)

    def test_previously_generic_devices_take_their_own_gear_speeds(self):
        # HTLF, MMX, MMX6, Low Rider, Tradrack, every ERCF version, BTT ViViD
        # and 3D Chameleon used to inherit the 5 mm/s feed, 3 mm/s retract
        # and 11 mm/s^2 accel fallbacks (a real LOAD would crawl and blow the
        # LOAD budget); they now join KLIPPER_MMU_HH_MOTION_DEFAULTS and take
        # their own dossier's gear_homing_speed/gear_unload_speed/
        # gear_load_accel/gear_short_move_accel, exactly like the eight
        # devices test_hh_device_motion_defaults already checks (only the
        # first four motion symbols are compared; UNLOAD_HUB_CLEAR_MM has its
        # own test below). This pass also gives every one of them (except
        # BTT ViViD, already correct) its own dossier rotation distance and
        # gear ratio in place of the BoxTurtle-shaped 22 mm/50:10 fallback --
        # mcu/Kconfig.control's KLIPPER_MMU_FEEDER_ROTATION_DISTANCE_UM/
        # GEAR_RATIO_A/B.
        dossiers = (('KLIPPER_MMU_DEVICE_HTLF', 'htlf'),
                    ('KLIPPER_MMU_DEVICE_MMX', 'mmx'),
                    ('KLIPPER_MMU_DEVICE_MMX6', 'mmx6'),
                    ('KLIPPER_MMU_DEVICE_LOW_RIDER', 'low_rider'),
                    ('KLIPPER_MMU_DEVICE_TRADRACK', 'tradrack'),
                    ('KLIPPER_MMU_DEVICE_ERCF_1_1', 'ercf_1_1'),
                    ('KLIPPER_MMU_DEVICE_ERCF_2_0', 'ercf_2_0'),
                    ('KLIPPER_MMU_DEVICE_ERCF_3_0', 'ercf_3_0'),
                    ('KLIPPER_MMU_DEVICE_BTT_VIVID', 'vivid'),
                    ('KLIPPER_MMU_DEVICE_CHAMELEON_3D', '3d_chameleon'),
                    ('KLIPPER_MMU_DEVICE_PICO_MMU', 'pico_mmu'))
        # Pico MMU's own dossier gear_ratio (1.28:1) is not two integers;
        # mcu/Kconfig.control's own help text picks 32:25, the same ratio
        # (1.28 == 32/25) as the menu's required whole numbers.
        gear_ratio_override = {'pico_mmu': ('32', '25')}
        # Every one of these dossiers' gear_load_speed is 100 mm/s, so the
        # driver ceiling (mcu/Kconfig.feeder) stays on the shared bucket
        # except HTLF, checked on its own below.
        for symbol, name in dossiers:
            with self.subTest(device=name):
                dossier = json.loads((ROOT / 'docs' / 'devices' / f'{name}.json').read_text())
                p = dossier['parameters']
                gear = dossier['hardware']['mmu_stepper unit0_gear']
                k, _ = self.configure(f'CONFIG_{symbol}=y\n')
                self.assertEqual(motion_defaults(k)[:4],
                                 [p['gear_homing_speed'], p['gear_unload_speed'],
                                  p['gear_load_accel'], p['gear_short_move_accel']])
                self.assertEqual(k.syms['KLIPPER_MMU_FEEDER_ROTATION_DISTANCE_UM'].str_value,
                                 str(round(float(gear['rotation_distance']) * 1000)))
                a, b = gear_ratio_override.get(name, gear['gear_ratio'].split(':'))
                self.assertEqual((k.syms['KLIPPER_MMU_FEEDER_GEAR_RATIO_A'].str_value,
                                  k.syms['KLIPPER_MMU_FEEDER_GEAR_RATIO_B'].str_value), (a, b))
                # KLIPPER_MMU_STEP_MAX_SPEED_MM_S lives under the pin-config
                # submenu (mcu/Kconfig.pins), invisible without it.
                k, _ = self.configure(f'CONFIG_{symbol}=y\nCONFIG_KLIPPER_MMU_PIN_CONFIG=y\n')
                self.assertEqual(k.syms['KLIPPER_MMU_STEP_MAX_SPEED_MM_S'].str_value,
                                 p['gear_load_speed'] if name == 'htlf' else '100')

    def test_previously_generic_devices_take_their_own_parking_distance(self):
        # MMX, MMX6, Low Rider, Pico MMU and HTLF all trace to the same 25 mm
        # gate_parking_distance (docs/devices/<device>.json); Tradrack's own
        # -17.5 mm rounds up to 18 mm, the nearest whole millimetre that does
        # not clear less than the dossier asks for. 3D Chameleon's own -250 mm
        # joins 3MS's existing bucket. See KLIPPER_MMU_UNLOAD_HUB_CLEAR_MM's
        # own help text (mcu/Kconfig.control).
        cases = (('KLIPPER_MMU_DEVICE_MMX', '25'),
                 ('KLIPPER_MMU_DEVICE_MMX6', '25'),
                 ('KLIPPER_MMU_DEVICE_LOW_RIDER', '25'),
                 ('KLIPPER_MMU_DEVICE_PICO_MMU', '25'),
                 ('KLIPPER_MMU_DEVICE_HTLF', '25'),
                 ('KLIPPER_MMU_DEVICE_TRADRACK', '18'),
                 ('KLIPPER_MMU_DEVICE_CHAMELEON_3D', '250'))
        for symbol, expect in cases:
            with self.subTest(symbol=symbol):
                k, _ = self.configure(f'CONFIG_{symbol}=y\n')
                self.assertEqual(k.syms['KLIPPER_MMU_UNLOAD_HUB_CLEAR_MM'].str_value, expect)

    def test_no_bay_devices_take_happy_hares_eject_distance(self):
        # KLIPPER_MMU_EJECT_FINAL_DISTANCE_MM lives under !KLIPPER_MMU_BAY_SENSORS
        # (mcu/Kconfig.control). Angry Beaver and the three QuattroBox revisions
        # default to Happy Hare's 20 mm, 3MS to its own 1500 mm. These are the
        # only five devices whose BAY sensors default off, so each sees the
        # symbol; BoxTurtle keeps BAY sensors on and hides it.
        cases = (('KLIPPER_MMU_DEVICE_ANGRY_BEAVER', '20'),
                 ('KLIPPER_MMU_DEVICE_THREE_MS', '1500'),
                 ('KLIPPER_MMU_DEVICE_QUATTROBOX_1_0', '20'),
                 ('KLIPPER_MMU_DEVICE_QUATTROBOX_1_1', '20'),
                 ('KLIPPER_MMU_DEVICE_QUATTROBOX_2', '20'))
        for symbol, expect in cases:
            with self.subTest(symbol=symbol):
                k, _ = self.configure(f'CONFIG_{symbol}=y\n')
                self.assertEqual(k.syms['KLIPPER_MMU_EJECT_FINAL_DISTANCE_MM'].str_value, expect)
        k, _ = self.configure('CONFIG_KLIPPER_MMU_DEVICE_BOXTURTLE=y\n')
        self.assertEqual(k.syms['KLIPPER_MMU_EJECT_FINAL_DISTANCE_MM'].visibility, 0)

    def test_emu_takes_its_own_gear_speeds_and_mechanics(self):
        # EMU's own dossier (docs/devices/emu.json) diverges from every other
        # Happy Hare device's shared bucket on every motion value, and its
        # 250 mm/s driver ceiling only clears the pulse budget
        # (src/step_dir_driver.c) at EMU's own 22.7316868 mm/1:1 mechanics,
        # not the BoxTurtle-shaped 22 mm/50:10 generic fallback -- see
        # KLIPPER_MMU_FEEDER_ROTATION_DISTANCE_UM/GEAR_RATIO_A/B's own help
        # text (mcu/Kconfig.control).
        k, _ = self.configure('CONFIG_KLIPPER_MMU_DEVICE_EMU=y\n')
        self.assertEqual(motion_defaults(k)[:4], ['80', '250', '120', '80'])
        k, _ = self.configure('CONFIG_KLIPPER_MMU_DEVICE_EMU=y\nCONFIG_KLIPPER_MMU_PIN_CONFIG=y\n')
        self.assertEqual(k.syms['KLIPPER_MMU_STEP_MAX_SPEED_MM_S'].str_value, '250')
        self.assertEqual(k.syms['KLIPPER_MMU_FEEDER_ROTATION_DISTANCE_UM'].str_value, '22732')
        self.assertEqual(k.syms['KLIPPER_MMU_FEEDER_GEAR_RATIO_A'].str_value, '1')
        self.assertEqual(k.syms['KLIPPER_MMU_FEEDER_GEAR_RATIO_B'].str_value, '1')
        self.compile(); p = self.profile()
        self.assertEqual(p['feed'], 11262)  # 80 mm/s at 140770 steps/m
        self.assertEqual(p['retract'], 35193)  # 250 mm/s: the driver ceiling itself
        # (mcu/feeder_config.c, not compiled by this harness) must clear the
        # 166666 steps/s pulse budget (3 us/3 us) for this same 250 mm/s;
        # at the old 22 mm/50:10 fallback it would have converted to 181818
        # steps/s, over that budget -- see KLIPPER_MMU_STEP_MAX_SPEED_MM_S's
        # own help text (mcu/Kconfig.feeder).
        self.assertLess(p['retract'], 166666)

    def test_claymore_takes_afcs_own_gear_speeds(self):
        # Claymore has no Happy Hare dossier. Its short_moves_speed/accel
        # (AFC.py:234-235, unoverridden by templates/AFC_Claymore_1-
        # AFC_Lite.cfg or extras/AFC_Claymore.py) are the feed/retract/accel
        # values; its own long_moves_speed (the template's override, line 17)
        # is the driver ceiling, the same "faster bowden phase becomes only
        # the ceiling" pattern as QIDI Box, HTLF and EMU.
        k, _ = self.configure('CONFIG_KLIPPER_MMU_DEVICE_CLAYMORE=y\n')
        self.assertEqual(motion_defaults(k)[:4], ['25', '25', '400', '400'])
        k, _ = self.configure('CONFIG_KLIPPER_MMU_DEVICE_CLAYMORE=y\nCONFIG_KLIPPER_MMU_PIN_CONFIG=y\n')
        self.assertEqual(k.syms['KLIPPER_MMU_STEP_MAX_SPEED_MM_S'].str_value, '300')

    def test_ercf_gate_parking_distance_maps_to_unload_hub_clear(self):
        # ERCF has no shared/virtual HUB switch, but Kconfig.ercf's own
        # gate_parking_distance is the same "retract behind the switch this
        # build treats as HUB" concept every other device's dossier maps to
        # KLIPPER_MMU_UNLOAD_HUB_CLEAR_MM (docs/devices/ercf_*.md's own
        # concept-mapping table). Absolute value of Kconfig.ercf's own
        # negative mm: 1.1 -23 (:174-176), 2.0 -13/-11 with Thumper Blocks
        # (:220-222), 3.0 -16 (:264).
        cases = (('CONFIG_KLIPPER_MMU_DEVICE_ERCF_1_1=y\n', '23'),
                 ('CONFIG_KLIPPER_MMU_DEVICE_ERCF_2_0=y\n', '13'),
                 ('CONFIG_KLIPPER_MMU_DEVICE_ERCF_2_0=y\nCONFIG_KLIPPER_MMU_ERCF_THUMPER_BLOCKS=y\n', '11'),
                 ('CONFIG_KLIPPER_MMU_DEVICE_ERCF_3_0=y\n', '16'))
        for values, expect in cases:
            with self.subTest(values=values):
                k, _ = self.configure(values)
                self.assertEqual(k.syms['KLIPPER_MMU_UNLOAD_HUB_CLEAR_MM'].str_value, expect)

    def test_ercf_variant_options_are_scoped_to_their_own_version(self):
        # installer/mmu_types/Kconfig.ercf:58-107: Binky, Springy and Triple
        # Decky are ERCF 1.1 Project Options; Thumper Blocks is 2.0 only;
        # Jack Rabbit is 3.0 only. Selecting the option's device symbol makes
        # it visible and settable; any other ERCF version leaves it invisible
        # and stuck at its default (n), even if a stale saved .config from a
        # different device tried to set it.
        cases = (('KLIPPER_MMU_ERCF_BINKY', 'ERCF_1_1', ('ERCF_2_0', 'ERCF_3_0')),
                 ('KLIPPER_MMU_ERCF_SPRINGY', 'ERCF_1_1', ('ERCF_2_0', 'ERCF_3_0')),
                 ('KLIPPER_MMU_ERCF_TRIPLE_DECKY', 'ERCF_1_1', ('ERCF_2_0', 'ERCF_3_0')),
                 ('KLIPPER_MMU_ERCF_THUMPER_BLOCKS', 'ERCF_2_0', ('ERCF_1_1', 'ERCF_3_0')),
                 ('KLIPPER_MMU_ERCF_JACK_RABBIT', 'ERCF_3_0', ('ERCF_1_1', 'ERCF_2_0')))
        for option, owner, others in cases:
            with self.subTest(option=option, owner=owner):
                k, _ = self.configure(f'CONFIG_KLIPPER_MMU_DEVICE_{owner}=y\nCONFIG_{option}=y\n')
                self.assertEqual(k.syms[option].str_value, 'y')
                self.assertGreater(k.syms[option].visibility, 0)
            for other in others:
                with self.subTest(option=option, other=other):
                    k, _ = self.configure(f'CONFIG_KLIPPER_MMU_DEVICE_{other}=y\nCONFIG_{option}=y\n')
                    self.assertEqual(k.syms[option].str_value, 'n')
                    self.assertEqual(k.syms[option].visibility, 0)

    def test_ercf_1_1_triple_decky_and_springy_change_cad_defaults(self):
        # extras/mmu/unit/selectors/mmu_linear_selector.py:94-107: "t" in the
        # version string changes cad_gate_width/cad_block_width; "s" changes
        # only cad_last_gate_offset (never a gate position). Every one of the
        # four combinations is a real Happy Hare version string (Kconfig.ercf:
        # 139-146: "1.1", "1.1s", "1.1t", "1.1st").
        cases = {
            '':                                                  dict(width=21000, block=5000, last=2000,
                positions='4.2,25.2,46.2,72.2,93.2,114.2,140.2,161.2,182.2'),
            'CONFIG_KLIPPER_MMU_ERCF_SPRINGY=y\n':                dict(width=21000, block=5000, last=1200,
                positions='4.2,25.2,46.2,72.2,93.2,114.2,140.2,161.2,182.2'),
            'CONFIG_KLIPPER_MMU_ERCF_TRIPLE_DECKY=y\n':           dict(width=23000, block=0, last=2000,
                positions='4.2,27.2,50.2,73.2,96.2,119.2,142.2,165.2,188.2'),
            'CONFIG_KLIPPER_MMU_ERCF_SPRINGY=y\nCONFIG_KLIPPER_MMU_ERCF_TRIPLE_DECKY=y\n':
                dict(width=23000, block=0, last=1200,
                positions='4.2,27.2,50.2,73.2,96.2,119.2,142.2,165.2,188.2'),
        }
        for mods, expect in cases.items():
            with self.subTest(mods=mods or 'none'):
                k, _ = self.configure('CONFIG_KLIPPER_MMU_DEVICE_ERCF_1_1=y\n' + mods)
                self.assertEqual(int(k.syms['KLIPPER_MMU_SELECTOR_GATE_WIDTH_UM'].str_value), expect['width'])
                self.assertEqual(int(k.syms['KLIPPER_MMU_SELECTOR_BLOCK_WIDTH_UM'].str_value), expect['block'])
                self.assertEqual(int(k.syms['KLIPPER_MMU_SELECTOR_LAST_GATE_OFFSET_UM'].str_value), expect['last'])
                self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_GATE_POSITIONS_MM'].str_value, expect['positions'])

    def test_ercf_3_0_jack_rabbit_clears_the_grip_servo(self):
        # installer/mmu_types/Kconfig.ercf:97-104: Jack Rabbit implies the
        # plain servo-less LinearSelector and PARAM_FILAMENT_ALWAYS_GRIPPED.
        k, _ = self.configure('CONFIG_KLIPPER_MMU_DEVICE_ERCF_3_0=y\n')
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_LINEAR_HAS_SERVO'].str_value, 'y')
        self.assertEqual(k.syms['KLIPPER_MMU_SERVO'].str_value, 'y')
        k, _ = self.configure('CONFIG_KLIPPER_MMU_DEVICE_ERCF_3_0=y\nCONFIG_KLIPPER_MMU_ERCF_JACK_RABBIT=y\n')
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_LINEAR_HAS_SERVO'].str_value, 'n')
        self.assertEqual(k.syms['KLIPPER_MMU_SERVO'].str_value, 'n')
        self.compile(); p = self.profile()
        self.assertEqual(p['has_servo'], 0)
        self.assertEqual(p['selector_kind'], 1)  # KM_SELECTOR_LINEAR, unchanged

    def test_emu_is_profile_implemented_like_every_other_catalogue_device(self):
        # Owner follow-up (2026-09-26): EMU's "(planned)"/"unimplemented"
        # label was stale -- it passes profile, resource, native startup,
        # saved-image hosted cycle and release-matrix checks
        # (docs/plan/device-and-stm32-rollout.md EMU row). Folding it into
        # KLIPPER_MMU_PROFILE_IMPLEMENTED removes the EMU-only bypasses on
        # KLIPPER_MMU_LOAD_SENSORS/LOAD_ACTIVE_HIGH/LOAD_PULL/PINS_LOAD and
        # mcu/config.c's runtime check with no change to what an EMU build
        # resolves to.
        k, _ = self.configure('CONFIG_KLIPPER_MMU_DEVICE_EMU=y\n')
        self.assertEqual(k.syms['KLIPPER_MMU_PROFILE_IMPLEMENTED'].str_value, 'y')
        self.assertEqual(k.syms['KLIPPER_MMU_LOAD_SENSORS'].str_value, 'y')
        self.assertEqual(k.syms['KLIPPER_MMU_LOAD_SENSORS'].visibility, 0)  # forced, not user-editable
        self.compile(); p = self.profile()
        self.assertEqual([p['load'], p['virtual_hub']], [1, 1])


    # -- Selector devices (LINEAR/ROTARY/SERVO, mcu/Kconfig.selector) --

    SELECTOR_MOTION = ('SELECTOR_KIND_LINEAR', 'SELECTOR_KIND_ROTARY', 'SELECTOR_KIND_SERVO',
                        'SELECTOR_STEPPER', 'SERVO', 'LANE_COUNT',
                        'SELECTOR_ROTATION_DISTANCE_UM', 'SELECTOR_GEAR_RATIO_A', 'SELECTOR_GEAR_RATIO_B',
                        'SELECTOR_MOVE_SPEED_MM_S', 'SELECTOR_HOMING_SPEED_MM_S', 'SELECTOR_ACCEL_MM_S2',
                        'SELECTOR_GATE0_POS_UM', 'SELECTOR_GATE_WIDTH_UM', 'SELECTOR_TOLERANCE_UM',
                        'SELECTOR_LAST_GATE_OFFSET_UM', 'SELECTOR_BLOCK_WIDTH_UM')

    def selector_syms(self, k):
        return {name: k.syms['KLIPPER_MMU_' + name].str_value for name in self.SELECTOR_MOTION}

    def test_selector_kind_and_mechanics_resolve_per_device(self):
        # Every value here is Happy Hare's own (mcu/Kconfig.selector cites
        # file:line); see that file's help text for the citation.
        cases = {
            'ERCF_1_1':     dict(kind='LINEAR', lanes=9,  rot=40000, gate0=4200, width=21000,
                                  last=2000, block=5000, move=200, homing=60, accel=1200),
            'ERCF_2_0':     dict(kind='LINEAR', lanes=8,  rot=40000, gate0=4000, width=23000,
                                  last=14400, block=0, move=200, homing=60, accel=1200),
            'ERCF_3_0':     dict(kind='LINEAR', lanes=8,  rot=40000, gate0=4000, width=23000,
                                  last=14400, block=0, move=200, homing=60, accel=1200),
            'TRADRACK':     dict(kind='LINEAR', lanes=10, rot=40000, gate0=2500, width=17000,
                                  last=0, block=0, move=200, homing=60, accel=1200),
            'CHAMELEON_3D': dict(kind='ROTARY', lanes=4,  rot=40000, gate0=4000, width=25000,
                                  last=0, block=0, move=200, homing=60, accel=1200),
            'MMX6':         dict(kind='ROTARY', lanes=6,  rot=40000, gate0=4000, width=25000,
                                  last=0, block=0, move=200, homing=60, accel=1200),
            'HTLF':         dict(kind='ROTARY', lanes=4,  rot=32000, gate0=4000, width=25000,
                                  last=0, block=0, move=50, homing=60, accel=50),
        }
        for device, expect in cases.items():
            with self.subTest(device=device):
                k, _ = self.configure(f'CONFIG_KLIPPER_MMU_DEVICE_{device}=y\n')
                s = self.selector_syms(k)
                self.assertEqual(s['SELECTOR_KIND_' + expect['kind']], 'y')
                self.assertEqual(s['SELECTOR_STEPPER'], 'y')
                self.assertEqual(s['SERVO'], 'y' if device != 'CHAMELEON_3D' and device != 'MMX6'
                                 and device != 'HTLF' else 'n')
                self.assertEqual(int(s['LANE_COUNT']), expect['lanes'])
                self.assertEqual(int(s['SELECTOR_ROTATION_DISTANCE_UM']), expect['rot'])
                self.assertEqual((s['SELECTOR_GEAR_RATIO_A'], s['SELECTOR_GEAR_RATIO_B']), ('1', '1'))
                self.assertEqual(int(s['SELECTOR_MOVE_SPEED_MM_S']), expect['move'])
                self.assertEqual(int(s['SELECTOR_HOMING_SPEED_MM_S']), expect['homing'])
                self.assertEqual(int(s['SELECTOR_ACCEL_MM_S2']), expect['accel'])
                self.assertEqual(int(s['SELECTOR_GATE0_POS_UM']), expect['gate0'])
                self.assertEqual(int(s['SELECTOR_GATE_WIDTH_UM']), expect['width'])
                self.assertEqual(int(s['SELECTOR_TOLERANCE_UM']), 15000)
                self.assertEqual(int(s['SELECTOR_LAST_GATE_OFFSET_UM']), expect['last'])
                self.assertEqual(int(s['SELECTOR_BLOCK_WIDTH_UM']), expect['block'])

    def test_mmx_is_servo_kind_with_no_selector_stepper(self):
        k, _ = self.configure('CONFIG_KLIPPER_MMU_DEVICE_MMX=y\n')
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_KIND_SERVO'].str_value, 'y')
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_STEPPER'].str_value, 'n')
        self.assertEqual(k.syms['KLIPPER_MMU_SERVO'].str_value, 'y')
        self.assertEqual(k.syms['KLIPPER_MMU_LANE_COUNT'].str_value, '4')
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_SERVO_GATE_ANGLES_DEG'].str_value, '60,0,180,120')

    @staticmethod
    def km_max_lanes():
        """KM_MAX_LANES straight from the contract header: this worktree only
        has the contract, so it does not know whether Agent A has raised it
        from 8 to 12 yet."""
        text = (ROOT / 'include/klipper_mmu/types.h').read_text()
        return int(re.search(r'#define\s+KM_MAX_LANES\s+(\d+)u?', text).group(1))

    def test_ercf_1_1_and_tradrack_gate_count_vs_this_builds_max_lanes(self):
        # Both devices' own dossier gate counts (9, 10) are real Happy Hare
        # numbers, not invented. With KM_MAX_LANES=8 (this contract-only
        # worktree today) the profile is correctly rejected without
        # corrupting the output -- the "expected" runtime rejection the
        # brief describes. Once KM_MAX_LANES reaches 12 (Agent A, elsewhere)
        # both devices build fully with their own lane count instead; this
        # test passes either way by reading KM_MAX_LANES itself.
        max_lanes = self.km_max_lanes()
        cases = {'ERCF_1_1': (9, [336, 2016, 3696, 5776, 7456, 9136, 11216, 12896, 14576]),
                 'TRADRACK': (10, [200, 1560, 2920, 4280, 5640, 7000, 8360, 9720, 11080, 12440])}
        for device, (lanes, steps) in cases.items():
            with self.subTest(device=device):
                self.configure(f'CONFIG_KLIPPER_MMU_DEVICE_{device}=y\n')
                self.compile()
                if lanes > max_lanes:
                    self.profile('invalid')
                else:
                    p = self.profile()
                    self.assertEqual(p['lanes'], lanes)
                    self.assertEqual(p['gate_steps'][:lanes], steps)

    def test_linear_gate_positions_are_a_cad_estimate_matching_happy_hare_geometry(self):
        # Owner follow-up (2026-09-26): LINEAR ships a CAD-geometry ESTIMATE
        # (_get_max_selector_movement's own per-gate terms,
        # mmu_linear_selector.py:338-355), not Happy Hare's own measured
        # value -- Happy Hare only measures a LINEAR selector with
        # MMU_CALIBRATE_SELECTOR. ERCF 2.0/3.0 fit under KM_MAX_LANES in
        # every worktree, so their full profile build is checked here;
        # ERCF 1.1/Tradrack's estimate is checked above wherever they fit.
        cases = {
            # gate0_pos 4.0, gate_width 23.0, no block term at version >= 2.0
            # (mmu_linear_selector.py:97-98,349-351).
            'ERCF_2_0': [320, 2160, 4000, 5840, 7680, 9520, 11360, 13200],
            'ERCF_3_0': [320, 2160, 4000, 5840, 7680, 9520, 11360, 13200],
        }
        for device, steps in cases.items():
            with self.subTest(device=device):
                k, _ = self.configure(f'CONFIG_KLIPPER_MMU_DEVICE_{device}=y\n')
                self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_GATE_POSITIONS_MM'].str_value,
                                 '4.0,27.0,50.0,73.0,96.0,119.0,142.0,165.0')
                self.compile(); p = self.profile()
                self.assertEqual(p['has_selector'], 1)
                self.assertEqual(p['selector_kind'], 1)  # KM_SELECTOR_LINEAR
                self.assertEqual(p['gate_steps'], steps + [0] * (self.km_max_lanes() - len(steps)))
                self.assertEqual(p['has_servo'], 1)

    def test_ercf_1_1_gate_estimate_includes_the_bearing_block_term(self):
        # gate0_pos 4.2, gate_width 21.0, plus (i // 3) * block_width 5.0
        # every third gate (mmu_linear_selector.py:83,352; version < 2.0,
        # no "t" in the version string) -- the one LINEAR device where the
        # estimate is not simply gate0 + i*width.
        k, _ = self.configure('CONFIG_KLIPPER_MMU_DEVICE_ERCF_1_1=y\n')
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_GATE_POSITIONS_MM'].str_value,
                         '4.2,25.2,46.2,72.2,93.2,114.2,140.2,161.2,182.2')

    def test_tradrack_gate_estimate_has_no_bearing_block_term(self):
        # gate0_pos 2.5, gate_width 17.0 (mmu_linear_selector.py:111-112);
        # cad_block_width only ever applies to ERCF's pre-2.0 branch.
        k, _ = self.configure('CONFIG_KLIPPER_MMU_DEVICE_TRADRACK=y\n')
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_GATE_POSITIONS_MM'].str_value,
                         '2.5,19.5,36.5,53.5,70.5,87.5,104.5,121.5,138.5,155.5')

    def test_malformed_or_short_gate_list_is_rejected_not_truncated(self):
        # A ROTARY device's default list works; overriding it to the wrong
        # length or with a malformed entry must still be refused, never
        # silently zero-filled (the first attempt's bug).
        base = 'CONFIG_KLIPPER_MMU_DEVICE_HTLF=y\n'
        for bad in ('CONFIG_KLIPPER_MMU_SELECTOR_GATE_POSITIONS_MM="4.0,29.0,54.0"\n',
                    'CONFIG_KLIPPER_MMU_SELECTOR_GATE_POSITIONS_MM="4.0,29.x,54.0,79.0"\n',
                    'CONFIG_KLIPPER_MMU_SELECTOR_GATE_POSITIONS_MM="4.0,,54.0,79.0"\n'):
            with self.subTest(bad=bad):
                self.configure(base + bad)
                self.compile()
                self.profile('invalid')

    def test_rotary_devices_gate_steps_directions_and_release_match_happy_hare(self):
        # cad_gate0_pos + i*cad_gate_width (mmu_rotary_selector.py:575-576),
        # converted at the cited mechanics, and the raw direction/release
        # lists mapped through set_gear_direction's 0=normal/1=reverse.
        cases = {
            'CHAMELEON_3D': dict(gate_steps=[320, 2320, 4320, 6320],
                                  directions=[0, 0, 1, 1], release=[2, 3, 0, 1],
                                  home_speed=4800, home_accel=96000, max_travel=7520,
                                  deadline=4700000),
            'MMX6':         dict(gate_steps=[320, 2320, 4320, 6320, 8320, 10320],
                                  directions=[1, 1, 1, 1, 1, 1], release=[1, 2, 3, 4, 5, 0],
                                  home_speed=4800, home_accel=96000, max_travel=11520,
                                  deadline=7200000),
            'HTLF':         dict(gate_steps=[400, 2900, 5400, 7900],
                                  directions=[1, 1, 1, 1], release=[1, 2, 3, 0],
                                  home_speed=6000, home_accel=5000, max_travel=9400,
                                  deadline=4700000),
        }
        for device, expect in cases.items():
            with self.subTest(device=device):
                self.configure(f'CONFIG_KLIPPER_MMU_DEVICE_{device}=y\n')
                self.compile(); p = self.profile()
                self.assertEqual(p['has_selector'], 1)
                self.assertEqual(p['selector_kind'], 2)  # KM_SELECTOR_ROTARY
                lanes = len(expect['gate_steps'])
                self.assertEqual(p['gate_steps'][:lanes], expect['gate_steps'])
                self.assertEqual(p['gate_direction'][:lanes], expect['directions'])
                self.assertEqual(p['release_gate'][:lanes], expect['release'])
                self.assertEqual(p['home_speed'], expect['home_speed'])
                self.assertEqual(p['home_accel'], expect['home_accel'])
                self.assertEqual(p['home_decel'], expect['home_accel'])
                self.assertEqual(p['home_max_travel'], expect['max_travel'])
                self.assertEqual(p['home_deadline_us'], expect['deadline'])
                self.assertEqual(p['home_switch_age_us'], 200000)
                self.assertEqual(p['home_direction'], 0)  # KM_DIR_REVERSE (toward min)
                self.assertEqual(p['has_servo'], 0)
                self.assertEqual((p['servo_engage_us'], p['servo_release_us'], p['servo_move_us']), (0, 0, 0))

    def test_indexed_selector_gate_order_and_mechanics_match_happy_hare(self):
        # BTT ViViD (klipper_mmu/selector.h KM_SELECTOR_INDEXED): no gate
        # position table -- selector_gate_order (installer/mmu_types/
        # Kconfig.vvd:84-85), the 25:10/360 mm selector mechanics
        # (Kconfig.vvd:99-102) and the generic 10 mm endstop width
        # (installer/Kconfig.selector_stepper:140-143) resolve to these
        # steps; the whole-drum travel bound is 4 gates * 90 mm * 2
        # rotations = 720 mm (mmu_indexed_selector.py:184-186). Unlike
        # LINEAR/ROTARY, homing uses the same speed/accel as an ordinary
        # move (no separate Happy Hare homing speed for this kind).
        k, _ = self.configure('CONFIG_KLIPPER_MMU_DEVICE_BTT_VIVID=y\n')
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_KIND_INDEXED'].str_value, 'y')
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_STEPPER'].str_value, 'y')
        self.assertEqual(k.syms['KLIPPER_MMU_SERVO'].str_value, 'n')
        self.assertEqual(k.syms['KLIPPER_MMU_LANE_COUNT'].str_value, '4')
        self.assertEqual((k.syms['KLIPPER_MMU_SELECTOR_GEAR_RATIO_A'].str_value,
                          k.syms['KLIPPER_MMU_SELECTOR_GEAR_RATIO_B'].str_value), ('25', '10'))
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_ROTATION_DISTANCE_UM'].str_value, '360000')
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_GATE_ORDER'].str_value, '0,3,1,2')
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_ENDSTOP_WIDTH_MM'].str_value, '10')
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_INDEXED_MAX_TRAVEL_MM'].str_value, '720')
        self.compile(); p = self.profile()
        self.assertEqual(p['has_selector'], 1)
        self.assertEqual(p['selector_kind'], 4)  # KM_SELECTOR_INDEXED
        self.assertEqual(p['gate_order'][:4], [0, 3, 1, 2])
        self.assertEqual(p['gate_steps'][:4], [0, 0, 0, 0])  # No position table.
        self.assertEqual(p['center_nudge_steps'], 111)  # Half of 10 mm's 222 steps.
        self.assertEqual(p['selector_velocity'], 4444)
        self.assertEqual(p['selector_accel'], 26666)
        self.assertEqual(p['home_speed'], p['selector_velocity'])  # No separate homing speed.
        self.assertEqual(p['home_accel'], p['selector_accel'])
        self.assertEqual(p['home_decel'], p['selector_accel'])
        self.assertEqual(p['home_max_travel'], 16000)  # 720 mm.
        self.assertEqual(p['home_deadline_us'], 10800000)
        self.assertEqual(p['home_switch_age_us'], 200000)
        self.assertEqual(p['has_servo'], 0)
        self.assertEqual((p['servo_engage_us'], p['servo_release_us'], p['servo_move_us']), (0, 0, 0))

    def test_claymore_forward_only_indexed_profile_matches_afc(self):
        # Claymore has no Happy Hare dossier; every number here is AFC's own
        # (build/upstream/afc-5ea84ea, pinned read-only), not Happy Hare's:
        # templates/AFC_Claymore_1-AFC_Lite.cfg's [AFC_stepper Claymore_drive]
        # (rotation_distance 35.1 mm, gear_ratio 40:20, run_current 0.8 A) and
        # [AFC_stepper Claymore_selector] (35.5 mm, 40:20, 0.8 A); AFC_vivid.py
        # (Claymore's own base class) :74-76 selector_homing_speed=150,
        # selector_homing_accel=150, max_selector_movement=800, none
        # overridden by AFC_Claymore or its template. Claymore's AFC_hub
        # wires a real HUB switch (config/mcu/AFC_Lite_Claymore.cfg's
        # HUB=PE8, distinct from LOAD1-4), so it does not select
        # KLIPPER_MMU_VIRTUAL_HUB, unlike EMU/KMS. Its own per-lane sensor is
        # LOAD1-4, but AFC_lane.py's prep_state stays permanently unset
        # without a separate prep pin (none exists here), so this catalogue's
        # BAY sensor -- not LOAD -- is what those pins implement; LOAD_SENSORS
        # and VIRTUAL_HUB both stay at their ordinary off default.
        k, _ = self.configure('CONFIG_KLIPPER_MMU_PIN_CONFIG=y\n'
                              'CONFIG_KLIPPER_MMU_DEVICE_CLAYMORE=y\n')
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_KIND_INDEXED'].str_value, 'y')
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_STEPPER'].str_value, 'y')
        self.assertEqual(k.syms['KLIPPER_MMU_LANE_COUNT'].str_value, '4')
        self.assertEqual(k.syms['KLIPPER_MMU_POWERED_REWIND'].str_value, 'y')
        self.assertEqual(k.syms['KLIPPER_MMU_BAY_SENSORS'].str_value, 'y')
        self.assertEqual(k.syms['KLIPPER_MMU_LOAD_SENSORS'].str_value, 'n')
        self.assertEqual(k.syms['KLIPPER_MMU_VIRTUAL_HUB'].str_value, 'n')
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_GATE_ORDER'].str_value, '')
        self.assertEqual((k.syms['KLIPPER_MMU_SELECTOR_GEAR_RATIO_A'].str_value,
                          k.syms['KLIPPER_MMU_SELECTOR_GEAR_RATIO_B'].str_value), ('40', '20'))
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_ROTATION_DISTANCE_UM'].str_value, '35500')
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_INDEXED_MAX_TRAVEL_MM'].str_value, '800')
        self.assertEqual((k.syms['KLIPPER_MMU_FEEDER_GEAR_RATIO_A'].str_value,
                          k.syms['KLIPPER_MMU_FEEDER_GEAR_RATIO_B'].str_value), ('40', '20'))
        self.assertEqual(k.syms['KLIPPER_MMU_FEEDER_ROTATION_DISTANCE_UM'].str_value, '35100')
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_RUN_CURRENT'].str_value, '800')
        self.assertEqual(k.syms['KLIPPER_MMU_RUN_CURRENT'].str_value, '800')
        self.compile(); p = self.profile()
        self.assertEqual(p['has_selector'], 1)
        self.assertEqual(p['selector_kind'], 4)  # KM_SELECTOR_INDEXED
        self.assertEqual(p['powered'], 1)
        self.assertEqual(p['bay'], 1)
        self.assertEqual(p['load'], 0)
        self.assertEqual(p['virtual_hub'], 0)
        self.assertEqual(p['gate_order'][:4], [0, 0, 0, 0])  # AFC's forward-only rule: no order.
        self.assertEqual(p['gate_steps'][:4], [0, 0, 0, 0])  # No position table.
        self.assertEqual(p['center_nudge_steps'], 0)  # No calibrated switch width either.
        self.assertEqual(p['selector_velocity'], 27042)
        self.assertEqual(p['selector_accel'], 27042)
        self.assertEqual(p['home_speed'], p['selector_velocity'])  # No separate homing speed.
        self.assertEqual(p['home_accel'], p['selector_accel'])
        self.assertEqual(p['home_decel'], p['selector_accel'])
        self.assertEqual(p['home_max_travel'], 144225)  # 800 mm.
        self.assertEqual(p['home_deadline_us'], 16000000)
        self.assertEqual(p['home_switch_age_us'], 200000)
        self.assertEqual(p['has_servo'], 0)

    def test_indexed_selector_without_gate_order_or_travel_bound_refuses_to_start(self):
        # "Never invent a number" (AGENTS.md): the bare INDEXED kind with no
        # device match has empty gate_order and a zero travel bound, so the
        # profile is refused at startup rather than guessing a device this
        # catalogue carries no CAD evidence for.
        k, _ = self.configure('CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED=y\n'
                              'CONFIG_KLIPPER_MMU_DEVICE_CUSTOM=y\n')
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_GATE_ORDER'].str_value, '')
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_INDEXED_MAX_TRAVEL_MM'].str_value, '')
        self.compile()
        self.profile('invalid')

    def test_mmx_servo_gate_angles_convert_to_pulse_widths(self):
        # servo_gate_angles "60,0,180,120" (docs/devices/mmx.json:159) across
        # the generic 180 degree / 1000-2000 us hardware range.
        self.configure('CONFIG_KLIPPER_MMU_DEVICE_MMX=y\n')
        self.compile(); p = self.profile()
        self.assertEqual(p['has_selector'], 1)
        self.assertEqual(p['selector_kind'], 3)  # KM_SELECTOR_SERVO
        self.assertEqual(p['has_servo'], 1)
        self.assertEqual(p['servo_gate_us'][:4], [1333, 1000, 2000, 1667])
        self.assertEqual(p['servo_dwell_ms'], 1000)
        self.assertEqual(p['servo_hold'], 0)
        # No selector stepper: the home/gate-step fields never apply.
        self.assertEqual(p['home_speed'], 0)
        self.assertEqual(p['gate_steps'], [0] * self.km_max_lanes())

    def test_low_rider_servo_angles_convert_to_pulse_widths(self):
        # servo_gate_angles "30, 60, 90, 120, 150, 180", release 0 (docs/devices/low_rider.json)
        self.configure('CONFIG_KLIPPER_MMU_DEVICE_LOW_RIDER=y\n')
        self.compile(); p = self.profile()
        self.assertEqual(p['has_selector'], 1)
        self.assertEqual(p['selector_kind'], 3)  # KM_SELECTOR_SERVO
        self.assertEqual(p['has_servo'], 1)
        self.assertEqual(p['lanes'], 6)
        self.assertEqual(p['servo_gate_us'][:6], [1167, 1333, 1500, 1667, 1833, 2000])
        self.assertEqual(p['servo_release_us'], 1000)
        self.assertEqual(p['servo_dwell_ms'], 1000)
        self.assertEqual(p['servo_hold'], 0)
        self.assertEqual(p['home_speed'], 0)

    def test_pico_mmu_servo_angles_empty_starts_uncalibrated(self):
        # Happy Hare ships Pico MMU with no servo_gate_angles (it calibrates
        # its cam instead); startup now succeeds with every gate uncalibrated
        # (servo_gate_us 0, klipper_mmu/selector.h) instead of refusing the
        # whole profile. Selecting an uncalibrated gate is refused later, at
        # admission (tests/test_selector_core.c).
        self.configure('CONFIG_KLIPPER_MMU_DEVICE_PICO_MMU=y\n')
        self.compile(); p = self.profile()
        self.assertEqual(p['has_selector'], 1)
        self.assertEqual(p['selector_kind'], 3)  # KM_SELECTOR_SERVO
        self.assertEqual(p['servo_gate_us'][:p['lanes']], [0] * p['lanes'])

        # Compile with explicit angles (140 deg range)
        self.configure('CONFIG_KLIPPER_MMU_DEVICE_PICO_MMU=y\nCONFIG_KLIPPER_MMU_SELECTOR_SERVO_GATE_ANGLES_DEG="0,45,90,140"\n')
        self.compile(); p = self.profile()
        self.assertEqual(p['has_selector'], 1)
        self.assertEqual(p['selector_kind'], 3)
        self.assertEqual(p['lanes'], 4)
        self.assertEqual(p['servo_gate_us'][:4], [1000, 1321, 1643, 2000])
        self.assertEqual(p['servo_dwell_ms'], 1200)

    def test_linear_grip_servo_angles_match_dossier(self):
        cases = {
            'ERCF_1_1': dict(engage=100, release=30, move=30, dwell=500),
            'ERCF_2_0': dict(engage=100, release=30, move=50, dwell=500),
            'ERCF_3_0': dict(engage=100, release=30, move=50, dwell=500),
            'TRADRACK': dict(engage=1, release=145, move=145, dwell=1000),
        }
        for device, expect in cases.items():
            with self.subTest(device=device):
                k, _ = self.configure(f'CONFIG_KLIPPER_MMU_DEVICE_{device}=y\n')
                self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_LINEAR_HAS_SERVO'].str_value, 'y')
                self.assertEqual(int(k.syms['KLIPPER_MMU_SELECTOR_SERVO_ENGAGE_ANGLE_DEG'].str_value),
                                 expect['engage'])
                self.assertEqual(int(k.syms['KLIPPER_MMU_SELECTOR_SERVO_RELEASE_ANGLE_DEG'].str_value),
                                 expect['release'])
                self.assertEqual(int(k.syms['KLIPPER_MMU_SELECTOR_SERVO_MOVE_ANGLE_DEG'].str_value),
                                 expect['move'])
                self.assertEqual(int(k.syms['KLIPPER_MMU_SELECTOR_SERVO_DWELL_MS'].str_value),
                                 expect['dwell'])

    def test_non_selector_device_dumps_has_selector_zero(self):
        k, _ = self.configure()
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_KIND_NONE'].str_value, 'y')
        self.assertEqual(k.syms['KLIPPER_MMU_SELECTOR_STEPPER'].str_value, 'n')
        self.assertEqual(k.syms['KLIPPER_MMU_SERVO'].str_value, 'n')
        self.compile(); p = self.profile()
        self.assertEqual(p['has_selector'], 0)
        self.assertEqual(p['selector_kind'], 0)  # KM_SELECTOR_NONE
        self.assertEqual(p['gate_steps'], [0] * self.km_max_lanes())
        self.assertEqual(p['has_servo'], 0)

    def test_feeder_rotation_distances_stay_unset_when_blank(self):
        # BoxTurtle (default device, 4 lanes): a blank
        # KLIPPER_MMU_FEEDER_LANE_ROTATION_DISTANCES_UM (the default, no host
        # override either) leaves feeder_rotation_distance_um at 0 -- the
        # feature untouched, every lane implicitly 1:1 with the reference,
        # engage_feeder never calling the trajectory's lane-scale API at all
        # (src/operation_core.c) -- not merely a 1:1 scale applied every time.
        self.configure()
        self.compile(); p = self.profile()
        self.assertEqual(p['feeder_rotation_distance_um'], 0)
        self.assertEqual(p['lane_rotation_distance_um'][:4], [0] * 4)

    def test_feeder_rotation_distances_list_overrides_kconfig_default(self):
        self.configure('CONFIG_KLIPPER_MMU_FEEDER_LANE_ROTATION_DISTANCES_UM='
                       '"22000,23000,21000,22500"\n')
        self.compile(); p = self.profile()
        self.assertEqual(p['lane_rotation_distance_um'][:4], [22000, 23000, 21000, 22500])

    def test_feeder_rotation_distances_malformed_or_short_list_rejected(self):
        # Same rule as every other per-lane Kconfig list here: never silently
        # zero-filled or truncated.
        for bad in ('CONFIG_KLIPPER_MMU_FEEDER_LANE_ROTATION_DISTANCES_UM="22000,23000,21000"\n',
                   'CONFIG_KLIPPER_MMU_FEEDER_LANE_ROTATION_DISTANCES_UM="22000,21.x,21000,22500"\n',
                   'CONFIG_KLIPPER_MMU_FEEDER_LANE_ROTATION_DISTANCES_UM="22000,,21000,22500"\n'):
            with self.subTest(bad=bad):
                self.configure(bad)
                self.compile()
                self.profile('invalid')

    def test_feeder_rotation_distances_out_of_0_8_to_1_25x_range_rejected(self):
        # 22000 x 0.8 = 17600, 22000 x 1.25 = 27500 (km_profile_validate);
        # one step outside either bound is refused, one step inside accepts.
        for bad in ('CONFIG_KLIPPER_MMU_FEEDER_LANE_ROTATION_DISTANCES_UM="17599,22000,22000,22000"\n',
                   'CONFIG_KLIPPER_MMU_FEEDER_LANE_ROTATION_DISTANCES_UM="27501,22000,22000,22000"\n'):
            with self.subTest(bad=bad):
                self.configure(bad)
                self.compile()
                self.profile('invalid')
        self.configure('CONFIG_KLIPPER_MMU_FEEDER_LANE_ROTATION_DISTANCES_UM="17600,27500,22000,22000"\n')
        self.compile(); p = self.profile()
        self.assertEqual(p['lane_rotation_distance_um'][:4], [17600, 27500, 22000, 22000])

    def test_feeder_rotation_command_overrides_default_and_rejects_out_of_range(self):
        self.configure()
        self.compile()
        p = self.profile('feeder-rotation', 2, 23000)
        self.assertEqual(p['lane_rotation_distance_um'][2], 23000)
        self.assertEqual(p['lane_rotation_distance_um'][0], 22000)  # untouched lane keeps the default
        self.assertEqual(self.profile('feeder-rotation', 4, 22000),
                         {'shutdown': 'Invalid feeder rotation distance'})  # BoxTurtle has 4 lanes (0..3)

    def test_selector_gate_command_overrides_default_and_rejects_out_of_range(self):
        self.configure('CONFIG_KLIPPER_MMU_DEVICE_HTLF=y\n')
        self.compile()
        # Inside HTLF's travel (4 + 4 x 25 + 15 mm at 100 steps/mm); the core
        # refuses a calibrated gate past the selector's max travel.
        p = self.profile('selector-gate', 2, 5000)
        self.assertEqual(p['gate_steps'][2], 5000)
        self.assertEqual(p['gate_steps'][0], 400)  # untouched gates keep the Kconfig default
        self.assertEqual(self.profile('selector-gate', 4, 0),
                         {'shutdown': 'Invalid selector gate index'})  # HTLF has 4 gates (0..3)


if __name__ == '__main__':
    unittest.main()
