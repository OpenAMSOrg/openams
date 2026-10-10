# SPDX-License-Identifier: GPL-3.0-or-later
"""The host's one copy of the Klipper-MMU MCU wire contract.

Formats are the firmware's exact message declarations; Klipper's lookups
reject any that the connected dictionary lacks. Enumerated field values are
the firmware's DECL_ENUMERATION entries (mcu/enumerations.c), compared by
check_enumerations() at connect, so a renumbered value is never read with the
host's meaning. Their dictionary names match no message parameter, so
Klipper's parser still returns plain integers.
"""

# Commands and their replies, exactly as the firmware declares them.
TRANSPORT_QUERY = 'km_transport_query challenge=%u'
TRANSPORT_STATE = ('km_transport_state challenge=%u instance_hi=%u '
                   'instance_lo=%u last_query=%u state=%c')
DIAGNOSTIC_QUERY = 'km_diagnostic_query challenge=%u'
DIAGNOSTIC_STATE = 'km_diagnostic_state challenge=%u state=%c result=%i'
DIAGNOSTIC_START_HOST = 'km_diagnostic_start_host epoch_hi=%u epoch_lo=%u'
DIAGNOSTIC_DETAIL_QUERY = 'km_diagnostic_detail_query challenge=%u'
DIAGNOSTIC_DETAIL = ('km_diagnostic_detail challenge=%u step=%c lane=%c cause=%i '
                     'reason=%c pin0=%hu role0=%c lane0=%c pin1=%hu role1=%c '
                     'lane1=%c')
DIFF_PROBE_QUERY = 'km_diff_probe_query challenge=%u'
DIFF_PROBE_STATE = ('km_diff_probe_state challenge=%u state=%c result=%i '
                    'a_raw=%hu b_raw=%hu a_attempt_us=%u b_ready_us=%u '
                    'observed_us=%u')
SWITCH_DELAY_QUERY = ('km_switch_delay_query challenge=%u instance_hi=%u '
                      'instance_lo=%u')
SWITCH_DELAY_STATE = ('km_switch_delay_state challenge=%u instance_hi=%u '
                      'instance_lo=%u valid=%c delay_us=%u count=%u max_us=%u')
START = ('km_start instance_hi=%u instance_lo=%u client=%c session=%u '
         'request=%u operation=%c lane=%c direction=%c')
CANCEL = ('km_cancel instance_hi=%u instance_lo=%u client=%c session=%u '
          'request=%u operation=%c lane=%c')
HEARTBEAT = 'km_heartbeat instance_hi=%u instance_lo=%u session=%u'
STOP = 'km_stop instance_hi=%u instance_lo=%u'
ADMISSION = ('km_admission instance_hi=%u instance_lo=%u client=%c session=%u '
             'request=%u operation=%c lane=%c admission=%c fault=%c')
COMMAND_RESULT = ('km_command_result instance_hi=%u instance_lo=%u command=%c '
                  'query=%u result=%c')
PATH_RECONCILE = ('km_path_reconcile instance_hi=%u instance_lo=%u query=%u '
                  'occupied=%c lane=%c')
PATH_RECONCILE_RESULT = ('km_path_reconcile_result instance_hi=%u '
                         'instance_lo=%u query=%u result=%c status=%c')
DRIVER_RESTART = ('km_driver_restart instance_hi=%u instance_lo=%u query=%u '
                  'lane=%c')
DRIVER_RESTART_RESULT = ('km_driver_restart_result instance_hi=%u '
                         'instance_lo=%u query=%u result=%c status=%c')
CONFIG_SELECTOR_GATE = 'config_km_selector_gate oid=%c gate=%c steps=%i'
CONFIG_FEEDER_ROTATION = 'config_km_feeder_rotation oid=%c lane=%c um=%u'
CALIBRATED_ARRIVAL_SET = ('km_calibrated_arrival_set instance_hi=%u instance_lo=%u '
                          'steps=%u')
CALIBRATED_ARRIVAL_SET_RESULT = ('km_calibrated_arrival_set_result instance_hi=%u '
                                 'instance_lo=%u result=%c status=%c')
SELECTOR_GATE_SET = ('km_selector_gate_set instance_hi=%u instance_lo=%u '
                     'gate=%c value=%i')
SELECTOR_GATE_SET_RESULT = ('km_selector_gate_set_result instance_hi=%u '
                            'instance_lo=%u result=%c status=%c')
SELECTOR_SERVO_TEST = ('km_selector_servo_test instance_hi=%u instance_lo=%u '
                       'degrees=%i')
