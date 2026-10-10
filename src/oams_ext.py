# OpenAMS local extension point
#
# Copyright (C) 2026 JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# The host is complete without an extension. An installation may add a module
# named oams_ext_local.py next to this one; when it is present, its classes
# replace the defaults below and the unit driver (oams.py), the family
# registry (ams_family.py), the tag reader (oams_tags.py) and the manager
# (oams_manager.py) call into it at the points named here. Every default does
# nothing, so a caller never has to check whether an extension is installed.

import importlib


class FamilyHooks(object):
    """Hooks of the pure family registry (ams_family.py)."""

    def profile_extras(self, family):
        """Fields added to a family's profile."""
        return {}

    def refine(self, capabilities, constants):
        """Refine resolved capabilities from the unit's dictionary, before
        the registry applies its own motion rules."""

    def motion_block_reason(self, capabilities):
        """Why a unit that may not move filament cannot, or None."""
        return None

    def ui_unit_actions(self):
        """Extra unit-level actions openams_ui carries: {action: the
        capability that must not be false for it to be offered}."""
        return {}


class UnitHooks(object):
    """Hooks of the unit driver (oams.py). OAMS inherits these."""

    #: Extra telemetry channel -> capability names (see _telemetry_expected).
    _EXT_TELEMETRY_CAPABILITY = {}
    #: The telemetry poll order, one channel per step, and the time between
    #: steps: each channel is asked about once every 1.3 s. The unit
    #: rate-limits its own replies.
    _POLL_SEQUENCE = ("board", "domain", "dryer")
    _POLL_INTERVAL_S = 1.3 / 3

    #: Extra ams_ht_domain_status motion stage names.
    _EXT_MOTION_STAGE_NAMES = {}

    def _ext_init(self, config):
        """End of the driver's constructor."""

    def _ext_fault_name(self, value):
        """The name of a wire fault number, or None for the default."""
        return None

    def _ext_capabilities_resolved(self):
        """After the unit's capabilities were resolved at connect."""

    def _ext_reset_commands(self):
        """Start of the telemetry setup: forget extension command handles."""

    def _ext_telemetry_specs(self, specs):
        """The telemetry channels to resolve: (name, command, response,
        callback) tuples. Returns the list to use."""
        return specs

    def _ext_after_channels(self):
        """After the telemetry channels were resolved."""

    def _ext_control_commands(self, commands):
        """The unit's control commands to resolve: (attribute, command,
        operation) tuples. Returns the list to use."""
        return commands

    def _ext_after_controls(self):
        """After the control commands were resolved."""

    def _ext_poll(self, eventtime, name):
        """One telemetry poll step, after channel `name` was polled."""

    def _ext_disconnect(self):
        """The MCU disconnected."""

    def _ext_status(self, status):
        """The unit status about to be published. Returns the status."""
        return status

    def _ext_actions(self, actions):
        """The unit's supported actions. Returns the list to publish."""
        return actions

    def _ext_register_commands(self, gcode, oams_id):
        """Register extension G-code commands for this unit."""

    def _ext_adapter_status(self):
        """Whether the dryer's power adapter is connected; None when the
        unit does not report it."""
        return None

    def _ext_rfid_scan(self, gcmd):
        """OAMS_RFID_SCAN on a unit without a tray read (AMS_CAP_TRAY_READ)."""
        raise gcmd.error("This unit cannot be told to read its tags")

    def _ext_tag_reader_class(self):
        """The tag reader class for this unit, or None for the default."""
        return None


class ManagerHooks(object):
    """Hooks of the manager (oams_manager.py). OAMSManager inherits these."""

    def _ext_init(self, config):
        """While the manager reads its configuration section."""

    def _ext_monitor(self, eventtime):
        """End of a monitor tick that ran the world."""


def _load_extension():
    """The installed extension module, or None. Only its absence is quiet:
    an extension that is present but fails to import raises."""
    # Klipper loads this as extras.oams_ext; the contract tools and some
    # tests put src/ on sys.path and import it as a top-level module.
    name = (__package__ + "." if __package__ else "") + "oams_ext_local"
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as error:
        if error.name != name:
            raise
        return None


_extension = _load_extension()
if _extension is not None:
    FamilyHooks = _extension.FamilyHooks
    UnitHooks = _extension.UnitHooks
    ManagerHooks = _extension.ManagerHooks

FAMILY = FamilyHooks()
