# SPDX-License-Identifier: GPL-3.0-or-later
"""Klipper extra for physical observation and opt-in OpenAMS HOST operation.

The default path is diagnostic only. An explicitly configured OpenAMS unit may
prepare a cold HOST session after physical reconciliation and maintain its
lease on the reactor; normal connect and gcode diagnostics send no heartbeat. Optional
diagnostic_startup may register one init command, km_diagnostic_start; host-epoch
firmware instead receives a fresh km_diagnostic_start_host epoch on each
connect. The firmware owns both latches. A snapshot is a manual diagnostic read of one
retained firmware capture. Klipper is imported
only while constructing an extra, so standalone imports of host.protocol and
host.requests do not need it on sys.path.
"""
import hashlib
import json
import logging
import math
import os
import secrets
import sys
from dataclasses import dataclass, replace

from .calibration import encode_calibration
from .diagnostics import (
    commission_eject_result, commission_fps, commission_lane_insert,
    commission_preload_result, commission_respooler_answer, commission_switches,
    commission_switch_wiggle, diagnose, explain_fault, explain_startup_failure,
    format_finding, summarize,
    Finding,
    INSTRUCTIONS, LANE_SLIP_HISTORY, SWITCH_WIGGLE_S)
from .diag_reader import read_diag
from .event_reader import peek_event
from .sensor_reader import read_sensors
from .physical_state_reader import read_physical_state
from .openams_sensor_projection import project_sensor_presence
from .physical_ownership import reconcile_path_ownership
from .openams_session_bootstrap import (prepare_cold_host_session,
                                        prepare_occupied_host_session,
                                        prepare_preloaded_host_session)
from .openams_host_lease import HostLease
from .openams_readonly_unit import OpenAMSReadOnlyUnit
from .openams_managed_unit import OpenAMSManagedUnit
from .operation_stats import OperationStats
from .gate_metadata import GateMetadata
from .tool_mapping import ToolMapping
from .maintenance_counters import MaintenanceCounters
from .bowden_estimate import (BowdenEstimate, is_step_estimate,
                              send_calibrated_arrival_set)
from .selector_calibration import (CLEAR as SELECTOR_CLEAR,
                                   STATUS_NAMES as SELECTOR_STATUS_NAMES,
                                   SelectorCalibration, extrapolate_linear,
                                   send_selector_gate_set, send_selector_motors_off,
                                   send_selector_servo_test, spaced_angles)
from .load_report import describe, gate_learned_from_load, query_load_attempts

from .protocol import (
    STATUS_MESSAGES, Admission, CommandResult, Identity, SnapshotAssembler,
    decode_command_result, decode_transport_state)
from .command_transport import exchange
from .terminal_observer import query_terminal
from .wire import (
    ADMITTED, COMMAND_RESULT, CONFIG_FEEDER_ROTATION, CONFIG_SELECTOR_GATE,
    DIAGNOSTIC_DETAIL,
    DIAGNOSTIC_DETAIL_QUERY, DIAGNOSTIC_QUERY, DIAGNOSTIC_START_HOST,
    DIAGNOSTIC_STATE, DIFF_PROBE_QUERY, DIFF_PROBE_STATE, SELECTOR_SERVO_TEST,
    SNAPSHOT_PART, STATUS_REPLIES, SWITCH_DELAY_QUERY, SWITCH_DELAY_STATE,
    TERMINAL_EVENTS, TRANSPORT_QUERY, TRANSPORT_STATE, AdmissionResult, Binding,
    Client, Command, CommandCode, EventType, Fault, OpState, OpType, Result,
    Stage, StartupState, check_enumerations, known)


def _unique_json_pairs(pairs):
    record = {}
    for key, value in pairs:
        if key in record:
            raise ValueError('duplicate JSON key %s' % (key,))
        record[key] = value
    return record

_STATE_NAMES = {Binding.UNBOUND: 'unbound', Binding.ATTACHED: 'attached',
                Binding.SHUTDOWN: 'shutdown',
                Binding.CLOCK_UNAVAILABLE: 'clock_unavailable'}
def _host_epoch_mode(constants):
    """Return 0/1 for KLIPPER_MMU_BOOT_IDENTITY_HOST (absent is 0), else None."""
    value = constants.get('KLIPPER_MMU_BOOT_IDENTITY_HOST', 0)
    return value if type(value) is int and value in (0, 1) else None


_STARTUP_STATE_NAMES = {StartupState.IDLE: 'idle',
                        StartupState.WAITING: 'waiting',
                        StartupState.READY: 'ready',
                        StartupState.FAULTED: 'faulted'}
_OPENAMS_DEVICE_IDS = {'boxturtle': 1, 'nightowl': 2, 'custom': 3, '3d_chameleon': 4, '3ms': 5, 'angry_beaver': 6, 'emu': 7, 'ercf_1_1': 8, 'ercf_2_0': 9, 'ercf_3_0': 10, 'htlf': 11, 'kms': 12, 'mmx': 14, 'mmx6': 15, 'quattrobox_1_0': 18, 'quattrobox_1_1': 19, 'quattrobox_2': 20, 'tradrack': 21, 'low_rider': 13, 'pico_mmu': 16, 'qidi_box': 17, 'btt_vivid': 22, 'claymore': 23}
_SNAPSHOT_TIMEOUT = 5.0
# KLIPPER_MMU_CALIBRATE_SELECTOR (stepper kinds): only a guard against a lost
# reply; firmware ends every HOME itself within its own homing deadline
# (mcu/config.c: 3x a full traverse at the homing speed).
_SELECTOR_HOME_TIMEOUT = 120.0
_SELECTOR_HOME_POLL = 0.1
# KLIPPER_MMU_COMMISSION: bounded waits for physical evidence, on the reactor.
_COMMISSION_WAIT = 60.0
_COMMISSION_POLL = 0.5
_FPS_SAMPLE_S = 10.0
_FPS_SAMPLE_PERIOD = 1.0 / 15.0


@dataclass(frozen=True)
class HostRecoveryRecord:
    """The old session's exact unresolved state, retained across disconnect."""
    instance: int
    session: int
    generation: int
    request_generation: int
    pending_ack: object
    pending_ack_eligible: bool
    pending_requests: tuple
    query_highwater: int = 0


