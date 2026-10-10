#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The one per-device table tests/linux_mcu's whole-stack sim keys off of:
build.py's Kconfig fragment, device_scenarios.py's OpenAMS topology, and a
device's Happy Hare dossier (docs/devices/<name>.json) for the PRELOAD/
CALIBRATE/LOAD/UNLOAD phase-order and rest-position expectations this
harness traces (see tests/linux_mcu/README.md, "Proving a device"). A new
device -- such as the passive feeders with profiles in
tests/release_matrix/ -- needs exactly one Device row here plus a Kconfig
fragment in tests/linux_mcu/configs/; nothing else in this directory names
a device by hand.

Pins are board wiring, not device behaviour (AGENTS.md): every device row
below simulates the identical physical bench wiring sim/board/plant.c
already models for BoxTurtle (same STEP/DIR/ENABLE/UART/DiffA/DiffB pins),
so plant.c itself needs no per-device branching -- only which Kconfig
mechanism symbols are on (LOAD_SENSORS, POWERED_REWIND, virtual hub) and
which OpenAMS family/lane count describe the resulting composition differ.
A device whose sim needs genuinely different wiring (not just a different
composition on the same bench) would still add only a fragment plus a row
here; sim/board/plant.c's pin tables would need a matching per-device
#if only at that point, which is why this module -- not plant.c -- is the
one hook the brief asks new devices to extend.