SELECTOR_SERVO_TEST_RESULT = ('km_selector_servo_test_result instance_hi=%u '
                              'instance_lo=%u result=%c status=%c')
SELECTOR_MOTORS_OFF = 'km_selector_motors_off instance_hi=%u instance_lo=%u'
SELECTOR_MOTORS_OFF_RESULT = ('km_selector_motors_off_result instance_hi=%u '
                              'instance_lo=%u result=%c status=%c')
EVENT_PEEK = 'km_event_peek instance_hi=%u instance_lo=%u'
EVENT_ACK = ('km_event_ack instance_hi=%u instance_lo=%u client=%c session=%u '
             'request=%u operation=%c lane=%c seq=%u')
TERMINAL_QUERY = ('km_terminal_query instance_hi=%u instance_lo=%u client=%c '
                  'session=%u request=%u operation=%c lane=%c')
EVENT = ('km_event instance_hi=%u instance_lo=%u seq=%u timestamp_ms=%u '
         'client=%c session=%u request=%u operation=%c lane=%c '
         'event_type=%c result_code=%c value=%u flags=%c')
LOAD_ATTEMPTS_QUERY = ('km_load_attempts_query instance_hi=%u instance_lo=%u '
                       'client=%c session=%u request=%u operation=%c lane=%c')
LOAD_ATTEMPTS = ('km_load_attempts instance_hi=%u instance_lo=%u session=%u '
                 'request=%u count=%c stage0=%c fault0=%c travel0=%u stage1=%c '
                 'fault1=%c travel1=%u stage2=%c fault2=%c travel2=%u')
SNAPSHOT_PART = 'km_snapshot_part instance_hi=%u instance_lo=%u query=%u part=%c'
# Replies in part order; one retained capture per query.
STATUS_REPLIES = (
    'km_status_identity instance_hi=%u instance_lo=%u query=%u state=%c '
    'stage=%c operation=%c client=%c session=%u request=%u lane=%c '
    'owner_lane=%c occupant_lane=%c flags=%hu queued_events=%c '
    'head_event_seq=%u',
    'km_status_motion instance_hi=%u instance_lo=%u query=%u '
    'velocity_bits=%u position_hi=%u position_lo=%u '
    'buffer_position_bits=%u system_fault=%c motion_fault=%c '
    'follower_state=%c follower_direction=%c follower_fault=%c',
    'km_status_time instance_hi=%u instance_lo=%u query=%u '
    'commanded_current_ma=%hu measured_current_ma=%hu '
    'measured_current_sample_ms=%u control_ms=%u control_us=%u '
    'acquired_ms=%u acquired_us=%u',
    'km_status_sessions instance_hi=%u instance_lo=%u query=%u '
    'client0=%u client1=%u client2=%u client3=%u',
    'km_status_requests instance_hi=%u instance_lo=%u query=%u '
    'client0=%u client1=%u client2=%u client3=%u',
    'km_status_selector instance_hi=%u instance_lo=%u query=%u '
    'homed=%c gate=%c',
)
SENSOR_PART = 'km_sensor_part instance_hi=%u instance_lo=%u query=%u part=%c'
SENSOR_REPLIES = (
    'km_sensor_flags instance_hi=%u instance_lo=%u query=%u lane_count=%c '
    'observed_ms=%u configured=%hu present=%hu valid=%hu fresh=%hu',
    'km_sensor_bays0 instance_hi=%u instance_lo=%u query=%u '
    'sample0=%u sample1=%u sample2=%u sample3=%u',
    'km_sensor_bays1 instance_hi=%u instance_lo=%u query=%u '
    'sample4=%u sample5=%u sample6=%u sample7=%u '
    'hub_sample_ms=%u toolhead_sample_ms=%u',
    # Parts 3-4 only with KLIPPER_MMU_SENSOR_LOADS=1.
    'km_sensor_loads0 instance_hi=%u instance_lo=%u query=%u '
    'configured=%c present=%c valid=%c fresh=%c '
    'sample0=%u sample1=%u sample2=%u sample3=%u',
    'km_sensor_loads1 instance_hi=%u instance_lo=%u query=%u '
    'sample4=%u sample5=%u sample6=%u sample7=%u',
    # Part 5 only with lane_count > 8 (KLIPPER_MMU_LANE_COUNT); appended after
    # the original five parts so an 8-lane image's part numbers are unchanged.
    'km_sensor_bays2 instance_hi=%u instance_lo=%u query=%u '
    'sample8=%u sample9=%u sample10=%u sample11=%u',
    # Part 6 only with KLIPPER_MMU_SENSOR_LOADS=1 and lane_count > 8.
    'km_sensor_loads2 instance_hi=%u instance_lo=%u query=%u '
    'configured=%c present=%c valid=%c fresh=%c '
    'sample8=%u sample9=%u sample10=%u sample11=%u',
)
# Switch bounce/idle-edge and TMC gstat/drv_status counters (docs/diagnostics.md).
# Shares the sensor/snapshot query high-water; KLIPPER_MMU_DIAG_COUNTERS gates
# availability and KLIPPER_MMU_TMC_DIAG says whether the TMC parts are real.
DIAG_PART = 'km_diag_part instance_hi=%u instance_lo=%u query=%u part=%c'
DIAG_REPLIES = (
    'km_diag_bay_bounce instance_hi=%u instance_lo=%u query=%u '
    'count0=%hu count1=%hu count2=%hu count3=%hu count4=%hu count5=%hu count6=%hu count7=%hu',
    'km_diag_bay_idle instance_hi=%u instance_lo=%u query=%u '
    'edges0=%hu edges1=%hu edges2=%hu edges3=%hu edges4=%hu edges5=%hu edges6=%hu edges7=%hu '
    'hub_edges=%hu hub_bounce=%hu toolhead_bounce=%hu selector_bounce=%hu',
    'km_diag_load_bounce instance_hi=%u instance_lo=%u query=%u '
    'count0=%hu count1=%hu count2=%hu count3=%hu count4=%hu count5=%hu count6=%hu count7=%hu',
    'km_diag_load_idle instance_hi=%u instance_lo=%u query=%u '
    'edges0=%hu edges1=%hu edges2=%hu edges3=%hu edges4=%hu edges5=%hu edges6=%hu edges7=%hu',
    'km_diag_tmc0 instance_hi=%u instance_lo=%u query=%u '
    'gstat0=%c gstat1=%c gstat2=%c gstat3=%c '
    'drv_status0=%u drv_status1=%u drv_status2=%u drv_status3=%u',
    'km_diag_tmc1 instance_hi=%u instance_lo=%u query=%u '
    'gstat4=%c gstat5=%c gstat6=%c gstat7=%c '
    'drv_status4=%u drv_status5=%u drv_status6=%u drv_status7=%u',
)
_WIDTHS = {'%c': 8, '%hu': 16, '%u': 32}


