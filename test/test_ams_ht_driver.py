import importlib
import os
import sys
import types
import unittest

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if "oamspkg" not in sys.modules:
    pkg = types.ModuleType("oamspkg")
    pkg.__path__ = [_SRC]
    sys.modules["oamspkg"] = pkg
sys.modules.setdefault("mcu", types.ModuleType("mcu"))
oams = importlib.import_module("oamspkg.oams")
OAMS = oams.OAMS


class FakeQuery:
    def __init__(self, result=0, payload=None):
        self.result = result
        self.payload = payload
        self.sent = []

    def send(self, args=()):
        self.sent.append(args)
        if callable(self.payload):
            return self.payload(args)
        if self.payload is not None:
            return dict(self.payload)
        return {"result": self.result}


class FakeGcmd:
    def __init__(self, values):
        self.values = values
        self.responses = []

    def get_float(self, name, default=None, minval=None, maxval=None):
        value = float(self.values.get(name, default))
        if minval is not None and value < minval:
            raise ValueError("below minimum")
        if maxval is not None and value > maxval:
            raise ValueError("above maximum")
        return value

    def get(self, name, default=None):
        return self.values.get(name, default)

    def get_int(self, name, default=None, minval=None, maxval=None):
        value = int(self.values.get(name, default))
        if minval is not None and value < minval:
            raise ValueError("below minimum")
        if maxval is not None and value > maxval:
            raise ValueError("above maximum")
        return value

    def error(self, message):
        return RuntimeError(message)

    def respond_info(self, message):
        self.responses.append(message)


class FakeMCU:
    def __init__(self, missing=()):
        self.missing = set(missing)
        self.commands = {}
        self.responses = []

    def lookup_command(self, command):
        cmd_name = command.split()[0]
        if command in self.missing or cmd_name in self.missing:
            raise ValueError("unsupported command")
        result = FakeQuery()
        self.commands[command] = result
        return result

    def register_serial_response(self, callback, response):
        resp_name = response.split()[0]
        if response in self.missing or resp_name in self.missing:
            raise ValueError("unsupported response")
        self.responses.append(response)

    def check_valid_response(self, response):
        resp_name = response.split()[0]
        return response not in self.missing and resp_name not in self.missing

    def alloc_command_queue(self):
        return object()

    def lookup_query_command(self, command, response, cq=None):
        cmd_name = command.split()[0]
        resp_name = response.split()[0]
        if (command in self.missing or cmd_name in self.missing
                or response in self.missing or resp_name in self.missing):
            raise ValueError("unsupported response")
        return self.lookup_command(command)


class FakeTimerReactor:
    NOW = 0.0
    NEVER = 1.e30

    def __init__(self, now=12.5):
        self.now = now
        self.timers = []

    def monotonic(self):
        return self.now

    def register_timer(self, callback, waketime):
        timer = {"callback": callback, "waketime": waketime}
        self.timers.append(timer)
        return timer

    def unregister_timer(self, timer):
        if timer in self.timers:
            self.timers.remove(timer)


def make_driver(result=0):
    driver = OAMS.__new__(OAMS)
    driver.device_family = "ams_ht"
    driver.oams_idx = 1
    driver.current_spool = None
    driver.bay_count = 1
    driver.fps_value = 0.0
    driver.fps_upper_threshold = 0.0
    driver.fps_lower_threshold = 0.0
    driver.fps_target = 0.0
    driver.current_kp = 0
    driver.current_ki = 0
    driver.current_kd = 0
    driver.current_target = 0
    driver.f1s_hes_value = [0, 0, 0, 0]
    driver.hub_hes_value = [0, 0, 0, 0]
    driver.kp = 0
    driver.ki = 0
    driver.kd = 0
    driver.encoder_clicks = 0
    driver.i_value = 0.0
    driver.protocol_version = 2
    driver.capability_warnings = []
    driver.action_status = None
    driver.action_status_code = None
    driver.action_status_value = None
    driver.capabilities = {
        "dryer_target_min_c": 30.0, "dryer_target_max_c": 80.0,
        "ptfe_calibration": True, "hub_hes_calibration": False,
    }
    driver._use_gen_protocol = False
    driver.oams_load_spool_cmd = object()
    driver.oams_unload_spool_cmd = object()
    driver.oams_follower_cmd = object()
    driver.oams_calibrate_ptfe_length_cmd = object()
    driver.oams_calibrate_hub_hes_cmd = object()
    driver.reactor = types.SimpleNamespace(monotonic=lambda: 12.5)
    driver._telemetry = {}
    driver.ams_ht_dryer_start_cmd = FakeQuery(result)
    driver.ams_ht_dryer_stop_cmd = FakeQuery(result)
    driver.ams_ht_clear_fault_cmd = FakeQuery(result)
    return driver