Host device id/openams family strings must match host/__init__.py's
_OPENAMS_DEVICE_IDS exactly (checked against the firmware's own
KLIPPER_MMU_DEVICE_ID/KLIPPER_MMU_LANE_COUNT dictionary constants at
runtime) and mcu/Kconfig.devices' KLIPPER_MMU_DEVICE_ID table.
"""
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, '..', '..'))

# Happy Hare dossier sensor name -> the plant's ground-truth field that
# reads as 1 once the tip is at/past that sensor. Both nightowl.json and
# emu.json (tools/upstream_dossier.py's harness output) use the same names
# for the same per-lane switches, so one mapping serves every device row:
# mmu_exit_0 is HH's per-lane "exit" switch, which the concept-mapping table
# in each dossier's .md maps to has_load_sensors/PINS_LOAD (our LOAD
# switch); mmu_entry is the per-lane "entry"/prep switch, our BAY switch.
SENSOR_TO_FIELD = {
    'mmu_entry': 'bay',
    'mmu_exit_0': 'load',
    'mmu_exit': 'load',
    # HH's toolhead/extruder-side arrival reference (nightowl.json/emu.json
    # "Homing filament to extruder endstop") -- our own toolhead-arrival
    # ground truth field, which only reads 1 once the tip has passed the
    # plant's HUB+toolhead-distance mark (sim/board/plant.c
    # toolhead_threshold_steps()).
    'filament_compression': 'toolhead',
    # Same toolhead-arrival concept, HH's other spelling for it
    # (docs/devices/qidi_box.json/3ms.json/low_rider.json's own "extruder"
    # endstop) -- QIDI Box's real physical toolhead switch (arrival note,
    # docs/devices/qidi_box.md) trips the identical ground-truth field.
    'extruder': 'toolhead',
    # HH's shared gear-exit/HUB sensor (many dossiers' preload/load/unload,
    # e.g. docs/devices/qidi_box.json, boxturtle.json, mmx.json) -- our own
    # physical shared HUB switch ground truth (has_hub_switch=True rows).
    'unit0:mmu_shared_exit': 'hub',
}


class Device:
    def __init__(self, name, openams_family, lane_count, virtual_hub,
                 has_hub_switch, motion_config, dossier=None,
                 rest_decisions=(), diff_buffer=True, supports_calibrate=True):
        self.name = name
        # host/__init__.py _OPENAMS_DEVICE_IDS key; must also equal
        # mcu/Kconfig.devices' KLIPPER_MMU_DEVICE_NAME lowercased.
        self.openams_family = openams_family
        self.lane_count = lane_count
        # LOAD switch doubles as the HUB boundary; no physical HUB pin
        # (mcu/Kconfig.devices' EMU help text, mcu/Kconfig.pins'
        # "depends on !KLIPPER_MMU_DEVICE_EMU" on PINS_HUB).
        self.virtual_hub = virtual_hub
        self.has_hub_switch = has_hub_switch
        self.motion_config = motion_config
        # Every device row so far uses the DiffA/DiffB analog buffer (True),
        # so device_scenarios.py's write_printer_config() only omits
        # diff_calibration_file for the one device that does not (QIDI
        # Box's own tension-only switched buffer, KLIPPER_MMU_
        # BUFFER_SINGLE_SWITCH) -- passing that option with no
        # CONFIG_KLIPPER_MMU_DIFF_BUFFER built in is a host config_error
        # (klippy/extras/klipper_mmu/__init__.py's own calibration-delivery
        # gate), not a firmware fact this table would otherwise track.
        self.diff_buffer = diff_buffer
        # Step-estimated PTFE calibration (KLIPPER_MMU_CALIBRATE) has no
        # distance to measure without an encoder or FPS arrival
        # (src/operation_core.c km_core_request_ptfe_cal: "!encoder && (!
        # fps_arrival || ...)" refuses with KM_REQ_UNAVAILABLE) -- a plain
        # toolhead switch and no encoder, QIDI Box's own shape, has neither.
        # LOAD does not need it either: the calibrated-arrival-steps gate
        # at LOAD admission is itself "profile.fps_arrival &&
        # !calibrated_arrival_steps", so a toolhead-switch device (fps_
        # arrival off) loads by feeding to the real switch every time,
        # calibrated or not. device_scenarios.py's phase_one_boot() skips
        # the CALIBRATE macro call for a device with this False.
        self.supports_calibrate = supports_calibrate
        self._dossier_name = dossier
        self.dossier_path = (os.path.join(_REPO_ROOT, 'docs', 'devices', dossier)
                              if dossier else None)
        # Operations whose dossier rest-side already disagrees with this
        # firmware's own (device-independent) rest behaviour for a reason
        # already recorded as a decision in this device's dossier (see
        # docs/devices/<name>.json "decisions") -- check_rest() logs instead
        # of raising for these, per the brief: "Differences that are
        # recorded decisions in the dossier are expected; anything else is
        # a finding to report."
        self.rest_decisions = frozenset(rest_decisions)

    def sim_out(self):
        """out-sim-<device>-motion under build/linux-mcu/klipper, the sim
        image build.py's build_sim_only() produces for this device."""
        return 'out-sim-%s-motion' % self.name

    def dossier(self):
        if not self.dossier_path:
            return None
        with open(self.dossier_path) as f:
            return json.load(f)

    def expected_rest(self, op):
        """(sensor, forward) from the dossier's operations[op]['end'] block:
        the sensor HH ended that operation against, and whether the rest
        position is at/forward of it (offset >= 0) or behind it. None if
        the op has no sensor (SELECT, or an op this device's dossier omits).
        Distances themselves (offset) are HH's own synthetic numbers, not
        checked here -- see the brief: "phase order, which sensor ends each
        phase, and rest side and approximate distance, not absolute
        millimetres."""
        d = self.dossier()
        if not d:
            return None
        end = d['operations'][op]['end']
        sensor = end.get('sensor')
        if sensor is None:
            return None
        offset = end.get('offset')
        return sensor, (offset is None or offset >= 0)

    def phase_order(self, op):
        """The dossier's ordered (motor, homing, endstop) moves for one
        operation -- what a phase-order comparison is against. homing is
        HH's own sign (1 forward-homing, -1 reverse-homing, 0 a plain
        move)."""
        d = self.dossier()
        if not d:
            return []
        return [(m['motor'], m['homing'], m.get('endstop'))
                for m in d['operations'][op]['moves']]

    def check_rest(self, op, lane_truth):
        """Raise if lane_truth (one plant ground-truth lane dict) disagrees
        with this device's dossier about which side of the rest sensor the
        tip should be on after `op`. Silently passes for a sensor this
        table doesn't have a ground-truth field for (e.g. an arrival sensor
        this device's own dossier records as a gap, per AGENTS.md) or an op
        with no dossier expectation."""
        expected = self.expected_rest(op)
        if expected is None:
            return
        sensor, forward = expected
        field = SENSOR_TO_FIELD.get(sensor)
        if field is None:
            return
        want = 1 if forward else 0
        got = lane_truth.get(field)
        if got != want:
            if op in self.rest_decisions:
                print('%s %s: dossier rest-side differs from this firmware '
                     '(recorded decision, not a finding): wanted %s=%d, '
                     'ground truth %s' % (self.name, op, field, want, lane_truth))
                return
            raise Exception(
                '%s: dossier says rest is %s of %s (ground truth %s=%d), '
                'plant disagrees: %s'
                % (op, 'at/forward' if forward else 'behind', sensor,
                   field, want, lane_truth))


