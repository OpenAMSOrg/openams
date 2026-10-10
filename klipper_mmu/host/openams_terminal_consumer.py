# SPDX-License-Identifier: GPL-3.0-or-later
"""Match a retained MMU terminal to an ams-family operation.

Pass ``consume_terminal`` as EventDelivery.observe's consumer on the Klipper
reactor thread. OpenAMS must provide the public Runtime.accept_terminal hook
(this repository's src/oams_runtime.py does).
"""


class _NotSettled(Exception):
    pass


def consume_terminal(event, requests, generation, runtime):
    """Return true only after OpenAMS applies the matching terminal.

    A false return keeps the firmware queue head and Requests match available
    for retry or reconciliation. Unexpected runtime exceptions propagate; the
    caller must not ACK the event after one.
    """
    match = requests.event(event, generation)
    if match is None:
        return False

    def settle(fps, code, value, gen):
        if (not runtime.accept_terminal(fps, code, value, gen)
                and not runtime.accept_reload_terminal(
                    fps, code, value, gen, match.identity.lane)):
            raise _NotSettled()

    try:
        return requests.deliver(match, settle)
    except _NotSettled:
        return False
