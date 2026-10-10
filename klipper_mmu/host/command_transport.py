# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded Klipper MCU exchanges; callers own session reconciliation.

Every host exchange uses exchange() below on the reactor thread while holding
the observer's MCU mutex. An admission only says that firmware processed a
request; it is not a terminal.
"""

from .protocol import Identity, decode_admission, decode_command_result
from .wire import (ADMISSION, CANCEL, COMMAND_RESULT, HEARTBEAT, START, STOP,
                   AdmissionResult, Client, Command, CommandCode, Direction,
                   Fault, OpType, known)

_HOST_OPERATIONS = known(OpType) - {OpType.NONE, OpType.CANCEL}


def _uint(value, bits, nonzero=False):
    if type(value) is not int or not int(nonzero) <= value < 1 << bits:
        raise ValueError('Invalid %d-bit command field' % bits)


def exchange(mcu, reactor, sends, replies, is_current, timeout, what):
    """Send, return the first reply a handler accepts, and always unregister.

    Call on the reactor thread while holding the observer's MCU mutex. sends
    lists (format, args). replies maps a response format to handler(params),
    run on the reactor only while this exchange is open and is_current() is
    true. A handler returns None to ignore a reply or returns the result; a
    ValueError it raises becomes this exchange's error.
    """
    if not is_current():
        raise ValueError('%s connection changed' % what)
    completion = reactor.completion()
    deadline = reactor.monotonic() + timeout
    wrappers = []
    open_ = [True]

    def receive(handler, params):
        if not open_[0] or completion.test() or not is_current():
            return
        try:
            result = handler(params)
        except ValueError as exc:
            result = exc
        if result is not None:
            completion.complete(result)

    def marshal(handler):
        def serial(params):
            copied = dict(params)
            reactor.register_async_callback(
                lambda eventtime: receive(handler, copied))
        return serial

    try:
        commands = {fmt: mcu.lookup_command(fmt) for fmt, _ in sends}
        for fmt, handler in replies.items():
            wrappers.append(mcu.register_serial_response(marshal(handler), fmt))
        for fmt, args in sends:
            if completion.test():
                break
            if not is_current():
                raise ValueError('%s connection changed before send' % what)
            commands[fmt].send(args)
        outcome = completion.wait(deadline)
        if not is_current():
            raise ValueError('Stale %s response' % what)
        if outcome is None or reactor.monotonic() > deadline:
            raise ValueError('%s timed out' % what)
        if isinstance(outcome, ValueError):
            raise outcome
        return outcome
    finally:
        open_[0] = False
        for wrapper in wrappers:
            wrapper.unregister()


def _command(mcu, reactor, instance, kind, query, fmt, args, identity,
             is_current, timeout):
    if (not callable(is_current) or type(timeout) not in (int, float)
            or not 0 < timeout <= 60):
        raise ValueError('Invalid command guard or timeout')

    def on_admission(params):
        fields = ('instance_hi', 'instance_lo', 'client', 'session', 'request',
                  'operation', 'lane')
        expected = (instance >> 32, instance & 0xffffffff, identity.client,
                    identity.session, identity.request, identity.operation,
                    identity.lane)
        if tuple(params.get(key) for key in fields) != expected:
            return None
        reply = decode_admission(params)
        if (reply.admission not in known(AdmissionResult)
                or reply.fault not in known(Fault)):
            raise ValueError('Unsupported admission or fault')
        return reply

    def on_result(params):
        if params.get('command') != kind or params.get('query') != query:
            return None
        reply = decode_command_result(params)
        if reply.instance != instance:
            raise ValueError('MMU controller instance changed')
        if identity is not None and reply.result == CommandCode.OK:
            raise ValueError('START/CANCEL success requires admission')
        return reply

    replies = {COMMAND_RESULT: on_result}
    if identity is not None:
        replies = {ADMISSION: on_admission, COMMAND_RESULT: on_result}
    return exchange(mcu, reactor, [(fmt, args)], replies, is_current, timeout,
                    'MMU command')


def send_operation(mcu, reactor, instance, identity, is_current,
                   direction=None, cancel=False, timeout=5.0):
    """Return Admission or CommandResult, retaining rejection codes verbatim.

    The caller must allocate a unique request in its reconciled host session and
    serialize exchanges. A successful admission does not authorize completion.
    """
    _uint(instance, 64, True)
    if not isinstance(identity, Identity):
        raise ValueError('Invalid request identity')
    for value, bits, nonzero in ((identity.client, 8, False),
                                 (identity.session, 32, True),
                                 (identity.request, 32, True),
                                 (identity.operation, 8, False),
                                 (identity.lane, 8, False)):
        _uint(value, bits, nonzero)
    if (identity.client != Client.HOST
            or identity.operation not in _HOST_OPERATIONS):
        raise ValueError('Unsupported host operation')
    if cancel:
        if direction is not None:
            raise ValueError('Cancel has no direction')
    elif direction not in known(Direction) or type(direction) is not int:
        raise ValueError('Invalid motion direction')
    prefix = [instance >> 32, instance & 0xffffffff, identity.client,
              identity.session, identity.request, identity.operation,
              identity.lane]
    return _command(mcu, reactor, instance,
                    Command.CANCEL if cancel else Command.START,
                    identity.request, CANCEL if cancel else START,
                    prefix if cancel else prefix + [direction], identity,
                    is_current, timeout)


def send_heartbeat(mcu, reactor, instance, session, is_current, timeout=5.0):
    _uint(instance, 64, True)
    _uint(session, 32, True)
    return _command(mcu, reactor, instance, Command.HEARTBEAT, session,
                    HEARTBEAT,
                    [instance >> 32, instance & 0xffffffff, session], None,
                    is_current, timeout)


def send_stop(mcu, reactor, instance, is_current, timeout=5.0):
    """Send explicit emergency stop. Caller serializes zero-query responses.

    Firmware gives STOP no unique request field; no host-side exchange can
    distinguish two STOP replies within one connection and instance.
    """
    _uint(instance, 64, True)
    return _command(mcu, reactor, instance, Command.STOP, 0, STOP,
                    [instance >> 32, instance & 0xffffffff], None,
                    is_current, timeout)