class DryerControlTests(unittest.TestCase):

    def test_reconnect_removes_old_commands_and_disabled_heater_cannot_start(self):
        driver = make_driver()
        driver.capabilities["heater_control"] = False
        self.assertNotIn("dryer_start", driver._supported_actions())
        with self.assertRaisesRegex(RuntimeError, "heater control"):
            driver.cmd_OAMS_DRYER_START(FakeGcmd({"TARGET": 60}))
        self.assertEqual(driver.ams_ht_dryer_start_cmd.sent, [])
        driver.device_family = "ams1"
        driver._telemetry_commands = {"domain": FakeQuery()}
        driver._setup_ams_ht_telemetry()
        self.assertEqual(driver._telemetry_commands, {})
        self.assertIsNone(driver.ams_ht_dryer_start_cmd)

    def test_legacy_ams1_does_not_acquire_shared_commands(self):
        driver = make_driver()
        driver.device_family = "ams1"
        driver.mcu = FakeMCU()
        driver._setup_ams_ht_telemetry()
        self.assertFalse(driver.mcu.commands)
        self.assertFalse(driver.capabilities["telemetry"])

    def test_ams2_single_path_status_preserves_four_bay_snapshot(self):
        driver = make_driver()
        driver.device_family = "ams2"
        driver.hardware_snapshot_ready = False
        driver.f1s_hes_value = [0, 1, 0, 1]
        driver.hub_hes_value = [0, 0, 1, 0]
        driver._board_status_received({
            "filament_present": 1, "hub_filament_present": 1})
        self.assertEqual(driver.f1s_hes_value, [0, 1, 0, 1])
        self.assertEqual(driver.hub_hes_value, [0, 0, 1, 0])
        self.assertFalse(driver.hardware_snapshot_ready)
        self.assertEqual(driver._telemetry["board"]["filament_present"], 1)

    def test_unit_that_needs_unloading_refuses_to_dry_while_loaded(self):
        # AMS_CAP_DRYER_REQUIRES_UNLOADED (an AMS 2 Pro): with filament out
        # the start is refused before anything is sent, and dryer_start is
        # withheld from the actions; once unloaded both come back.
        driver = make_driver()
        driver.name = "oams ams2"
        driver.capabilities["dryer_requires_unloaded"] = True
        driver.current_spool = 0
        self.assertNotIn("dryer_start", driver._supported_actions())
        with self.assertRaisesRegex(RuntimeError, "unload .*Spool 1 first"):
            driver.cmd_OAMS_DRYER_START(FakeGcmd({"TARGET": 50}))
        self.assertEqual(driver.ams_ht_dryer_start_cmd.sent, [])
        # A hub sensor that still sees filament counts too.
        driver.current_spool = None
        driver.hub_hes_value = [0, 0, 1, 0]
        with self.assertRaisesRegex(RuntimeError, "Spool 3"):
            driver.cmd_OAMS_DRYER_START(FakeGcmd({"TARGET": 50}))
        driver.hub_hes_value = [0, 0, 0, 0]
        self.assertIn("dryer_start", driver._supported_actions())
        driver.cmd_OAMS_DRYER_START(FakeGcmd({"TARGET": 50}))
        self.assertEqual(len(driver.ams_ht_dryer_start_cmd.sent), 1)

    def test_rotate_goes_out_on_the_start_that_carries_it(self):
        # Cortex Bridge firmware 20: AMS_CAP_DRYER_SPOOL_ROTATION=1 and
        # ams_ht_dryer_start2. ROTATE=1 sends rotate 1 on start2; a plain
        # start keeps the old command.
        driver = make_driver()
        driver.capabilities["spool_rotation_during_drying"] = True
        driver.ams_ht_dryer_start2_cmd = FakeQuery()
        self.assertIn("dryer_rotate", driver._supported_actions())
        gcmd = FakeGcmd({"TARGET": 50, "DURATION": 7200, "ROTATE": 1})
        driver.cmd_OAMS_DRYER_START(gcmd)
        self.assertEqual(driver.ams_ht_dryer_start2_cmd.sent,
                         [[5000, 7200, 0, 1]])
        self.assertEqual(driver.ams_ht_dryer_start_cmd.sent, [])
        self.assertIn("the spools turn while it dries", gcmd.responses[0])
        driver.cmd_OAMS_DRYER_START(FakeGcmd({"TARGET": 50}))
        self.assertEqual(len(driver.ams_ht_dryer_start_cmd.sent), 1)
        self.assertEqual(len(driver.ams_ht_dryer_start2_cmd.sent), 1)

    def test_rotate_needs_the_capability_and_the_command(self):
        driver = make_driver()
        driver.capabilities["spool_rotation_during_drying"] = True
        driver.ams_ht_dryer_start2_cmd = None       # bridge 19 and older
        self.assertNotIn("dryer_rotate", driver._supported_actions())
        with self.assertRaisesRegex(RuntimeError, "firmware 20 or later"):
            driver.cmd_OAMS_DRYER_START(FakeGcmd({"TARGET": 50, "ROTATE": 1}))
        driver.capabilities["spool_rotation_during_drying"] = False
        driver.ams_ht_dryer_start2_cmd = FakeQuery()
        self.assertNotIn("dryer_rotate", driver._supported_actions())
        with self.assertRaisesRegex(RuntimeError, "cannot rotate spools"):
            driver.cmd_OAMS_DRYER_START(FakeGcmd({"TARGET": 50, "ROTATE": 1}))
        self.assertEqual(driver.ams_ht_dryer_start2_cmd.sent, [])

    def test_rotate_is_refused_with_filament_out(self):
        # A turning spool would drag loaded filament, on any unit.
        driver = make_driver()
        driver.name = "oams ams_ht"
        driver.capabilities["spool_rotation_during_drying"] = True
        driver.ams_ht_dryer_start2_cmd = FakeQuery()
        driver.current_spool = 0
        with self.assertRaisesRegex(RuntimeError,
                                    "unload .*Spool 1 first.*without ROTATE"):
            driver.cmd_OAMS_DRYER_START(FakeGcmd({"TARGET": 50, "ROTATE": 1}))
        driver.current_spool = None
        driver.hub_hes_value = [1, 0, 0, 0]
        with self.assertRaisesRegex(RuntimeError, "Spool 1"):
            driver.cmd_OAMS_DRYER_START(FakeGcmd({"TARGET": 50, "ROTATE": 1}))
        self.assertEqual(driver.ams_ht_dryer_start2_cmd.sent, [])
        # Without ROTATE this unit still dries loaded.
        driver.cmd_OAMS_DRYER_START(FakeGcmd({"TARGET": 50}))
        self.assertEqual(len(driver.ams_ht_dryer_start_cmd.sent), 1)

    # -- printing, drying and rotation (2026-11-12, AMS HT on 05.00.22.19:
    # it dries with filament loaded and feeds a print while it dries; spools
    # never turn during a print) -------------------------------------------

    @staticmethod
    def with_print(driver, state):
        stats = types.SimpleNamespace(get_status=lambda now: {"state": state})
        driver.printer = types.SimpleNamespace(
            lookup_object=lambda name, default=None:
            stats if name == "print_stats" else default)
        return driver

    def rotating_ht(self, state="standby"):
        driver = self.with_print(make_driver(), state)
        driver.name = "oams ams_ht"
        driver.capabilities["spool_rotation_during_drying"] = True
        driver.capabilities["dryer_feeds_while_drying"] = True
        driver.ams_ht_dryer_start2_cmd = FakeQuery()
        return driver

    def test_rotate_is_refused_during_a_print_and_not_offered(self):
        for state in ("printing", "paused"):
            driver = self.rotating_ht(state)
            self.assertIn("dryer_start", driver._supported_actions())
            self.assertNotIn("dryer_rotate", driver._supported_actions())
            with self.assertRaisesRegex(
                    RuntimeError, "cannot turn its spools during a print.*"
                    "ROTATE=0.*dries while you print"):
                driver.cmd_OAMS_DRYER_START(
                    FakeGcmd({"TARGET": 80, "ROTATE": 1}))
            self.assertEqual(driver.ams_ht_dryer_start2_cmd.sent, [])
            # Without rotation it dries while the print runs.
            driver.cmd_OAMS_DRYER_START(FakeGcmd({"TARGET": 80}))
            self.assertEqual(len(driver.ams_ht_dryer_start_cmd.sent), 1)

    def test_rotate_is_offered_only_when_nothing_is_out_and_no_print(self):
        driver = self.rotating_ht()
        self.assertIn("dryer_rotate", driver._supported_actions())
        driver.current_spool = 0
        self.assertIn("dryer_start", driver._supported_actions())
        self.assertNotIn("dryer_rotate", driver._supported_actions())

    def test_a_start_while_drying_is_told_it_already_dries(self):
        # Mid-print, someone tries to switch the spools turning on: the unit
        # is already drying, and nothing is sent.
        driver = self.rotating_ht("printing")
        driver._telemetry["dryer"] = {"state_name": "timed_dry",
                                      "remaining_s": 3600}
        for params in ({"TARGET": 80, "ROTATE": 1}, {"TARGET": 60}):
            with self.assertRaisesRegex(
                    RuntimeError, "already drying \\(timed_dry, 60 min left"
                    "\\).*OAMS_DRYER_STOP"):
                driver.cmd_OAMS_DRYER_START(FakeGcmd(params))
        self.assertEqual(driver.ams_ht_dryer_start_cmd.sent, [])
        self.assertEqual(driver.ams_ht_dryer_start2_cmd.sent, [])

    def test_feeding_during_a_dry(self):
        driver = self.rotating_ht()
        self.assertIsNone(driver._dryer_blocks_feeding())      # not drying
        driver.cmd_OAMS_DRYER_START(FakeGcmd({"TARGET": 80, "ROTATE": 1}))
        driver._telemetry["dryer"] = {"state_name": "timed_dry"}
        self.assertEqual(driver._dryer_blocks_feeding(), "rotating")
        driver.cmd_OAMS_DRYER_STOP(FakeGcmd({}))
        driver._telemetry["dryer"] = {"state_name": "off"}
        self.assertIsNone(driver._dryer_blocks_feeding())
        # A dry without rotation: a unit that feeds while it dries feeds.
        driver.cmd_OAMS_DRYER_START(FakeGcmd({"TARGET": 80}))
        driver._telemetry["dryer"] = {"state_name": "timed_dry"}
        self.assertIsNone(driver._dryer_blocks_feeding())
        # A unit that does not say so does not.
        driver.capabilities["dryer_feeds_while_drying"] = False
        self.assertEqual(driver._dryer_blocks_feeding(), "drying")
        # A rotating cycle that ended by itself no longer counts.
        driver.capabilities["dryer_feeds_while_drying"] = True
        driver._dryer_rotate_started = True
        driver._telemetry["dryer"] = {"state_name": "off"}
        self.assertIsNone(driver._dryer_blocks_feeding())
        self.assertFalse(driver._dryer_rotate_started)

    def test_loaded_unit_without_the_capability_may_still_dry(self):
        driver = make_driver()
        driver.current_spool = 0
        self.assertIn("dryer_start", driver._supported_actions())
        driver.cmd_OAMS_DRYER_START(FakeGcmd({"TARGET": 50}))
        self.assertEqual(len(driver.ams_ht_dryer_start_cmd.sent), 1)

    def test_start_uses_centic_and_seconds(self):
        driver = make_driver()
        gcmd = FakeGcmd({"TARGET": 65.5, "DURATION": 7200, "HOLD_AFTER": 1})
        driver.cmd_OAMS_DRYER_START(gcmd)
        self.assertEqual(
            driver.ams_ht_dryer_start_cmd.sent, [[6550, 7200, 1]])
        self.assertIn("65.5C", gcmd.responses[0])

    def test_a_busy_unit_says_so_when_no_cycle_runs(self):
        # The unit is not idle (feeding, a loaded spool, an error, starting
        # up): Busy with no cycle of ours running is about the unit itself.
        driver = make_driver(result=1)
        with self.assertRaisesRegex(RuntimeError, "is busy \\(feeding"):
            driver.cmd_OAMS_DRYER_START(FakeGcmd({"TARGET": 55}))

    def test_firmware_rejection_is_an_error(self):
        driver = make_driver(result=4)
        with self.assertRaisesRegex(RuntimeError, "faulted"):
            driver.cmd_OAMS_DRYER_STOP(FakeGcmd({}))

    def test_dryer_replies_name_the_unit(self):
        driver = make_driver()
        driver.name = "oams ams2"
        driver.device_family = "ams2"
        start = FakeGcmd({"TARGET": 50})
        driver.cmd_OAMS_DRYER_START(start)
        stop = FakeGcmd({})
        driver.cmd_OAMS_DRYER_STOP(stop)
        self.assertTrue(start.responses[0].startswith(
            "ams2 (AMS 2 Pro): dryer started at 50.0C"))
        self.assertEqual(stop.responses, ["ams2 (AMS 2 Pro): dryer stopped"])

    def test_motion_and_calibration_actions_follow_active_protocol_commands(self):
        driver = make_driver()
        driver.oams_unload_spool_cmd = None
        driver.oams_follower_cmd = None
        driver.oams_calibrate_ptfe_length_cmd = None
        actions = driver._supported_actions()
        self.assertIn("load", actions)
        self.assertNotIn("unload", actions)
        self.assertNotIn("follower", actions)
        self.assertNotIn("calibrate_ptfe", actions)

        driver._use_gen_protocol = True
        driver.oams_load_spool2_cmd = None
        driver.oams_unload_spool2_cmd = object()
        driver.oams_calibrate_ptfe_length2_cmd = object()
        actions = driver._supported_actions()
        self.assertNotIn("load", actions)
        self.assertIn("unload", actions)
        self.assertIn("calibrate_ptfe", actions)

    def test_board_status_marks_first_hardware_snapshot_ready(self):
        driver = make_driver()
        driver.hardware_snapshot_ready = False
        driver._board_status_received({
            "filament_present": 1,
            "hub_filament_present": 1,
            "#receive_time": 12.5,
        })
        self.assertTrue(driver.hardware_snapshot_ready)
        self.assertEqual(driver.f1s_hes_value, [1, 0, 0, 0])
        self.assertEqual(driver.hub_hes_value, [1, 0, 0, 0])


    def test_board_status_does_not_overwrite_the_stats_sensors(self):
        # Bench, 2026-11-12: after an AMS HT power cycle with T0 loaded, a
        # Cortex Bridge's board status said hub 0 (it forgot the loaded bay)
        # while the HT's own outlet sensor in oams_cmd_stats said 1. The board
        # poll overwrote it every 1.3 s, and the next print read a runout.
        driver = make_driver()
        stats = {"fps_value": 0, "encoder_clicks": 0, "#receive_time": 100.0}
        for b in range(4):
            stats["f1s_hes_value_%d" % b] = 1 if b == 0 else 0
            stats["hub_hes_value_%d" % b] = 1 if b == 0 else 0
        driver._oams_cmd_stats(stats)
        driver._board_status_received({
            "filament_present": 1, "hub_filament_present": 0,
            "#receive_time": 101.3})
        self.assertEqual(driver.hub_hes_value, [1, 0, 0, 0])
        self.assertEqual(driver.f1s_hes_value, [1, 0, 0, 0])
        self.assertEqual(driver._telemetry["board"]["hub_filament_present"], 0)
        # With no stats for a while, the board status is all there is.
        driver._board_status_received({
            "filament_present": 1, "hub_filament_present": 0,
            "#receive_time": 100.0 + driver.BOARD_STATS_FRESH_S + 1.0})
        self.assertEqual(driver.hub_hes_value, [0, 0, 0, 0])

    def test_domain_status_publishes_fault_names(self):
        driver = make_driver()
        driver.capabilities["firmware_capability_schema"] = 5
        driver._domain_status_received({
            "motion_state": 4, "motion_stage": 0, "motion_fault": 12,
            "dryer_state": 0, "dryer_fault": 3, "system_fault": 0,
            "#receive_time": 12.5,
        })

        domain = driver._telemetry["domain"]
        self.assertEqual(domain["motion_state_name"], "fault")
        self.assertEqual(domain["motion_stage_name"], "stopped")
        self.assertEqual(domain["motion_fault_name"], "motor_drive_fault")
        self.assertEqual(domain["dryer_fault_name"], "heater_overtemperature")
        self.assertEqual(domain["system_fault_name"], "none")


