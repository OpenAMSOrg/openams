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
OAMS = importlib.import_module("oamspkg.oams").OAMS


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
