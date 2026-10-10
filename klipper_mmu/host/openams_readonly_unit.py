# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional OpenAMS physical view; no command or event binding."""

from .openams_device import OpenAMSDevice


class OpenAMSReadOnlyUnit(OpenAMSDevice):
    motion_available = False

    def __init__(self, *args):
        super().__init__(*args)
        self._runtime = None

    def bind_runtime(self, runtime, fps_name):
        if (fps_name != self.fps_name
                or not callable(getattr(runtime, 'dispatch', None))):
            raise ValueError('OpenAMS runtime or FPS does not match this unit')
        if self._runtime is not None and self._runtime is not runtime:
            raise ValueError('OpenAMS unit is already bound to another runtime')
        self._runtime = runtime

    def start_load_spool(self, *args):
        raise RuntimeError('OpenAMS MMU motion is not available')

    def start_unload_spool(self, *args):
        raise RuntimeError('OpenAMS MMU motion is not available')

    def load_spool_cancel(self, *args):
        raise RuntimeError('OpenAMS MMU motion is not available')

    def set_oams_follower(self, *args):
        raise RuntimeError('OpenAMS MMU motion is not available')

    def start_calibrate(self, *args):
        raise RuntimeError('OpenAMS MMU motion is not available')

    def preload(self, *args):
        """OAMS_PRELOAD SPOOL=<bay> (C3 §2): a physical observation stages
        nothing; only a session-bound unit owns a HOST request to send."""
        raise RuntimeError('OpenAMS MMU motion is not available')

    def unpreload(self, *args):
        raise RuntimeError('OpenAMS MMU motion is not available')

    def eject(self, *args):
        raise RuntimeError('OpenAMS MMU motion is not available')

    def home(self):
        raise RuntimeError('OpenAMS MMU motion is not available')

    def clear_errors(self):
        raise RuntimeError('OpenAMS MMU fault recovery is not available')

    def acknowledge_fault(self):
        """OAMS_CLEAR_FAULT OAMS=<idx>: nothing here can ACK a fault event.

        A read-only unit never binds a HOST session, so it has no event
        delivery to acknowledge into."""
        raise RuntimeError('OpenAMS MMU fault acknowledgement is not available')

    def set_led_error(self, *args):
        raise RuntimeError('OpenAMS MMU LED control is not available')