class FakeConfig:
    """Just enough of Klipper's ConfigWrapper for the dryer vent options."""

    def __init__(self, values):
        self.values = values

    def getfloat(self, name, default=None, minval=None, maxval=None):
        if name not in self.values:
            return default
        value = float(self.values[name])
        if minval is not None and value < minval:
            raise self.error("%s below minimum" % name)
        if maxval is not None and value > maxval:
            raise self.error("%s above maximum" % name)
        return value

    def error(self, message):
        return ValueError(message)


class DryerVentLoopTests(unittest.TestCase):
    """A unit that runs the dryer vent loop (AMS_CAP_DRYER_VENT_LOOP) gets its
    tuning before every dry start; 0 keeps the unit's built-in value."""

    def loop_driver(self, **options):
        driver = make_driver()
        driver.capabilities["dryer_vent_loop"] = True
        driver.ams_dryer_vent_tuning_cmd = FakeQuery()
        driver.dryer_vent_tuning = OAMS._read_dryer_vent_tuning(
            FakeConfig(options))
        return driver

    def test_the_tuning_goes_before_the_start(self):
        driver = self.loop_driver(dryer_vent_min_interval=900,
                                  dryer_vent_rise=1.5,
                                  dryer_cooldown_below=40)
        order = []
        driver.ams_dryer_vent_tuning_cmd.payload = \
            lambda args: order.append("tuning") or {"result": 0}
        driver.ams_ht_dryer_start_cmd.payload = \
            lambda args: order.append("start") or {"result": 0}
        gcmd = FakeGcmd({"TARGET": 55})
        driver.cmd_OAMS_DRYER_START(gcmd)
        self.assertEqual(order, ["tuning", "start"])
        self.assertEqual(driver.ams_dryer_vent_tuning_cmd.sent,
                         [[1500, 0, 0, 0, 0, 900, 0, 4000, 1]])
        self.assertIn("the bridge drives the vents", gcmd.responses[0])

    def test_vent_unit_leaves_the_vents_to_the_unit(self):
        driver = self.loop_driver()
        gcmd = FakeGcmd({"TARGET": 55, "VENT": "unit"})
        driver.cmd_OAMS_DRYER_START(gcmd)
        self.assertEqual(driver.ams_dryer_vent_tuning_cmd.sent,
                         [[0, 0, 0, 0, 0, 0, 0, 0, 0]])
        self.assertIn("the unit moves its vents", gcmd.responses[0])
        with self.assertRaisesRegex(RuntimeError, "VENT must be"):
            driver.cmd_OAMS_DRYER_START(FakeGcmd({"TARGET": 55,
                                                  "VENT": "open"}))

    def test_a_running_dry_keeps_its_tuning(self):
        # A second start is refused before anything is sent: the tuning must
        # not be changed under the running cycle.
        driver = self.loop_driver()
        driver._telemetry["dryer"] = {"state_name": "timed_dry",
                                      "remaining_s": 600}
        with self.assertRaisesRegex(RuntimeError, "already drying .*10 min"):
            driver.cmd_OAMS_DRYER_START(FakeGcmd({"TARGET": 55}))
        self.assertEqual(driver.ams_dryer_vent_tuning_cmd.sent, [])
        self.assertEqual(driver.ams_ht_dryer_start_cmd.sent, [])

    def test_a_start_refused_for_a_latched_fault_says_how_to_clear_it(self):
        # A tester's AMS HT, 2026-11-11: the first start never ran with the
        # filament out to the toolhead, the retry read only "faulted (4)",
        # and they power-cycled the unit to get past it.
        driver = make_driver()
        driver.ams_ht_dryer_start_cmd.result = 4
        driver._telemetry["domain"] = {
            "dryer_fault": 26, "dryer_fault_name": "dryer_start_refused"}
        with self.assertRaisesRegex(
                RuntimeError, "does not dry while filament is loaded.*"
                "press Clear errors \\(OAMSM_CLEAR_ERRORS\\).*faulted \\(4\\)"):
            driver.cmd_OAMS_DRYER_START(FakeGcmd({"TARGET": 55}))
        driver._telemetry["domain"] = {
            "dryer_fault": 3, "dryer_fault_name": "heater_overtemperature"}
        with self.assertRaisesRegex(
                RuntimeError, "latched heater_overtemperature fault.*"
                "OAMSM_CLEAR_ERRORS"):
            driver.cmd_OAMS_DRYER_START(FakeGcmd({"TARGET": 55}))
        driver._telemetry["domain"] = {}
        with self.assertRaisesRegex(RuntimeError, "latched dryer fault"):
            driver.cmd_OAMS_DRYER_START(FakeGcmd({"TARGET": 55}))

    def test_a_unit_without_the_loop_gets_no_tuning(self):
        driver = make_driver()
        gcmd = FakeGcmd({"TARGET": 55, "VENT": "auto"})
        driver.cmd_OAMS_DRYER_START(gcmd)
        self.assertEqual(len(driver.ams_ht_dryer_start_cmd.sent), 1)
        self.assertNotIn("vents", gcmd.responses[0])

    def test_options_are_checked_against_the_units_ranges(self):
        with self.assertRaisesRegex(ValueError, "below minimum"):
            OAMS._read_dryer_vent_tuning(
                FakeConfig({"dryer_vent_min_interval": 60}))
        with self.assertRaisesRegex(ValueError, "must not be more than"):
            OAMS._read_dryer_vent_tuning(
                FakeConfig({"dryer_vent_min_open": 600,
                            "dryer_vent_max_open": 120}))
        self.assertEqual(
            OAMS._read_dryer_vent_tuning(FakeConfig(
                {"dryer_vent_flat": 0.05, "dryer_vent_hold": 90,
                 "dryer_vent_min_open": 60, "dryer_vent_max_open": 900,
                 "dryer_vent_max_interval": 3600})),
            [0, 50, 90, 60, 900, 0, 3600, 0])

    def test_the_status_shows_the_vents_only_with_the_loop(self):
        driver = self.loop_driver()
        driver.capabilities["dryer"] = True
        driver._telemetry["dryer"] = {
            "state_name": "timed_dry", "vent_position_name": "open",
            "purging": 1, "humidity_gm3": 18.25}
        self.assertEqual(driver._dryer_status()["vent"],
                         {"position": "open", "purging": True,
                          "ah_gm3": 18.25})
        driver._telemetry["dryer"]["humidity_gm3"] = 0.0
        self.assertIsNone(driver._dryer_status()["vent"]["ah_gm3"])
        driver.capabilities["dryer_vent_loop"] = False
        self.assertNotIn("vent", driver._dryer_status())


