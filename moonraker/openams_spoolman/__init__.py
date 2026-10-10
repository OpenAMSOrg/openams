# openams_spoolman: the C4 Moonraker component (§1, §6-§8).
#
# ``load_component`` is Moonraker's plugin entry point (``server.py``'s
# ``load_component``, which imports this package and calls this function).

from .component import OpenAmsSpoolman


def load_component(config):
    return OpenAmsSpoolman(config)
