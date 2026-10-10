# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit OpenAMS unit facade for the guarded MMU command bridges.

This is a composition boundary, not an installed OpenAMS device. The caller
must supply a prepared session and explicitly drive retained event delivery.
Command replies below are admissions, not completed motion.
"""

from .openams_cancel_bridge import OpenAMSLoadCancelBridge
from .openams_device import OpenAMSDevice
from .event_cycle import run_event_cycle
from .openams_follow_bridge import OpenAMSFollowCommandBridge
from .openams_operation_bridge import (OpenAMSLoadCommandBridge,
                                       OpenAMSPTFECalibrationCommandBridge,
                                       OpenAMSUnloadCommandBridge)
from .openams_session_bootstrap import PreparedHostSession


class OpenAMSCommandDevice(OpenAMSDevice):
    """One physical view and its explicitly wired, guarded command callbacks."""

    def __init__(self, name, observer, oams_idx, fps_name, device_family,
                 bay_count, path_length_mm, load=None, unload=None,
                 cancel=None, follow=None, calibrate=None, cache_ttl=2.0):
        super().__init__(name, observer, oams_idx, fps_name, device_family,
                         bay_count, path_length_mm, cache_ttl)
        bridges = ((load, OpenAMSLoadCommandBridge),
                   (unload, OpenAMSUnloadCommandBridge),
                   (cancel, OpenAMSLoadCancelBridge),
                   (follow, OpenAMSFollowCommandBridge),
                   (calibrate, OpenAMSPTFECalibrationCommandBridge))
        for bridge, kind in bridges:
            if bridge is not None and not isinstance(bridge, kind):
                raise ValueError('Invalid OpenAMS command bridge')
            if bridge is not None and (getattr(bridge, 'fps', fps_name) != fps_name
                                       or getattr(bridge, 'bay_count', bay_count)
                                       != bay_count):
                raise ValueError('OpenAMS command bridge topology differs from unit')
        active = [bridge for bridge, _ in bridges if bridge is not None]
        if active and any(bridge.requests is not active[0].requests
                          or bridge.mcu is not active[0].mcu
                          or bridge.reactor is not active[0].reactor
                          for bridge in active[1:]):
            raise ValueError('OpenAMS command bridges must share one connection')
        self._load, self._unload = load, unload
        self._cancel, self._follow = cancel, follow
        self._calibrate = calibrate
        self._event_binding = None
        self._retired_requests = None
        self._retired_pending_ack = None
        self._retired_pending_ack_eligible = False

    @staticmethod
    def _require(bridge, command):
        if bridge is None:
            raise RuntimeError('OpenAMS %s bridge is not configured' % command)
        return bridge

    def _require_live(self, bridge, command):
        bridge = self._require(bridge, command)
        if not self._event_current():
            raise RuntimeError('OpenAMS event connection is not bound and current')
        return bridge

    def start_load_spool(self, spool_idx, gen=None):
        return self._require_live(self._load, 'load').start_load_spool(spool_idx, gen)

    def start_unload_spool(self, gen=None):
        return self._require_live(self._unload, 'unload').start_unload_spool(gen)

    def load_spool_cancel(self):
        return self._require_live(self._cancel, 'cancel').load_spool_cancel()

    def set_oams_follower(self, enable, direction):
        return self._require_live(self._follow, 'follower').set_oams_follower(
            enable, direction)

    def bind_runtime(self, runtime, fps_name, prepared=None, mcu=None,
                     reactor=None):
        """Bind a prepared HOST session; the caller drives each event cycle.

        Call on Klipper's reactor thread. This does not authorize motion or
        schedule background work.
        """
        if self._event_binding is not None:
            raise RuntimeError('OpenAMS runtime is already bound')
        if (self._retired_requests is not None
                and (self._retired_requests.pending
                     or self._retired_requests.pending_preload is not None
                     or self._retired_requests.pending_unpreload is not None
                     or self._retired_requests.pending_eject is not None)):
            raise RuntimeError('Retired OpenAMS waiters require reconciliation')
        if self._retired_pending_ack is not None:
            raise RuntimeError('Retired OpenAMS ACK head requires reconciliation')
        if (fps_name != self.fps_name or not isinstance(prepared, PreparedHostSession)
                or not callable(getattr(runtime, 'accept_terminal', None))
                or not callable(getattr(runtime, 'accept_reload_terminal', None))
                or mcu is None or reactor is None):
            raise ValueError('Invalid OpenAMS event binding')
        requests, delivery = prepared.requests, prepared.delivery
        identity = (delivery.generation, delivery.instance)
        if (requests.instance != delivery.instance
                or not requests.connected or not delivery.connected
                or self._identity() != identity):
            raise ValueError('OpenAMS event connection differs from physical unit')
        for bridge in (self._load, self._unload, self._cancel, self._follow,
                       self._calibrate):
            if bridge is not None and (bridge.requests is not requests
                                       or bridge.mcu is not mcu
                                       or bridge.reactor is not reactor):
                raise ValueError('OpenAMS event connection differs from command bridge')
        self._event_binding = (runtime, prepared, mcu, reactor, identity,
                               requests.generation)
        self._retired_requests = None

    def service_event_once(self, timeout=5.0, consumer_boundary=None):
        """Attempt one retained terminal delivery; return empty/blocked/acked."""
        if self._event_binding is None:
            raise RuntimeError('OpenAMS event runtime is not bound')
        runtime, prepared, mcu, reactor, _, _ = self._event_binding
        requests, delivery = prepared.requests, prepared.delivery
        if not self._event_current():
            raise ValueError('OpenAMS event connection changed; reconcile')
        # The observer accounts confirmed ACKs and retains calibration estimates.
        observer = self._observer
        return run_event_cycle(mcu, reactor, delivery, requests, runtime,
                               self._event_current, timeout, consumer_boundary,
                               observer if callable(getattr(observer, 'record_acked', None))
                               else None)

    def _event_current(self):
        if self._event_binding is None:
            return False
        _, prepared, _, _, identity, request_generation = self._event_binding
        requests, delivery = prepared.requests, prepared.delivery
        return (self._identity() == identity and requests.connected
                and requests.instance == identity[1]
                and requests.generation == request_generation
                and delivery.connected and delivery.instance == identity[1]
                and delivery.generation == identity[0])

    def retire_event_binding(self):
        """Stop the old connection; return its request matches and ACK head.

        The caller must reconcile both before using a new HOST session. Old
        command bridges are dropped because they still reference old requests.
        A new binding may deliver events, but cannot issue motion commands.
        """
        if self._event_binding is None:
            raise RuntimeError('OpenAMS event runtime is not bound')
        prepared = self._event_binding[1]
        identity, request_generation = self._event_binding[4:]
        requests, delivery = prepared.requests, prepared.delivery
        pending_ack = delivery.pending
        ack_eligible = delivery.ack_eligible
        recovery = getattr(self._observer, 'host_recovery_record', None)
        record = recovery() if callable(recovery) else None
        if (pending_ack is None and record is not None
                and (record.instance, record.session, record.generation,
                     record.request_generation)
                == (requests.instance, requests.session, identity[0],
                    request_generation)):
            pending_ack = record.pending_ack
            ack_eligible = record.pending_ack_eligible
        self._event_binding = None
        self._retired_requests = requests
        self._retired_pending_ack = pending_ack
        self._retired_pending_ack_eligible = ack_eligible
        if requests.connected:
            requests.disconnect()
        if delivery.connected:
            delivery.disconnect()
        self._load = self._unload = self._cancel = self._follow = None
        self._calibrate = None
        return requests, pending_ack

    def clear_errors(self):
        raise RuntimeError('MMU fault recovery is not implemented')

    def start_calibrate(self, kind, bay, gen=None):
        return self._require_live(self._calibrate, 'calibration').start_calibrate(
            kind, bay, gen)

    def set_led_error(self, bay, value):
        raise RuntimeError('MMU error LED command is not implemented')
