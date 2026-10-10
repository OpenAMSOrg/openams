# SPDX-License-Identifier: MIT
# openams_bridge: the Cortex Bridge in Moonraker (status and firmware update).
#
# ``load_component`` is Moonraker's plugin entry point.

from .component import OpenAmsBridge


def load_component(config):
    return OpenAmsBridge(config)
