# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in, manually edited maintenance counts; no firmware inference."""
import json
import os
import re
import tempfile

MAX_BYTES = 8192
MAX_VALUE = 1000000000
MAX_COUNTERS = 32
MAX_WARNING_LENGTH = 160
_NAME = re.compile(r'[a-z][a-z0-9_]{0,31}\Z')


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key %s' % key)
        result[key] = value
    return result


def _validate(record):
    if type(record) is not dict or set(record) != {'counters'} or type(record['counters']) is not dict:
        raise ValueError('expected a counters object')
    counters = record['counters']
    if len(counters) > MAX_COUNTERS:
        raise ValueError('too many counters')
    for name, entry in counters.items():
        if type(name) is not str or _NAME.fullmatch(name) is None:
            raise ValueError('invalid counter name')
        if type(entry) is not dict or set(entry) != {'value', 'limit', 'warning'}:
            raise ValueError('invalid counter fields')
        for field, value in entry.items():
            if field == 'warning':
                if (type(value) is not str or len(value) > MAX_WARNING_LENGTH
                        or any(ord(char) < 32 or ord(char) == 127 for char in value)):
                    raise ValueError('warning must be at most 160 printable characters')
            elif type(value) is not int or not 0 <= value <= MAX_VALUE:
                raise ValueError('%s must be 0..%d' % (field, MAX_VALUE))
    return counters


class MaintenanceCounters:
    def __init__(self, path):
        path = os.path.expanduser(path)
        if not os.path.isabs(path):
            raise ValueError('maintenance_counters_file must be an absolute path')
        self.path = path
        self._counters = {}
        try:
            with open(path, 'rb') as source:
                contents = source.read(MAX_BYTES + 1)
        except FileNotFoundError:
            return
        if len(contents) > MAX_BYTES:
            raise ValueError('file exceeds %d bytes' % MAX_BYTES)
        self._counters = _validate(json.loads(contents.decode('utf-8'),
                                               object_pairs_hook=_unique_pairs))

    def status(self):
        return {name: dict(entry) for name, entry in self._counters.items()}

    def edit(self, name, increment=None, reset=False, limit=None, warning=None):
        if type(name) is not str or _NAME.fullmatch(name) is None:
            raise ValueError('invalid counter name')
        if type(reset) is not bool or (increment is not None and reset):
            raise ValueError('reset and increment are exclusive')
        if increment is not None and (type(increment) is not int or not 1 <= increment <= MAX_VALUE):
            raise ValueError('increment must be 1..%d' % MAX_VALUE)
        if limit is not None and (type(limit) is not int or not 0 <= limit <= MAX_VALUE):
            raise ValueError('limit must be 0..%d' % MAX_VALUE)
        if warning is not None and (type(warning) is not str or len(warning) > MAX_WARNING_LENGTH
                                    or any(ord(char) < 32 or ord(char) == 127 for char in warning)):
            raise ValueError('warning must be at most 160 printable characters')
        if increment is None and not reset and limit is None and warning is None:
            raise ValueError('no edit supplied')
        counters = self.status()
        entry = counters.setdefault(name, {'value': 0, 'limit': 0, 'warning': ''})
        if reset:
            entry['value'] = 0
        if increment is not None:
            entry['value'] += increment
        if limit is not None:
            entry['limit'] = limit
        if warning is not None:
            entry['warning'] = warning
        _validate({'counters': counters})
        contents = (json.dumps({'counters': counters}, sort_keys=True,
                               separators=(',', ':')) + '\n').encode('utf-8')
        if len(contents) > MAX_BYTES:
            raise ValueError('counters exceed %d bytes' % MAX_BYTES)
        fd, temporary = tempfile.mkstemp(prefix='.maintenance-counters-',
                                         dir=os.path.dirname(self.path))
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(contents)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self._counters = counters
        return dict(entry)