def payload_fields(fmt):
    """Return ((name, bits), ...) after the instance/query header of a reply."""
    fields = [arg.split('=') for arg in fmt.split()[1:]]
    return tuple((name, _WIDTHS[kind]) for name, kind in fields
                 if name not in ('instance_hi', 'instance_lo', 'query'))


# Enumerations: dictionary name -> C enumerator suffixes and values.
class OpState:  # km_op_state_t, status `state`
    IDLE = 0
    LOADING = 1
    LOADED = 2
    UNLOADING = 3
    FOLLOWING = 4
    CALIBRATING = 5
    FAULT = 6
    PRELOADING = 7
    PRELOADED = 8
    EJECTING = 9


class Stage:  # km_motion_stage_t, status `stage`
    STOPPED = 0
    LOAD_FEED_TO_HUB = 1
    LOAD_FEED_TO_BUFFER = 2
    FOLLOW = 3
    UNLOAD_RETRACT_TO_HUB = 4
    UNLOAD_REWIND_TO_SPOOL = 5
    UNLOAD_STOP_SETTLE = 6
    CALIBRATE_PROBE = 7
    CALIBRATE_RETRACT = 8
    BRAKING = 9
    UNLOAD_RETRACT_TO_LOAD = 10
    EJECT_TO_BAY = 11
    LOAD_RETRY_RETRACT = 12
    SELECT_HOME = 13
    SELECT_MOVE = 14
    SERVO_DWELL = 15


class OpType:  # km_operation_type_t, `operation`
    NONE = 0
    LOAD = 1
    UNLOAD = 2
    FOLLOW = 3
    CALIBRATE = 4
    CANCEL = 5
    PRELOAD = 6
    EJECT = 7
    HOME = 8


class Client:  # km_client_id_t, `client`; also the client0..3 index
    NONE = 0
    HOST = 1
    LOCAL_UI = 2
    INTERNAL = 3


class AdmissionResult:  # km_admission_result_t (KM_REQ_*), `admission`
    ACCEPTED = 0
    ALREADY_IN_PROGRESS = 1
    BUSY = 2
    INVALID_LANE = 3
    NO_SPOOL = 4
    PATH_OCCUPIED = 5
    FAULTED = 6
    UNAVAILABLE = 7
    REJECTED_STALE = 8
    REJECTED_CONFLICT = 9
    ALREADY_COMPLETED = 10


