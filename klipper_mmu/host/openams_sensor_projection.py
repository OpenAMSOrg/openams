# SPDX-License-Identifier: GPL-3.0-or-later
"""Physical sensor facts available to an OpenAMS device adapter.

The MMU has one shared hub sensor. Its value cannot be copied into OpenAMS's
per-bay hub array without a separately reconciled path occupant. This module
only projects readings whose configured, valid and fresh flags are all true.
A toolhead switch is optional (FPS arrival needs none); when it is not
configured its presence is None, never an invented absence. A per-lane BAY
switch is optional the same way (a device with no BAY sensor at all still
projects a usable snapshot; its bay_present entries are None).
"""

from dataclasses import dataclass

from .protocol import SensorSnapshot


@dataclass(frozen=True)
class SensorPresence:
    instance: int
    query: int
    observed_ms: int
    bay_present: tuple  # each entry bool, or None when that lane's BAY switch is not configured
    load_present: tuple
    shared_hub_present: bool
    toolhead_present: object  # bool, or None when no switch is configured


def project_sensor_presence(snapshot, expected_instance, expected_bays,
                            virtual_hub=False):
    """Return physical presence or None when any required reading is unknown.

    The caller must also guard the transport connection generation and fetch a
    fresh query. This result does not establish per-bay loaded state, a route
    length, or OpenAMS hardware readiness.

    virtual_hub: true for a device whose shared "hub" boundary is projected
    from the owning lane's own LOAD switch, not a separate physical sensor
    (mcu/Kconfig.devices' KLIPPER_MMU_VIRTUAL_HUB; EMU and KMS). The firmware
    always reports hub.configured=True (src/operation_core.c's has_hub_sensor
    is unconditionally true), so the wire alone cannot tell a virtual hub
    apart from a real one with a stale reading -- the caller must say so
    from the firmware's own KLIPPER_MMU_CONFIG_VIRTUAL_HUB dictionary
    constant. A virtual hub's own reading (src/operation_core.c
    virtual_hub_lane) is valid only while some lane already owns the shared
    path, which a fresh boot or an idle unit never does; every LOAD switch
    is its own directly read, ownership-independent sensor, so requiring
    every one of them fresh and deriving hub occupancy from them instead
    proves the same fact without that gap.
    """
    if (not isinstance(snapshot, SensorSnapshot)
            or type(expected_instance) is not int
            or type(expected_bays) is not int
            or not 0 < expected_instance < 1 << 64
            or not 0 < expected_bays <= 12
            or snapshot.instance != expected_instance
            or snapshot.lane_count != expected_bays
            or len(snapshot.bays) != expected_bays
            or len(snapshot.loads) < expected_bays):
        return None
    samples = () if virtual_hub else (snapshot.hub,)
    if snapshot.toolhead.configured is not False:
        samples += (snapshot.toolhead,)
    if any(sample.configured is not True or sample.valid is not True
           or sample.fresh is not True for sample in samples):
        return None
    # An unconfigured BAY has no reading on a device with no per-lane switch
    # (as LOAD already tolerates below). Once configured, an invalid or stale
    # BAY cannot establish presence.
    if any(sample.configured is True and
           (sample.valid is not True or sample.fresh is not True)
           for sample in snapshot.bays):
        return None
    # An unconfigured LOAD has no reading on legacy hardware. Once configured,
    # an invalid or stale LOAD cannot establish an empty physical path. A
    # virtual hub has no hub reading to fall back on, so every LOAD must be
    # configured, valid and fresh, not just the ones that happen to be.
    loads = snapshot.loads[:expected_bays]
    if virtual_hub:
        if any(sample.configured is not True or sample.valid is not True
               or sample.fresh is not True for sample in loads):
            return None
    elif any(sample.configured is True and
            (sample.valid is not True or sample.fresh is not True)
            for sample in loads):
        return None
    load_present = tuple(sample.present if (sample.configured and
                         sample.valid and sample.fresh) else None
                         for sample in loads)
    shared_hub_present = (any(load_present) if virtual_hub
                          else snapshot.hub.present)
    return SensorPresence(snapshot.instance, snapshot.query,
                          snapshot.observed_ms,
                          tuple(sample.present if (sample.configured and
                                sample.valid and sample.fresh) else None
                                for sample in snapshot.bays),
                          load_present, shared_hub_present,
                          snapshot.toolhead.present
                          if snapshot.toolhead.configured else None)
