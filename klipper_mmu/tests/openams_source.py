# SPDX-License-Identifier: GPL-3.0-or-later
"""Import real OpenAMS modules from the checkout named by OPENAMS_SOURCE."""
import importlib
import os
from pathlib import Path
import sys
import types


def load(*names):
    """Return the named modules from OPENAMS_SOURCE/src as one package."""
    source = os.environ.get('OPENAMS_SOURCE')
    if not source or not (Path(source) / 'src/oams_runtime.py').is_file():
        raise RuntimeError('Set OPENAMS_SOURCE to an OpenAMS checkout (this repository\'s root)')
    sys.dont_write_bytecode = True
    package = sys.modules.get('openams_src')
    if package is None:
        package = sys.modules['openams_src'] = types.ModuleType('openams_src')
        package.__path__ = [str(Path(source).resolve() / 'src')]
    return [importlib.import_module('openams_src.' + name) for name in names]
