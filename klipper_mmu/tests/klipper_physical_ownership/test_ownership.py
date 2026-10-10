# SPDX-License-Identifier: GPL-3.0-or-later
import unittest

from host.openams_sensor_projection import SensorPresence, project_sensor_presence
from host.physical_ownership import PathOwnership, reconcile_path_ownership
from host.protocol import SensorSample, SensorSnapshot, Snapshot


INSTANCE = 0x123456789


def status(**overrides):
    values = dict(path_occupied=True, lane_active=True, terminal_pending=False,
                  position_uncertain=False, pending_fault_event=False,
                  owner_lane=2, occupant_lane=2, system_fault=0,
                  motion_fault=0, acquired_ms=100)
    values.update(overrides)
    return Snapshot(INSTANCE, 1, values)


def sensors(**overrides):
    values = dict(instance=INSTANCE, query=1, observed_ms=100,
                  bay_present=(True, False, True, False),
                  load_present=(None, None, None, None),
                  shared_hub_present=True, toolhead_present=False)
    values.update(overrides)
    return SensorPresence(**values)


class OwnershipTests(unittest.TestCase):
    def test_active_host_load_is_local_until_hub_arrival(self):
        active = dict(path_occupied=False, lane_active=True,
                      terminal_pending=True, owner_lane=2,
                      occupant_lane=255, client=1, session=7, request=11,
                      lane=2, operation=1, state=1, stage=1,
                      client_sessions=(0, 7, 0, 0),
                      client_requests=(0, 11, 0, 0))
        staged = sensors(shared_hub_present=False,
                         load_present=(False, False, True, False))
        self.assertEqual(reconcile_path_ownership(status(**active), staged),
                         PathOwnership('feeding', 2))
        self.assertEqual(reconcile_path_ownership(  # PTFE calibration
            status(**dict(active, operation=4, state=5)), staged),
            PathOwnership('feeding', 2))
        arrived = dict(active, path_occupied=True, occupant_lane=2)
        self.assertEqual(reconcile_path_ownership(
            status(**arrived), sensors(load_present=staged.load_present)),
            PathOwnership('known', 2))
        for change in (dict(client=2), dict(lane=1),
                       dict(client_sessions=(0, 6, 0, 0)),
                       dict(client_requests=(0, 10, 0, 0)),
                       dict(operation=2), dict(stage=2),
                       dict(system_fault=1)):
            with self.subTest(change=change):
                self.assertEqual(reconcile_path_ownership(
                    status(**dict(active, **change)), staged),
                    PathOwnership('unknown'))
        self.assertEqual(reconcile_path_ownership(
            status(**active), sensors(shared_hub_present=False,
                                      load_present=(False,) * 4)),
            PathOwnership('unknown'))
        # A LOAD retry backs out of HUB and feeds again from this lane.
        for retry in (dict(stage=12), dict(stage=12, path_occupied=True, occupant_lane=2),
                      dict(path_occupied=True, occupant_lane=2)):
            with self.subTest(retry=retry):
                self.assertEqual(reconcile_path_ownership(
                    status(**dict(active, **retry)), staged), PathOwnership('feeding', 2))
        self.assertEqual(reconcile_path_ownership(status(**dict(
            active, stage=12, path_occupied=True, occupant_lane=1)), staged),
            PathOwnership('unknown'))

    def test_selector_positioning_stages_stage_their_lane_before_any_sensor(self):
        # ercf_2_0's own dossier records no per-lane LOAD switch at all (only
        # a per-gate BAY entry and the family's shared HUB/gear sensor): a
        # shared-drive selector homes, moves and grips (selector.h's
        # SELECT_HOME/SELECT_MOVE/SERVO_DWELL) before any lane reaches the
        # shared path, regardless of which operation goes on to run.
        no_load_switch = sensors(shared_hub_present=False,
                                 load_present=(None, None, None, None))
        for select_stage in (13, 14, 15):  # SELECT_HOME, SELECT_MOVE, SERVO_DWELL
            for op, state in ((1, 1), (4, 5)):  # LOAD, CALIBRATE
                positioning = dict(path_occupied=False, lane_active=True,
                                   terminal_pending=True, owner_lane=2,
                                   occupant_lane=255, client=1, session=7,
                                   request=11, lane=2, operation=op, state=state,
                                   stage=select_stage,
                                   client_sessions=(0, 7, 0, 0),
                                   client_requests=(0, 11, 0, 0))
                with self.subTest(select_stage=select_stage, op=op):
                    self.assertEqual(reconcile_path_ownership(
                        status(**positioning), no_load_switch),
                        PathOwnership('staging', 2))
        # A stage outside the selector's own pre-motion set is unaffected.
        other_stage = dict(path_occupied=False, lane_active=True,
                           terminal_pending=True, owner_lane=2,
                           occupant_lane=255, client=1, session=7, request=11,
                           lane=2, operation=4, state=5, stage=7)  # CALIBRATE_PROBE
        self.assertEqual(reconcile_path_ownership(
            status(**other_stage), no_load_switch), PathOwnership('unknown'))

    def test_feeding_and_retracting_tolerate_an_unconfigured_load_switch(self):
        # Some shared-drive selector families (ercf_2_0's dossier again) have
        # no per-lane LOAD switch; an unconfigured (None) reading must not
        # block the classification a configured switch would otherwise
        # confirm, but a real switch reporting the lane empty still must.
        feeding = dict(path_occupied=False, lane_active=True,
                       terminal_pending=True, owner_lane=2, occupant_lane=255,
                       client=1, session=7, request=11, lane=2, operation=4,
                       state=5, stage=1,  # CALIBRATE, LOAD_FEED_TO_HUB
                       client_sessions=(0, 7, 0, 0), client_requests=(0, 11, 0, 0))
        no_load_switch = sensors(shared_hub_present=False,
                                 load_present=(None, None, None, None))
        self.assertEqual(reconcile_path_ownership(status(**feeding), no_load_switch),
                         PathOwnership('feeding', 2))
        empty_load_switch = sensors(shared_hub_present=False,
                                    load_present=(None, None, False, None))
        self.assertEqual(reconcile_path_ownership(status(**feeding), empty_load_switch),
                         PathOwnership('unknown'))
        retracting = dict(feeding, stage=8, path_occupied=True, occupant_lane=2)
        self.assertEqual(reconcile_path_ownership(status(**retracting), no_load_switch),
                         PathOwnership('retracting', 2))
        self.assertEqual(reconcile_path_ownership(status(**retracting), empty_load_switch),
                         PathOwnership('unknown'))

    def test_active_unload_retains_lane_after_hub_clears(self):
        active = dict(path_occupied=True, lane_active=True,
                      terminal_pending=True, owner_lane=2,
                      occupant_lane=2, client=1, session=7, request=12,
                      lane=2, operation=2, state=3, stage=5,
                      client_sessions=(0, 7, 0, 0),
                      client_requests=(0, 12, 0, 0))
        clear_hub = sensors(shared_hub_present=False,
                            load_present=(False, False, True, False))
        self.assertEqual(reconcile_path_ownership(status(**active), clear_hub),
                         PathOwnership('retracting', 2))
        self.assertEqual(reconcile_path_ownership(  # PTFE calibration braking
            status(**dict(active, operation=4, state=5, stage=9)), clear_hub),
            PathOwnership('retracting', 2))
        for change in (dict(stage=3), dict(operation=1),
                       dict(owner_lane=1), dict(terminal_pending=False),
                       dict(system_fault=1)):
            with self.subTest(change=change):
                self.assertEqual(reconcile_path_ownership(
                    status(**dict(active, **change)), clear_hub),
                    PathOwnership('unknown'))
        self.assertEqual(reconcile_path_ownership(
            status(**active), sensors(shared_hub_present=False,
                                      load_present=(False,) * 4)),
            PathOwnership('unknown'))

    def test_unload_retract_tolerates_bay_clearing_with_no_load_switch(self):
        # QIDI Box's dossier (docs/devices/qidi_box.md): no per-lane LOAD
        # switch at all (every reading None) and a real HUB. Its one
        # continuous UNLOAD retract clears BAY on the way past HUB and
        # toolhead, before firmware's own terminal settles -- unlike a
        # device with a real LOAD switch, where BAY staying occupied through
        # the retract is the only physical evidence of continued ownership.
        # A retained 'unknown' verdict for this expected mid-retract shape
        # left OpenAMS's UNLOAD waiter with no path to ever settle (the
        # firmware side completes and reports IDLE cleanly; only the host's
        # own reconciliation got stuck), hanging OAMSM_UNLOAD_FILAMENT.
        active = dict(path_occupied=True, lane_active=True,
                      terminal_pending=True, owner_lane=2,
                      occupant_lane=2, client=1, session=7, request=12,
                      lane=2, operation=2, state=3, stage=5,
                      client_sessions=(0, 7, 0, 0),
                      client_requests=(0, 12, 0, 0))
        bay_cleared_no_load_switch = sensors(
            shared_hub_present=False, bay_present=(False, False, False, False),
            load_present=(None, None, None, None))
        self.assertEqual(reconcile_path_ownership(
            status(**active), bay_cleared_no_load_switch),
            PathOwnership('retracting', 2))
        self.assertEqual(reconcile_path_ownership(  # PTFE calibration braking
            status(**dict(active, operation=4, state=5, stage=9)),
            bay_cleared_no_load_switch),
            PathOwnership('retracting', 2))
        # A real LOAD switch elsewhere on the device (even unrelated to this
        # lane) still proves nothing either way on its own -- a None mixed
        # among real readings must not grant this, exactly like the 'local'
        # rest above and the feeding branch's own unconfigured-switch case.
        bay_cleared_mixed_load_switch = sensors(
            shared_hub_present=False, bay_present=(False, False, False, False),
            load_present=(None, None, False, None))
        self.assertEqual(reconcile_path_ownership(
            status(**active), bay_cleared_mixed_load_switch),
            PathOwnership('unknown'))
        # A device with a real per-lane LOAD switch (BoxTurtle-shaped) still
        # requires BAY to stay occupied through the retract; BAY clearing
        # there remains 'unknown', unchanged from before this fix.
        bay_cleared_real_load_switch = sensors(
            shared_hub_present=False, bay_present=(False, False, False, False),
            load_present=(False, False, True, False))
        self.assertEqual(reconcile_path_ownership(
            status(**active), bay_cleared_real_load_switch),
            PathOwnership('unknown'))

    def test_no_bay_switch_reaches_feeding_empty_and_known(self):
        # Angry Beaver / 3MS / QuattroBox 1.0/1.1/2 have no per-lane BAY
        # switch at all (CONFIG_KLIPPER_MMU_BAY_SENSORS unset): every
        # bay_present reading is None, tolerated exactly like an
        # unconfigured LOAD reading, never treated as a real switch
        # reporting the lane empty.
        no_bay_switch = sensors(bay_present=(None, None, None, None),
                                load_present=(None, None, None, None))
        feeding = dict(path_occupied=False, lane_active=True,
                       terminal_pending=True, owner_lane=2, occupant_lane=255,
                       client=1, session=7, request=11, lane=2, operation=1,
                       state=1, stage=1,  # LOAD, LOAD_FEED_TO_HUB
                       client_sessions=(0, 7, 0, 0), client_requests=(0, 11, 0, 0))
        no_bay_no_hub = sensors(bay_present=(None, None, None, None),
                               load_present=(None, None, None, None),
                               shared_hub_present=False)
        self.assertEqual(reconcile_path_ownership(status(**feeding), no_bay_no_hub),
                         PathOwnership('feeding', 2))
        self.assertEqual(reconcile_path_ownership(
            status(path_occupied=False, lane_active=False), no_bay_no_hub),
            PathOwnership('empty'))
        self.assertEqual(reconcile_path_ownership(status(), no_bay_switch),
                         PathOwnership('known', 2))

    def test_in_flight_preload_and_eject_stage_their_lane(self):
        # A HOST PRELOAD or EJECT moves filament inside its lane with the
        # shared path clear: the lane is taken ('staging'), not 'unknown'
        # (which made OpenAMS drop the session mid-operation).
        moving = dict(path_occupied=False, lane_active=True,
                      terminal_pending=True, owner_lane=2, occupant_lane=0,
                      client=1, session=7, request=12, lane=2,
                      operation=6, state=7, stage=1,
                      client_sessions=(0, 7, 0, 0),
                      client_requests=(0, 12, 0, 0))
        clear = sensors(shared_hub_present=False,
                        load_present=(False, False, False, False))
        self.assertEqual(reconcile_path_ownership(status(**moving), clear),
                         PathOwnership('staging', 2))
        self.assertEqual(reconcile_path_ownership(  # EJECT, BAY already clear
            status(**dict(moving, operation=7, state=9)),
            sensors(shared_hub_present=False, bay_present=(True, False, False, False),
                    load_present=(False, False, False, False))),
            PathOwnership('staging', 2))
        self.assertEqual(reconcile_path_ownership(  # after the LOAD switch trips
            status(**dict(moving, path_occupied=True, occupant_lane=2)), clear),
            PathOwnership('staging', 2))
        # UNPRELOAD: UNLOADING/UNLOAD, retracting to LOAD, then braking with
        # LOAD clear; a full unload's braking with LOAD occupied is unchanged.
        unpreload = dict(moving, operation=2, state=3, stage=10,
                         path_occupied=True, occupant_lane=2)
        loaded = sensors(shared_hub_present=False,
                         load_present=(False, False, True, False))
        self.assertEqual(reconcile_path_ownership(status(**unpreload), loaded),
                         PathOwnership('staging', 2))
        self.assertEqual(reconcile_path_ownership(
            status(**dict(unpreload, stage=9)), clear),
            PathOwnership('staging', 2))
        self.assertEqual(reconcile_path_ownership(
            status(**dict(unpreload, stage=9)), loaded),
            PathOwnership('retracting', 2))
        self.assertNotEqual(reconcile_path_ownership(  # full unload to the hub
            status(**dict(unpreload, stage=4)), loaded).kind, 'staging')
        for change in (dict(operation=1, state=1), dict(terminal_pending=False),
                       dict(path_occupied=True, occupant_lane=0),
                       dict(system_fault=1)):
            with self.subTest(change=change):
                self.assertNotEqual(reconcile_path_ownership(
                    status(**dict(moving, **change)), clear).kind, 'staging')
        self.assertEqual(reconcile_path_ownership(  # shared path not clear
            status(**moving), sensors(shared_hub_present=True,
                                      load_present=(False, False, False, False))),
            PathOwnership('unknown'))

    def test_known_lane_requires_matching_status_and_physical_path(self):
        self.assertEqual(reconcile_path_ownership(status(), sensors()),
                         PathOwnership('known', 2))
        self.assertEqual(reconcile_path_ownership(status(lane_active=False), sensors()),
                         PathOwnership('known', 2))
        self.assertEqual(reconcile_path_ownership(status(), sensors(
            shared_hub_present=False, toolhead_present=True)),
            PathOwnership('known', 2))

    def test_unknown_source_and_contradictions_remain_unknown(self):
        cases = (
            (status(occupant_lane=255), sensors()),
            (status(owner_lane=1), sensors()),
            (status(), sensors(bay_present=(True, False, False, False))),
            (status(), sensors(shared_hub_present=False)),
            (status(path_occupied=False), sensors()),
            (status(path_occupied=False, lane_active=False), sensors()),
            (status(terminal_pending=True), sensors()),
            (status(position_uncertain=True), sensors()),
            (status(system_fault=1), sensors()),
        )
        for observed_status, observed_sensors in cases:
            with self.subTest(observed_status=observed_status,
                              observed_sensors=observed_sensors):
                self.assertEqual(reconcile_path_ownership(
                    observed_status, observed_sensors), PathOwnership('unknown'))

    def test_empty_requires_clear_logical_and_shared_physical_path(self):
        self.assertEqual(reconcile_path_ownership(status(
            path_occupied=False, lane_active=False), sensors(
                shared_hub_present=False)), PathOwnership('empty'))
        self.assertEqual(reconcile_path_ownership(status(
            path_occupied=False, lane_active=False), sensors(
                shared_hub_present=False,
                load_present=(False, True, False, False))),
            PathOwnership('unknown'))
        self.assertEqual(reconcile_path_ownership(status(
            path_occupied=True, lane_active=False), sensors(
                shared_hub_present=False)), PathOwnership('unknown'))

    def test_unconfigured_toolhead_relies_on_hub(self):
        idle = status(path_occupied=False, lane_active=False)
        self.assertEqual(reconcile_path_ownership(idle, sensors(
            shared_hub_present=False, toolhead_present=None)), PathOwnership('empty'))
        self.assertEqual(reconcile_path_ownership(idle, sensors(
            shared_hub_present=True, toolhead_present=None)), PathOwnership('unknown'))
        self.assertEqual(reconcile_path_ownership(status(), sensors(
            toolhead_present=None)), PathOwnership('known', 2))
        with self.assertRaises(ValueError):
            reconcile_path_ownership(idle, sensors(toolhead_present=0))

    def test_rejects_malformed_or_mismatched_observations(self):
        with self.assertRaises(ValueError):
            reconcile_path_ownership(status(), sensors(instance=INSTANCE + 1))
        with self.assertRaises(ValueError):
            reconcile_path_ownership(status(owner_lane=True), sensors())
        with self.assertRaises(ValueError):
            reconcile_path_ownership(status(), sensors(bay_present=(True, 1)))

    def test_distinct_retained_captures_cannot_establish_ownership(self):
        self.assertEqual(reconcile_path_ownership(status(), sensors(query=2)),
                         PathOwnership('unknown'))
        self.assertEqual(reconcile_path_ownership(status(acquired_ms=99), sensors()),
                         PathOwnership('unknown'))
        self.assertEqual(reconcile_path_ownership(status(
            path_occupied=False, lane_active=False), sensors(
                query=2, shared_hub_present=False)), PathOwnership('unknown'))
        with self.assertRaises(ValueError):
            reconcile_path_ownership(Snapshot(INSTANCE, 0, status().values), sensors())

    def test_local_preload_reserves_only_proven_load_lane(self):
        local = status(state=8, stage=0, operation=0, lane_active=False)
        presence = sensors(shared_hub_present=False,
                           load_present=(False, False, True, False))
        self.assertEqual(reconcile_path_ownership(local, presence),
                         PathOwnership('local', 2))
        for changed in (dict(state=2), dict(stage=1), dict(operation=6),
                        dict(lane_active=True)):
            self.assertEqual(reconcile_path_ownership(status(**{
                **dict(state=8, stage=0, operation=0, lane_active=False),
                **changed}),
                presence), PathOwnership('unknown'))
        for loads in ((False, False, None, False),
                      (False, True, True, False),
                      (False, None, True, False)):
            self.assertEqual(reconcile_path_ownership(
                local, sensors(shared_hub_present=False, load_present=loads)),
                PathOwnership('unknown'))

    def test_local_preload_without_a_load_switch_relies_on_hub_alone(self):
        # QIDI Box, Angry Beaver and QuattroBox 1.0/1.1/2 have no per-lane
        # LOAD switch at all (firmware's preload_rests_behind_hub()): every
        # load reading is None, and the settled PRELOAD rest is proven only
        # by the shared HUB having cleared plus the occupant's own BAY.
        local = status(state=8, stage=0, operation=0, lane_active=False)
        no_load_switch = sensors(shared_hub_present=False)  # load_present all None
        self.assertEqual(reconcile_path_ownership(local, no_load_switch),
                         PathOwnership('local', 2))
        for changed in (dict(state=2), dict(stage=1), dict(operation=6),
                        dict(lane_active=True)):
            self.assertEqual(reconcile_path_ownership(status(**{
                **dict(state=8, stage=0, operation=0, lane_active=False),
                **changed}),
                no_load_switch), PathOwnership('unknown'))
        self.assertEqual(reconcile_path_ownership(
            local, sensors(shared_hub_present=False,
                           bay_present=(True, False, False, False))),
            PathOwnership('unknown'))
        self.assertEqual(reconcile_path_ownership(local, sensors()),  # HUB still present
                         PathOwnership('known', 2))

    def test_preloaded_shared_path_contradicts_configured_load(self):
        preloaded = status(state=8, stage=0, operation=0, lane_active=False)
        self.assertEqual(reconcile_path_ownership(preloaded, sensors()),
                         PathOwnership('known', 2))
        self.assertEqual(reconcile_path_ownership(preloaded, sensors(
            load_present=(False, False, True, False))),
            PathOwnership('unknown'))

    def test_stale_or_invalid_sensor_never_becomes_known_absence(self):
        good = SensorSample(True, False, True, True, 100)
        stale = SensorSample(True, False, True, False, 100)
        invalid = SensorSample(True, False, False, False, 100)
        for sample in (stale, invalid):
            captured = SensorSnapshot(INSTANCE, 2, 4, 300,
                                      (good, good, good, good), sample, good)
            self.assertIsNone(project_sensor_presence(captured, INSTANCE, 4))

    def test_configured_unreliable_load_cannot_prove_empty(self):
        clear = SensorSample(True, False, True, True, 100)
        absent = SensorSample(False, False, False, False, 0)
        base = SensorSnapshot(INSTANCE, 1, 4, 100,
                              (clear,) * 4, clear, clear)
        self.assertEqual(reconcile_path_ownership(
            status(path_occupied=False, lane_active=False),
            project_sensor_presence(base, INSTANCE, 4)),
            PathOwnership('empty'))
        for load in (SensorSample(True, False, False, True, 100),
                     SensorSample(True, False, True, False, 99)):
            with self.subTest(load=load):
                captured = SensorSnapshot(INSTANCE, 1, 4, 100,
                                          (clear,) * 4, clear, clear,
                                          (absent, load, absent, absent))
                self.assertIsNone(project_sensor_presence(captured, INSTANCE, 4))

    def test_virtual_hub_unload_flips_load_polarity_past_hub(self):
        """A virtual-hub device's own LOAD switch IS the shared boundary: it
        already reads empty for every UNLOAD/calibration retract stage this
        branch covers, not just the ones past HUB. `not shared_present`
        (shared_present is any() of every LOAD switch for a virtual hub)
        already forces this lane's own reading false to reach here at all,
        so a real separate-hub device's still-made polarity can never also
        satisfy a virtual hub's check, at any of these stages."""
        active = dict(path_occupied=True, lane_active=True,
                      terminal_pending=True, owner_lane=2,
                      occupant_lane=2, client=1, session=7, request=12,
                      lane=2, operation=2, state=3, stage=5,
                      client_sessions=(0, 7, 0, 0),
                      client_requests=(0, 12, 0, 0))
        past_hub = sensors(shared_hub_present=False,
                           load_present=(False, False, False, False))
        self.assertEqual(
            reconcile_path_ownership(status(**active), past_hub, virtual_hub=True),
            PathOwnership('retracting', 2))
        self.assertEqual(reconcile_path_ownership(  # PTFE calibration braking
            status(**dict(active, operation=4, state=5, stage=9)), past_hub,
            virtual_hub=True), PathOwnership('retracting', 2))
        # The real-hub polarity (this lane's own LOAD still made) must not
        # also satisfy the virtual-hub check past HUB.
        self.assertEqual(reconcile_path_ownership(status(**active), sensors(
            shared_hub_present=False, load_present=(False, False, True, False)),
            virtual_hub=True),
            PathOwnership('unknown'))
        # Retracting to HUB (or a calibration retract still approaching it):
        # a virtual hub's own LOAD switch already reads empty here too, the
        # same flip as past HUB above -- not the real-hub device's made
        # polarity a stale reading might suggest.
        for change in (dict(stage=4), dict(operation=4, state=5, stage=8)):
            with self.subTest(change=change):
                self.assertEqual(reconcile_path_ownership(
                    status(**dict(active, **change)), past_hub, virtual_hub=True),
                    PathOwnership('retracting', 2))
        # A real-hub device still requires its own LOAD switch made (never
        # flipped) at these same stages.
        for change in (dict(stage=4), dict(operation=4, state=5, stage=8)):
            with self.subTest(change=change, virtual_hub=False):
                self.assertEqual(reconcile_path_ownership(
                    status(**dict(active, **change)), past_hub, virtual_hub=False),
                    PathOwnership('unknown'))

    def test_virtual_hub_load_flips_load_polarity_feeding_to_hub(self):
        """A virtual-hub device's own LOAD switch is the boundary being fed
        toward, so it reads empty the whole approach (including a retry
        backing out and feeding again), the opposite of a real separate-hub
        device's still-made assurance switch. `not shared_present` (any()
        of every LOAD switch for a virtual hub) already forces this lane's
        own reading false to reach here at all."""
        active = dict(path_occupied=False, lane_active=True,
                      terminal_pending=True, owner_lane=2,
                      occupant_lane=255, client=1, session=7, request=11,
                      lane=2, operation=1, state=1, stage=1,
                      client_sessions=(0, 7, 0, 0),
                      client_requests=(0, 11, 0, 0))
        clear = sensors(shared_hub_present=False,
                        load_present=(False, False, False, False))
        self.assertEqual(
            reconcile_path_ownership(status(**active), clear, virtual_hub=True),
            PathOwnership('feeding', 2))
        self.assertEqual(reconcile_path_ownership(  # PTFE calibration
            status(**dict(active, operation=4, state=5)), clear, virtual_hub=True),
            PathOwnership('feeding', 2))
        self.assertEqual(reconcile_path_ownership(  # LOAD retry
            status(**dict(active, stage=12)), clear, virtual_hub=True),
            PathOwnership('feeding', 2))
        # The real-hub polarity (this lane's own LOAD still made) must not
        # also satisfy the virtual-hub check.
        self.assertEqual(reconcile_path_ownership(status(**active), sensors(
            shared_hub_present=False, load_present=(False, False, True, False)),
            virtual_hub=True),
            PathOwnership('unknown'))
        # A real-hub device still requires its own LOAD switch made (never
        # flipped) at this stage.
        self.assertEqual(
            reconcile_path_ownership(status(**active), clear, virtual_hub=False),
            PathOwnership('unknown'))

    def test_virtual_hub_preload_settles_with_its_load_switch_clear(self):
        """EMU/KMS back a settled PRELOAD off past their own LOAD switch (it
        is the shared virtual-hub boundary, mcu/Kconfig.devices), the
        opposite of a real separate-hub device's still-made LOAD switch."""
        local = status(state=8, stage=0, operation=0, lane_active=False)
        clear = sensors(shared_hub_present=False,
                        load_present=(False, False, False, False))
        self.assertEqual(
            reconcile_path_ownership(local, clear, virtual_hub=True),
            PathOwnership('local', 2))
        # The real-hub polarity (this lane's own LOAD still made) must not
        # also satisfy the virtual-hub check.
        self.assertEqual(reconcile_path_ownership(local, sensors(
            shared_hub_present=False, load_present=(False, False, True, False)),
            virtual_hub=True),
            PathOwnership('unknown'))


if __name__ == '__main__':
    unittest.main()