class Fault:  # km_fault_t, `fault` and status *_fault
    NONE = 0
    HOST_COMM_LOST = 1
    DRIVER_OVERCURRENT = 2
    DRIVER_FAULT = 3
    NO_PROGRESS = 4
    JAM = 5
    BUFFER_SIGNAL = 6
    FILAMENT_LOST = 7
    STARVED = 8
    STALLED = 9
    EMERGENCY_STOP = 10
    SENSOR_FAILURE = 11
    # A deliberate append-only protocol addition: the control timing fault has
    # its own code, so a user is not sent to check motor power and the UART
    # cable when the step queue merely ran dry. No protocol bump; never renumber.
    CONTROL_TIMING = 12
    SELECTOR = 13


class EventType:  # km_event_type_t, `event_type`
    NONE = 0
    LOAD_COMPLETE = 1
    LOAD_CANCELLED = 2
    LOAD_FAILED = 3
    UNLOAD_COMPLETE = 4
    UNLOAD_CANCELLED = 5
    UNLOAD_FAILED = 6
    FOLLOW_FAULT = 7
    PTFE_CAL_COMPLETE = 8
    PTFE_CAL_CANCELLED = 9
    PTFE_CAL_FAILED = 10
    STOPPED = 11
    FAULT_ENTERED = 12
    PRELOAD_COMPLETE = 13
    PRELOAD_CANCELLED = 14
    PRELOAD_FAILED = 15
    EJECT_COMPLETE = 16
    EJECT_CANCELLED = 17
    EJECT_FAILED = 18
    HOME_COMPLETE = 19
    HOME_FAILED = 20


class EventFlag:  # KM_EVENT_FLAG_* bits, `flags`
    OVERFLOW = 1
    # PTFE_CAL_COMPLETE of CALIBRATE only: value is completed feeder
    # microsteps from HUB to qualified FPS arrival, not encoder clicks.
    STEP_ESTIMATE = 2


class OpCode:  # KM_WIRE_OP_CODE_* (OpenAMS), event `result_code`
    SUCCESS = 0
    ERROR_UNSPECIFIED = 1
    ERROR_BUSY = 2
    SPOOL_ALREADY_IN_BAY = 3
    NO_SPOOL_IN_BAY = 4
    ERROR_KLIPPER_CALL = 5
    CANCEL = 6
    TIMEOUT = 7


class Command:  # enum km_command_kind, km_command_result `command`
    START = 1
    CANCEL = 2
    HEARTBEAT = 3
    STOP = 4
    EVENT_PEEK = 5
    EVENT_ACK = 6
    TERMINAL = 7
    SNAPSHOT = 8


class CommandCode:  # enum km_command_result, `result` of command replies
    OK = 0
    UNBOUND = 1
    INSTANCE_MISMATCH = 2
    INVALID = 3
    STALE = 4
    EMPTY = 5
    SHUTDOWN = 6
    UNAVAILABLE = 7


class Binding:  # enum km_binding_state, km_transport_state `state`
    UNBOUND = 0
    ATTACHED = 1
    SHUTDOWN = 2
    CLOCK_UNAVAILABLE = 3


class FollowerState:  # km_follower_state_t, `follower_state`
    DISARMED = 0
    IDLE = 1
    DRIVING = 2
    FAULT = 3


class Direction:  # km_direction_t, `direction` / `follower_direction`
    REVERSE = 0
    FORWARD = 1


class StartupState:  # enum km_diagnostic_state, km_diagnostic_state `state`
    IDLE = 0
    WAITING = 1
    READY = 2
    FAULTED = 3


class StartupStep:  # enum km_startup_step, km_diagnostic_detail `step`
    NONE = 0
    CLOCK = 1
    IDENTITY = 2
    CALIBRATION = 3
    DEADLINE = 4
    ALREADY_STARTED = 5
    PROFILE = 6
    PINS = 7
    RESOURCES = 8
    SAVED_CONFIG = 9
    RESERVE = 10
    SWITCHES = 11
    ADC = 12
    DRIVER_SETUP = 13
    REWIND_SETUP = 14
    DRIVER_CONFIRM = 15
    BIND = 16
    ATTACH = 17
    SELECTOR_SETUP = 18


class PinRole:  # enum km_pin_role, km_diagnostic_detail `role0`/`role1`
    STEP = 1
    DIR = 2
    ENABLE = 3
    UART = 4
    BAY = 5
    HUB = 6
    TOOLHEAD = 7
    BUFFER_A = 8
    BUFFER_B = 9
    REWIND_FORWARD = 10
    REWIND_REVERSE = 11
    REWIND_ENABLE = 12
    ENCODER_A = 13
    ENCODER_B = 14
    CURRENT = 15
    LOAD = 16
    SELECTOR_STEP = 17
    SELECTOR_DIR = 18
    SELECTOR_ENABLE = 19
    SELECTOR_UART = 20
    SELECTOR_ENDSTOP = 21
    SERVO = 22


