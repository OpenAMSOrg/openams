"""Log assertions the host plugin's tests share.

Klipper runs on Python 3.8 and later, and unittest's assertNoLogs only came
in Python 3.10, so the tests use assert_no_logs here instead: the same check
on every Python the plugin supports.
"""

import contextlib
import logging


class _Collector(logging.Handler):
    def __init__(self):
        logging.Handler.__init__(self)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@contextlib.contextmanager
def assert_no_logs(test, logger=None, level=logging.INFO):
    """What ``test.assertNoLogs(logger, level)`` does from Python 3.10: fails
    `test` when `logger` (a name or a Logger; the root logger by default)
    logs a record at `level` (a number, or a name such as "WARNING") or above
    inside the block. Records of child loggers count, as they reach the
    logger's handlers; nothing reaches the usual handlers meanwhile."""
    target = logger if isinstance(logger, logging.Logger) \
        else logging.getLogger(logger)
    if isinstance(level, str):
        level = logging.getLevelName(level.upper())
    collector = _Collector()
    collector.setLevel(level)
    handlers, old_level, propagate = (target.handlers[:], target.level,
                                      target.propagate)
    target.handlers = [collector]
    target.setLevel(level)
    target.propagate = False
    try:
        yield
    finally:
        target.handlers = handlers
        target.setLevel(old_level)
        target.propagate = propagate
    if collector.records:
        test.fail("Unexpected logs found: %r" % [
            "%s:%s:%s" % (record.levelname, record.name, record.getMessage())
            for record in collector.records])