class KlipperMMU:
    def __init__(self, config):
        import mcu
        self._printer = printer = config.get_printer()
        parts = config.get_name().split(None, 1)
        if len(parts) != 2 or parts[0] != 'klipper_mmu' or not parts[1]:
            raise printer.config_error(
                'klipper_mmu: section requires a NAME suffix')
        self._name = parts[1]
        openams_keys = ('openams_idx', 'openams_fps', 'openams_family',
                        'openams_bays', 'openams_path_length_mm')
        openams = {key: config.get(key, None) for key in openams_keys}
        openams_motion = config.getboolean('openams_motion', False)
        if any(value is not None for value in openams.values()):
            if any(value is None for value in openams.values()):
                raise printer.config_error(
                    'klipper_mmu: all OpenAMS topology options are required')
            try:
                unit_type = (OpenAMSManagedUnit if openams_motion
                             else OpenAMSReadOnlyUnit)
                lease_options = {}
                if openams_motion:
                    lease_options = dict(
                        host_watchdog_ms=int(str(config.get(
                            'openams_host_watchdog_ms', None))),
                        lease_max_age_ms=int(str(config.get(
                            'openams_lease_max_age_ms', None))))
                self._openams_unit = unit_type(
                    self._name, self, int(str(openams['openams_idx'])),
                    openams['openams_fps'], openams['openams_family'],
                    int(str(openams['openams_bays'])),
                    float(str(openams['openams_path_length_mm'])),
                    **lease_options)
            except (TypeError, ValueError) as exc:
                raise printer.config_error(
                    'klipper_mmu: invalid OpenAMS topology: %s' % exc)
        else:
            if openams_motion:
                raise printer.config_error(
                    'klipper_mmu: OpenAMS motion requires full topology')
            self._openams_unit = None
        self._gate_metadata = None
        metadata_path = config.get('gate_metadata_file', None)
        metadata_gates = config.get('gate_metadata_gates', None)
        if metadata_path is not None:
            try:
                if self._openams_unit is None:
                    if metadata_gates is None:
                        raise ValueError('gate_metadata_gates is required without OpenAMS topology')
                    gate_count = int(str(metadata_gates))
                else:
                    gate_count = self._openams_unit.bay_count
                    if (metadata_gates is not None
                            and int(str(metadata_gates)) != gate_count):
                        raise ValueError('gate_metadata_gates conflicts with OpenAMS bay count')
                self._gate_metadata = GateMetadata(metadata_path, gate_count)
            except (OSError, UnicodeError, ValueError, TypeError) as exc:
                raise printer.config_error(
                    'klipper_mmu: invalid gate_metadata_file: %s' % exc)
        elif metadata_gates is not None:
            raise printer.config_error(
                'klipper_mmu: gate_metadata_gates requires gate_metadata_file')
        self._tool_mapping = None
        mapping_path = config.get('tool_mapping_file', None)
        mapping_gates = config.get('tool_mapping_gates', None)
        if mapping_path is not None:
            try:
                if self._openams_unit is None:
                    if mapping_gates is None:
                        raise ValueError('tool_mapping_gates is required without OpenAMS topology')
                    mapping_count = int(str(mapping_gates))
                else:
                    mapping_count = self._openams_unit.bay_count
                    if (mapping_gates is not None
                            and int(str(mapping_gates)) != mapping_count):
                        raise ValueError('tool_mapping_gates conflicts with OpenAMS bay count')
                self._tool_mapping = ToolMapping(mapping_path, mapping_count)
            except (OSError, UnicodeError, ValueError, TypeError) as exc:
                raise printer.config_error('klipper_mmu: invalid tool_mapping_file: %s' % exc)
        elif mapping_gates is not None:
            raise printer.config_error('klipper_mmu: tool_mapping_gates requires tool_mapping_file')
        self._maintenance_counters = None
        counter_path = config.get('maintenance_counters_file', None)
        if counter_path is not None:
            try:
                self._maintenance_counters = MaintenanceCounters(counter_path)
            except (OSError, UnicodeError, ValueError, TypeError) as exc:
                raise printer.config_error(
                    'klipper_mmu: invalid maintenance_counters_file: %s' % exc)
        self._bowden_estimate = None
        estimate_path = config.get('bowden_estimate_file', None)
        if estimate_path is not None:
            try:
                self._bowden_estimate = BowdenEstimate(estimate_path)
            except (OSError, UnicodeError, ValueError, TypeError) as exc:
                raise printer.config_error(
                    'klipper_mmu: invalid bowden_estimate_file: %s' % exc)
        # A measured record is opt-in. Parse and validate it before registering
        # callbacks or allocating an MCU object so bad files have no effects.
        self._calibration_file = config.get('diff_calibration_file', None)
        self._calibration_data = None
        self._calibration_sha256 = None
        if self._calibration_file is not None:
            path = os.path.expanduser(self._calibration_file)
            if not os.path.isabs(path):
                raise printer.config_error(
                    'klipper_mmu: diff_calibration_file must be an absolute path')
            try:
                with open(path, 'rb') as source:
                    contents = source.read(8193)
                if len(contents) > 8192:
                    raise ValueError('file exceeds 8192 bytes')
                record = json.loads(contents.decode('utf-8'),
                                    object_pairs_hook=_unique_json_pairs)
                self._calibration_data = encode_calibration(record)
            except (OSError, UnicodeError, ValueError, TypeError) as exc:
                raise printer.config_error(
                    'klipper_mmu: invalid diff_calibration_file %s: %s'
                    % (path, exc))
            self._calibration_file = path
            self._calibration_sha256 = hashlib.sha256(contents).hexdigest()
        mcu_name = config.get('mcu', 'mcu')
        # Resolve the MCU before this extra's connect handler is registered.
        self._mcu = mcu.get_printer_mcu(printer, mcu_name)
        for existing_name, existing in printer.lookup_objects('klipper_mmu'):
            if existing.get_mcu() is self._mcu:
                raise printer.config_error(
                    "klipper_mmu: MCU '%s' already has observer %s"
                    % (mcu_name, existing_name))
        self._diagnostic_startup = config.getboolean(
            'diagnostic_startup', False)
        self._diff_probe = config.getboolean('diff_probe', False)
        if self._diff_probe and (
                self._diagnostic_startup or self._calibration_data is not None
                or openams_motion):
            raise printer.config_error(
                'klipper_mmu: diff_probe is mutually exclusive with '
                'diagnostic_startup, diff_calibration_file, and openams_motion')
        if openams_motion and not self._diagnostic_startup:
            raise printer.config_error(
                'klipper_mmu: openams_motion requires diagnostic_startup')
        steps_str = config.get('selector_gate_steps', None)
        if steps_str is not None:
            try:
                self._selector_gate_steps = [int(s.strip()) for s in steps_str.split(',') if s.strip()]
            except ValueError:
                raise printer.config_error('klipper_mmu: invalid selector_gate_steps')
        else:
            self._selector_gate_steps = None
        self._selector_calibration = None
        calibration_path = config.get('selector_calibration_file', None)
        if calibration_path is not None:
            try:
                self._selector_calibration = SelectorCalibration(calibration_path)
            except (OSError, UnicodeError, ValueError, TypeError) as exc:
                raise printer.config_error(
                    'klipper_mmu: invalid selector_calibration_file: %s' % exc)
        if self._selector_gate_steps is not None or self._selector_calibration is not None:
            self._mcu.register_config_callback(self._config_selector_gates)
        rotation_str = config.get('feeder_rotation_distances', None)
        if rotation_str is not None:
            try:
                self._feeder_rotation_distances_mm = [
                    float(s.strip()) for s in rotation_str.split(',') if s.strip()]
            except ValueError:
                raise printer.config_error(
                    'klipper_mmu: invalid feeder_rotation_distances')
            if any(mm <= 0 for mm in self._feeder_rotation_distances_mm):
                raise printer.config_error(
                    'klipper_mmu: feeder_rotation_distances must be positive')
            self._mcu.register_config_callback(self._config_feeder_rotation_distances)
        else:
            self._feeder_rotation_distances_mm = None
        if (self._diagnostic_startup or self._calibration_data is not None
                or self._diff_probe):
            self._mcu.register_config_callback(
                self._config_calibration_and_startup)
        # Initial value is 0..0xfffffffe; see _allocate_challenge.
        self._challenge = secrets.randbelow(0xffffffff)
        # Last consumed snapshot query id. Zero means none yet; never reset.
        self._snapshot_query = 0
        self._generation = 0
        # Serializes every MCU exchange. Holders may pause; others wait.
        self._mcu_lock = printer.get_reactor().mutex()
        self._active = False
        self._transport_query = None
        self._startup_query = None
        self._detail_query = None
        self._diff_probe_query = None
        self._switch_delay_query = None
        self._firmware_actuation = None
        self._firmware_composition = None
        self._firmware_host_lease_ms = None
        self._sensor_load_capable = False
        self._sensor_wide_lanes = False
        self._virtual_hub_capable = False
        self._diag_capable = False
        self._connected = False
        self._state = 'disconnected'
        self._instance = None
        # Retain only an observed attached identity across connection loss.
        self._last_attached_instance = None
        self._boot_transition = None
        self._boot_instance_changed = False
        self._last_query = None
        self._startup_state = None
        self._startup_result = None
        self._startup_detail = None
        self._snapshot = None
        self._prepared_host_session = None
        self._host_lease = None
        self._host_session_ambiguous = False
        self._host_recovery_record = None
        self.retained_fault_diagnostic = None
        self._retained_fault_event = None
        # The exact (lane, identity) last accepted into OpenAMS by
        # retry_retained_fault_ack, kept until a verified RECONCILE_PATH clear
        # consumes it (or another accept overwrites it).
        self._accepted_firmware_fault = None
        self.operation_stats = OperationStats()
        self._last_error = None
        self.last_load = None
        # Error/warning findings already surfaced by the post-terminal DIAGNOSE
        # hook, so a persisting problem is not repeated every operation.
        self._diagnostic_seen = frozenset()
        # Previous switch bounce/idle-edge capture (host/diagnostics.py phase
        # 2 delta rules); None until the first successful diag capture.
        self._diag_baseline = None
        # Recent arrival travels per lane for DIAGNOSE's lane-slip rule.
        self._arrival_history = {}
        printer.register_event_handler('klippy:connect', self._connect)
        printer.register_event_handler('klippy:disconnect', self._disconnect)
        printer.register_event_handler('klippy:shutdown', self._disconnect)
        gcode = printer.lookup_object('gcode')
        gcode.register_mux_command(
            'KLIPPER_MMU_QUERY', 'MMU', self._name,
            self.cmd_KLIPPER_MMU_QUERY,
            desc='Observe klipper_mmu transport diagnostics')
        gcode.register_mux_command(
            'KLIPPER_MMU_SNAPSHOT', 'MMU', self._name,
            self.cmd_KLIPPER_MMU_SNAPSHOT,
            desc='Read one klipper_mmu diagnostic snapshot')
        gcode.register_mux_command(
            'KLIPPER_MMU_TERMINAL', 'MMU', self._name,
            self.cmd_KLIPPER_MMU_TERMINAL,
            desc='Read one retained klipper_mmu terminal event')
        gcode.register_mux_command(
            'KLIPPER_MMU_EVENT', 'MMU', self._name,
            self.cmd_KLIPPER_MMU_EVENT,
            desc='Read the retained klipper_mmu event queue head')
        gcode.register_mux_command(
            'KLIPPER_MMU_SENSORS', 'MMU', self._name,
            self.cmd_KLIPPER_MMU_SENSORS,
            desc='Read one klipper_mmu physical sensor capture')
        gcode.register_mux_command(
            'KLIPPER_MMU_PHYSICAL', 'MMU', self._name,
            self.cmd_KLIPPER_MMU_PHYSICAL,
            desc='Read and reconcile one klipper_mmu physical capture')
        gcode.register_mux_command(
            'KLIPPER_MMU_DIAGNOSE', 'MMU', self._name,
            self.cmd_KLIPPER_MMU_DIAGNOSE,
            desc='Explain the current klipper_mmu state in plain language')
        gcode.register_mux_command(
            'KLIPPER_MMU_COMMISSION', 'MMU', self._name,
            self.cmd_KLIPPER_MMU_COMMISSION,
            desc='Guided per-switch, per-lane and FPS commissioning check')
        gcode.register_mux_command(
            'KLIPPER_MMU_STATS', 'MMU', self._name,
            self.cmd_KLIPPER_MMU_STATS,
            desc='Read session-local confirmed operation counts')
        gcode.register_mux_command(
            'KLIPPER_MMU_SWITCH_DELAY', 'MMU', self._name,
            self.cmd_KLIPPER_MMU_SWITCH_DELAY,
            desc='Read latest GPIO sample-to-core software delay')
        if self._diff_probe:
            gcode.register_mux_command(
                'KLIPPER_MMU_DIFF_PROBE', 'MMU', self._name,
                self.cmd_KLIPPER_MMU_DIFF_PROBE,
                desc='Capture one fresh raw DiffA/DiffB calibration pair')
        if self._gate_metadata is not None:
            gcode.register_mux_command(
                'KLIPPER_MMU_SET_GATE_METADATA', 'MMU', self._name,
                self.cmd_KLIPPER_MMU_SET_GATE_METADATA,
                desc='Persist explicit manual gate annotations')
        if self._tool_mapping is not None:
            gcode.register_mux_command(
                'KLIPPER_MMU_TOOL_MAP', 'MMU', self._name,
                self.cmd_KLIPPER_MMU_TOOL_MAP,
                desc='Set or query a logical tool-to-gate assignment')
        if self._maintenance_counters is not None:
            gcode.register_mux_command(
                'KLIPPER_MMU_MAINTENANCE', 'MMU', self._name,
                self.cmd_KLIPPER_MMU_MAINTENANCE,
                desc='Read or edit manual maintenance counters')
        if config.getboolean('path_reconciliation', False):
            gcode.register_mux_command(
                'KLIPPER_MMU_RECONCILE_PATH', 'MMU', self._name,
                self.cmd_KLIPPER_MMU_RECONCILE_PATH,
                desc='CLEAR or ADOPT an unknown occupied path; never moves')
        if config.getboolean('driver_restart', False):
            gcode.register_mux_command(
                'KLIPPER_MMU_RESTART_DRIVER', 'MMU', self._name,
                self.cmd_KLIPPER_MMU_RESTART_DRIVER,
                desc='Restart one stopped lane or the selector TMC driver; never moves')
        if openams_motion:
            gcode.register_mux_command(
                'KLIPPER_MMU_PRELOAD', 'MMU', self._name,
                self.cmd_KLIPPER_MMU_PRELOAD,
                desc='Start or inspect a guarded firmware PRELOAD')
            gcode.register_mux_command(
                'KLIPPER_MMU_HOME', 'MMU', self._name,
                self.cmd_KLIPPER_MMU_HOME,
                desc='Start or inspect a guarded firmware HOME')
            gcode.register_mux_command(
                'KLIPPER_MMU_CALIBRATE_SELECTOR', 'MMU', self._name,
                self.cmd_KLIPPER_MMU_CALIBRATE_SELECTOR,
                desc='Measure or set one selector gate position/angle')
            gcode.register_mux_command(
                'KLIPPER_MMU_SERVO', 'MMU', self._name,
                self.cmd_KLIPPER_MMU_SERVO,
                desc='Idle-only test move of the SERVO kind selection servo')
            gcode.register_mux_command(
                'KLIPPER_MMU_MOTORS_OFF', 'MMU', self._name,
                self.cmd_KLIPPER_MMU_MOTORS_OFF,
                desc='Release the selector so it can be moved by hand')
            gcode.register_mux_command(
                'KLIPPER_MMU_UNPRELOAD', 'MMU', self._name,
                self.cmd_KLIPPER_MMU_UNPRELOAD,
                desc='Unload or inspect a guarded local reservation')
            gcode.register_mux_command(
                'KLIPPER_MMU_EJECT', 'MMU', self._name,
                self.cmd_KLIPPER_MMU_EJECT,
                desc='Eject from a bay after UNLOAD clears the shared path')
            gcode.register_mux_command(
                'KLIPPER_MMU_RECOVER_LOCAL', 'MMU', self._name,
                self.cmd_KLIPPER_MMU_RECOVER_LOCAL,
                desc='Explicitly reconcile one retired local terminal or ACK')
            gcode.register_mux_command(
                'KLIPPER_MMU_RECOVER_WAITER', 'MMU', self._name,
                self.cmd_KLIPPER_MMU_RECOVER_WAITER,
                desc='Explicitly reconcile one retired request waiter terminal')
            gcode.register_mux_command(
                'KLIPPER_MMU_RECOVER_SESSION', 'MMU', self._name,
                self.cmd_KLIPPER_MMU_RECOVER_SESSION,
                desc='Release one clean retired HOST session after physical proof')
            gcode.register_mux_command(
                'KLIPPER_MMU_ACK_FAULT', 'MMU', self._name,
                self.cmd_KLIPPER_MMU_ACK_FAULT,
                desc='Retry the exact retained managed fault ACK')
            gcode.register_mux_command(
                'KLIPPER_MMU_CALIBRATE', 'MMU', self._name,
                self.cmd_KLIPPER_MMU_CALIBRATE,
                desc='Estimate the HUB-to-FPS length for the next restart')

    def get_mcu(self):
        return self._mcu

    def record_acked(self, event):
        """Account one retained terminal after its confirmed MCU ACK.

        A LOAD terminal is explained from its attempt records. A
        step-estimated PTFE calibration is also retained with the connected
        dictionary's constants. A failed write cannot reverse the ACK; it is
        reported and the calibration must be repeated.
        """
        self.operation_stats.record_acked(event)
        if event.event_type in TERMINAL_EVENTS[OpType.LOAD]:
            try:
                self._report_load(event)
            except Exception as exc:
                logging.warning('klipper_mmu %s: LOAD attempts unavailable: %s',
                                self._name, exc)
        if (event.event_type in TERMINAL_EVENTS[OpType.LOAD]
                or event.event_type == EventType.EJECT_COMPLETE):
            try:
                self._learn_gate_status(event)
            except Exception as exc:
                logging.warning('klipper_mmu %s: gate status not updated: %s',
                                self._name, exc)
        store = getattr(self, '_bowden_estimate', None)
        if store is not None and is_step_estimate(event):
            try:
                store.record(event, self._name, self._mcu.get_constants(),
                             getattr(self, '_calibration_sha256', None))
            except (OSError, ValueError, TypeError) as exc:
                self._last_error = 'klipper_mmu: bowden estimate not saved: %s' % exc
                logging.warning('klipper_mmu %s: bowden estimate not saved: %s',
                                self._name, exc)
        try:
            self._diagnose_after_terminal()
        except Exception as exc:
            logging.warning('klipper_mmu %s: diagnostics unavailable: %s',
                            self._name, exc)

    def _bowden_arrival_steps(self):
        """The stored HUB-to-FPS estimate for this MMU, or None."""
        store = getattr(self, '_bowden_estimate', None)
        if store is None:
            return None
        estimate = store.status()
        return estimate['steps'] if estimate and estimate['mmu'] == self._name else None

    def _report_load(self, event):
        """Explain each attempt of an ACKed LOAD terminal (docs/fps-arrival.md).

        Holds the MCU mutex like every ACK path. Always logged and kept as
        last_load; retries and failures are also shown on the console.
        """
        connection = self.physical_connection_identity()
        if connection is None or connection[1] != event.instance:
            return
        attempts = query_load_attempts(
            self._mcu, self._printer.get_reactor(), event.instance, event.identity,
            lambda: self.physical_connection_identity() == connection)
        bowden_steps = self._bowden_arrival_steps()
        steps_per_m = self._mcu.get_constants().get('KLIPPER_MMU_FEEDER_STEPS_PER_M', 0)
        summary, lines = describe(
            event.event_type, event.identity.lane, attempts,
            steps_per_m if type(steps_per_m) is int else 0, bowden_steps)
        # The completed arrival travel (docs/fps-arrival.md), for DIAGNOSE's
        # Bowden-drift rule; None without a successful HUB-to-extruder arrival.
        arrival_steps = next(
            (a.travel for a in reversed(attempts)
             if a.stage == Stage.LOAD_FEED_TO_BUFFER and a.fault == Fault.NONE), None)
        self.last_load = {'lane': event.identity.lane, 'request': event.identity.request,
                          'summary': summary, 'attempts': lines,
                          'arrival_steps': arrival_steps}
        if arrival_steps is not None:
            history = getattr(self, '_arrival_history', {})
            lane = event.identity.lane
            history[lane] = (history.get(lane, []) + [arrival_steps])[-LANE_SLIP_HISTORY:]
            self._arrival_history = history
        message = '\n'.join(['klipper_mmu %s: %s' % (self._name, summary)] + lines)
        logging.info(message)
        if len(attempts) > 1 or event.event_type != EventType.LOAD_COMPLETE:
            self._printer.lookup_object('gcode').respond_info(message)

    def _learn_gate_status(self, event):
        """Learn AVAILABLE/EMPTY for a lane with no BAY switch from a LOAD's
        own outcome, or EMPTY after an EJECT (Happy Hare's three gate states, mmu_constants.py:79-82
        GATE_UNKNOWN/GATE_EMPTY/GATE_AVAILABLE). A device with a real BAY
        switch is left alone here: bay_loadable() only ever asks gate status
        when that switch itself has no reading (see openams_device.py).
        Holds the MCU mutex like every ACK path.
        """
        metadata = getattr(self, '_gate_metadata', None)
        lane = event.identity.lane
        if (metadata is None or type(lane) is not int
                or not 0 <= lane < metadata.gate_count):
            return
        connection = self.physical_connection_identity()
        if connection is None or connection[1] != event.instance:
            return
        capture = self.capture_physical_state()
        if capture is None:
            return
        _, _, _, sensors = capture
        if any(bay.configured for bay in sensors.bays):
            return  # A real BAY switch exists; never overwrite the user's own note.
        if event.event_type == EventType.EJECT_COMPLETE:
            available = False  # Happy Hare marks an ejected gate GATE_EMPTY.
        else:
            attempts = query_load_attempts(
                self._mcu, self._printer.get_reactor(), event.instance, event.identity,
                lambda: self.physical_connection_identity() == connection)
            available = gate_learned_from_load(event.event_type, attempts)
        if available is not None:
            metadata.edit(lane, {'available': available})

    def _diagnose_after_terminal(self):
        """Apply the DIAGNOSE rules once after each ACKed terminal.

        Only NEW error/warning findings are shown, so a persisting problem is
        not repeated every operation. Never raises; a caught failure here must
        not affect the ACK, motion or statistics already recorded above. The
        caller already holds the MCU mutex, like every ACK path.
        """
        capture = self.capture_physical_state()
        if capture is None:
            return
        _, _, status, sensors = capture
        # Short, like the LOAD report: the ACK path holds the MCU mutex.
        diag = self.capture_diag(timeout=0.5) if self._diag_capable else None
        findings = diagnose(
            status, sensors, retained_fault=self.retained_fault_diagnostic,
            last_load=self.last_load, bowden_steps=self._bowden_arrival_steps(),
            startup_state=self._startup_state, startup_result=self._startup_result,
            diag=diag, diag_baseline=self._diag_baseline,
            arrival_history=getattr(self, '_arrival_history', None))
        if diag is not None:
            self._diag_baseline = diag
        current = frozenset(f for f in findings if f.severity in ('error', 'warning'))
        new = current - self._diagnostic_seen
        self._diagnostic_seen = current
        if new:
            gcode = self._printer.lookup_object('gcode')
            for finding in findings:
                if finding in new:
                    gcode.respond_info(format_finding(self._name, finding))

    def cmd_KLIPPER_MMU_SET_GATE_METADATA(self, gcmd):
        try:
            gate = gcmd.get_int('GATE', minval=0,
                                maxval=self._gate_metadata.gate_count - 1)
            updates = {}
            for field in ('name', 'material', 'color'):
                value = gcmd.get(field.upper(), None)
                if value is not None:
                    updates[field] = value
            available = gcmd.get('AVAILABLE', None)
            if available is not None:
                if available not in ('0', '1'):
                    raise ValueError('AVAILABLE must be 0 or 1')
                updates['available'] = available == '1'
            self._gate_metadata.edit(gate, updates)
        except (OSError, ValueError, TypeError) as exc:
            raise self._printer.command_error(
                'klipper_mmu: gate metadata: %s' % exc)
        gcmd.respond_info('klipper_mmu %s gate %d metadata saved'
                          % (self._name, gate))

    def cmd_KLIPPER_MMU_TOOL_MAP(self, gcmd):
        try:
            tool = gcmd.get_int('TOOL', minval=0,
                                maxval=self._tool_mapping.count - 1)
            gate = gcmd.get('GATE', None)
            clear = gcmd.get('CLEAR', None)
            if gate is not None and clear is not None:
                raise ValueError('GATE and CLEAR are mutually exclusive')
            if clear is not None:
                if gcmd.get_int('CLEAR', minval=1, maxval=1) != 1:
                    raise ValueError('CLEAR must be 1')
                self._tool_mapping.clear(tool)
            elif gate is not None:
                self._tool_mapping.set(tool, gcmd.get_int(
                    'GATE', minval=0, maxval=self._tool_mapping.count - 1))
            resolved = self._tool_mapping.resolve(tool)
        except (OSError, ValueError, TypeError) as exc:
            raise self._printer.command_error('klipper_mmu: tool mapping: %s' % exc)
        gcmd.respond_info('klipper_mmu %s tool %d gate=%s logical_only=true'
                          % (self._name, tool, 'unmapped' if resolved is None else resolved))

    def cmd_KLIPPER_MMU_STATS(self, gcmd):
        counts = self.operation_stats.snapshot()
        details = ' '.join('%s_lane%d_%s=%d' % (operation, lane, outcome, count)
                           for (operation, lane, outcome), count in sorted(counts.items()))
        gcmd.respond_info('klipper_mmu %s stats scope=process_observed_confirmed_ack %s'
                          % (self._name, details or 'empty=true'))

    def cmd_KLIPPER_MMU_MAINTENANCE(self, gcmd):
        try:
            name = gcmd.get('COUNTER', None)
            raw = {key: gcmd.get(key, None) for key in ('INCR', 'RESET', 'LIMIT', 'WARNING')}
            if name is None:
                if any(value is not None for value in raw.values()):
                    raise ValueError('COUNTER is required for edits')
                counters = self._maintenance_counters.status()
                gcmd.respond_info('klipper_mmu %s maintenance %s' %
                                  (self._name, json.dumps(counters, sort_keys=True)))
                return
            if raw['RESET'] not in (None, '1'):
                raise ValueError('RESET must be 1')
            if not any(value is not None for value in raw.values()):
                entry = self._maintenance_counters.status().get(name)
                if entry is None:
                    raise ValueError('unknown counter')
            else:
                entry = self._maintenance_counters.edit(
                    name,
                    increment=(None if raw['INCR'] is None else int(raw['INCR'])),
                    reset=raw['RESET'] == '1',
                    limit=(None if raw['LIMIT'] is None else int(raw['LIMIT'])),
                    warning=raw['WARNING'])
            gcmd.respond_info('klipper_mmu %s maintenance %s value=%d limit=%d due=%d warning=%s'
                              % (self._name, name, entry['value'], entry['limit'],
                                 entry['limit'] > 0 and entry['value'] > entry['limit'],
                                 json.dumps(entry['warning'])))
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise self._printer.command_error('klipper_mmu: maintenance: %s' % exc)

    def cmd_KLIPPER_MMU_SWITCH_DELAY(self, gcmd):
        with self._mcu_lock:
            if (not self._active or not self._connected
                    or self._state != 'attached' or not self._instance):
                raise self._printer.command_error('klipper_mmu: switch delay unavailable')
            generation, instance = self._generation, self._instance
            challenge = self._allocate_challenge()
            try:
                if self._switch_delay_query is None:
                    self._switch_delay_query = self._mcu.lookup_query_command(
                        SWITCH_DELAY_QUERY, SWITCH_DELAY_STATE)
                params = self._switch_delay_query.send(
                    [challenge, instance >> 32, instance & 0xffffffff])
                if (generation != self._generation or instance != self._instance
                        or not self._active or not self._connected):
                    raise ValueError('connection changed')
                echoed = params.get('challenge')
                hi, lo = params.get('instance_hi'), params.get('instance_lo')
                valid, delay = params.get('valid'), params.get('delay_us')
                count, max_us = params.get('count'), params.get('max_us')
                if (type(echoed) is not int or echoed != challenge
                        or type(hi) is not int or type(lo) is not int
                        or not 0 <= hi <= 0xffffffff or not 0 <= lo <= 0xffffffff
                        or (hi << 32 | lo) != instance
                        or type(valid) is not int or valid not in (0, 1)
                        or type(delay) is not int or not 0 <= delay < 0x7fffffff
                        or type(count) is not int or not 0 <= count <= 0xffffffff
                        or type(max_us) is not int or not 0 <= max_us < 0x7fffffff
                        or (count == 0 and max_us != 0)
                        or (valid and (count == 0 or delay > max_us))
                        or (not valid and delay != 0)):
                    raise ValueError('invalid switch delay reply')
                gcmd.respond_info('klipper_mmu %s switch_delay valid=%s delay_us=%s '
                                  'accepted_since_attach=%d max_delay_us=%d'
                                  % (self._name, bool(valid),
                                     delay if valid else 'unavailable', count, max_us))
            except Exception as exc:
                raise self._printer.command_error('klipper_mmu: switch delay: %s' % exc)

    def cmd_KLIPPER_MMU_RECONCILE_PATH(self, gcmd):
        """ACTION=CLEAR, or ACTION=ADOPT LANE=n as an explicit operator assertion.

        Firmware accepts an idle path of unknown occupancy, or a stopped fault
        whose events are all acknowledged and whose drivers are healthy, with
        fresh, consistent sensors. It never moves, enables outputs or grants a
        lease.
        """
        from .path_reconciliation import STATUS_NAMES, send_path_reconcile
        try:
            action = gcmd.get('ACTION', '').upper()
            has_lane = gcmd.get('LANE', None) is not None
            if action == 'CLEAR' and not has_lane:
                lane = None
            elif action == 'ADOPT' and has_lane:
                lane = gcmd.get_int('LANE', minval=0, maxval=7)
            else:
                raise ValueError('use ACTION=CLEAR, or ACTION=ADOPT LANE=<lane>')
            with self._mcu_lock:
                identity = self.physical_connection_identity()
                if (identity is None
                        or getattr(self, '_event_cycle_in_progress', False)):
                    raise ValueError('MMU connection is unavailable')
                capture = self.capture_physical_state()
                if capture is None or capture[0] != identity:
                    raise ValueError('MMU path reconciliation requires a fresh capture')
                _, instance = identity
                query = capture[2].query
                was_faulted = capture[2].values.get('state') == OpState.FAULT
                reply = send_path_reconcile(
                    self._mcu, self._printer.get_reactor(), instance, query,
                    lane, lambda: self.physical_connection_identity() == identity,
                    timeout=_SNAPSHOT_TIMEOUT)
            if reply.result != CommandCode.OK:
                raise ValueError('command result=%d' % reply.result)
            if reply.status != 0:
                raise ValueError('refused: %s' % STATUS_NAMES[reply.status])
        except Exception as exc:
            raise self._printer.command_error(
                'klipper_mmu: RECONCILE_PATH %s: %s' % (self._name, exc))
        openams_note = ''
        pair = getattr(self, '_accepted_firmware_fault', None)
        if was_faulted and pair is not None:
            unit = getattr(self, '_openams_unit', None)
            clear = getattr(getattr(unit, '_runtime', None),
                            'accept_firmware_fault_cleared', None)
            cleared = False
            # OpenAMS resyncs the lane from the unit's own snapshot, which is
            # stale from the fault; refresh it and verify that same capture.
            if callable(clear) and unit.refresh() and unit._bound == identity:
                values = unit._captured_status.values
                if (values.get('state') != OpState.FAULT
                        and values.get('system_fault') == Fault.NONE):
                    cleared = clear(*pair) is True
            if cleared:
                self._accepted_firmware_fault = None
                openams_note = '; OpenAMS lane cleared'
            else:
                openams_note = ('; OpenAMS lane still latched, '
                                'restart Klipper before using it')
        gcmd.respond_info(
            'klipper_mmu %s %spath %s; no motion performed%s'
            % (self._name, 'fault cleared; ' if was_faulted else '',
               'cleared' if lane is None else 'adopted by lane %d' % lane,
               openams_note))

    def cmd_KLIPPER_MMU_RESTART_DRIVER(self, gcmd):
        """LANE=n: restart one stopped lane's TMC driver and clear its latch.
        SELECTOR=1 instead restarts the selector's own TMC2209 (docs/
        selectors.md); exactly one of LANE or SELECTOR is required.

        Each exchange uses its own fresh capture. Firmware restarts the chip
        on the first, reports restarting until the chip reconfirms its
        configuration, then clears the lane's (or the selector's) STEP/DIR
        fault. It never enables an output; a stopped operation fault still
        needs KLIPPER_MMU_RECONCILE_PATH.
        """
        from .path_reconciliation import (DRIVER_RESTART_SELECTOR, RESTART_POLL,
                                          RESTART_STATUS_NAMES, RESTART_WAIT,
                                          RESTARTING, send_driver_restart)
        try:
            selector = gcmd.get_int('SELECTOR', 0, minval=0, maxval=1)
            lane = gcmd.get_int('LANE', None, minval=0, maxval=7)
            if bool(selector) == (lane is not None):
                raise ValueError('specify exactly one of LANE or SELECTOR')
            target = DRIVER_RESTART_SELECTOR if selector else lane
            what = 'selector' if selector else 'lane %d' % lane
            reactor = self._printer.get_reactor()
            deadline = reactor.monotonic() + RESTART_WAIT
            while True:
                with self._mcu_lock:
                    identity = self.physical_connection_identity()
                    if (identity is None
                            or getattr(self, '_event_cycle_in_progress', False)):
                        raise ValueError('MMU connection is unavailable')
                    capture = self.capture_physical_state()
                    if capture is None or capture[0] != identity:
                        raise ValueError('MMU driver restart requires a fresh capture')
                    reply = send_driver_restart(
                        self._mcu, reactor, identity[1], capture[2].query, target,
                        lambda: self.physical_connection_identity() == identity,
                        timeout=_SNAPSHOT_TIMEOUT)
                if reply.result != 0:
                    raise ValueError('command result=%d' % reply.result)
                if reply.status != RESTARTING:
                    break
                if reactor.monotonic() >= deadline:
                    raise ValueError('%s is still restarting; run again to check'
                                     % what)
                reactor.pause(reactor.monotonic() + RESTART_POLL)
            if reply.status != 0:
                raise ValueError('refused: %s' % RESTART_STATUS_NAMES[reply.status])
        except Exception as exc:
            raise self._printer.command_error(
                'klipper_mmu: RESTART_DRIVER %s: %s' % (self._name, exc))
        gcmd.respond_info(
            'klipper_mmu %s %s driver ready; no motion performed%s'
            % (self._name, what,
               '; KLIPPER_MMU_RECONCILE_PATH clears the stopped fault'
               if capture[2].values.get('state') == 6 else ''))

    def cmd_KLIPPER_MMU_DIFF_PROBE(self, gcmd):
        if not self._diff_probe:
            raise self._printer.command_error(
                'klipper_mmu: DiffA/DiffB probe is not enabled')
        with self._mcu_lock:
            if not self._active or not self._connected:
                raise self._printer.command_error(
                    'klipper_mmu: DiffA/DiffB probe unavailable')
            generation = self._generation
            challenge = self._allocate_challenge()
            try:
                if self._diff_probe_query is None:
                    self._diff_probe_query = self._mcu.lookup_query_command(
                        DIFF_PROBE_QUERY, DIFF_PROBE_STATE)
                params = self._diff_probe_query.send([challenge])
                if (generation != self._generation or not self._active
                        or not self._connected):
                    raise ValueError('connection changed')
                echoed = params.get('challenge')
                state, result = params.get('state'), params.get('result')
                a_raw, b_raw = params.get('a_raw'), params.get('b_raw')
                a_attempt = params.get('a_attempt_us')
                b_ready = params.get('b_ready_us')
                observed = params.get('observed_us')
                if (type(echoed) is not int or echoed != challenge
                        or type(state) is not int or state not in (0, 1, 2, 3)
                        or type(result) is not int
                        or any(type(value) is not int or not 0 <= value <= 0xffff
                               for value in (a_raw, b_raw))
                        or any(type(value) is not int or not 0 <= value <= 0xffffffff
                               for value in (a_attempt, b_ready, observed))):
                    raise ValueError('malformed probe response')
                if state != 2 or result != 0:
                    raise ValueError('no fresh DiffA/DiffB pair (state=%d result=%d)'
                                     % (state, result))
                gcmd.respond_info(
                    'klipper_mmu %s diff_probe state=valid a_raw=%d b_raw=%d '
                    'a_attempt_us=%d b_ready_us=%d observed_us=%d'
                    % (self._name, a_raw, b_raw, a_attempt, b_ready, observed))
            except Exception as exc:
                raise self._printer.command_error(
                    'klipper_mmu: DiffA/DiffB probe failed: %s' % exc)

    def get_openams_unit(self):
        """Return the configured manager unit, initially physical-only."""
        return self._openams_unit

    def cmd_KLIPPER_MMU_PRELOAD(self, gcmd):
        """BAY starts PRELOAD; no BAY reports its retained host-side result."""
        unit = self._openams_unit
        if not isinstance(unit, OpenAMSManagedUnit):
            raise self._printer.command_error('PRELOAD requires openams_motion')
        try:
            cancel = gcmd.get_int('CANCEL') if gcmd.get('CANCEL', None) is not None else 0
            if cancel not in (0, 1):
                raise ValueError('CANCEL must be 0 or 1')
            if cancel:
                if gcmd.get('BAY', None) is not None:
                    raise ValueError('CANCEL does not take BAY')
                identity, response = unit.cancel_local_request('PRELOAD')
                gcmd.respond_info('PRELOAD cancel request=%d bay=%d admission=%d; terminal pending'
                                  % (identity.request, identity.lane, response.admission))
                return
            bay = gcmd.get('BAY', None)
            if bay is None:
                binding = unit._event_binding
                if binding is not None:
                    requests = binding[1].requests
                elif unit._retired_requests is not None:
                    requests = unit._retired_requests
                else:
                    raise ValueError('PRELOAD connection is unavailable')
                pending, result = requests.pending_preload, requests.preload_terminal
                if pending is not None:
                    message = 'PRELOAD pending request=%d bay=%d' % (pending.request, pending.lane)
                elif result is not None:
                    identity = getattr(result, 'identity', None)
                    if identity is None:
                        message = 'PRELOAD administrative result=%d' % result.result
                    elif hasattr(result, 'event_type'):
                        message = ('PRELOAD terminal request=%d bay=%d event=%d code=%d'
                                   % (identity.request, identity.lane,
                                      result.event_type, result.result_code))
                    else:
                        message = ('PRELOAD rejected request=%d bay=%d admission=%d fault=%d'
                                   % (identity.request, identity.lane,
                                      result.admission, result.fault))
                else:
                    message = 'PRELOAD has no recorded request'
            else:
                identity, response = unit.start_preload(gcmd.get_int('BAY'))
                message = ('PRELOAD request=%d bay=%d admission=%d' %
                           (identity.request, identity.lane, response.admission)
                           if isinstance(response, Admission)
                           else 'PRELOAD request=%d bay=%d result=%d' %
                           (identity.request, identity.lane, response.result))
        except Exception as exc:
            raise self._printer.command_error('PRELOAD: %s' % exc)
        gcmd.respond_info(message)

    def cmd_KLIPPER_MMU_HOME(self, gcmd):
        """Starts KM_OP_TYPE_HOME through the existing START path."""
        unit = self._openams_unit
        if not isinstance(unit, OpenAMSManagedUnit):
            raise self._printer.command_error('HOME requires openams_motion')
        if not (getattr(self, '_selector_capable', False)
                or getattr(self, '_selector_servo_kind', False)):
            raise self._printer.command_error('HOME requires a selector firmware build')
        try:
            cancel = gcmd.get_int('CANCEL') if gcmd.get('CANCEL', None) is not None else 0
            if cancel not in (0, 1):
                raise ValueError('CANCEL must be 0 or 1')
            if cancel:
                identity, response = unit.cancel_local_request('HOME')
                gcmd.respond_info('HOME cancel request=%d admission=%d; terminal pending'
                                  % (identity.request, response.admission))
                return
            
            binding = unit._event_binding
            if binding is not None:
                requests = binding[1].requests
            elif unit._retired_requests is not None:
                requests = unit._retired_requests
            else:
                raise ValueError('HOME connection is unavailable')
            
            pending = requests.pending_home
            if pending is not None:
                message = 'HOME pending request=%d' % (pending.request,)
            else:
                identity, response = unit.start_home()
                message = ('HOME request=%d admission=%d' %
                           (identity.request, response.admission)
                           if isinstance(response, Admission)
                           else 'HOME request=%d result=%d' %
                           (identity.request, response.result))
        except Exception as exc:
            raise self._printer.command_error('HOME: %s' % exc)
        gcmd.respond_info(message)

    def _apply_selector_gate_values(self, values):
        """Send {gate: value} through km_selector_gate_set, in gate order,
        one runtime exchange per gate; value is steps or, for the SERVO
        kind, degrees, whatever the caller already resolved. Stops at the
        first refusal -- nothing after it is sent -- and raises; the caller
        decides whether to save anything from a partial result. Idle-only:
        firmware refuses this while any operation, homing or selection is
        in progress (operation_core.h km_core_set_selector_gate)."""
        identity = self.physical_connection_identity()
        if identity is None:
            raise ValueError('MMU connection is unavailable')
        reactor = self._printer.get_reactor()
        _, instance = identity
        is_current = lambda: self.physical_connection_identity() == identity
        applied = {}
        for gate, value in sorted(values.items()):
            with self._mcu_lock:
                reply = send_selector_gate_set(self._mcu, reactor, instance,
                                               is_current, gate, value)
            if reply.result != CommandCode.OK:
                raise ValueError('gate %d: command result=%d' % (gate, reply.result))
            if reply.status != 0:
                raise ValueError('gate %d: %s' % (
                    gate, SELECTOR_STATUS_NAMES.get(reply.status,
                                                    'status=%d' % reply.status)))
            applied[gate] = value
        return applied

    def cmd_KLIPPER_MMU_MOTORS_OFF(self, gcmd):
        """Happy Hare's MMU_MOTORS_OFF equivalent: release the selector (and
        its shared gear) so you can move it by hand to align it at a gate,
        before KLIPPER_MMU_CALIBRATE_SELECTOR. Refused while any operation,
        homing or selection is in progress; the next selection re-homes."""
        if not (getattr(self, '_selector_capable', False)
                or getattr(self, '_selector_servo_kind', False)):
            raise self._printer.command_error(
                'MOTORS_OFF requires a selector firmware build')
        try:
            identity = self.physical_connection_identity()
            if identity is None:
                raise ValueError('MMU connection is unavailable')
            reactor = self._printer.get_reactor()
            _, instance = identity
            is_current = lambda: self.physical_connection_identity() == identity
            with self._mcu_lock:
                reply = send_selector_motors_off(self._mcu, reactor, instance, is_current)
            if reply.result != CommandCode.OK:
                raise ValueError('command result=%d' % reply.result)
            if reply.status != 0:
                raise ValueError(
                    'refused (%s); wait for the current operation to finish' %
                    SELECTOR_STATUS_NAMES.get(reply.status, 'status=%d' % reply.status))
        except Exception as exc:
            raise self._printer.command_error('MOTORS_OFF: %s' % exc)
        gcmd.respond_info(
            'klipper_mmu %s selector motors off; the next selection will home again'
            % self._name)

    def cmd_KLIPPER_MMU_SERVO(self, gcmd):
        """ANGLE=a: idle-only test move of the SERVO kind's selection servo,
        so you can find each gate's angle before saving one with
        KLIPPER_MMU_CALIBRATE_SELECTOR. Never selects anything, and never
        touches any saved calibration."""
        if not getattr(self, '_selector_servo_kind', False):
            raise self._printer.command_error(
                'SERVO requires a SERVO-kind selector firmware build')
        angle = gcmd.get_int('ANGLE')
        try:
            identity = self.physical_connection_identity()
            if identity is None:
                raise ValueError('MMU connection is unavailable')
            reactor = self._printer.get_reactor()
            _, instance = identity
            is_current = lambda: self.physical_connection_identity() == identity
            with self._mcu_lock:
                reply = send_selector_servo_test(self._mcu, reactor, instance,
                                                 is_current, angle)
            if reply.result != CommandCode.OK:
                raise ValueError('command result=%d' % reply.result)
            if reply.status != 0:
                raise ValueError(SELECTOR_STATUS_NAMES.get(
                    reply.status, 'status=%d' % reply.status))
        except Exception as exc:
            raise self._printer.command_error('SERVO: %s' % exc)
        gcmd.respond_info('klipper_mmu %s selector servo moved to %d deg (not saved)'
                          % (self._name, angle))

    def cmd_KLIPPER_MMU_CALIBRATE_SELECTOR(self, gcmd):
        """GATE=n [SAVE=0|1]: stepper kinds (LINEAR/ROTARY). Run
        KLIPPER_MMU_MOTORS_OFF, position the selector by hand at gate n,
        then run this; it homes (KLIPPER_MMU_HOME's own START path), waits
        for that HOME to finish, and records the travel to the switch as
        gate n's position. When gate 0 and the last gate are both measured
        this way, Happy Hare's own rule extrapolates every other gate evenly
        between them. One call, like Happy Hare's own MMU_CALIBRATE_SELECTOR.

        GATE=n ANGLE=a [SPACING=s] [SAVE=0|1]: the SERVO kind instead. No
        homing -- this just saves the angle you already found with
        KLIPPER_MMU_SERVO. SPACING fills every other gate `SPACING` degrees
        from this one (Happy Hare's MMU_CALIBRATE_SERVO_SELECTOR SPACING).

        SAVE defaults to 1 and requires selector_calibration_file; SAVE=0
        only measures/moves and reports, same as Happy Hare's SAVE=0."""
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        constants = self._mcu.get_constants()
        if gcmd.get('ANGLE', None) is not None:
            if not getattr(self, '_selector_servo_kind', False):
                raise self._printer.command_error(
                    'CALIBRATE_SELECTOR ANGLE= requires a SERVO-kind selector '
                    'firmware build')
            gate = gcmd.get_int('GATE', minval=0, maxval=254)
            angle = gcmd.get_int('ANGLE')
            spacing = gcmd.get_int('SPACING', None)
            lane_count = constants.get('KLIPPER_MMU_LANE_COUNT')
            values = (spaced_angles(gate, angle, spacing, lane_count)
                     if spacing is not None else {gate: angle})
            try:
                applied = self._apply_selector_gate_values(values)
                message = ('CALIBRATE_SELECTOR %s gate angle(s): %s'
                          % (self._name, ', '.join(
                              '%d=%d deg' % (g, v) for g, v in sorted(applied.items()))))
                if save:
                    if self._selector_calibration is None:
                        raise ValueError('SAVE requires selector_calibration_file')
                    self._selector_calibration.save(self._name, constants, applied)
                    message += '; saved to %s' % self._selector_calibration.path
                else:
                    message += '; not saved'
            except Exception as exc:
                raise self._printer.command_error('CALIBRATE_SELECTOR: %s' % exc)
            gcmd.respond_info(message)
            return

        unit = self._openams_unit
        if not isinstance(unit, OpenAMSManagedUnit):
            raise self._printer.command_error('CALIBRATE_SELECTOR requires openams_motion')
        if not getattr(self, '_selector_capable', False):
            raise self._printer.command_error(
                'CALIBRATE_SELECTOR requires a selector-stepper firmware build')
        gate = gcmd.get_int('GATE', minval=0, maxval=254)
        try:
            binding = unit._event_binding
            if binding is not None:
                requests = binding[1].requests
            elif unit._retired_requests is not None:
                requests = unit._retired_requests
            else:
                raise ValueError('CALIBRATE_SELECTOR connection is unavailable')
            if requests.pending_home is not None:
                raise ValueError(
                    'a HOME is already pending; call KLIPPER_MMU_HOME to check on it')
            identity, response = unit.start_home()
            if not isinstance(response, Admission) or response.admission != AdmissionResult.ACCEPTED:
                raise ValueError('HOME admission=%s' % getattr(response, 'admission', response))
            # One call, like Happy Hare's own MMU_CALIBRATE_SELECTOR and our
            # own KLIPPER_MMU_CALIBRATE: wait here for HOME's terminal instead
            # of making the caller poll a second time. _SELECTOR_HOME_TIMEOUT
            # is a generous margin over the firmware's own homing deadline
            # (mcu/config.c: 3x a full traverse at homing speed), which is not
            # exported as a host constant.
            reactor = self._printer.get_reactor()
            deadline = reactor.monotonic() + _SELECTOR_HOME_TIMEOUT
            while requests.pending_home is not None:
                if reactor.monotonic() >= deadline:
                    raise ValueError('HOME timed out waiting for the selector to find its switch')
                reactor.pause(reactor.monotonic() + _SELECTOR_HOME_POLL)
            terminal = requests.home_terminal
            if terminal is None or terminal.identity.request != identity.request:
                raise ValueError('HOME ended without a matching terminal event')
            if terminal.event_type != EventType.HOME_COMPLETE:
                raise ValueError('HOME did not complete (event=%s code=%s)'
                                 % (terminal.event_type, terminal.result_code))
            lane_count = constants.get('KLIPPER_MMU_LANE_COUNT')
            known = (self._selector_calibration.gates_for(self._name, constants)
                    if self._selector_calibration is not None else {})
            values = extrapolate_linear(known, lane_count, gate, terminal.value)
            applied = self._apply_selector_gate_values(values)
            message = ('CALIBRATE_SELECTOR gate %d measured %d step(s); gate position(s): %s'
                      % (gate, terminal.value, ', '.join(
                          '%d=%d' % (g, v) for g, v in sorted(applied.items()))))
            if save:
                if self._selector_calibration is None:
                    raise ValueError('SAVE requires selector_calibration_file')
                self._selector_calibration.save(self._name, constants, applied)
                message += '; saved to %s' % self._selector_calibration.path
            else:
                message += '; not saved'
        except Exception as exc:
            raise self._printer.command_error('CALIBRATE_SELECTOR: %s' % exc)
        gcmd.respond_info(message)

    def cmd_KLIPPER_MMU_UNPRELOAD(self, gcmd):
        """BAY unloads its local reservation; no BAY reports its result."""
        unit = self._openams_unit
        if not isinstance(unit, OpenAMSManagedUnit):
            raise self._printer.command_error('UNPRELOAD requires openams_motion')
        try:
            cancel = gcmd.get_int('CANCEL') if gcmd.get('CANCEL', None) is not None else 0
            if cancel not in (0, 1):
                raise ValueError('CANCEL must be 0 or 1')
            if cancel:
                if gcmd.get('BAY', None) is not None:
                    raise ValueError('CANCEL does not take BAY')
                identity, response = unit.cancel_local_request('UNPRELOAD')
                gcmd.respond_info('UNPRELOAD cancel request=%d bay=%d admission=%d; terminal pending'
                                  % (identity.request, identity.lane, response.admission))
                return
            if gcmd.get('BAY', None) is None:
                binding = unit._event_binding
                if binding is not None:
                    requests = binding[1].requests
                elif unit._retired_requests is not None:
                    requests = unit._retired_requests
                else:
                    raise ValueError('UNPRELOAD connection is unavailable')
                pending = requests.pending_unpreload
                result = requests.unpreload_terminal
                if pending is not None:
                    message = 'UNPRELOAD pending request=%d bay=%d' % (pending.request, pending.lane)
                elif result is None:
                    message = 'UNPRELOAD has no recorded request'
                elif isinstance(result, CommandResult):
                    message = 'UNPRELOAD administrative result=%d' % result.result
                elif isinstance(result, Admission):
                    identity = result.identity
                    message = ('UNPRELOAD rejected request=%d bay=%d admission=%d fault=%d'
                               % (identity.request, identity.lane,
                                  result.admission, result.fault))
                else:
                    identity = result.identity
                    message = ('UNPRELOAD terminal request=%d bay=%d event=%d code=%d'
                               % (identity.request, identity.lane,
                                  result.event_type, result.result_code))
            else:
                identity, response = unit.start_unpreload(gcmd.get_int('BAY'))
                message = ('UNPRELOAD request=%d bay=%d admission=%d'
                           % (identity.request, identity.lane, response.admission)
                           if isinstance(response, Admission)
                           else 'UNPRELOAD request=%d bay=%d result=%d'
                           % (identity.request, identity.lane, response.result))
        except Exception as exc:
            raise self._printer.command_error('UNPRELOAD: %s' % exc)
        gcmd.respond_info(message)

    def cmd_KLIPPER_MMU_EJECT(self, gcmd):
        """BAY starts full firmware ejection; no BAY reports its result."""
        unit = self._openams_unit
        if not isinstance(unit, OpenAMSManagedUnit):
            raise self._printer.command_error('EJECT requires openams_motion')
        try:
            cancel = gcmd.get_int('CANCEL') if gcmd.get('CANCEL', None) is not None else 0
            if cancel not in (0, 1):
                raise ValueError('CANCEL must be 0 or 1')
            if cancel:
                if gcmd.get('BAY', None) is not None:
                    raise ValueError('CANCEL does not take BAY')
                identity, response = unit.cancel_local_request('EJECT')
                gcmd.respond_info('EJECT cancel request=%d bay=%d admission=%d; terminal pending'
                                  % (identity.request, identity.lane, response.admission))
                return
            if gcmd.get('BAY', None) is None:
                binding = unit._event_binding
                if binding is not None:
                    requests = binding[1].requests
                elif unit._retired_requests is not None:
                    requests = unit._retired_requests
                else:
                    raise ValueError('EJECT connection is unavailable')
                pending = requests.pending_eject
                result = requests.eject_terminal
                if pending is not None:
                    message = 'EJECT pending request=%d bay=%d' % (pending.request, pending.lane)
                elif result is None:
                    message = 'EJECT has no recorded request'
                elif isinstance(result, CommandResult):
                    message = 'EJECT administrative result=%d' % result.result
                elif isinstance(result, Admission):
                    identity = result.identity
                    message = ('EJECT rejected request=%d bay=%d admission=%d fault=%d'
                               % (identity.request, identity.lane,
                                  result.admission, result.fault))
                else:
                    identity = result.identity
                    message = ('EJECT terminal request=%d bay=%d event=%d code=%d'
                               % (identity.request, identity.lane,
                                  result.event_type, result.result_code))
            else:
                identity, response = unit.start_eject(gcmd.get_int('BAY'))
                message = ('EJECT request=%d bay=%d admission=%d'
                           % (identity.request, identity.lane, response.admission)
                           if isinstance(response, Admission)
                           else 'EJECT request=%d bay=%d result=%d'
                           % (identity.request, identity.lane, response.result))
        except Exception as exc:
            raise self._printer.command_error('EJECT: %s' % exc)
        gcmd.respond_info(message)

    def cmd_KLIPPER_MMU_RECOVER_LOCAL(self, gcmd):
        unit = self._openams_unit
        if not isinstance(unit, OpenAMSManagedUnit):
            raise self._printer.command_error('RECOVER_LOCAL requires openams_motion')
        try:
            record = self.host_recovery_record()
            head = unit._retired_pending_ack
            if record is not None and record.pending_ack is not None:
                requests = unit._retired_requests
                if (requests is None or head is None or requests.connected
                        or requests.instance != record.instance
                        or requests.session != record.session
                        or not record.pending_ack_eligible
                        or not unit._retired_pending_ack_eligible
                        or record.pending_ack != head
                        or record.pending_requests
                        or requests.pending
                        or requests.pending_preload is not None
                        or requests.pending_unpreload is not None
                        or requests.pending_eject is not None
                        or head not in (requests.preload_terminal,
                                        requests.unpreload_terminal,
                                        requests.eject_terminal)):
                    raise ValueError('No exact retired local ACK to retry')
                unit.retry_retired_ack()
                message = 'RECOVER_LOCAL exact terminal ACK reconciled; HOST motion remains revoked'
            else:
                unit.recover_retired_local_terminal()
                message = 'RECOVER_LOCAL exact terminal and ACK reconciled; HOST motion remains revoked'
        except Exception as exc:
            raise self._printer.command_error('RECOVER_LOCAL: %s' % exc)
        gcmd.respond_info(message)

    def cmd_KLIPPER_MMU_ACK_FAULT(self, gcmd):
        unit = self._openams_unit
        if not isinstance(unit, OpenAMSManagedUnit):
            raise self._printer.command_error('ACK_FAULT requires openams_motion')
        try:
            unit.retry_retained_fault_ack()
        except Exception as exc:
            raise self._printer.command_error('ACK_FAULT: %s' % exc)
        gcmd.respond_info('ACK_FAULT exact retained fault ACK confirmed; firmware fault and HOST recovery remain latched')

    def cmd_KLIPPER_MMU_CALIBRATE(self, gcmd):
        """BAY=n [SAVE=0|1]: step-estimated PTFE calibration, run by the
        bound OpenAMS runtime.

        OpenAMS's reducer and waiter own it, as in OAMS_CALIBRATE_PTFE_LENGTH.
        OpenAMS receives 0 for an estimate; firmware adopts the measured
        HUB-to-arrival distance at once, so this session's next LOAD aims at
        it without a restart. bowden_estimate_file keeps the ACK-confirmed
        microsteps so a later restart restores the same distance.

        SAVE defaults to 1 (today's behaviour). SAVE=0 still measures lane n
        and still lets the firmware adopt it for a moment, but then puts back
        whatever was stored before this call, on both the file and the live
        firmware (calibrated_arrival_steps is one distance shared by every
        lane, unlike a per-gate selector calibration) -- use it to check
        another lane's own Bowden length against a reference lane's already-
        saved one without disturbing it. With a stored estimate to compare
        against, the report also suggests a scaled feeder_rotation_distances
        entry for lane n (docs/g-code-commands.md's calibration procedure).
        """
        save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        unit = self._openams_unit
        runtime = getattr(unit, '_runtime', None)
        store = self._bowden_estimate
        if runtime is None or store is None:
            raise self._printer.command_error(
                'KLIPPER_MMU_CALIBRATE requires openams_motion, a bound OpenAMS '
                'runtime and bowden_estimate_file')
        bay = gcmd.get_int('BAY', minval=0, maxval=unit.bay_count - 1)
        before = store._estimate
        # The runtime's own OpenAMS state module; OpenAMS is not imported here.
        action = sys.modules[type(runtime).__module__].S.Calibrate(
            unit.fps_name, unit.oams_idx, bay, 'ptfe')
        result = runtime.request(unit.fps_name, action).wait()
        if not result.ok:
            raise self._printer.command_error(
                'KLIPPER_MMU_CALIBRATE %s bay %d failed: %s'
                % (self._name, bay, getattr(result, 'message', result)))
        # The event transaction stores the estimate after its ACK, under this mutex.
        with self._mcu_lock:
            estimate = store.status() if store._estimate is not before else None
        if estimate is None:
            raise self._printer.command_error(
                'KLIPPER_MMU_CALIBRATE %s bay %d stored no step estimate: %s'
                % (self._name, bay, self._last_error))
        steps_per_m = self._mcu.get_constants().get('KLIPPER_MMU_FEEDER_STEPS_PER_M', 0)
        if type(steps_per_m) is int and steps_per_m > 0:
            length = ('%.1f mm (use this for openams_path_length_mm)'
                     % (estimate['steps'] * 1000. / steps_per_m))
        else:
            length = 'no mm: firmware reports no feeder geometry'
        tail = ('saved to %s; LOAD in this session already aims at it, and '
                'a restart restores it from the saved file' % store.path)
        if not save:
            try:
                store.restore(before)
                self._restore_calibrated_arrival(before['steps'] if before else 0)
            except Exception as exc:
                raise self._printer.command_error(
                    'KLIPPER_MMU_CALIBRATE %s bay %d: measured but could not '
                    'restore the previous estimate: %s' % (self._name, bay, exc))
            if (before is not None and type(steps_per_m) is int and steps_per_m > 0
                    and before['steps'] > 0):
                measured_mm = estimate['steps'] * 1000. / steps_per_m
                stored_mm = before['steps'] * 1000. / steps_per_m
                rd_hint = ''
                distances = getattr(self, '_feeder_rotation_distances_mm', None)
                if distances is not None and 0 <= bay < len(distances):
                    rd_hint = (' (currently %.4f mm: set it to %.4f mm)'
                              % (distances[bay], distances[bay] * measured_mm / stored_mm))
                tail = ('not saved; kept the stored estimate; lane %d measured '
                        '%.1f mm against the stored %.1f mm -- if the path is '
                        'unchanged, scale lane %d\'s rotation distance by '
                        '%.1f/%.1f%s' % (bay, measured_mm, stored_mm, bay,
                                         measured_mm, stored_mm, rd_hint))
            else:
                tail = 'not saved; kept the stored estimate (none to compare against)'
        gcmd.respond_info(
            'klipper_mmu %s bay %d: HUB to FPS arrival estimated at %d feeder '
            'microsteps (%s); %s'
            % (self._name, bay, estimate['steps'], length, tail))

    def _restore_calibrated_arrival(self, steps):
        """Push `steps` (reference feeder microsteps, 0 clears it) into the
        running firmware's calibrated_arrival_steps, idle-only
        (operation_core.h km_core_set_calibrated_arrival_steps). Used only by
        KLIPPER_MMU_CALIBRATE SAVE=0, right after its own measurement."""
        identity = self.physical_connection_identity()
        if identity is None:
            raise ValueError('MMU connection is unavailable')
        reactor = self._printer.get_reactor()
        _, instance = identity
        is_current = lambda: self.physical_connection_identity() == identity
        with self._mcu_lock:
            reply = send_calibrated_arrival_set(self._mcu, reactor, instance,
                                                is_current, steps)
        if reply.result != CommandCode.OK:
            raise ValueError('command result=%d' % reply.result)
        if reply.status != 0:
            raise ValueError('status=%d' % reply.status)

    def cmd_KLIPPER_MMU_RECOVER_WAITER(self, gcmd):
        unit = self._openams_unit
        if not isinstance(unit, OpenAMSManagedUnit):
            raise self._printer.command_error('RECOVER_WAITER requires openams_motion')
        try:
            record = self.host_recovery_record()
            head = unit._retired_pending_ack
            if record is not None and record.pending_ack is not None:
                requests = unit._retired_requests
                if (requests is None or head is None or unit._event_binding is not None
                        or requests.connected
                        or requests.instance != record.instance
                        or requests.session != record.session
                        or requests.generation != record.request_generation + 1
                        or not record.pending_ack_eligible
                        or not unit._retired_pending_ack_eligible
                        or record.pending_ack != head
                        or record.pending_requests
                        or requests.pending
                        or requests.pending_preload is not None
                        or requests.pending_unpreload is not None
                        or requests.pending_eject is not None
                        or requests.preload_terminal == head
                        or requests.unpreload_terminal == head
                        or requests.eject_terminal == head
                        or head.identity.client != Client.HOST
                        or head.identity.session != record.session
                        or head.identity.operation not in (
                            OpType.LOAD, OpType.UNLOAD, OpType.CALIBRATE)
                        or head.instance != record.instance):
                    raise ValueError('No exact retired waiter ACK to retry')
                unit.retry_retired_ack(timeout=_SNAPSHOT_TIMEOUT)
            else:
                unit.recover_retired_waiter_terminal(timeout=_SNAPSHOT_TIMEOUT)
        except Exception as exc:
            raise self._printer.command_error('RECOVER_WAITER: %s' % exc)
        gcmd.respond_info('RECOVER_WAITER exact terminal reconciled; HOST motion remains revoked')

    def cmd_KLIPPER_MMU_RECOVER_SESSION(self, gcmd):
        unit = self._openams_unit
        if not isinstance(unit, OpenAMSManagedUnit):
            raise self._printer.command_error('RECOVER_SESSION requires openams_motion')
        try:
            self.reconcile_clean_host_recovery(unit.bay_count)
        except Exception as exc:
            raise self._printer.command_error('RECOVER_SESSION: %s' % exc)
        gcmd.respond_info('RECOVER_SESSION clean HOST record released; HOST motion remains revoked')

    def host_recovery_record(self):
        """Read-only old HOST state; a new session requires explicit proof."""
        return self._host_recovery_record

    def retry_retained_host_ack(self, timeout=5.0):
        """Explicitly ACK one already accepted terminal from a lost HOST lease.

        The caller runs on the reactor thread. Success only removes the ACK
        obligation; other old-session work still requires reconciliation.
        """
        from .event_delivery import EventDelivery, send_pending_ack
        from .protocol import Event

        with self._mcu_lock:
            record = self._host_recovery_record
            event = record.pending_ack if record is not None else None
            if (getattr(self, '_event_cycle_in_progress', False)
                    or record is None or not self._host_session_ambiguous
                    or not record.pending_ack_eligible
                    or not isinstance(event, Event)
                    or event.instance != record.instance
                    or event.identity.client != Client.HOST
                    or event.identity.session != record.session
                    or event.event_type not in TERMINAL_EVENTS.get(
                        event.identity.operation, ())
                    or type(timeout) not in (int, float)
                    or not math.isfinite(timeout) or timeout <= 0):
                raise ValueError('Retained HOST ACK recovery is unavailable')
            capture = self.capture_physical_state()
            if capture is None:
                raise ValueError('Retained HOST ACK requires a live capture')
            identity, _, status, _ = capture
            if (identity != self.physical_connection_identity()
                    or status.query <= record.query_highwater
                    or identity[1] != record.instance
                    or status.instance != record.instance
                    or status.values.get('client_sessions', ())[
                        Client.HOST:Client.HOST + 1] != (record.session,)
                    or self._host_recovery_record is not record):
                raise ValueError('Retained HOST ACK connection or session changed')

            def current():
                return (self.physical_connection_identity() == identity
                        and self._host_recovery_record is record
                        and self._host_session_ambiguous)

            delivery = EventDelivery(record.instance, identity[0])
            head = peek_event(self._mcu, self._printer.get_reactor(),
                              record.instance, current, timeout)
            if not current() or head != event:
                raise ValueError('Retained HOST ACK head changed')
            delivery.pending = event
            delivery.ack_eligible = True
            send_pending_ack(self._mcu, self._printer.get_reactor(), delivery,
                             delivery.generation, current, timeout)
            self._host_recovery_record = replace(
                record, pending_ack=None, pending_ack_eligible=False)
            try:
                self.record_acked(event)
            except Exception:
                pass
            return True

    def reconcile_clean_host_recovery(self, expected_bays):
        """Release a disconnected HOST session with no unresolved work.

        This is an explicit diagnostic gate. It sends no heartbeat and grants
        no motion. A later cold preparation takes its own fresh capture.
        Pending ACKs and request waiters require a separate recovery protocol.
        """
        record = self._host_recovery_record
        prepared = self._prepared_host_session
        if (record is None or not self._host_session_ambiguous
                # A firmware fault leaves this retired session disconnected.
                or prepared is not None and (prepared.requests.connected
                                             or prepared.requests.session
                                             != record.session)
                or record.pending_ack is not None or record.pending_ack_eligible
                or record.pending_requests
                or type(expected_bays) is not int or expected_bays < 1
                or self._firmware_actuation != 1
                or not self._diagnostic_startup
                or self._startup_state != 'ready'
                or self._startup_result != 0):
            raise ValueError('Clean HOST recovery is unavailable')
        with self._mcu_lock:
            capture = self.capture_physical_state()
        if capture is None:
            raise ValueError('Clean HOST recovery requires a live capture')
        identity, _, status, sensors = capture
        values = status.values
        if (identity != self.physical_connection_identity()
                or status.query <= record.query_highwater
                or identity[1] != record.instance
                or self._firmware_actuation != 1
                or self._startup_state != 'ready'
                or self._startup_result != 0
                or values.get('motor_ready') is not True
                or type(values.get('host_connected')) is not bool
                or values.get('system_fault') != Fault.NONE
                or values.get('motion_fault') != Fault.NONE
                or values.get('follower_fault') != Fault.NONE
                or values.get('position_uncertain') is not False
                or values.get('pending_fault_event') is not False):
            raise ValueError('Clean HOST recovery connection changed')
        from .session_reconciliation import (choose_host_session_with_physical,
                                             choose_occupied_host_session,
                                             choose_preloaded_host_session)
        # LOADED is a quiescent known lane, e.g. after RECONCILE_PATH ADOPT.
        choose = {OpState.PRELOADED: choose_preloaded_host_session,
                  OpState.LOADED: choose_occupied_host_session}.get(
                      values.get('state'), choose_host_session_with_physical)
        selected = choose(status, sensors, identity[1], expected_bays)
        # The firmware must still identify the old HOST session. Otherwise
        # its counters cannot prove that this is the session being retired.
        if status.values['client_sessions'][Client.HOST] != record.session:
            raise ValueError('Clean HOST recovery session changed')
        if (identity != self.physical_connection_identity()
                or self._host_recovery_record is not record
                or self._prepared_host_session is not prepared
                or self._firmware_actuation != 1
                or self._startup_state != 'ready'
                or self._startup_result != 0):
            raise ValueError('Clean HOST recovery connection changed')
        self._host_recovery_record = None
        self._host_session_ambiguous = False
        self._prepared_host_session = None
        return selected

    def _retain_host_recovery(self):
        prepared = self._prepared_host_session
        if prepared is None:
            return
        requests, delivery = prepared.requests, prepared.delivery
        if self._host_recovery_record is None:
            lease = self._host_lease
            disconnected = lease is not None and not lease.available
            pending_ack = (lease.pending_ack_on_disconnect if disconnected
                           else delivery.pending)
            pending_ack_eligible = (lease.ack_eligible_on_disconnect
                                    if disconnected else delivery.ack_eligible)
            pending_requests = (lease.pending_requests_on_disconnect
                                if disconnected else tuple(sorted(
                                    (waiter, identity, token)
                                    for waiter, (identity, token)
                                    in requests.pending.items())))
            if not disconnected and requests.pending_preload is not None:
                pending_requests += (('PRELOAD', requests.pending_preload, None),)
            if not disconnected and requests.pending_unpreload is not None:
                pending_requests += (('UNPRELOAD', requests.pending_unpreload, None),)
            if not disconnected and requests.pending_eject is not None:
                pending_requests += (('EJECT', requests.pending_eject, None),)
            if not disconnected and requests.pending_home is not None:
                pending_requests += (('HOME', requests.pending_home, None),)
            self._host_recovery_record = HostRecoveryRecord(
                requests.instance, requests.session,
                lease.delivery_generation_on_disconnect if disconnected
                else delivery.generation,
                lease.request_generation_on_disconnect if disconnected
                else requests.generation,
                pending_ack, pending_ack_eligible, pending_requests,
                max(getattr(self, '_snapshot_query', 0),
                    self._last_query or 0))
        self._host_session_ambiguous = True
        if self._host_lease is not None:
            self._host_lease.disconnect()
            self._host_lease = None
        else:
            if requests.connected:
                requests.disconnect()
            if delivery.connected:
                delivery.disconnect()
        # A lease lost with the MCU connection otherwise intact (the common
        # case: a host stall or a paused reactor, not a serial replug) never
        # runs _invalidate()/_fail(), so _prepared_host_session stays set.
        # The bound OpenAMS unit's own event binding still points at the
        # requests/delivery pair just disconnected above; nothing else ever
        # retires it in that case (refresh()'s own lazy retire only fires
        # once _prepared_host_session is already None), which otherwise
        # leaves RECOVER_LOCAL/RECOVER_WAITER permanently unable to run at
        # all. Retire it here too, uniformly with a full connection
        # invalidate, so a same-connection lease loss with an outstanding
        # local PRELOAD/UNPRELOAD/EJECT or FPS waiter is recoverable the
        # same way a dropped connection already is.
        unit = getattr(self, '_openams_unit', None)
        if getattr(unit, '_event_binding', None) is not None:
            unit.retire_event_binding()

    def _config_selector_gates(self):
        if (self._mcu.is_fileoutput()
                or self._mcu.try_lookup_command(CONFIG_SELECTOR_GATE) is None):
            return
        lane_count = self._mcu.get_constants().get('KLIPPER_MMU_LANE_COUNT')
        gates = {}
        if self._selector_gate_steps is not None:
            if lane_count is None or len(self._selector_gate_steps) != lane_count:
                raise self._printer.config_error(
                    'klipper_mmu: selector_gate_steps length must match lane count')
            gates.update(enumerate(self._selector_gate_steps))
        if self._selector_calibration is not None:
            # Measured entries override selector_gate_steps and the Kconfig
            # default, per gate (docs/selectors.md). A stepper build's table
            # may hold SERVO-kind (degrees) entries too, from the same file
            # shared with a different firmware image; gates_for() already
            # refuses those by firmware identity, so nothing here can send a
            # degree value as if it were steps.
            gates.update(self._selector_calibration.gates_for(
                self._name, self._mcu.get_constants()))
        for gate, steps in sorted(gates.items()):
            self._mcu.add_config_cmd(
                'config_km_selector_gate oid=%d gate=%d steps=%d'
                % (self._mcu.create_oid(), gate, steps))

    def _config_feeder_rotation_distances(self):
        """feeder_rotation_distances (mm, one per lane): overrides the
        Kconfig KLIPPER_MMU_FEEDER_LANE_ROTATION_DISTANCES_UM list entirely
        (docs/install.md), one config_km_feeder_rotation per lane. A restart
        is needed after changing this option, exactly like Klipper's own
        rotation_distance."""
        if self._mcu.is_fileoutput():
            return
        if self._mcu.try_lookup_command(CONFIG_FEEDER_ROTATION) is None:
            raise self._printer.config_error(
                'klipper_mmu: feeder_rotation_distances is set but this '
                'firmware has no per-lane rotation distance; update it')
        lane_count = self._mcu.get_constants().get('KLIPPER_MMU_LANE_COUNT')
        if lane_count is None or len(self._feeder_rotation_distances_mm) != lane_count:
            raise self._printer.config_error(
                'klipper_mmu: feeder_rotation_distances length must match lane count')
        for lane, mm in enumerate(self._feeder_rotation_distances_mm):
            self._mcu.add_config_cmd(
                'config_km_feeder_rotation oid=%d lane=%d um=%d'
                % (self._mcu.create_oid(), lane, round(mm * 1000)))

    def _config_calibration_and_startup(self):
        # File output must not gain an init command. A missing or non-integer
        # gate is unsupported; ACTUATION is not permission to start.
        if self._mcu.is_fileoutput():
            return
        constants = self._mcu.get_constants()
        protocol = constants.get('KLIPPER_MMU_PROTOCOL_VERSION')
        integration = constants.get('KLIPPER_MMU_INTEGRATION_VERSION')
        if (type(protocol) is not int or protocol != 1
                or type(integration) is not int or integration != 1):
            raise self._printer.config_error(
                'klipper_mmu: calibration/diagnostic startup requires integer '
                'KLIPPER_MMU_PROTOCOL_VERSION 1, '
                'KLIPPER_MMU_INTEGRATION_VERSION 1')
        if self._diff_probe:
            actuation = constants.get('KLIPPER_MMU_ACTUATION')
            probe = constants.get('KLIPPER_MMU_DIFF_PROBE')
            if type(actuation) is not int or actuation != 0:
                raise self._printer.config_error(
                    'klipper_mmu: diff_probe requires integer '
                    'KLIPPER_MMU_ACTUATION 0')
            if type(probe) is not int or probe != 1:
                raise self._printer.config_error(
                    'klipper_mmu: diff_probe requires integer '
                    'KLIPPER_MMU_DIFF_PROBE 1')
            self._mcu.add_config_cmd('km_diff_probe_start', is_init=True)
        if self._diagnostic_startup:
            gate = constants.get('KLIPPER_MMU_DIAGNOSTIC_STARTUP')
            if type(gate) is not int or gate != 1:
                raise self._printer.config_error(
                    'klipper_mmu: diagnostic_startup requires integer '
                    'KLIPPER_MMU_DIAGNOSTIC_STARTUP 1')
        if self._calibration_data is not None:
            actuation = constants.get('KLIPPER_MMU_ACTUATION')
            if (type(actuation) is not int or actuation not in (0, 1)
                    or (actuation == 1 and not self._diagnostic_startup)):
                raise self._printer.config_error(
                    'klipper_mmu: calibration delivery requires integer '
                    'KLIPPER_MMU_ACTUATION 0 or gated diagnostic startup with 1')
            for name in ('KLIPPER_MMU_CONFIG_DIFF_BUFFER',
                         'KLIPPER_MMU_DIFF_CALIBRATION'):
                if type(constants.get(name)) is not int or constants[name] != 1:
                    raise self._printer.config_error(
                        'klipper_mmu: calibration delivery requires integer %s 1'
                        % (name,))
            oid = self._mcu.create_oid()
            self._mcu.add_config_cmd(
                'config_km_diff_calibration oid=%d data=%s'
                % (oid, self._calibration_data.hex()))
            # A stored step estimate for this firmware and calibration restores
            # the calibrated HUB-to-arrival distance; otherwise LOAD is refused
            # until KLIPPER_MMU_CALIBRATE measures one this session.
            steps = self._bowden_estimate and self._bowden_estimate.fps_steps(
                self._name, constants, self._calibration_sha256)
            if steps:
                self._mcu.add_config_cmd(
                    'config_km_fps_steps oid=%d steps=%d'
                    % (self._mcu.create_oid(), steps))
            elif type(constants.get('KLIPPER_MMU_FPS_MIN_STEPS')) is int:
                # No restored distance on an FPS-arrival build: firmware will
                # refuse every LOAD (KM_REQ_UNAVAILABLE) until this session's
                # own CALIBRATE measures one. The distance is one HUB-to-arrival
                # measurement for the whole shared path, not per lane: any
                # loaded bay calibrates every lane at once.
                logging.warning(
                    'klipper_mmu %s: no calibrated HUB-to-arrival distance; '
                    'LOAD is refused until calibrated -- run '
                    'KLIPPER_MMU_CALIBRATE BAY=<any loaded bay> once', self._name)
        if self._diagnostic_startup:
            host_epoch = _host_epoch_mode(constants)
            if host_epoch is None:
                raise self._printer.config_error(
                    'klipper_mmu: KLIPPER_MMU_BOOT_IDENTITY_HOST must be '
                    'integer 0 or 1')
            # Host-epoch firmware has no no-argument start; _connect binds it.
            if not host_epoch:
                self._mcu.add_config_cmd('km_diagnostic_start', is_init=True)

    def _send_host_epoch(self):
        # Fresh per connect; independent of observation challenges. Firmware
        # binds only the first nonzero epoch in its lifetime.
        epoch = secrets.randbelow((1 << 64) - 1) + 1
        command = self._mcu.lookup_command(DIAGNOSTIC_START_HOST)
        command.send([epoch >> 32, epoch & 0xffffffff])

    def _decode_startup(self, params, challenge):
        echoed = params.get('challenge')
        state = params.get('state')
        result = params.get('result')
        if (type(echoed) is not int or echoed != challenge
                or echoed < 0 or echoed > 0xffffffff):
            raise self._printer.command_error(
                'klipper_mmu: diagnostic startup challenge was not echoed')
        if (type(state) is not int or type(result) is not int
                or state not in _STARTUP_STATE_NAMES):
            raise self._printer.command_error(
                'klipper_mmu: diagnostic startup response was malformed')
        if state in (StartupState.IDLE, StartupState.WAITING):
            consistent = result == Result.ERR_UNAVAILABLE
        elif state == StartupState.READY:
            consistent = result == Result.OK
        else:
            consistent = result in known(Result) - {Result.OK,
                                                     Result.ERR_UNAVAILABLE}
        if not consistent:
            raise self._printer.command_error(
                'klipper_mmu: diagnostic startup response was malformed')
        return _STARTUP_STATE_NAMES[state], result

    def get_status(self, eventtime):
        return {
            'connected': self._connected,
            'state': self._state,
            'instance': self._instance,
            'last_attached_instance': self._last_attached_instance,
            'boot_transition': self._boot_transition,
            'boot_instance_changed': self._boot_instance_changed,
            'last_query': self._last_query,
            'firmware_actuation': self._firmware_actuation,
            'firmware_composition': self._firmware_composition,
            'motion_composition_match': self._motion_composition_matches(
                getattr(self._openams_unit, 'bay_count', None)),
            'diagnostic_startup': self._diagnostic_startup,
            'diff_calibration_file': self._calibration_file,
            'diff_calibration_sha256': self._calibration_sha256,
            'gate_metadata': (None if getattr(self, '_gate_metadata', None) is None
                              else self._gate_metadata.status()),
            'tool_mapping': (None if getattr(self, '_tool_mapping', None) is None
                             else self._tool_mapping.status()),
            'print_session': (None if getattr(self, '_print_session', None) is None
                              else self._print_session.management_status()),
            'maintenance_counters': (None if getattr(self, '_maintenance_counters', None) is None
                                     else self._maintenance_counters.status()),
            'bowden_estimate': (None if getattr(self, '_bowden_estimate', None) is None
                                else self._bowden_estimate.status()),
            'last_load': getattr(self, 'last_load', None),
            'startup_state': self._startup_state,
            'startup_result': self._startup_result,
            'motion_available': False,
            'retained_fault_diagnostic': (None if getattr(self, 'retained_fault_diagnostic', None) is None
                                          else dict(self.retained_fault_diagnostic)),
            'last_error': self._last_error,
            'snapshot': None if self._snapshot is None else dict(self._snapshot),
            'selector_homed': bool(self._snapshot.get('homed')) if self._snapshot and 'homed' in self._snapshot else None,
            'selector_gate': self._snapshot.get('gate') if self._snapshot and 'gate' in self._snapshot else None,
        }

    def _invalidate(self, state, error=None):
        self._retain_host_recovery()
        self._generation += 1
        self._active = False
        self._connected = False
        self._state = state
        self._instance = None
        self._boot_transition = None
        self._last_query = None
        self._startup_state = None
        self._startup_result = None
        self._startup_detail = None
        self._snapshot = None
        self._prepared_host_session = None
        self._firmware_composition = None
        self._firmware_host_lease_ms = None
        self._last_error = error
        # Connection-scoped encoders. An outstanding send keeps _mcu_lock
        # until it unwinds. The snapshot high-water and challenge counters
        # stay unchanged.
        self._transport_query = None
        self._startup_query = None
        self._detail_query = None
        self._diff_probe_query = None
        self._switch_delay_query = None

    def _fail(self, generation, error):
        # A stale in-flight call must not replace a newer generation.
        if generation != self._generation:
            return
        self._retain_host_recovery()
        self._connected = False
        self._state = 'unavailable'
        self._instance = None
        self._boot_transition = None
        self._last_query = None
        self._startup_state = None
        self._startup_result = None
        self._startup_detail = None
        self._snapshot = None
        self._prepared_host_session = None
        # Dictionary metadata remains valid on this connection. A transient
        # observation failure revokes liveness, not the identified build.
        # _invalidate clears it when the connection actually changes.
        self._last_error = error

    def _connect(self):
        self._firmware_actuation = None
        self._firmware_composition = None
        self._firmware_host_lease_ms = None
        self._sensor_load_capable = False
        self._sensor_wide_lanes = False
        self._virtual_hub_capable = False
        self._diag_capable = False
        if self._mcu.is_fileoutput():
            self._invalidate('file_output')
            return
        self._invalidate('unavailable')
        constants = self._mcu.get_constants()
        protocol = constants.get('KLIPPER_MMU_PROTOCOL_VERSION')
        integration = constants.get('KLIPPER_MMU_INTEGRATION_VERSION')
        if (type(protocol) is not int or protocol != 1
                or type(integration) is not int or integration != 1):
            message = (
                'klipper_mmu: KLIPPER_MMU_PROTOCOL_VERSION and '
                'KLIPPER_MMU_INTEGRATION_VERSION must both be 1')
            self._last_error = message
            raise self._printer.config_error(message)
        actuation = constants.get('KLIPPER_MMU_ACTUATION')
        if type(actuation) is not int or actuation not in (0, 1):
            message = (
                'klipper_mmu: KLIPPER_MMU_ACTUATION must be the integer 0 or 1')
            self._last_error = message
            raise self._printer.config_error(message)
        self._firmware_actuation = actuation
        firmware_lease_ms = constants.get('KLIPPER_MMU_HOST_LEASE_MS')
        self._firmware_host_lease_ms = (
            firmware_lease_ms if type(firmware_lease_ms) is int
            and 0 < firmware_lease_ms <= 60000 else None)
        host_epoch = _host_epoch_mode(constants)
        if host_epoch is None:
            message = (
                'klipper_mmu: KLIPPER_MMU_BOOT_IDENTITY_HOST must be integer 0 or 1')
            self._last_error = message
            raise self._printer.config_error(message)
        try:
            if not check_enumerations(self._mcu.get_enumerations()):
                logging.info('klipper_mmu %s: firmware predates exported '
                             'enumerations; using protocol 1 values', self._name)
        except ValueError as exc:
            # Nothing is sent to an incompatible build; the printer continues.
            self._last_error = 'klipper_mmu: %s' % (exc,)
            logging.warning('klipper_mmu %s: unavailable after connect: %s',
                            self._name, exc)
            return
        if host_epoch and self._diagnostic_startup:
            # MCU config/post-init connect handling has already run.
            self._send_host_epoch()
        device_id = constants.get('KLIPPER_MMU_DEVICE_ID')
        lane_count = constants.get('KLIPPER_MMU_LANE_COUNT')
        if (type(device_id) is int and 1 <= device_id <= 25
                and type(lane_count) is int and 1 <= lane_count <= 12):
            self._firmware_composition = (device_id, lane_count)
        self._sensor_wide_lanes = type(lane_count) is int and lane_count > 8
        self._sensor_load_capable = (type(constants.get('KLIPPER_MMU_SENSOR_LOADS'))
                                     is int and constants['KLIPPER_MMU_SENSOR_LOADS'] == 1)
        self._virtual_hub_capable = (
            type(constants.get('KLIPPER_MMU_CONFIG_VIRTUAL_HUB')) is int
            and constants['KLIPPER_MMU_CONFIG_VIRTUAL_HUB'] == 1)
        self._selector_capable = self._mcu.try_lookup_command(CONFIG_SELECTOR_GATE) is not None
        # A selector-servo-test command exists on any build with a servo
        # (a LINEAR grip too); it is the SERVO *kind* (no stepper axis at
        # all) only once the stepper gate-position command is absent.
        self._selector_servo_kind = (
            not self._selector_capable
            and self._mcu.try_lookup_command(SELECTOR_SERVO_TEST) is not None)
        self._diag_capable = (type(constants.get('KLIPPER_MMU_DIAG_COUNTERS'))
                              is int and constants['KLIPPER_MMU_DIAG_COUNTERS'] == 1)
        self._active = True
        generation = self._generation
        try:
            with self._mcu_lock:
                self._observe()
        except self._printer.command_error as exc:
            # Optional observation failure must not stop printer startup.
            # A stale call may not overwrite a newer connection generation.
            self._fail(generation, str(exc))
            logging.warning('klipper_mmu %s: unavailable after connect: %s',
                            self._name, exc)
        self._send_selector_servo_calibration()

    def _send_selector_servo_calibration(self):
        """SERVO-kind calibration-file angles have no config-time command
        like config_km_selector_gate (mcu/config.c's servo_gate_us has no
        per-gate override there), so they are restored here, once per
        connect, through the same runtime km_selector_gate_set that
        KLIPPER_MMU_CALIBRATE_SELECTOR/KLIPPER_MMU_SERVO use."""
        if (not self._selector_servo_kind or self._selector_calibration is None
                or self._mcu.is_fileoutput()):
            return
        identity = self.physical_connection_identity()
        if identity is None:
            return
        gates = self._selector_calibration.gates_for(self._name, self._mcu.get_constants())
        if not gates:
            return
        reactor = self._printer.get_reactor()
        _, instance = identity
        is_current = lambda: self.physical_connection_identity() == identity
        for gate, value in sorted(gates.items()):
            try:
                with self._mcu_lock:
                    reply = send_selector_gate_set(self._mcu, reactor, instance,
                                                   is_current, gate, value)
            except ValueError as exc:
                logging.warning(
                    'klipper_mmu %s: selector calibration for gate %d not '
                    'restored: %s', self._name, gate, exc)
                return
            if reply.result != CommandCode.OK or reply.status != 0:
                logging.warning(
                    'klipper_mmu %s: selector calibration for gate %d refused '
                    '(result=%d status=%d)',
                    self._name, gate, reply.result, reply.status)

    def _disconnect(self):
        self._invalidate('disconnected')

    def _motion_composition_matches(self, expected_bays):
        """Only known catalogue families with exact lane counts admit motion."""
        unit = getattr(self, '_openams_unit', None)
        family = getattr(unit, 'device_family', None)
        expected_id = (_OPENAMS_DEVICE_IDS.get(family.strip().lower())
                       if type(family) is str else None)
        return (expected_id is not None and type(expected_bays) is int
                and self._firmware_composition == (expected_id, expected_bays)
                and getattr(unit, 'bay_count', None) == expected_bays)

    def _host_lease_configuration_matches(self):
        """Match OpenAMS intervals to the exact watchdog in the MCU dictionary."""
        unit = getattr(self, '_openams_unit', None)
        configured = getattr(unit, '_host_watchdog_ms', None)
        max_age = getattr(unit, '_lease_max_age_ms', None)
        firmware = getattr(self, '_firmware_host_lease_ms', None)
        # Lightweight direct protocol users have no managed-unit configuration;
        # the managed OpenAMS path must have an exact advertised bound.
        if configured is None and max_age is None:
            return True
        return (type(firmware) is int and configured == firmware
                and type(max_age) is int and 0 < max_age < firmware)

    def _allocate_challenge(self):
        # Nonzero; 0xffffffff wraps to 1. _mcu_lock admits one exchange at a
        # time and Klipper delivers replies in order, so the next exchange to
        # receive any reply consumes or rejects every older one. A pre-wrap
        # reply could match only after 2**32 - 1 consecutive unanswered
        # exchanges; every reply must still echo exactly in this generation.
        self._challenge = self._challenge % 0xffffffff + 1
        return self._challenge

    def _allocate_snapshot_query(self, generation, firmware_last):
        if (type(firmware_last) is not int or firmware_last < 0
                or firmware_last > 0xffffffff):
            message = 'klipper_mmu: observed snapshot high-water is invalid'
            self._fail(generation, message)
            raise self._printer.command_error(message)
        base = (firmware_last if firmware_last > self._snapshot_query
                else self._snapshot_query)
        if base >= 0xffffffff:
            message = 'klipper_mmu: snapshot query space exhausted'
            self._fail(generation, message)
            raise self._printer.command_error(message)
        # Consume before any snapshot command so a timed-out id is not reused.
        self._snapshot_query = base + 1
        return self._snapshot_query

    def _observe(self):
        """One transport (and startup) observation; caller holds _mcu_lock."""
        generation = self._generation
        challenge = self._allocate_challenge()
        try:
            # Drop any cached snapshot before this observation can yield.
            self._snapshot = None
            if self._transport_query is None:
                self._transport_query = self._mcu.lookup_query_command(
                    TRANSPORT_QUERY, TRANSPORT_STATE)
            params = self._transport_query.send([challenge])
            if generation != self._generation:
                raise self._printer.command_error(
                    'klipper_mmu: stale transport response')
            try:
                observed = decode_transport_state(params)
            except ValueError as exc:
                raise self._printer.command_error(
                    'klipper_mmu: %s' % (exc,))
            if (observed.challenge != challenge
                    or generation != self._generation):
                raise self._printer.command_error(
                    'klipper_mmu: transport challenge was not echoed')
            startup_state = None
            startup_result = None
            startup_detail = None
            if self._diagnostic_startup:
                if self._startup_query is None:
                    self._startup_query = self._mcu.lookup_query_command(
                        DIAGNOSTIC_QUERY, DIAGNOSTIC_STATE)
                startup_params = self._startup_query.send([challenge])
                if generation != self._generation:
                    raise self._printer.command_error(
                        'klipper_mmu: stale transport response')
                startup_state, startup_result = self._decode_startup(
                    startup_params, challenge)
                if generation != self._generation:
                    raise self._printer.command_error(
                        'klipper_mmu: stale transport response')
                if startup_state == 'faulted':
                    startup_detail = self._query_startup_detail(challenge)
            self._connected = True
            self._state = _STATE_NAMES[observed.state]
            self._instance = observed.instance
            self._boot_transition = None
            if observed.state == Binding.ATTACHED:
                if self._last_attached_instance is not None:
                    self._boot_transition = (
                        'same' if observed.instance == self._last_attached_instance
                        else 'changed')
                    if self._boot_transition == 'changed':
                        self._boot_instance_changed = True
                self._last_attached_instance = observed.instance
            self._last_query = observed.last_query
            self._startup_state = startup_state
            self._startup_result = startup_result
            self._startup_detail = startup_detail
            self._last_error = None
        except self._printer.command_error as exc:
            self._fail(generation, str(exc))
            raise
        except Exception as exc:
            message = 'klipper_mmu: transport observation failed: %s' % (exc,)
            self._fail(generation, message)
            raise self._printer.command_error(message)

    def _query_startup_detail(self, challenge):
        """Where a faulted startup stopped; None from firmware without it."""
        if self._detail_query is None:
            if self._mcu.try_lookup_command(DIAGNOSTIC_DETAIL_QUERY) is None:
                return None
            self._detail_query = self._mcu.lookup_query_command(
                DIAGNOSTIC_DETAIL_QUERY, DIAGNOSTIC_DETAIL)
        params = self._detail_query.send([challenge])
        if params.get('challenge') != challenge:
            raise self._printer.command_error(
                'klipper_mmu: diagnostic detail challenge was not echoed')
        return {key: params[key] for key in (
            'step', 'lane', 'cause', 'reason', 'pin0', 'role0', 'lane0',
            'pin1', 'role1', 'lane1')}

    def _startup_problem(self):
        """A Finding explaining a faulted diagnostic startup, else None."""
        if self._startup_state != 'faulted':
            return None
        pins = self._mcu.get_enumerations().get('pin', {})
        return explain_startup_failure(
            self._startup_result, self._startup_detail,
            {number: name for name, number in pins.items()})

    def cmd_KLIPPER_MMU_QUERY(self, gcmd):
        with self._mcu_lock:
            if self._active:
                self._observe()
        status = self.get_status(None)
        gcmd.respond_info(
            'klipper_mmu %s diagnostic state=%s connected=%s instance=%s '
            'last_query=%s firmware_actuation=%s motion_available=%s '
            'last_error=%s diagnostic_startup=%s startup_state=%s '
            'startup_result=%s%s'
            % (self._name, status['state'], status['connected'],
               status['instance'], status['last_query'],
               status['firmware_actuation'], status['motion_available'],
               status['last_error'], status['diagnostic_startup'],
               status['startup_state'], status['startup_result'],
               (' selector_homed=%s selector_gate=%s' % (
                   status['selector_homed'], status['selector_gate'])
                if getattr(self, '_selector_capable', False) else '')))
        problem = self._startup_problem()
        if problem is not None:
            gcmd.respond_info(format_finding(self._name, problem))

    def _observe_attached(self, what):
        """Observe; return the attached (generation, instance). Holds _mcu_lock."""
        if not self._active or self._mcu.is_fileoutput():
            raise self._printer.command_error(
                'klipper_mmu: %s requires an active MCU connection' % what)
        self._observe()
        identity = self.physical_connection_identity()
        if identity is None:
            raise self._printer.command_error(
                'klipper_mmu: %s requires state attached and a nonzero '
                'instance (state=%s)' % (what, self._state))
        return identity

    def cmd_KLIPPER_MMU_TERMINAL(self, gcmd):
        identity = Identity(
            gcmd.get_int('CLIENT', minval=1, maxval=3),
            gcmd.get_int('SESSION', minval=1, maxval=0xffffffff),
            gcmd.get_int('REQUEST', minval=1, maxval=0xffffffff),
            gcmd.get_int('OPERATION', minval=1, maxval=4),
            gcmd.get_int('LANE', minval=0, maxval=7))
        with self._mcu_lock:
            connection = self._observe_attached('terminal query')
            instance = connection[1]
            try:
                found = query_terminal(
                    self._mcu, self._printer.get_reactor(), instance, identity,
                    lambda: self.physical_connection_identity() == connection)
            except Exception as exc:
                raise self._printer.command_error(
                    'klipper_mmu: terminal query failed: %s' % exc)
        if found is None:
            gcmd.respond_info(
                'klipper_mmu %s terminal instance=%s client=%d session=%d '
                'request=%d operation=%d lane=%d history_miss=unknown'
                % (self._name, instance, identity.client, identity.session,
                   identity.request, identity.operation, identity.lane))
        else:
            gcmd.respond_info(
                'klipper_mmu %s terminal instance=%s client=%d session=%d '
                'request=%d operation=%d lane=%d seq=%d timestamp_ms=%d '
                'event_type=%d result_code=%d value=%d flags=%d'
                % (self._name, found.instance, found.identity.client,
                   found.identity.session, found.identity.request,
                   found.identity.operation, found.identity.lane, found.seq,
                   found.timestamp_ms, found.event_type, found.result_code,
                   found.value, found.flags))

    def cmd_KLIPPER_MMU_EVENT(self, gcmd):
        with self._mcu_lock:
            connection = self._observe_attached('event peek')
            instance = connection[1]
            try:
                found = peek_event(
                    self._mcu, self._printer.get_reactor(), instance,
                    lambda: self.physical_connection_identity() == connection)
            except Exception as exc:
                raise self._printer.command_error(
                    'klipper_mmu: event peek failed: %s' % exc)
        if found is None:
            gcmd.respond_info('klipper_mmu %s event instance=%s queue_empty=true'
                              % (self._name, instance))
        else:
            gcmd.respond_info(
                'klipper_mmu %s event instance=%s client=%d session=%d '
                'request=%d operation=%d lane=%d seq=%d timestamp_ms=%d '
                'event_type=%d result_code=%d value=%d flags=%d'
                % (self._name, found.instance, found.identity.client,
                   found.identity.session, found.identity.request,
                   found.identity.operation, found.identity.lane, found.seq,
                   found.timestamp_ms, found.event_type, found.result_code,
                   found.value, found.flags))

    def cmd_KLIPPER_MMU_SENSORS(self, gcmd):
        with self._mcu_lock:
            connection = self._observe_attached('sensor query')
            generation, instance = connection
            try:
                query = self._allocate_snapshot_query(generation, self._last_query)
                current = lambda: self.physical_connection_identity() == connection
                # Only pass a capability kwarg when it is actually set: callers
                # (and tests) that mock read_sensors with a plain 5-argument
                # signature must keep working on an unconfigured connection.
                kwargs = {}
                if getattr(self, '_sensor_load_capable', False):
                    kwargs['load_capable'] = True
                if getattr(self, '_sensor_wide_lanes', False):
                    kwargs['wide_lanes'] = True
                sensors = read_sensors(self._mcu, self._printer.get_reactor(),
                                       instance, query, current, **kwargs)
                if (not current() or sensors.instance != instance
                        or sensors.query != query):
                    raise ValueError('Sensor query connection changed')
                self._last_query = query
                self._last_error = None
            except Exception as exc:
                message = 'klipper_mmu: sensor query failed: %s' % exc
                self._fail(generation, message)
                raise self._printer.command_error(message)

        def describe(name, sample):
            if not sample.configured:
                state = 'unconfigured'
            elif not sample.valid or not sample.fresh:
                state = 'unknown'
            else:
                state = 'present' if sample.present else 'absent'
            return '%s=%s valid=%s fresh=%s sample_ms=%u' % (
                name, state, sample.valid, sample.fresh, sample.sample_ms)

        readings = [describe('bay%d' % i, sample)
                    for i, sample in enumerate(sensors.bays)]
        if getattr(self, '_sensor_load_capable', False):
            readings.extend(describe('load%d' % i, sample)
                            for i, sample in enumerate(sensors.loads[:sensors.lane_count]))
        readings.extend((describe('hub', sensors.hub),
                         describe('toolhead', sensors.toolhead)))
        gcmd.respond_info(
            'klipper_mmu %s sensors instance=%u query=%u observed_ms=%u '
            'lane_count=%u %s' % (
                self._name, sensors.instance, sensors.query,
                sensors.observed_ms, sensors.lane_count, ' '.join(readings)))

    def physical_connection_identity(self):
        """Return the current generation and instance, or None."""
        if (not self._active or not self._connected or self._state != 'attached'
                or type(self._instance) is not int
                or not 0 < self._instance < (1 << 64)):
            return None
        return self._generation, self._instance

    def physical_monotonic(self):
        return self._printer.get_reactor().monotonic()

    def capture_physical_state(self):
        """Explicitly read one coherent capture with a generation proof.

        The caller holds _mcu_lock. Return (identity, start time, status,
        sensors), or None when a read cannot start or the transport
        observation has no attached identity.
        """
        if not self._active or self._mcu.is_fileoutput():
            return None
        self._observe()
        identity = self.physical_connection_identity()
        if identity is None:
            return None
        generation, instance = identity
        try:
            query = self._allocate_snapshot_query(generation, self._last_query)

            def current():
                return self.physical_connection_identity() == identity

            started_at = self.physical_monotonic()
            # Only the capabilities this image has, so an ordinary image makes
            # exactly the call it always did.
            options = {name: True for name, flag in (
                ('load_capable', getattr(self, '_sensor_load_capable', False)),
                ('selector_capable', getattr(self, '_selector_capable', False)),
                ('wide_lanes', getattr(self, '_sensor_wide_lanes', False))) if flag}
            status, sensors = read_physical_state(
                self._mcu, self._printer.get_reactor(), instance, query,
                current, **options)
            if not current():
                return None
            if (status.instance != instance or sensors.instance != instance
                    or status.query != query
                    or sensors.query != query
                    or status.values['acquired_ms'] != sensors.observed_ms):
                raise ValueError('Physical query connection or capture changed')
            self._last_query = query
            self._last_error = None
            return identity, started_at, status, sensors
        except Exception as exc:
            message = 'klipper_mmu: physical query failed: %s' % exc
            self._fail(generation, message)
            raise

    def capture_diag(self, timeout=5.0):
        """Fresh switch bounce/idle-edge and TMC gstat/drv_status capture.

        The caller holds _mcu_lock. Return a DiagSnapshot, or None when the
        connection or KLIPPER_MMU_DIAG_COUNTERS is unavailable. Diagnostic
        only: a failure is reported to the caller and never fails the
        connection.
        """
        if not self._active or self._mcu.is_fileoutput() or not self._diag_capable:
            return None
        self._observe()
        identity = self.physical_connection_identity()
        if identity is None:
            return None
        generation, instance = identity
        try:
            query = self._allocate_snapshot_query(generation, self._last_query)

            def current():
                return self.physical_connection_identity() == identity

            diag = read_diag(self._mcu, self._printer.get_reactor(), instance,
                             query, current, timeout)
            if not current() or diag.instance != instance or diag.query != query:
                return None
            self._last_query = query
            self._last_error = None
            return diag
        except Exception as exc:
            self._last_error = 'klipper_mmu: diag query failed: %s' % exc
            raise

    def prepare_cold_host_session(self, expected_bays, timeout=5.0):
        """Explicitly prepare one HOST session from a fresh, empty capture.

        This is for a future installed adapter. It grants no motion permission.
        A heartbeat with an uncertain result requires external reconciliation;
        this object will not attempt another session on the same lifetime.
        """
        return self._prepare_host_session('Cold', expected_bays, timeout)

    def prepare_occupied_host_session(self, expected_bays, timeout=5.0):
        """Prepare one HOST session for a proven, stopped LOADED lane.

        The returned lane is physical evidence, not motion permission. A
        heartbeat with an uncertain result latches this connection lifetime.
        """
        return self._prepare_host_session('Occupied', expected_bays, timeout)

    def prepare_preloaded_host_session(self, expected_bays, timeout=5.0):
        """Prepare one HOST session for a stopped, lane-local PRELOAD."""
        return self._prepare_host_session('Preloaded', expected_bays, timeout)

    def _prepare_host_session(self, kind, expected_bays, timeout):
        from .session_reconciliation import (choose_host_session_with_physical,
                                             choose_occupied_host_session,
                                             choose_preloaded_host_session)
        choose, prepare = {
            'Cold': (choose_host_session_with_physical, prepare_cold_host_session),
            'Occupied': (choose_occupied_host_session, prepare_occupied_host_session),
            'Preloaded': (choose_preloaded_host_session,
                          prepare_preloaded_host_session)}[kind]
        with self._mcu_lock:
            if (getattr(self, '_host_session_ambiguous', False)
                    or getattr(self, '_prepared_host_session', None) is not None
                    or self._firmware_actuation != 1
                    or not self._diagnostic_startup
                    or self._startup_state != 'ready'
                    or self._startup_result != 0
                    or not self._host_lease_configuration_matches()
                    or not self._motion_composition_matches(expected_bays)
                    or type(expected_bays) is not int or expected_bays < 1):
                raise ValueError('%s HOST session is unavailable' % kind)
            capture = self.capture_physical_state()
            if capture is None:
                raise ValueError('%s HOST session requires a live capture' % kind)
            identity, _, status, sensors = capture
            generation, instance = identity
            if (self.physical_connection_identity() != identity
                    or self._startup_state != 'ready'
                    or self._startup_result != 0
                    or self._firmware_actuation != 1):
                raise ValueError('%s HOST session connection changed' % kind)

            def current(candidate_instance, candidate_generation):
                return (self.physical_connection_identity()
                        == (candidate_generation, candidate_instance)
                        and self._startup_state == 'ready'
                        and self._startup_result == 0
                        and self._firmware_actuation == 1)

            # The helper checks physical eligibility before sending. Only a
            # send attempt has an ambiguous result, so mark that boundary.
            choose(status, sensors, instance, expected_bays,
                  getattr(self, '_virtual_hub_capable', False))
            self._host_session_ambiguous = True
            result = prepare(
                self._mcu, self._printer.get_reactor(), status, sensors,
                instance, expected_bays, generation, current, timeout=timeout,
                virtual_hub=getattr(self, '_virtual_hub_capable', False))
            if not current(instance, generation):
                raise ValueError('%s HOST session connection changed' % kind)
            self._prepared_host_session = (result if kind == 'Cold'
                                           else result.prepared)
            self._host_session_ambiguous = False
            return result

    def start_host_lease(self, prepared, host_watchdog_ms, max_age_ms,
                         timeout=5.0):
        """Explicitly confirm the first bounded renewal of this prepared session."""
        if (self._host_session_ambiguous or self._host_lease is not None
                or prepared is not self._prepared_host_session
                or type(host_watchdog_ms) is not int
                or host_watchdog_ms != getattr(
                    self, '_firmware_host_lease_ms', None)
                or type(max_age_ms) is not int
                or not 0 < max_age_ms < host_watchdog_ms):
            raise ValueError('HOST lease is unavailable')
        generation = prepared.delivery.generation

        def current(instance, candidate_generation):
            return (self.physical_connection_identity()
                    == (candidate_generation, instance)
                    and self._firmware_actuation == 1
                    and self._startup_state == 'ready'
                    and self._startup_result == 0)

        lease = HostLease(prepared, self._mcu, self._printer.get_reactor(),
                          generation, current, host_watchdog_ms, max_age_ms)
        self._host_lease = lease
        self.renew_host_lease(timeout=timeout)
        return lease

    def renew_host_lease(self, timeout=5.0):
        """Refresh board observation and confirm one heartbeat, on the reactor.

        A board fault on this live session refuses renewal like any other
        readiness change, but first revokes HOST motion and services the
        event queue once, so the terminal of the interrupted operation (its
        fault code) is delivered and ACKed instead of stranded behind the
        retained recovery. The firmware ACK needs no lease.
        """
        with self._mcu_lock:
            lease = self._host_lease
            if lease is None or self._host_session_ambiguous:
                raise ValueError('HOST lease is unavailable')
            try:
                capture = self.capture_physical_state()
                if capture is None:
                    raise ConnectionError('HOST lease requires a live board capture')
                identity, _, status, _ = capture
                values = status.values
                sessions = values.get('client_sessions')
                session = lease.prepared.requests.session
                if (identity != (lease.generation, lease.prepared.requests.instance)
                        or values.get('host_connected') is not True
                        or type(sessions) not in (tuple, list) or len(sessions) != 4
                        # HEARTBEAT keeps the previous counter; the first accepted
                        # START advances it to this prepared session.
                        or sessions[Client.HOST] not in (session - 1, session)):
                    raise ConnectionError('HOST lease board readiness changed')
                faulted = (values.get('system_fault') != Fault.NONE
                           or values.get('motion_fault') != Fault.NONE
                           or values.get('follower_fault') != Fault.NONE
                           or values.get('position_uncertain') is not False
                           or values.get('pending_fault_event') is not False)
                if not faulted and values.get('motor_ready') is not True:
                    raise ConnectionError('HOST lease board readiness changed')
                if not faulted:
                    return lease.renew(timeout=timeout)
                lease.confirmed_at = None
            except Exception:
                self._retain_host_recovery()
                raise
        # The event cycle takes the (non-reentrant) mutex itself. A fault the
        # consumer leaves queued (FOLLOW_FAULT, FAULT_ENTERED) is then peeked
        # while the binding is still live: retaining recovery retires it.
        for step in (self.service_openams_event_once, self.peek_retained_fault_once):
            try:
                step(self._openams_unit, timeout=timeout)
            except Exception:
                pass
        with self._mcu_lock:
            self._retain_host_recovery()
        raise ConnectionError('HOST lease board readiness changed')

    def peek_retained_fault_once(self, device, timeout=0.2):
        """Inspect one fault head after readiness or lease renewal is refused.

        This diagnostic read grants no lease, motion, or ACK authority. A
        disconnected prepared session is intentionally allowed here so the
        fault which revoked its lease remains observable.
        """
        from .openams_command_device import OpenAMSCommandDevice

        with self._mcu_lock:
            prepared = self._prepared_host_session
            identity = self.physical_connection_identity()
            binding = getattr(device, '_event_binding', None)
            if (getattr(self, '_event_cycle_in_progress', False)
                    or not isinstance(device, OpenAMSCommandDevice)
                    or device._observer is not self or prepared is None
                    or identity is None or binding is None
                    or binding[1] is not prepared or binding[2] is not self._mcu
                    or binding[3] is not self._printer.get_reactor()
                    or binding[4] != identity
                    or prepared.requests.instance != identity[1]
                    or timeout <= 0):
                raise ValueError('Retained fault peek connection is unavailable')
            session = prepared.requests.session
            status = getattr(device, '_captured_status', None)
            if status is None:
                # A sensor fault is exactly when the unit's own refresh drops
                # its cached status; read the board directly instead.
                capture = self.capture_physical_state()
                status = capture[2] if capture is not None and capture[0] == identity else None
            values = getattr(status, 'values', {}) if status is not None else {}
            lane = values.get('lane')
            if (getattr(status, 'instance', None) != identity[1]
                    or type(lane) is not int or not 0 <= lane <= 255):
                raise ValueError('Retained fault lane is unavailable')

            def current():
                return (self.physical_connection_identity() == identity
                        and self._prepared_host_session is prepared
                        and device._event_binding is binding)

            event = peek_event(self._mcu, self._printer.get_reactor(),
                               identity[1], current, timeout)
        if event is None:
            return 'empty'
        host_fault = (event.event_type in (EventType.FOLLOW_FAULT,
                                           EventType.FAULT_ENTERED)
                      and event.identity.client == Client.HOST
                      and event.identity.session == session
                      and lane < device.bay_count
                      and event.identity.lane == lane)
        global_fault = (event.event_type == EventType.FAULT_ENTERED
                        and event.identity.client == Client.NONE
                        and event.identity.session == 0
                        and event.identity.request == 0
                        and event.identity.operation == OpType.NONE
                        and event.identity.lane == lane
                        and device._ready()
                        and values.get('state') == OpState.FAULT
                        and values.get('client') == Client.NONE
                        and values.get('session') == 0
                        and values.get('request') == 0
                        and values.get('operation') == OpType.NONE
                        and values.get('system_fault') == event.value
                        and values.get('motion_fault') == event.value
                        and type(event.value) is int
                        and event.value != Fault.NONE)
        if (event.instance != identity[1]
                or not (host_fault or global_fault)):
            return 'foreign'
        record = dict(instance=event.instance, generation=identity[0],
                      client=event.identity.client, session=event.identity.session,
                      request=event.identity.request,
                      operation=event.identity.operation, lane=event.identity.lane,
                      seq=event.seq, event_type=event.event_type,
                      result_code=event.result_code, value=event.value,
                      flags=event.flags, pause='not_printing')
        previous = self.retained_fault_diagnostic
        if (previous is not None and previous.get('instance') == event.instance
                and previous.get('generation') == identity[0]
                and previous.get('seq') == event.seq):
            return 'fault'
        self.retained_fault_diagnostic = record
        self._retain_host_recovery()
        self._retained_fault_event = event
        try:
            # Say what the fault means now; the post-operation check then
            # does not repeat it while firmware stays faulted.
            finding = explain_fault(event.value, event.identity.lane)
            if finding is not None:
                self._diagnostic_seen = (
                    getattr(self, '_diagnostic_seen', frozenset()) | {finding})
                self._printer.lookup_object('gcode').respond_info(
                    format_finding(self._name, finding))
        except Exception as exc:
            logging.warning('klipper_mmu: fault explanation unavailable: %s', exc)
        try:
            idle = self._printer.lookup_object('idle_timeout', None)
            if (idle is None or idle.get_status(
                    self._printer.get_reactor().monotonic()).get('state') != 'Printing'):
                return 'fault'
            pause_resume = self._printer.lookup_object('pause_resume', None)
            if (pause_resume is None or not callable(
                    getattr(pause_resume, 'send_pause_command', None))):
                record['pause'] = 'unavailable'
                return 'fault'
            record['pause'] = 'requested'
            pause_resume.send_pause_command()
        except Exception as exc:
            record['pause'] = 'failed: %s' % exc
        return 'fault'

    def retry_retained_fault_ack(self, device, timeout=5.0):
        """Accept and ACK only the exact diagnosed fault or follow-fault head.

        This leaves the old HOST recovery record and fault latch intact.
        A failed reducer effect or ACK can be retried with the same event.
        """
        from .event_delivery import EventDelivery, send_pending_ack
        from .openams_managed_unit import OpenAMSManagedUnit
        from .protocol import Event

        with self._mcu_lock:
            event = getattr(self, '_retained_fault_event', None)
            record = self._host_recovery_record
            runtime = getattr(device, '_runtime', None)
            accept = getattr(runtime, 'accept_firmware_fault', None)
            if (getattr(self, '_event_cycle_in_progress', False)
                    or not isinstance(device, OpenAMSManagedUnit)
                    or device._observer is not self
                    or self._openams_unit is not device
                    or not callable(accept)
                    or not isinstance(event, Event)
                    or event.event_type not in (EventType.FAULT_ENTERED,
                                                EventType.FOLLOW_FAULT)
                    or record is None or not self._host_session_ambiguous
                    or self._host_lease is not None
                    or record.pending_ack is not None
                    or event.instance != record.instance
                    or event.identity.client not in (Client.NONE, Client.HOST)
                    or (event.identity.client == Client.HOST
                        and event.identity.session != record.session)
                    or (event.identity.client == Client.NONE
                        and (event.identity.session, event.identity.request,
                             event.identity.operation) != (0, 0, OpType.NONE))
                    or type(event.value) is not int or event.value <= 0
                    or type(timeout) not in (int, float)
                    or not math.isfinite(timeout) or timeout <= 0):
                raise ValueError('Retained fault ACK recovery is unavailable')
            capture = self.capture_physical_state()
            if capture is None:
                raise ValueError('Retained fault ACK requires a live capture')
            identity, _, status, _ = capture
            fields = status.values
            lane = event.identity.lane
            if (identity != self.physical_connection_identity()
                    or identity[0] < record.generation
                    or identity[1] != event.instance
                    or status.instance != event.instance
                    or self._host_recovery_record is not record
                    or self._retained_fault_event != event
                    or fields.get('state') != OpState.FAULT
                    or fields.get('lane') != lane
                    or fields.get('system_fault') != event.value
                    or fields.get('motion_fault') != event.value):
                raise ValueError('Retained fault capture changed')
            if event.identity.client == Client.HOST:
                sessions = fields.get('client_sessions')
                if (type(lane) is not int or not 0 <= lane < device.bay_count
                        or type(sessions) not in (tuple, list) or len(sessions) != 4
                        or sessions[Client.HOST] != record.session
                        or fields.get('client') != Client.HOST
                        or fields.get('session') != record.session
                        or fields.get('request') != event.identity.request
                        or fields.get('operation') != event.identity.operation):
                    raise ValueError('Retained HOST fault identity changed')
            else:
                if ((fields.get('client'), fields.get('session'),
                     fields.get('request'), fields.get('operation'))
                        != (Client.NONE, 0, 0, OpType.NONE)):
                    raise ValueError('Retained global fault identity changed')
            if lane == 255:
                fps = None
            else:
                if type(lane) is not int or not 0 <= lane < device.bay_count:
                    raise ValueError('Retained fault physical lane changed')
                fps = device.fps_name
                lane_state = getattr(runtime.get_state(), 'lanes', {}).get(fps)
                if (lane_state is None or getattr(lane_state, 'unit', None)
                        != (device.oams_idx, lane)):
                    raise ValueError('Retained fault FPS ownership changed')

            def current():
                return (self.physical_connection_identity() == identity
                        and self._host_recovery_record is record
                        and self._retained_fault_event == event
                        and self._host_session_ambiguous
                        and self._host_lease is None)

            delivery = EventDelivery(event.instance, identity[0])
            self._event_cycle_in_progress = True
            try:
                head = peek_event(self._mcu, self._printer.get_reactor(),
                                  event.instance, current, timeout)
                if not current() or head != event:
                    raise ValueError('Retained fault queue head changed')
                fault_identity = (event.instance, event.seq, event.timestamp_ms,
                                  event.identity.client, event.identity.session,
                                  event.identity.request, event.identity.operation,
                                  lane, event.event_type, event.result_code,
                                  event.value, event.flags)
                # The public reducer may pause or settle a waiter; it owns its
                # idempotence if the following ACK fails or times out. It can
                # run PAUSE or a follower STOP that need the MCU, so release the
                # mutex meanwhile; the event-cycle latch stays held. The only
                # caller is KLIPPER_MMU_ACK_FAULT, which already holds the
                # G-code mutex, so PAUSE must run as from_command.
                self._mcu_lock.__exit__()
                try:
                    # The fresh FAULT capture proves firmware disarmed the
                    # follower; OpenAMS accepts a fault only on a stopped one.
                    following = (fps is not None
                                 and getattr(lane_state, 'following', False) is not False)
                    accepted = ((not following or runtime.accept_verified_follower(
                        fps, lane_state.unit, False) is True)
                        and accept(fps, lane, event.value, fault_identity,
                                   from_command=True))
                finally:
                    self._mcu_lock.__enter__()
                if accepted is True:
                    # OpenAMS's latch is real now regardless of what the ACK
                    # below does; keep the exact pair for a later verified
                    # clear (KLIPPER_MMU_RECONCILE_PATH) to consume.
                    self._accepted_firmware_fault = (lane, fault_identity)
                if accepted is not True or not current():
                    raise ValueError('Retained fault consumer refused or connection changed')
                delivery.pending = event
                delivery.ack_eligible = True
                send_pending_ack(self._mcu, self._printer.get_reactor(), delivery,
                                 delivery.generation, current, timeout)
                self._retained_fault_event = None
                return True
            finally:
                self._event_cycle_in_progress = False

    def service_openams_event_once(self, device, timeout=5.0):
        """Explicitly service one retained event under the MCU mutex."""
        from contextlib import contextmanager
        from .openams_command_device import OpenAMSCommandDevice

        with self._mcu_lock:
            if getattr(self, '_event_cycle_in_progress', False):
                raise ValueError('OpenAMS event transaction is already in progress')
            prepared = self._prepared_host_session
            identity = self.physical_connection_identity()
            binding = getattr(device, '_event_binding', None)
            request_generation = (prepared.requests.generation
                                  if prepared is not None else None)
            if (not isinstance(device, OpenAMSCommandDevice)
                    or device._observer is not self
                    or prepared is None or self._host_session_ambiguous
                    or identity is None or binding is None
                    or binding[1] is not prepared or binding[2] is not self._mcu
                    or binding[3] is not self._printer.get_reactor()
                    or binding[4] != identity
                    or prepared.delivery.generation != identity[0]
                    or prepared.requests.instance != identity[1]
                    or not prepared.requests.connected
                    or not prepared.delivery.connected):
                raise ValueError('OpenAMS event connection is unavailable')

            def current():
                return (self.physical_connection_identity() == identity
                        and self._prepared_host_session is prepared
                        and device._event_binding is binding
                        and prepared.requests.connected
                        and prepared.requests.generation == request_generation
                        and prepared.delivery.connected)

            @contextmanager
            def consumer_boundary():
                # The peek is complete. The runtime may synchronously request a
                # guarded follower STOP while accepting a terminal, which takes
                # the MCU mutex, so release it. Keep the event-cycle latch held
                # until its ACK has been confirmed.
                if not current():
                    raise RuntimeError('OpenAMS event connection changed before consumer')
                original_follower = device.set_oams_follower
                instance_follower = device.__dict__.get('set_oams_follower')
                stop_failed = [False]

                def checked_follower(enable, direction):
                    try:
                        return original_follower(enable, direction)
                    except Exception:
                        if enable == 0:
                            stop_failed[0] = True
                        raise

                device.set_oams_follower = checked_follower
                # A consumed event may end an operation; OpenAMS must not
                # judge the next one from a capture taken before it ended.
                device._snapshot_stale = True
                self._mcu_lock.__exit__()
                try:
                    yield
                finally:
                    if instance_follower is None:
                        del device.set_oams_follower
                    else:
                        device.set_oams_follower = instance_follower
                    self._mcu_lock.__enter__()
                    if stop_failed[0]:
                        raise RuntimeError('OpenAMS follower STOP failed during terminal')

            self._event_cycle_in_progress = True
            try:
                outcome = device.service_event_once(
                    timeout=timeout, consumer_boundary=consumer_boundary)
                if not current():
                    raise ConnectionError('OpenAMS event connection changed; reconcile')
                return outcome
            except Exception:
                if not current():
                    # A callback may have accepted or ACKed a head before the
                    # connection changed. Preserve its pending record for recovery.
                    self._retain_host_recovery()
                raise
            finally:
                self._event_cycle_in_progress = False

    def cmd_KLIPPER_MMU_PHYSICAL(self, gcmd):
        try:
            with self._mcu_lock:
                capture = self.capture_physical_state()
        except Exception as exc:
            raise self._printer.command_error(
                'klipper_mmu: physical query failed: %s' % exc)
        if capture is None:
            raise self._printer.command_error(
                'klipper_mmu: physical query requires an available connection')
        identity, _, status, sensors = capture
        instance = identity[1]
        query = status.query
        presence = project_sensor_presence(
            sensors, instance, sensors.lane_count,
            virtual_hub=getattr(self, '_virtual_hub_capable', False))
        ownership = (reconcile_path_ownership(
                        status, presence,
                        virtual_hub=getattr(self, '_virtual_hub_capable', False))
                     if presence is not None else None)

        def describe(name, sample):
            known = sample.configured and sample.valid and sample.fresh
            state = (('present' if sample.present else 'absent') if known
                     else 'unknown' if sample.configured else 'not_configured')
            return ('%s=%s configured=%s valid=%s fresh=%s sample_ms=%u'
                    % (name, state, sample.configured, sample.valid,
                       sample.fresh, sample.sample_ms))

        readings = [describe('bay%d' % i, sample)
                    for i, sample in enumerate(sensors.bays)]
        readings.extend((describe('hub', sensors.hub),
                         describe('toolhead', sensors.toolhead)))
        owner = ('known lane %d' % ownership.lane if ownership is not None
                 and ownership.kind == 'known' else
                 'proven empty' if ownership is not None
                 and ownership.kind == 'empty' else 'unknown')
        gcmd.respond_info(
            'klipper_mmu %s physical instance=%u query=%u acquired_ms=%u '
            'ownership=%s %s' % (self._name, instance, query,
                                  sensors.observed_ms, owner,
                                  ' '.join(readings)))

    def cmd_KLIPPER_MMU_DIAGNOSE(self, gcmd):
        """Apply the docs/diagnostics.md rules to one fresh capture.

        Reuses the same physical capture path as KLIPPER_MMU_PHYSICAL, the
        retained fault diagnostic, the LOAD attempt report, the stored
        Bowden estimate and, with KLIPPER_MMU_DIAG_COUNTERS, one switch
        bounce/idle-edge and TMC gstat/drv_status capture. Read-only; never
        moves and never affects an ACK.
        """
        try:
            with self._mcu_lock:
                capture = self.capture_physical_state()
        except Exception as exc:
            raise self._printer.command_error(
                'klipper_mmu: DIAGNOSE failed: %s' % exc)
        if capture is None:
            # A faulted startup never attaches (latched until MCU reset);
            # explain it instead of refusing.
            problem = self._startup_problem()
            if problem is None:
                raise self._printer.command_error(
                    'klipper_mmu: DIAGNOSE requires an available connection')
            gcmd.respond_info(format_finding(self._name, problem))
            return
        _, _, status, sensors = capture
        diag = None
        if self._diag_capable:
            try:
                with self._mcu_lock:
                    diag = self.capture_diag()
            except Exception as exc:
                logging.warning('klipper_mmu %s: diag counters unavailable: %s',
                                self._name, exc)
        findings = diagnose(
            status, sensors, retained_fault=self.retained_fault_diagnostic,
            last_load=self.last_load, bowden_steps=self._bowden_arrival_steps(),
            startup_state=self._startup_state, startup_result=self._startup_result,
            diag=diag, diag_baseline=self._diag_baseline,
            arrival_history=getattr(self, '_arrival_history', None))
        if diag is not None:
            self._diag_baseline = diag
        if not findings:
            gcmd.respond_info('klipper_mmu %s: no problems found; %s'
                              % (self._name, summarize(status, sensors)))
            return
        for finding in findings:
            gcmd.respond_info(format_finding(self._name, finding))

    def cmd_KLIPPER_MMU_COMMISSION(self, gcmd):
        """STEP=switches|lane|fps [LANE=n] [ANSWER=yes|no]; docs/diagnostics.md.

        Motion only through the existing PRELOAD/EJECT managed-unit paths.
        STEP=switches and STEP=fps need no motion and work on a safe
        (ACTUATION=0) image; only STEP=lane requires openams_motion.
        """
        step = gcmd.get('STEP', '')
        if step not in ('switches', 'lane', 'fps'):
            raise self._printer.command_error(
                'klipper_mmu: COMMISSION STEP must be switches, lane or fps')
        try:
            if step == 'switches':
                self._commission_switches(gcmd)
            elif step == 'fps':
                self._commission_fps(gcmd)
            else:
                unit = self._openams_unit
                if not isinstance(unit, OpenAMSManagedUnit):
                    raise self._printer.command_error(
                        'COMMISSION STEP=lane requires openams_motion')
                self._commission_lane(gcmd, unit)
        except self._printer.command_error:
            raise
        except Exception as exc:
            raise self._printer.command_error(
                'klipper_mmu: COMMISSION %s: %s' % (step, exc))

    def _commission_capture(self):
        with self._mcu_lock:
            capture = self.capture_physical_state()
        if capture is None:
            raise self._printer.command_error(
                'klipper_mmu: COMMISSION requires an available connection')
        return capture

    def _commission_switches(self, gcmd):
        gcmd.respond_info('klipper_mmu %s: %s' % (self._name, INSTRUCTIONS['switches']))
        _, _, _, sensors = self._commission_capture()
        before = None
        if self._diag_capable:
            with self._mcu_lock:
                before = self.capture_diag()
        for name, state, note in commission_switches(sensors, before):
            gcmd.respond_info('klipper_mmu %s switch %s=%s %s'
                              % (self._name, name, state, note))
        if before is None:
            return
        gcmd.respond_info('klipper_mmu %s: %s'
                          % (self._name, INSTRUCTIONS['switches_wiggle']))
        reactor = self._printer.get_reactor()
        reactor.pause(reactor.monotonic() + SWITCH_WIGGLE_S)
        with self._mcu_lock:
            after = self.capture_diag()
        if after is None:
            raise self._printer.command_error(
                'klipper_mmu: COMMISSION requires an available connection')
        findings = commission_switch_wiggle(before, after, sensors.lane_count)
        if not findings:
            gcmd.respond_info('klipper_mmu %s: no bounce or idle-edge change '
                              'seen while wiggling' % self._name)
            return
        for finding in findings:
            gcmd.respond_info(format_finding(self._name, finding))

    def _commission_fps(self, gcmd):
        gcmd.respond_info('klipper_mmu %s: %s' % (self._name, INSTRUCTIONS['fps']))
        reactor = self._printer.get_reactor()
        deadline = reactor.monotonic() + _FPS_SAMPLE_S
        samples = []
        while reactor.monotonic() < deadline:
            _, _, status, _ = self._commission_capture()
            samples.append((status.values.get('buffer_position'),
                            status.values.get('buffer_valid')))
            reactor.pause(reactor.monotonic() + _FPS_SAMPLE_PERIOD)
        findings = commission_fps(samples)
        if not findings:
            gcmd.respond_info('klipper_mmu %s: FPS buffer OK' % self._name)
            return
        for finding in findings:
            gcmd.respond_info(format_finding(self._name, finding))

    def _commission_lane(self, gcmd, unit):
        lane = gcmd.get_int('LANE', minval=0, maxval=unit.bay_count - 1)
        answer = gcmd.get('ANSWER', None)
        if answer is not None:
            if answer not in ('yes', 'no'):
                raise self._printer.command_error(
                    'klipper_mmu: ANSWER must be yes or no')
            gcmd.respond_info(format_finding(
                self._name, commission_respooler_answer(lane, answer == 'yes')))
            return
        _, _, _, baseline = self._commission_capture()
        gcmd.respond_info('klipper_mmu %s: %s' % (self._name, INSTRUCTIONS['lane'] % lane))
        reactor = self._printer.get_reactor()
        deadline = reactor.monotonic() + _COMMISSION_WAIT
        while True:
            _, _, _, sensors = self._commission_capture()
            outcome, other = commission_lane_insert(baseline, sensors, lane)
            if outcome == 'ok':
                break
            if outcome == 'wrong_lane':
                gcmd.respond_info(format_finding(self._name, Finding(
                    'error', 'lane wiring',
                    "inserting into lane %d made lane %d's switch instead"
                    % (lane, other),
                    'lanes %d and %d are wired swapped at the AFC board'
                    % (lane, other))))
                return
            if reactor.monotonic() >= deadline:
                raise self._printer.command_error(
                    'klipper_mmu: COMMISSION timed out waiting for lane %d BAY switch'
                    % lane)
            reactor.pause(reactor.monotonic() + _COMMISSION_POLL)
        _, response = unit.start_preload(lane)
        if isinstance(response, Admission) and response.admission in ADMITTED:
            response = self._await_local_terminal(
                unit, 'preload_terminal', reactor.monotonic() + _COMMISSION_WAIT)
        gcmd.respond_info(format_finding(
            self._name, commission_preload_result(lane, response)))
        _, response = unit.start_eject(lane)
        if isinstance(response, Admission) and response.admission in ADMITTED:
            response = self._await_local_terminal(
                unit, 'eject_terminal', reactor.monotonic() + _COMMISSION_WAIT)
        gcmd.respond_info(format_finding(
            self._name, commission_eject_result(lane, response)))
        gcmd.respond_info(
            "klipper_mmu %s: watch lane %d's spool now; then run "
            'KLIPPER_MMU_COMMISSION MMU=%s STEP=lane LANE=%d ANSWER=yes or ANSWER=no'
            % (self._name, lane, self._name, lane))

    def _await_local_terminal(self, unit, field, deadline):
        """Poll a standalone PRELOAD/EJECT result the same way the plain
        BAY-less query reports it (cmd_KLIPPER_MMU_PRELOAD/EJECT), bounded."""
        reactor = self._printer.get_reactor()
        while True:
            binding = unit._event_binding
            if binding is None or not unit._event_current():
                raise self._printer.command_error(
                    'klipper_mmu: COMMISSION connection changed')
            result = getattr(binding[1].requests, field)
            if result is not None:
                return result
            if reactor.monotonic() >= deadline:
                raise self._printer.command_error(
                    'klipper_mmu: COMMISSION timed out waiting for a result')
            reactor.pause(reactor.monotonic() + _COMMISSION_POLL)

    def cmd_KLIPPER_MMU_SNAPSHOT(self, gcmd):
        with self._mcu_lock:
            # A fresh transport observation, then one snapshot of that instance.
            connection = self._observe_attached('snapshot')
            generation, instance = connection
            query = self._allocate_snapshot_query(generation, self._last_query)
            assembler = SnapshotAssembler(instance, query)

            def on_part(name, params):
                hi, lo = params.get('instance_hi'), params.get('instance_lo')
                part_query = params.get('query')
                if (type(hi) is not int or type(lo) is not int
                        or type(part_query) is not int
                        or not 0 <= hi <= 0xffffffff or not 0 <= lo <= 0xffffffff
                        or (hi << 32 | lo) != instance or part_query != query):
                    return None
                return assembler.add(name, params)

            def on_result(params):
                command, result_query = params.get('command'), params.get('query')
                if (type(command) is not int or type(result_query) is not int
                        or command != Command.SNAPSHOT or result_query != query):
                    return None
                decoded = decode_command_result(params)
                if decoded.result == CommandCode.OK:
                    return None
                raise ValueError('snapshot query %u rejected with command '
                                 'result %u' % (decoded.query, decoded.result))

            replies = {fmt: lambda params, name=name: on_part(name, params)
                       for name, fmt in zip(STATUS_MESSAGES, STATUS_REPLIES)}
            replies[COMMAND_RESULT] = on_result
            try:
                payload = exchange(
                    self._mcu, self._printer.get_reactor(),
                    [(SNAPSHOT_PART, [instance >> 32, instance & 0xffffffff,
                                      query, part]) for part in range(5)],
                    replies,
                    lambda: self.physical_connection_identity() == connection,
                    _SNAPSHOT_TIMEOUT, 'snapshot')
            except ValueError as exc:
                message = 'klipper_mmu: %s' % (exc,)
                self._fail(generation, message)
                raise self._printer.command_error(message)
            except Exception as exc:
                message = 'klipper_mmu: snapshot failed: %s' % (exc,)
                self._fail(generation, message)
                raise self._printer.command_error(message)
            stored = {'instance': payload.instance, 'query': payload.query}
            stored.update(payload.values)
            self._snapshot = stored
            self._last_query = query
            self._last_error = None
            published = dict(stored)
        details = ' '.join(
            '%s=%s' % (key, published[key]) for key in sorted(published))
        gcmd.respond_info('klipper_mmu %s snapshot %s' % (self._name, details))


def load_config_prefix(config):
    return KlipperMMU(config)