class ResourceDiag:  # km_resource_diag_reason_t, km_diagnostic_detail `reason`
    NONE = 0
    BAD_ARGUMENT = 1
    BAD_CLAIM = 2
    DUPLICATE_RESOURCE = 3
    TIMER_CONFIGURATION = 4
    MISSING_TIMER_BASE = 5
    MISSING_ADC_UNIT = 6


NO_LANE = 0xff  # km_diagnostic_detail lane fields: not a per-lane step
NO_PIN = 0xffff  # km_diagnostic_detail pin fields: unknown


class Result:  # km_result_t (KM_*): startup `result`, negated reconcile `status`
    OK = 0
    ERR_INVALID_PARAM = -1
    ERR_BUSY = -2
    ERR_UNAVAILABLE = -3
    ERR_FAULTED = -4
    ERR_CONFLICT = -5
    ERR_STALE = -6
    ERR_TIMEOUT = -7
    ERR_NOT_SUPPORTED = -8
    ERR_QUEUE_FULL = -9
    ERR_QUEUE_EMPTY = -10


ENUMERATIONS = {
    'km_op_state': OpState, 'km_stage': Stage, 'km_op_type': OpType,
    'km_client': Client, 'km_admission': AdmissionResult, 'km_fault': Fault,
    'km_event_type': EventType, 'km_event_flag': EventFlag,
    'km_op_code': OpCode, 'km_command': Command,
    'km_command_result': CommandCode, 'km_binding': Binding,
    'km_follower_state': FollowerState, 'km_direction': Direction,
    'km_startup_state': StartupState, 'km_result': Result,
    'km_startup_step': StartupStep, 'km_pin_role': PinRole,
    'km_resource_diag': ResourceDiag,
}


def members(namespace):
    """Return {NAME: value} for one enumeration class above."""
    return {name: value for name, value in vars(namespace).items()
            if name.isupper()}


def known(namespace):
    """Return the frozenset of values of one enumeration class."""
    return frozenset(members(namespace).values())


# Retained terminal events of each host operation: success, cancel, failure.
TERMINAL_EVENTS = {
    OpType.LOAD: (EventType.LOAD_COMPLETE, EventType.LOAD_CANCELLED,
                  EventType.LOAD_FAILED),
    OpType.UNLOAD: (EventType.UNLOAD_COMPLETE, EventType.UNLOAD_CANCELLED,
                    EventType.UNLOAD_FAILED),
    OpType.CALIBRATE: (EventType.PTFE_CAL_COMPLETE,
                       EventType.PTFE_CAL_CANCELLED,
                       EventType.PTFE_CAL_FAILED),
    OpType.PRELOAD: (EventType.PRELOAD_COMPLETE, EventType.PRELOAD_CANCELLED,
                     EventType.PRELOAD_FAILED),
    OpType.EJECT: (EventType.EJECT_COMPLETE, EventType.EJECT_CANCELLED,
                   EventType.EJECT_FAILED),
    OpType.HOME: (EventType.HOME_COMPLETE, None, EventType.HOME_FAILED),
}
EVENT_TYPES = known(EventType) - {EventType.NONE}
# Admissions that leave a request to a retained terminal, not a rejection.
ADMITTED = frozenset((AdmissionResult.ACCEPTED,
                      AdmissionResult.ALREADY_IN_PROGRESS,
                      AdmissionResult.ALREADY_COMPLETED))


def check_enumerations(firmware):
    """Compare the dictionary's enumerations with ENUMERATIONS.

    Return True when every host name is declared with the host's value (the
    firmware may declare more). Return False when none of these enumerations
    is declared: a protocol 1 image built before they were exported, whose
    values are the ones above (values have only been appended). Raise
    ValueError naming each missing or different entry otherwise.
    """
    if not any(name in firmware for name in ENUMERATIONS):
        return False
    problems = []
    for enum, namespace in ENUMERATIONS.items():
        declared = firmware.get(enum, {})
        for name, value in members(namespace).items():
            actual = declared.get(name)
            if actual is None:
                problems.append('%s %s missing' % (enum, name))
            elif type(actual) is not int or actual != value:
                problems.append('%s %s is %r, host expects %d'
                                % (enum, name, actual, value))
    if problems:
        raise ValueError('firmware enumerations differ from this host: '
                         + '; '.join(problems))
    return True