DEVICES = {
    'boxturtle': Device(
        name='boxturtle', openams_family='boxturtle', lane_count=4,
        virtual_hub=False, has_hub_switch=True,
        motion_config='configs/boxturtle-h723-usb.config'),
    'nightowl': Device(
        name='nightowl', openams_family='nightowl', lane_count=4,
        virtual_hub=False, has_hub_switch=True,
        motion_config='tests/linux_mcu/configs/nightowl-h723-sim-motion.config',
        dossier='nightowl.json'),
    'emu': Device(
        name='emu', openams_family='emu', lane_count=5,
        virtual_hub=True, has_hub_switch=False,
        motion_config='tests/linux_mcu/configs/emu-h723-sim-motion.config',
        dossier='emu.json',
        # docs/devices/emu.json's "preload_park" decision: Happy Hare parks
        # PRELOAD 1mm *behind* mmu_exit_0. src/operation_core.c's own
        # load_is_virtual_hub PRELOAD path does back off past LOAD once it
        # trips (KM_STAGE_LOAD_FEED_TO_HUB -> KM_STAGE_UNLOAD_RETRACT_TO_LOAD,
        # not a configurable distance -- it retracts until LOAD clears,
        # however far that is) so the virtual hub reads clear for other
        # lanes once PRELOAD finishes, matching Happy Hare's own park-behind
        # intent; it still rests at the switch edge rather than 1 mm behind
        # it, hence the recorded decision.
        rest_decisions=('preload',)),
    'kms': Device(
        # Same virtual-hub shape as EMU (mcu/Kconfig.devices' KMS help text:
        # no separate shared HUB sensor, the owning lane's own LOAD switch is
        # the boundary), but passive rewind and a kept gear TMC UART, like
        # NightOwl -- phase_one_boot, not selector_scenario. docs/devices/
        # kms.json's "buffer_type"/"arrival" decisions record that KMS's
        # real profile has no analog buffer and no wired toolhead switch;
        # this sim fragment still uses the bench's own DiffA/DiffB FPS
        # arrival (tests/linux_mcu/configs/kms-h723-sim-motion.config's own
        # comment), matching NightOwl's identical divergence, since our
        # firmware build requires either a toolhead switch or FPS arrival
        # (mcu/config.c km_config_profile) and neither dossier evidence nor
        # this one bench offers a third option.
        name='kms', openams_family='kms', lane_count=4,
        virtual_hub=True, has_hub_switch=False,
        motion_config='tests/linux_mcu/configs/kms-h723-sim-motion.config',
        dossier='kms.json'),
    'ercf_2_0': Device(
        # Shared-drive LINEAR selector machine (selector.h): one gear feeds
        # whichever of 8 gates the selector stepper is at, gripped by a
        # servo. has_hub_switch=True: the plant's shared "any lane past it"
        # HUB model stands in for the family's shared gate sensor (the
        # brief's own phrasing), same as every other physical-HUB device;
        # docs/devices/ercf_2_0.json's own dossier records no shared exit
        # switch at all (its hardware is per-gate entry/BAY only), which is
        # why selector_scenario in device_scenarios.py checks the plant's own
        # selector ground truth instead of a dossier rest-side.
        name='ercf_2_0', openams_family='ercf_2_0', lane_count=8,
        virtual_hub=False, has_hub_switch=True,
        motion_config='tests/linux_mcu/configs/ercf-2-0-h723-sim-motion.config',
        dossier='ercf_2_0.json'),
    'chameleon_3d': Device(
        # Shared-drive ROTARY selector machine (selector.h): one gear drives
        # a gate, the selector stepper releases by moving to that gate's
        # release gate, and each gate carries its own gear direction. Same
        # selector_scenario as every selector kind; kind and values come
        # from the image, not this row.
        name='chameleon_3d', openams_family='3d_chameleon', lane_count=4,
        virtual_hub=False, has_hub_switch=True,
        motion_config='tests/linux_mcu/configs/chameleon-3d-h723-sim-motion.config',
        dossier='3d_chameleon.json'),
    'mmx': Device(
        # Shared-drive SERVO selector machine (selector.h): no selector
        # stepper at all, one servo angle selects the gate. Same
        # selector_scenario.
        name='mmx', openams_family='mmx', lane_count=4,
        virtual_hub=False, has_hub_switch=True,
        motion_config='tests/linux_mcu/configs/mmx-h723-sim-motion.config',
        dossier='mmx.json'),
    'vivid': Device(
        # Shared-drive INDEXED selector machine (selector.h): one gear feeds
        # whichever of 4 gates the selector stepper's own index switch says
        # it is at -- no gate position table, no grip servo. Same
        # selector_scenario as every other kind; kind and values
        # (selector_gate_order, switch width, max travel) come from the
        # image, not this row. has_hub_switch=True: ViViD wires no separate
        # physical HUB pin either (mcu/Kconfig.devices' own
        # KLIPPER_MMU_VIRTUAL_HUB help text names only EMU/KMS), so this is
        # the same plant-side shared-HUB stand-in every other selector
        # device here already uses.
        #
        # openams_family below is host/__init__.py's own KLIPPER_MMU_DEVICE_
        # NAME-lowercased convention ("BTT ViViD" -> "btt_vivid"), matching
        # every other entry in this dict; host/__init__.py's
        # _OPENAMS_DEVICE_IDS carries 'btt_vivid' as device id 22.
        name='vivid', openams_family='btt_vivid', lane_count=4,
        virtual_hub=False, has_hub_switch=True,
        motion_config='tests/linux_mcu/configs/vivid-h723-sim-motion.config',
        dossier='vivid.json'),
    'claymore': Device(
        # Shared-drive INDEXED selector machine (selector.h), forward-only
        # (no gate_order, no centering nudge -- docs/selectors.md
        # "Forward-Only (No Gate Order Either)") plus a per-lane powered
        # rewind motor (mcu/Kconfig.control's own KLIPPER_MMU_POWERED_REWIND
        # default for this device). No Happy Hare dossier exists for
        # Claymore at all (mcu/Kconfig.devices' own help text); its only
        # written source is AFC (docs/devices/afc/claymore.md), which is
        # prose, not the docs/devices/<name>.json schema Device.dossier()
        # reads -- dossier stays None rather than fabricating one (AGENTS.md:
        # "Use real source evidence for device profiles").
        #
        # openams_family: host/__init__.py's _OPENAMS_DEVICE_IDS carries
        # 'claymore' as device id 23.
        name='claymore', openams_family='claymore', lane_count=4,
        virtual_hub=False, has_hub_switch=True,
        motion_config='tests/linux_mcu/configs/claymore-h723-sim-motion.config'),
    'ercf_1_1': Device(
        # Shared-drive LINEAR selector machine (selector.h), 9 gates, grip
        # servo, ERCF's own encoder default on (mcu/Kconfig.control). Same
        # selector_scenario as ercf_2_0; kind and values come from the
        # image, not this row.
        name='ercf_1_1', openams_family='ercf_1_1', lane_count=9,
        virtual_hub=False, has_hub_switch=True,
        motion_config='tests/linux_mcu/configs/ercf-1-1-h723-sim-motion.config',
        dossier='ercf_1_1.json'),
    'ercf_3_0': Device(
        # Shared-drive LINEAR selector machine (selector.h), 8 gates, grip
        # servo, encoder on. Same shape as ercf_2_0, one fewer gate-geometry
        # decision (no Project Options for 3.0 besides Jack Rabbit, its own
        # row below).
        name='ercf_3_0', openams_family='ercf_3_0', lane_count=8,
        virtual_hub=False, has_hub_switch=True,
        motion_config='tests/linux_mcu/configs/ercf-3-0-h723-sim-motion.config',
        dossier='ercf_3_0.json'),
    'jack_rabbit': Device(
        # ERCF 3.0's Jack Rabbit mod (KLIPPER_MMU_ERCF_JACK_RABBIT): the same
        # DEVICE_ERCF_3_0 firmware identity and lane count, but
        # KLIPPER_MMU_SELECTOR_LINEAR_HAS_SERVO cleared (mcu/Kconfig.devices),
        # so KLIPPER_MMU_SERVO resolves off and no servo pin is claimed --
        # "the filament stays gripped by the drive gear at all times"
        # (docs/selectors.md). openams_family is 'ercf_3_0', not a separate
        # id: host/__init__.py's _OPENAMS_DEVICE_IDS has no 'jack_rabbit' key
        # and the firmware's own KLIPPER_MMU_DEVICE_ID for this mod is still
        # ERCF 3.0's (10) -- the mod changes no device-identity constant.
        # No Happy Hare dossier exists for Jack Rabbit (it is an AFC/ERCF
        # community mod Happy Hare's own installer offers, not a separately
        # traced device); dossier stays None, same precedent as Claymore.
        name='jack_rabbit', openams_family='ercf_3_0', lane_count=8,
        virtual_hub=False, has_hub_switch=True,
        motion_config='tests/linux_mcu/configs/jack-rabbit-h723-sim-motion.config'),
    'tradrack': Device(
        # Shared-drive LINEAR selector machine (selector.h), 10 gates, grip
        # servo, no encoder (mcu/Kconfig.control's encoder default is
        # ERCF-family-only). Same selector_scenario as ercf_2_0/ercf_1_1.
        name='tradrack', openams_family='tradrack', lane_count=10,
        virtual_hub=False, has_hub_switch=True,
        motion_config='tests/linux_mcu/configs/tradrack-h723-sim-motion.config',
        dossier='tradrack.json'),
    'htlf': Device(
        # Shared-drive ROTARY selector machine (selector.h), 4 gates. Its own
        # Happy Hare dossier's selector_gate_directions is "0, 0, 0, 0"
        # (docs/devices/htlf.json:160) -- no gate ever reverses the gear,
        # unlike 3D Chameleon's mixed table -- so selector_scenario's ROTARY
        # pair-selection falls back to any two distinct gates for a device
        # with no reversed gate at all (device_scenarios.py), rather than
        # requiring one that this device genuinely does not have.
        name='htlf', openams_family='htlf', lane_count=4,
        virtual_hub=False, has_hub_switch=True,
        motion_config='tests/linux_mcu/configs/htlf-h723-sim-motion.config',
        dossier='htlf.json'),
    'mmx6': Device(
        # Shared-drive ROTARY selector machine (selector.h), 6 gates. Also an
        # all-normal selector_gate_directions ("0,0,0,0,0,0",
        # mcu/Kconfig.selector) -- the same no-reversed-gate fallback HTLF
        # needs.
        name='mmx6', openams_family='mmx6', lane_count=6,
        virtual_hub=False, has_hub_switch=True,
        motion_config='tests/linux_mcu/configs/mmx6-h723-sim-motion.config',
        dossier='mmx6.json'),
    'low_rider': Device(
        # Shared-drive SERVO selector machine (selector.h), 6 gates, its own
        # six-angle servo_gate_angles Kconfig default ("30,60,90,120,150,
        # 180", mcu/Kconfig.selector) -- unlike Pico MMU below, it ships
        # calibrated and needs no special-cased scenario handling.
        name='low_rider', openams_family='low_rider', lane_count=6,
        virtual_hub=False, has_hub_switch=True,
        motion_config='tests/linux_mcu/configs/low-rider-h723-sim-motion.config',
        dossier='low_rider.json'),
    'pico_mmu': Device(
        # Shared-drive SERVO selector machine (selector.h), 4 gates. Ships
        # with an empty KLIPPER_MMU_SELECTOR_SERVO_GATE_ANGLES_DEG (Happy
        # Hare calibrates them instead, mcu/Kconfig.selector's own default is
        # "" for this device) -- every gate starts uncalibrated and selecting
        # one is refused until KLIPPER_MMU_CALIBRATE_SELECTOR saves an angle,
        # the real runtime path tests/klipper_selector_runtime also
        # exercises. selector_scenario calibrates two gates itself, right
        # after boot, with its own angles (device_scenarios.py's
        # calibrate_gate_angles), before running the rest of the shared
        # scenario.
        name='pico_mmu', openams_family='pico_mmu', lane_count=4,
        virtual_hub=False, has_hub_switch=True,
        motion_config='tests/linux_mcu/configs/pico-mmu-h723-sim-motion.config',
        dossier='pico_mmu.json'),
    'qidi_box': Device(
        # Independent-feeder composition like NightOwl/EMU (phase_one_boot,
        # not selector_scenario): plain STEP/DIR gear (no gear TMC UART --
        # KLIPPER_MMU_GEAR_TMC_UART defaults off for this device,
        # mcu/Kconfig.control), a real shared HUB switch (unlike EMU's
        # virtual one), per-lane BAY entry switches, no LOAD switch at all
        # (docs/devices/qidi_box.md's own hardware table has no mmu_exit_
        # <n> pins), and a tension-only switched buffer
        # (KLIPPER_MMU_BUFFER_SINGLE_SWITCH) instead of NightOwl/EMU's
        # DiffA/DiffB pair. LOAD ends on a real physical toolhead switch
        # (KLIPPER_MMU_PINS_TOOLHEAD), not FPS arrival -- the dossier's own
        # stock wiring (extruder_switch_pin=!THR:PA1, on the printer's
        # toolhead MCU) is not reachable from this board, so this sim
        # fragment moves it onto a pin this board itself reads, exactly as
        # docs/devices/qidi_box.md's arrival note requires.
        #
        # Rest-position coverage: PRELOAD's dossier sensor
        # (unit0:mmu_shared_exit, the shared HUB) and LOAD's (extruder, the
        # toolhead-arrival concept) are both now in SENSOR_TO_FIELD above, so
        # phase_one_boot's check_rest() checks both sides for real. PRELOAD
        # now agrees with the dossier and needs no decision here any more:
        # a device with no per-lane LOAD switch and a real (not virtual) HUB
        # retracts until the HUB clears and parks behind it
        # (src/operation_core.c preload_rests_behind_hub()), matching Happy
        # Hare's own gate_parking_distance (-80mm) back-off -- the same rest
        # Angry Beaver and QuattroBox 1.0/1.1/2 would reach on this same
        # shared-HUB, no-LOAD shape (neither simulated here yet; PRELOAD is
        # unreachable for them today regardless -- their own images have no
        # BAY sensor). KMS is NOT in this group: its dossier parks behind its
        # own per-lane LOAD/exit switch, not the shared HUB, so it keeps the
        # LOAD-sensor settle-on-trip rest untouched. UNLOAD is still skipped:
        # the dossier's own UNLOAD trace failed ("Failed to reach extruder
        # entry sensor after moving -86.0mm", docs/devices/qidi_box.md
        # Decisions) because the traced `qidi` harness console profile's
        # synthetic filament_layout sits short of its own bowden-load
        # travel, not a firmware limitation -- comparing our firmware's
        # real, successful UNLOAD rest side against that failed trace would
        # not be a meaningful check. EJECT is not exercised by
        # phase_one_boot at all (PRELOAD/CALIBRATE/LOAD/extrude/UNLOAD
        # only), so it needs no decision here either.
        name='qidi_box', openams_family='qidi_box', lane_count=4,
        virtual_hub=False, has_hub_switch=True,
        motion_config='tests/linux_mcu/configs/qidi-box-h723-sim-motion.config',
        dossier='qidi_box.json', rest_decisions=('unload',),
        diff_buffer=False, supports_calibrate=False),
    'angry_beaver': Device(
        # Independent-feeder composition like QIDI Box (phase_one_boot, not
        # selector_scenario): plain gear with a TMC UART chip (unlike QIDI
        # Box), no per-lane BAY or LOAD switch at all
        # (KLIPPER_MMU_BAY_SENSORS defaults off for this device,
        # mcu/Kconfig.control -- its dossier renders no per-gate entry/exit
        # sensor), a real shared HUB switch (docs/devices/angry_beaver.json
        # "hub_sensor" decision: one mmu_shared_exit_switch_pin serves all
        # four gates), a real local toolhead-arrival switch (this firmware's
        # own default without FPS arrival, standing in for the dossier's
        # own open-loop, unsensed toolhead segment), and a tension-only
        # buffer switch standing in for hardware the dossier's own
        # "buffer_type" decision records as entirely absent.
        #
        # No BAY sensor means PRELOAD is unavailable (km_core_request_preload
        # requires one): device_scenarios.py reads
        # CONFIG_KLIPPER_MMU_BAY_SENSORS from the built image and starts this
        # device straight at LOAD, from the plant's "empty" (position 0)
        # state -- a filament_always_gripped feeder's lane starts with
        # filament at the spool, not at a BAY switch it has none of.
        # CALIBRATE is also unavailable (no encoder, no FPS arrival, same
        # reasoning as QIDI Box), so supports_calibrate is False here too.
        #
        # Rest-position coverage: PRELOAD has no dossier expectation to check
        # (it never runs). LOAD's dossier sensor (unit0:mmu_shared_exit, the
        # shared HUB) and UNLOAD's (the same) are both in SENSOR_TO_FIELD, so
        # check_rest checks the 'hub' field for both -- this firmware's own
        # LOAD/UNLOAD terminal events happen to agree (LOAD's real toolhead
        # switch sits well past the HUB, so hub reads 1 there too; UNLOAD
        # retracts past the HUB same as every other no-LOAD-switch,
        # real-HUB device), so no rest_decisions entry is needed.
        name='angry_beaver', openams_family='angry_beaver', lane_count=4,
        virtual_hub=False, has_hub_switch=True,
        motion_config='tests/linux_mcu/configs/angry-beaver-h723-sim-motion.config',
        dossier='angry_beaver.json', diff_buffer=False, supports_calibrate=False),
    '3ms': Device(
        # Independent-feeder composition, Angry Beaver's shape minus even
        # the shared HUB sensor: no per-lane BAY or LOAD switch
        # (KLIPPER_MMU_BAY_SENSORS defaults off, same reasoning as Angry
        # Beaver -- PRELOAD unavailable, device_scenarios.py starts lane 0
        # at "empty" and skips straight to LOAD), and no shared exit sensor
        # in its traced Happy Hare profile either (no MMU_HAS_SENSOR_
        # SHARED_EXIT symbol, no mmu_shared_exit_switch_pin rendered):
        # PRELOAD's and LOAD's only homing move in the dossier ends on the
        # toolhead/extruder switch instead
        # (docs/devices/3ms.json operations.load.end.sensor == "extruder"),
        # which is why has_hub_switch is False here, unlike Angry Beaver/
        # QuattroBox. This firmware's own device_profile still always
        # carries has_hub_sensor=true (mcu/config.c, no per-device way to
        # turn it off) and cannot use the virtual-hub path either (that
        # needs a per-lane LOAD switch, which 3MS has none of), so this
        # sim's own bench wiring still claims a real physical HUB pin for
        # the firmware's own state machine to clear en route to the
        # toolhead switch -- a board-wiring/firmware necessity, not a claim
        # that real 3MS hardware carries that switch. No buffer hardware
        # either (dossier "buffer_type": no [mmu_buffer] section): same
        # tension-only stand-in as Angry Beaver/QIDI Box. No encoder or FPS
        # arrival, so CALIBRATE is unavailable (supports_calibrate=False).
        #
        # Rest-position coverage: LOAD's and UNLOAD's dossier sensor is
        # "extruder" (SENSOR_TO_FIELD maps it to 'toolhead'), which is
        # exactly this firmware's own real LOAD/UNLOAD terminal event for a
        # device with a local toolhead switch and no FPS arrival -- no
        # rest_decisions entry needed.
        name='3ms', openams_family='3ms', lane_count=4,
        virtual_hub=False, has_hub_switch=False,
        motion_config='tests/linux_mcu/configs/3ms-h723-sim-motion.config',
        dossier='3ms.json', diff_buffer=False, supports_calibrate=False),
    'quattrobox_2': Device(
        # Independent-feeder composition, identical shape to Angry Beaver
        # (no BAY, no LOAD, real shared HUB, no buffer, toolhead-switch
        # arrival): docs/devices/quattrobox_1_0.json, quattrobox_1_1.json
        # and quattrobox_2_0.json all trace to the same
        # operations.{preload,load,unload,eject}.end blocks, the same
        # source.layout table and the same steps_per_mm
        # (140.77265924673924) in the pinned Happy Hare harness -- one
        # whole-stack sim device (QuattroBox 2) covers what all three would
        # separately prove, matching mcu/Kconfig.devices' own catalogue text
        # ("no version-specific difference is implemented"). No BAY sensor
        # means PRELOAD is unavailable and CALIBRATE has neither an encoder
        # nor FPS arrival to use, same reasoning as Angry Beaver/3MS.
        #
        # Rest-position coverage: same as Angry Beaver (LOAD/UNLOAD's
        # dossier sensor is the shared HUB, and this firmware's own
        # terminal events agree in ground truth), so no rest_decisions
        # entry is needed.
        name='quattrobox_2', openams_family='quattrobox_2', lane_count=4,
        virtual_hub=False, has_hub_switch=True,
        motion_config='tests/linux_mcu/configs/quattrobox-2-h723-sim-motion.config',
        dossier='quattrobox_2_0.json', diff_buffer=False, supports_calibrate=False),
}

# Devices device_scenarios.py proves against their dossier, in addition to
# the existing BoxTurtle-only preload_check.py/load_check.py coverage. Every
# selector device (ercf_2_0, chameleon_3d, mmx, vivid, claymore, and the
# ercf_1_1/ercf_3_0/jack_rabbit/tradrack/htlf/mmx6/low_rider/pico_mmu rows
# above) runs selector_scenario (one function for every kind) rather than
# the single-lane phase_one_boot every other entry here uses; Claymore and
# Jack Rabbit have no dossier to prove against (see their own rows above),
# but still run selector_scenario like the rest of their kind.
DOSSIER_DEVICES = ('nightowl', 'emu', 'kms', 'ercf_2_0', 'chameleon_3d', 'mmx',
                   'vivid', 'claymore', 'qidi_box', 'angry_beaver', '3ms',
                   'quattrobox_2', 'ercf_1_1', 'ercf_3_0', 'jack_rabbit',
                   'tradrack', 'htlf', 'mmx6', 'low_rider', 'pico_mmu')