class FaultClearTests(unittest.TestCase):
    def driver(self, result=0):
        driver = make_driver(result)
        driver.name = "oams ams2"
        driver.device_family = "ams2"
        driver.reactor = FakeTimerReactor()
        driver.console = []
        gcode = types.SimpleNamespace(respond_info=driver.console.append)
        driver.printer = types.SimpleNamespace(
            lookup_object=lambda name: gcode)
        return driver

    def check(self, driver, domain):
        (timer,) = driver.reactor.timers
        self.assertEqual(timer["waketime"], 12.5 + OAMS.CLEAR_FAULT_CHECK_S)
        if domain is not None:
            driver._telemetry["domain"] = domain
        self.assertEqual(timer["callback"](timer["waketime"]),
                         driver.reactor.NEVER)
        return driver.console

    MOTION_TIMEOUT = {"motion_fault": 1, "motion_fault_name": "motion_timeout",
                      "dryer_fault": 0, "dryer_fault_name": "none",
                      "system_fault": 0, "system_fault_name": "none"}

    def stale_clear(self, driver, domain):
        """Run the clear scheduled by an unload that succeeded."""
        driver._telemetry["domain"] = dict(domain)
        driver._schedule_stale_motion_clear()
        (timer,) = driver.reactor.timers
        self.assertEqual(timer["waketime"],
                         12.5 + OAMS.STALE_MOTION_CLEAR_DELAY_S)
        self.assertEqual(timer["callback"](timer["waketime"]),
                         driver.reactor.NEVER)
        return driver.ams_ht_clear_fault_cmd.sent

    def test_a_working_unload_clears_a_stale_motion_fault(self):
        # voron350, 2026-11-12: a retried unload worked, but the AMS 1 kept
        # the motion_timeout of the failed one and would refuse the next load.
        driver = self.driver()
        self.assertEqual(self.stale_clear(driver, self.MOTION_TIMEOUT), [[]])
        self.assertEqual(driver.console, [
            "ams2 (AMS 2 Pro): cleared the motion_timeout fault left by an"
            " earlier failure; the unload that just finished worked"])

    def test_an_unload_completion_schedules_the_clear(self):
        # The driver's own completion path, the old and the v4 protocol.
        import collections
        for wide in (False, True):
            driver = self.driver()
            driver.status_loading = oams.OAMS_STATUS_LOADING
            driver.status_unloading = oams.OAMS_STATUS_UNLOADING
            driver.status_calibrating = oams.OAMS_STATUS_CALIBRATING
            driver.status_error = oams.OAMS_STATUS_ERROR
            driver._use_wide_gen_protocol = wide
            driver._gen_queue = collections.deque([3])
            driver.on_action_complete = None
            driver.session_lost = None
            driver._active_action = driver.status_unloading
            driver._active_gen = 3
            driver.action_status = driver.status_unloading
            driver._apply_action_status(
                {"action": driver.status_unloading,
                 "code": oams.OAMS_OP_CODE_SUCCESS, "value": 0},
                wire_gen=3 if wide else None)
            (timer,) = driver.reactor.timers
            self.assertEqual(timer["callback"].__name__,
                             "_clear_stale_motion_fault", wide)
            # A failed unload schedules nothing.
            driver = self.driver()
            driver.status_loading = oams.OAMS_STATUS_LOADING
            driver.status_unloading = oams.OAMS_STATUS_UNLOADING
            driver.status_calibrating = oams.OAMS_STATUS_CALIBRATING
            driver.status_error = oams.OAMS_STATUS_ERROR
            driver._use_wide_gen_protocol = wide
            driver._gen_queue = collections.deque([3])
            driver.on_action_complete = None
            driver.session_lost = None
            driver._active_action = driver.status_unloading
            driver._active_gen = 3
            driver.action_status = driver.status_unloading
            driver._apply_action_status(
                {"action": driver.status_unloading, "code": 1, "value": 0},
                wire_gen=3 if wide else None)
            self.assertEqual(driver.reactor.timers, [], wide)

    def test_a_dryer_or_system_fault_is_never_cleared_with_it(self):
        for kind in ("dryer_fault", "system_fault"):
            driver = self.driver()
            domain = dict(self.MOTION_TIMEOUT, **{kind: 3})
            self.assertEqual(self.stale_clear(driver, domain), [], kind)
            self.assertEqual(driver.console, [], kind)

    def test_nothing_is_sent_without_a_motion_fault_or_during_a_new_op(self):
        driver = self.driver()
        self.assertEqual(self.stale_clear(
            driver, dict(self.MOTION_TIMEOUT, motion_fault=0)), [])
        driver = self.driver()
        driver.action_status = 1   # the next operation already started
        self.assertEqual(self.stale_clear(driver, self.MOTION_TIMEOUT), [])

    def test_a_refused_clear_says_how_to_clear_it(self):
        driver = self.driver(result=1)
        self.stale_clear(driver, self.MOTION_TIMEOUT)
        self.assertEqual(len(driver.console), 1)
        self.assertIn("could not clear the motion_timeout fault",
                      driver.console[0])
        self.assertIn("OAMSM_CLEAR_ERRORS", driver.console[0])

    def test_clear_names_the_unit_it_was_sent_to(self):
        # OAMS_CLEAR_FAULT OAMS=<idx> reaches the faulted unit; the reply
        # names that unit, not the AMS HT.
        driver = self.driver()
        gcmd = FakeGcmd({})
        driver.cmd_OAMS_CLEAR_FAULT(gcmd)
        self.assertEqual(driver.ams_ht_clear_fault_cmd.sent, [[]])
        self.assertEqual(gcmd.responses, [
            "ams2 (AMS 2 Pro): fault clear accepted; checking the result"
            " in 15s"])

    def test_clear_reports_a_fault_that_is_still_latched(self):
        # The firmware accepts the clear before its recovery ends; a fault
        # the unit latches again must not read as cleared.
        driver = self.driver()
        driver.cmd_OAMS_CLEAR_FAULT(FakeGcmd({}))
        console = self.check(driver, {
            "motion_fault": 12, "motion_fault_name": "motor_drive_fault",
            "dryer_fault": 0, "dryer_fault_name": "none",
            "system_fault": 0, "system_fault_name": "none"})
        self.assertEqual(console, [
            "ams2 (AMS 2 Pro): fault still latched after the clear:"
            " motor_drive_fault"])

    def test_clear_reports_success_when_no_fault_remains(self):
        driver = self.driver()
        driver.cmd_OAMS_CLEAR_FAULT(FakeGcmd({}))
        console = self.check(driver, {
            "motion_fault": 0, "dryer_fault": 0, "system_fault": 0})
        self.assertEqual(console, ["ams2 (AMS 2 Pro): fault cleared"])

    def test_clear_without_fault_status_says_so(self):
        driver = self.driver()
        driver.cmd_OAMS_CLEAR_FAULT(FakeGcmd({}))
        self.assertEqual(self.check(driver, None), [
            "ams2 (AMS 2 Pro): no fault status to confirm the clear"])

    def test_rejected_clear_names_the_unit_and_schedules_no_check(self):
        driver = self.driver(result=4)
        with self.assertRaisesRegex(
                RuntimeError, r"^ams2 \(AMS 2 Pro\) fault clear rejected"):
            driver.cmd_OAMS_CLEAR_FAULT(FakeGcmd({}))
        self.assertEqual(driver.reactor.timers, [])

    def test_unnamed_unit_falls_back_to_its_index(self):
        driver = make_driver()
        self.assertEqual(driver._unit_label(), "OAMS[1] (AMS HT)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
