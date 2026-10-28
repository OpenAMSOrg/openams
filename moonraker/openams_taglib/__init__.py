# SPDX-License-Identifier: MIT
# openams_taglib: the genuine Bambu Lab tag library, served to the display.
#
# ``load_component`` is Moonraker's plugin entry point.

from .component import OpenAmsTaglib


def load_component(config):
    return OpenAmsTaglib(config)
