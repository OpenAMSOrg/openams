# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only OpenAMS unit view over one Klipper-MMU physical capture.

The observer and this adapter run on Klipper's reactor. ``refresh`` is an
explicit query; cached properties never start transport work or motion.
"""

import math

from .openams_device_state import OpenAMSDeviceState
from .openams_fault_projection import project_fault


class OpenAMSDevice:
    def __init__(self, name, observer, oams_idx, fps_name, device_family,
                 bay_count, path_length_mm, cache_ttl=2.0):
        if type(name) is not str or not name.strip():
            raise ValueError('OpenAMS device name required')
        if (type(cache_ttl) not in (int, float) or not math.isfinite(cache_ttl)
                or not 0 < cache_ttl <= 60):
            raise ValueError('Finite physical capture cache lifetime required')
        self.name = name
        self._observer = observer
        self._cache_ttl = float(cache_ttl)
        self._state = OpenAMSDeviceState(
            oams_idx, fps_name, device_family, bay_count, path_length_mm)
        self._bound = None
        self._captured_at = None
        self._captured_status = None
        self._snapshot_stale = False

    @property
    def oams_idx(self):
        return self._state.oams_idx

    @property
    def fps_name(self):
        return self._state.fps_name

    @fps_name.setter
    def fps_name(self, resolved_name):
        # OpenAMS publishes its resolved lane through this assignment. The
        # MMU profile already names that lane; a mismatch must fail closed.
        if resolved_name != self._state.fps_name:
            raise ValueError('OpenAMS resolved FPS does not match MMU configuration')

    @property
    def device_family(self):
        return self._state.device_family

    @property
    def bay_count(self):
        return self._state.bay_count

    @property
    def filament_path_length(self):
        return self._state.path_length_mm

    @property
    def path_length_mm(self):
        return self._state.path_length_mm

    @property
    def connected(self):
        return self._identity() is not None

    @property
    def firmware_owns_liveness(self):
        # No operation watchdog has been negotiated for this read-only unit.
        return False

    @property
    def protocol_version(self):
        # The Klipper-MMU wire version is not an OpenAMS protocol version.
        return None

    def protocol_summary(self):
        return 'Klipper-MMU physical observation only'

    @property
    def hardware_snapshot_ready(self):
        # OpenAMS reads this first on every world build. After an operation
        # ends, the cached capture predates its last motion (a LOAD right
        # after UNLOAD or CALIBRATE would see the HUB still occupied), so
        # capture again before OpenAMS decides anything from it.
        if self._snapshot_stale:
            self._snapshot_stale = False
            self.refresh()
        return self._ready()

    @property
    def f1s_hes_value(self):
        return self._state.f1s_hes_value if self._ready() else None

    @property
    def load_present(self):
        """Per-lane LOAD switch readings; None means unknown."""
        return self._state.load_present if self._ready() else None

    @property
    def local_reserved_lane(self):
        """Bay held by a physically observed local PRELOAD, if current."""
        return self._state.local_reserved_lane if self._ready() else None

    @property
    def hub_hes_value(self):
        return self._state.hub_hes_value if self._ready() else None

    @property
    def fps_value(self):
        """Fresh calibrated buffer position for an OpenAMS FPS mirror."""
        status = self._captured_status
        if not self._ready() or status is None:
            raise ValueError('OpenAMS buffer feedback is unavailable')
        values = status.values
        position = values.get('buffer_position')
        if (values.get('buffer_valid') is not True
                or type(position) is not int or not 0 <= position <= 1000):
            raise ValueError('OpenAMS buffer feedback is invalid')
        return position / 1000.0

    @property
    def current_spool(self):
        if not self._ready():
            return None
        loaded = [i for i, present in enumerate(self._state.hub_hes_value)
                  if present]
        return loaded[0] if len(loaded) == 1 else None

    def owner_identity(self):
        """Return the active HOST path owner from a still-current capture."""
        return self._state.owner_identity if self._ready() else None

    def follower_observation(self):
        """Return controller truth from a current capture, or None.

        A disarmed observation with bay=None proves only that this device's
        follower stopped. Its consumer may clear an already recorded uncertain
        bay for this exact device; it cannot identify a new bay or path owner.
        Compare instance, generation and query with the earlier command when
        requiring a post-command observation.
        """
        return self._state.follower_observation if self._ready() else None

    def _identity(self):
        return self._observer.physical_connection_identity()

    def _ready(self):
        return (self._state.hardware_snapshot_ready
                and self._bound is not None and self._identity() == self._bound
                and self._captured_at is not None
                and 0 <= self._observer.physical_monotonic()
                - self._captured_at < self._cache_ttl)

    def disconnect(self):
        self._bound = None
        self._captured_at = None
        self._captured_status = None
        self._state.disconnect()

    def refresh(self):
        """Read a new capture, waiting while another MCU exchange runs.

        Waiting does not invalidate the previous observation. Its original
        identity and age still govern cached properties until replaced.
        """
        try:
            # This call yields to the reactor. Keep the previous bounded-age
            # capture visible until a replacement or an actual failure arrives.
            with self._observer._mcu_lock:
                capture = self._observer.capture_physical_state()
            if capture is None:
                self.disconnect()
                return False
            identity, started_at, status, sensors = capture
            generation, instance = identity
            if self._identity() != identity:
                self.disconnect()
                return False
            self.disconnect()
            self._state.begin_connection(instance, generation)
            self._bound = identity
            ready = self._state.observe(
                status, sensors, generation, True,
                virtual_hub=getattr(self._observer, '_virtual_hub_capable', False))
            if ready:
                self._captured_at = started_at
                self._captured_status = status
            return ready and self._ready()
        except Exception:
            self.disconnect()
            raise

    def _bay(self, values, bay_index):
        if type(bay_index) is not int or not 0 <= bay_index < self.bay_count:
            raise IndexError('OpenAMS bay out of range')
        if values is None:
            raise ValueError('OpenAMS physical bay state is unknown')
        return bool(values[bay_index])

    def is_bay_ready(self, bay_index):
        return self._bay(self.f1s_hes_value, bay_index)

    def bay_loadable(self, bay_index):
        """Whether OpenAMS may attempt a LOAD on this bay.

        A real BAY switch answers directly. A device with none at all (its
        reading is unknown, never fabricated as present or absent) answers
        from this MMU's own persisted per-lane gate status instead: Happy
        Hare's own tool-change rule refuses only a gate proven empty, and
        tries an unknown gate exactly like an available one
        (mmu_controller.py's _select_and_load_tool; the three states are
        extras/mmu/mmu_constants.py:79-82 GATE_UNKNOWN/GATE_EMPTY/
        GATE_AVAILABLE). Absent any gate status tracking at all, an unknown
        bay is likewise left attemptable rather than fabricated as refused.
        """
        if type(bay_index) is not int or not 0 <= bay_index < self.bay_count:
            raise IndexError('OpenAMS bay out of range')
        values = self.f1s_hes_value
        if values is None:
            raise ValueError('OpenAMS physical bay state is unknown')
        present = values[bay_index]
        if present is not None:
            return present
        metadata = getattr(self._observer, '_gate_metadata', None)
        return metadata is None or metadata.gate_status(bay_index) is not False

    def is_bay_loaded(self, bay_index):
        return self._bay(self.hub_hes_value, bay_index)

    def firmware_fault_info(self, value, physical_lane=None):
        """The C2 §4.8 entry for one latched km_fault (C5 §7.6).

        OpenAMS latches the raw wire value through accept_firmware_fault, so
        the unit, not the manager, is what names it."""
        return project_fault(value, physical_lane, self.bay_count)

    def register_fault_clear_command(self, gcode):
        """Publish OAMS_CLEAR_FAULT OAMS=<idx> for this unit.

        The UIs address a fault's `clear_fault` token with that command, so a
        klipper-mmu unit registers its own recovery under the same name the AMS
        units use; the mux key keeps the two apart."""
        unit = self

        def clear_fault(gcmd):
            try:
                unit.acknowledge_fault()
            except Exception as exc:
                raise gcmd.error('OAMS_CLEAR_FAULT: %s' % exc)
            gcmd.respond_info(
                'OAMS[%s] fault acknowledged; reconcile the path before the '
                'lane is usable again' % unit.oams_idx)

        gcode.register_mux_command('OAMS_CLEAR_FAULT', 'OAMS',
                                   str(self.oams_idx), clear_fault,
                                   desc='Acknowledge a klipper-mmu fault')

    def get_status(self, eventtime):
        return {'connected': self.connected,
                'hardware_snapshot_ready': self.hardware_snapshot_ready,
                'current_spool': self.current_spool,
                'bay_present': self.f1s_hes_value,
                'load_present': self.load_present,
                'hub_by_bay': self.hub_hes_value,
                'device_family': self.device_family,
                'fps_name': self.fps_name}

    def get_webhook_status(self):
        """Expose unknown readings as null; never synthesize empty bays."""
        return {'schema_version': 1, 'oams_idx': self.oams_idx,
                'connected': self.connected,
                'hardware_snapshot_ready': self.hardware_snapshot_ready,
                'family': self.device_family, 'bay_count': self.bay_count,
                'fps_name': self.fps_name,
                'path_length_mm': self.path_length_mm,
                'current_spool': self.current_spool,
                'f1s_hes_value': self.f1s_hes_value,
                'load_present': self.load_present,
                'hub_hes_value': self.hub_hes_value,
                'protocol_version': self.protocol_version,
                'firmware_owns_liveness': self.firmware_owns_liveness}
