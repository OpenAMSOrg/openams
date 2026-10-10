// SPDX-License-Identifier: GPL-3.0-or-later
/* Wire values the host reads as numbers, exported as Klipper dictionary
 * enumerations. At connect the host (host/wire.py) rejects firmware whose
 * values differ from its own. Each entry name is the C enumerator suffix, so
 * an entry cannot name one constant and carry another. The km_ enumeration
 * names match no message parameter name or `_name` suffix; Klipper therefore
 * still parses and encodes every message field as an integer, and no message
 * format changes. */
#include "command.h"
#include "commands.h"
#include "board_startup.h"
#include "diagnostic_startup.h"
#include "pin_config.h"
#include "klipper_mmu/operation_core.h"

#define KM_ENUM(ENUM, PREFIX, NAME) \
    DECL_ENUMERATION(ENUM, #NAME, (int)(PREFIX##NAME))

KM_ENUM("km_op_state", KM_OP_STATE_, IDLE);
KM_ENUM("km_op_state", KM_OP_STATE_, LOADING);
KM_ENUM("km_op_state", KM_OP_STATE_, LOADED);
KM_ENUM("km_op_state", KM_OP_STATE_, UNLOADING);
KM_ENUM("km_op_state", KM_OP_STATE_, FOLLOWING);
KM_ENUM("km_op_state", KM_OP_STATE_, CALIBRATING);
KM_ENUM("km_op_state", KM_OP_STATE_, FAULT);
KM_ENUM("km_op_state", KM_OP_STATE_, PRELOADING);
KM_ENUM("km_op_state", KM_OP_STATE_, PRELOADED);
KM_ENUM("km_op_state", KM_OP_STATE_, EJECTING);

KM_ENUM("km_stage", KM_STAGE_, STOPPED);
KM_ENUM("km_stage", KM_STAGE_, LOAD_FEED_TO_HUB);
KM_ENUM("km_stage", KM_STAGE_, LOAD_FEED_TO_BUFFER);
KM_ENUM("km_stage", KM_STAGE_, FOLLOW);
KM_ENUM("km_stage", KM_STAGE_, UNLOAD_RETRACT_TO_HUB);
KM_ENUM("km_stage", KM_STAGE_, UNLOAD_REWIND_TO_SPOOL);
KM_ENUM("km_stage", KM_STAGE_, UNLOAD_STOP_SETTLE);
KM_ENUM("km_stage", KM_STAGE_, CALIBRATE_PROBE);
KM_ENUM("km_stage", KM_STAGE_, CALIBRATE_RETRACT);
KM_ENUM("km_stage", KM_STAGE_, BRAKING);
KM_ENUM("km_stage", KM_STAGE_, UNLOAD_RETRACT_TO_LOAD);
KM_ENUM("km_stage", KM_STAGE_, EJECT_TO_BAY);
KM_ENUM("km_stage", KM_STAGE_, LOAD_RETRY_RETRACT);
KM_ENUM("km_stage", KM_STAGE_, SELECT_HOME);
KM_ENUM("km_stage", KM_STAGE_, SELECT_MOVE);
KM_ENUM("km_stage", KM_STAGE_, SERVO_DWELL);

KM_ENUM("km_op_type", KM_OP_TYPE_, NONE);
KM_ENUM("km_op_type", KM_OP_TYPE_, LOAD);
KM_ENUM("km_op_type", KM_OP_TYPE_, UNLOAD);
KM_ENUM("km_op_type", KM_OP_TYPE_, FOLLOW);
KM_ENUM("km_op_type", KM_OP_TYPE_, CALIBRATE);
KM_ENUM("km_op_type", KM_OP_TYPE_, CANCEL);
KM_ENUM("km_op_type", KM_OP_TYPE_, PRELOAD);
KM_ENUM("km_op_type", KM_OP_TYPE_, EJECT);
KM_ENUM("km_op_type", KM_OP_TYPE_, HOME);

KM_ENUM("km_client", KM_CLIENT_, NONE);
KM_ENUM("km_client", KM_CLIENT_, HOST);
KM_ENUM("km_client", KM_CLIENT_, LOCAL_UI);
KM_ENUM("km_client", KM_CLIENT_, INTERNAL);

KM_ENUM("km_admission", KM_REQ_, ACCEPTED);
KM_ENUM("km_admission", KM_REQ_, ALREADY_IN_PROGRESS);
KM_ENUM("km_admission", KM_REQ_, BUSY);
KM_ENUM("km_admission", KM_REQ_, INVALID_LANE);
KM_ENUM("km_admission", KM_REQ_, NO_SPOOL);
KM_ENUM("km_admission", KM_REQ_, PATH_OCCUPIED);
KM_ENUM("km_admission", KM_REQ_, FAULTED);
KM_ENUM("km_admission", KM_REQ_, UNAVAILABLE);
KM_ENUM("km_admission", KM_REQ_, REJECTED_STALE);
KM_ENUM("km_admission", KM_REQ_, REJECTED_CONFLICT);
KM_ENUM("km_admission", KM_REQ_, ALREADY_COMPLETED);

KM_ENUM("km_fault", KM_FAULT_, NONE);
KM_ENUM("km_fault", KM_FAULT_, HOST_COMM_LOST);
KM_ENUM("km_fault", KM_FAULT_, DRIVER_OVERCURRENT);
KM_ENUM("km_fault", KM_FAULT_, DRIVER_FAULT);
KM_ENUM("km_fault", KM_FAULT_, NO_PROGRESS);
KM_ENUM("km_fault", KM_FAULT_, JAM);
KM_ENUM("km_fault", KM_FAULT_, BUFFER_SIGNAL);
KM_ENUM("km_fault", KM_FAULT_, FILAMENT_LOST);
KM_ENUM("km_fault", KM_FAULT_, STARVED);
KM_ENUM("km_fault", KM_FAULT_, STALLED);
KM_ENUM("km_fault", KM_FAULT_, EMERGENCY_STOP);
KM_ENUM("km_fault", KM_FAULT_, SENSOR_FAILURE);
KM_ENUM("km_fault", KM_FAULT_, CONTROL_TIMING);
KM_ENUM("km_fault", KM_FAULT_, SELECTOR);

KM_ENUM("km_event_type", KM_EVENT_, NONE);
KM_ENUM("km_event_type", KM_EVENT_, LOAD_COMPLETE);
KM_ENUM("km_event_type", KM_EVENT_, LOAD_CANCELLED);
KM_ENUM("km_event_type", KM_EVENT_, LOAD_FAILED);
KM_ENUM("km_event_type", KM_EVENT_, UNLOAD_COMPLETE);
KM_ENUM("km_event_type", KM_EVENT_, UNLOAD_CANCELLED);
KM_ENUM("km_event_type", KM_EVENT_, UNLOAD_FAILED);
KM_ENUM("km_event_type", KM_EVENT_, FOLLOW_FAULT);
KM_ENUM("km_event_type", KM_EVENT_, PTFE_CAL_COMPLETE);
KM_ENUM("km_event_type", KM_EVENT_, PTFE_CAL_CANCELLED);
KM_ENUM("km_event_type", KM_EVENT_, PTFE_CAL_FAILED);
KM_ENUM("km_event_type", KM_EVENT_, STOPPED);
KM_ENUM("km_event_type", KM_EVENT_, FAULT_ENTERED);
KM_ENUM("km_event_type", KM_EVENT_, PRELOAD_COMPLETE);
KM_ENUM("km_event_type", KM_EVENT_, PRELOAD_CANCELLED);
KM_ENUM("km_event_type", KM_EVENT_, PRELOAD_FAILED);
KM_ENUM("km_event_type", KM_EVENT_, EJECT_COMPLETE);
KM_ENUM("km_event_type", KM_EVENT_, EJECT_CANCELLED);
KM_ENUM("km_event_type", KM_EVENT_, EJECT_FAILED);
KM_ENUM("km_event_type", KM_EVENT_, HOME_COMPLETE);
KM_ENUM("km_event_type", KM_EVENT_, HOME_FAILED);

KM_ENUM("km_event_flag", KM_EVENT_FLAG_, OVERFLOW);
KM_ENUM("km_event_flag", KM_EVENT_FLAG_, STEP_ESTIMATE);

KM_ENUM("km_op_code", KM_WIRE_OP_CODE_, SUCCESS);
KM_ENUM("km_op_code", KM_WIRE_OP_CODE_, ERROR_UNSPECIFIED);
KM_ENUM("km_op_code", KM_WIRE_OP_CODE_, ERROR_BUSY);
KM_ENUM("km_op_code", KM_WIRE_OP_CODE_, SPOOL_ALREADY_IN_BAY);
KM_ENUM("km_op_code", KM_WIRE_OP_CODE_, NO_SPOOL_IN_BAY);
KM_ENUM("km_op_code", KM_WIRE_OP_CODE_, ERROR_KLIPPER_CALL);
KM_ENUM("km_op_code", KM_WIRE_OP_CODE_, CANCEL);
KM_ENUM("km_op_code", KM_WIRE_OP_CODE_, TIMEOUT);

KM_ENUM("km_command", KM_COMMAND_, START);
KM_ENUM("km_command", KM_COMMAND_, CANCEL);
KM_ENUM("km_command", KM_COMMAND_, HEARTBEAT);
KM_ENUM("km_command", KM_COMMAND_, STOP);
KM_ENUM("km_command", KM_COMMAND_, EVENT_PEEK);
KM_ENUM("km_command", KM_COMMAND_, EVENT_ACK);
KM_ENUM("km_command", KM_COMMAND_, TERMINAL);
KM_ENUM("km_command", KM_COMMAND_, SNAPSHOT);

KM_ENUM("km_command_result", KM_COMMAND_, OK);
KM_ENUM("km_command_result", KM_COMMAND_, UNBOUND);
KM_ENUM("km_command_result", KM_COMMAND_, INSTANCE_MISMATCH);
KM_ENUM("km_command_result", KM_COMMAND_, INVALID);
KM_ENUM("km_command_result", KM_COMMAND_, STALE);
KM_ENUM("km_command_result", KM_COMMAND_, EMPTY);
KM_ENUM("km_command_result", KM_COMMAND_, SHUTDOWN);
KM_ENUM("km_command_result", KM_COMMAND_, UNAVAILABLE);

KM_ENUM("km_binding", KM_BINDING_, UNBOUND);
KM_ENUM("km_binding", KM_BINDING_, ATTACHED);
KM_ENUM("km_binding", KM_BINDING_, SHUTDOWN);
KM_ENUM("km_binding", KM_BINDING_, CLOCK_UNAVAILABLE);

KM_ENUM("km_follower_state", KM_FOLLOWER_, DISARMED);
KM_ENUM("km_follower_state", KM_FOLLOWER_, IDLE);
KM_ENUM("km_follower_state", KM_FOLLOWER_, DRIVING);
KM_ENUM("km_follower_state", KM_FOLLOWER_, FAULT);

KM_ENUM("km_direction", KM_DIR_, REVERSE);
KM_ENUM("km_direction", KM_DIR_, FORWARD);

KM_ENUM("km_startup_state", KM_DIAGNOSTIC_, IDLE);
KM_ENUM("km_startup_state", KM_DIAGNOSTIC_, WAITING);
KM_ENUM("km_startup_state", KM_DIAGNOSTIC_, READY);
KM_ENUM("km_startup_state", KM_DIAGNOSTIC_, FAULTED);

KM_ENUM("km_result", KM_, OK);
KM_ENUM("km_result", KM_, ERR_INVALID_PARAM);
KM_ENUM("km_result", KM_, ERR_BUSY);
KM_ENUM("km_result", KM_, ERR_UNAVAILABLE);
KM_ENUM("km_result", KM_, ERR_FAULTED);
KM_ENUM("km_result", KM_, ERR_CONFLICT);
KM_ENUM("km_result", KM_, ERR_STALE);
KM_ENUM("km_result", KM_, ERR_TIMEOUT);
KM_ENUM("km_result", KM_, ERR_NOT_SUPPORTED);
KM_ENUM("km_result", KM_, ERR_QUEUE_FULL);
KM_ENUM("km_result", KM_, ERR_QUEUE_EMPTY);

KM_ENUM("km_startup_step", KM_STARTUP_STEP_, NONE);
KM_ENUM("km_startup_step", KM_STARTUP_STEP_, CLOCK);
KM_ENUM("km_startup_step", KM_STARTUP_STEP_, IDENTITY);
KM_ENUM("km_startup_step", KM_STARTUP_STEP_, CALIBRATION);
KM_ENUM("km_startup_step", KM_STARTUP_STEP_, DEADLINE);
KM_ENUM("km_startup_step", KM_STARTUP_STEP_, ALREADY_STARTED);
KM_ENUM("km_startup_step", KM_STARTUP_STEP_, PROFILE);
KM_ENUM("km_startup_step", KM_STARTUP_STEP_, PINS);
KM_ENUM("km_startup_step", KM_STARTUP_STEP_, RESOURCES);
KM_ENUM("km_startup_step", KM_STARTUP_STEP_, SAVED_CONFIG);
KM_ENUM("km_startup_step", KM_STARTUP_STEP_, RESERVE);
KM_ENUM("km_startup_step", KM_STARTUP_STEP_, SWITCHES);
KM_ENUM("km_startup_step", KM_STARTUP_STEP_, ADC);
KM_ENUM("km_startup_step", KM_STARTUP_STEP_, DRIVER_SETUP);
KM_ENUM("km_startup_step", KM_STARTUP_STEP_, REWIND_SETUP);
KM_ENUM("km_startup_step", KM_STARTUP_STEP_, DRIVER_CONFIRM);
KM_ENUM("km_startup_step", KM_STARTUP_STEP_, BIND);
KM_ENUM("km_startup_step", KM_STARTUP_STEP_, ATTACH);
KM_ENUM("km_startup_step", KM_STARTUP_STEP_, SELECTOR_SETUP);

KM_ENUM("km_pin_role", KM_PIN_, STEP);
KM_ENUM("km_pin_role", KM_PIN_, DIR);
KM_ENUM("km_pin_role", KM_PIN_, ENABLE);
KM_ENUM("km_pin_role", KM_PIN_, UART);
KM_ENUM("km_pin_role", KM_PIN_, BAY);
KM_ENUM("km_pin_role", KM_PIN_, HUB);
KM_ENUM("km_pin_role", KM_PIN_, TOOLHEAD);
KM_ENUM("km_pin_role", KM_PIN_, BUFFER_A);
KM_ENUM("km_pin_role", KM_PIN_, BUFFER_B);
KM_ENUM("km_pin_role", KM_PIN_, REWIND_FORWARD);
KM_ENUM("km_pin_role", KM_PIN_, REWIND_REVERSE);
KM_ENUM("km_pin_role", KM_PIN_, REWIND_ENABLE);
KM_ENUM("km_pin_role", KM_PIN_, ENCODER_A);
KM_ENUM("km_pin_role", KM_PIN_, ENCODER_B);
KM_ENUM("km_pin_role", KM_PIN_, CURRENT);
KM_ENUM("km_pin_role", KM_PIN_, LOAD);
KM_ENUM("km_pin_role", KM_PIN_, SELECTOR_STEP);
KM_ENUM("km_pin_role", KM_PIN_, SELECTOR_DIR);
KM_ENUM("km_pin_role", KM_PIN_, SELECTOR_ENABLE);
KM_ENUM("km_pin_role", KM_PIN_, SELECTOR_UART);
KM_ENUM("km_pin_role", KM_PIN_, SELECTOR_ENDSTOP);
KM_ENUM("km_pin_role", KM_PIN_, SERVO);

KM_ENUM("km_resource_diag", KM_RESOURCE_DIAG_, NONE);
KM_ENUM("km_resource_diag", KM_RESOURCE_DIAG_, BAD_ARGUMENT);
KM_ENUM("km_resource_diag", KM_RESOURCE_DIAG_, BAD_CLAIM);
KM_ENUM("km_resource_diag", KM_RESOURCE_DIAG_, DUPLICATE_RESOURCE);
KM_ENUM("km_resource_diag", KM_RESOURCE_DIAG_, TIMER_CONFIGURATION);
KM_ENUM("km_resource_diag", KM_RESOURCE_DIAG_, MISSING_TIMER_BASE);
KM_ENUM("km_resource_diag", KM_RESOURCE_DIAG_, MISSING_ADC_UNIT);
