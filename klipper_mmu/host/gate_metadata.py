# SPDX-License-Identifier: GPL-3.0-or-later
"""Small, host-owned manual gate annotations; never a physical sensor source."""
import json
import os
import tempfile

MAX_BYTES = 8192
FIELDS = ('name', 'material', 'color', 'available')


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key %s' % key)
        result[key] = value
    return result


def _validate(record, gate_count):
    if type(record) is not dict or set(record) != {'gates'} or type(record['gates']) is not dict:
        raise ValueError('expected a gates object')
    gates = record['gates']
    if len(gates) > gate_count:
        raise ValueError('too many gates')
    for key, fields in gates.items():
        if (type(key) is not str or not key.isascii() or not key.isdecimal()
                or str(int(key)) != key or int(key) >= gate_count):
            raise ValueError('invalid gate index %r' % key)
        if type(fields) is not dict or not fields or set(fields) - set(FIELDS):
            raise ValueError('invalid fields for gate %s' % key)
        for field, value in fields.items():
            if field == 'available':
                if type(value) is not bool:
                    raise ValueError('available must be boolean')
            elif (type(value) is not str or not value or len(value) > 64
                  or any(ord(char) < 32 or ord(char) == 127 for char in value)):
                raise ValueError('%s must be 1..64 printable characters' % field)
    return gates


class GateMetadata:
    def __init__(self, path, gate_count):
        path = os.path.expanduser(path)
        if not os.path.isabs(path):
            raise ValueError('gate_metadata_file must be an absolute path')
        if type(gate_count) is not int or not 1 <= gate_count <= 12:
            raise ValueError('gate count must be 1..8')
        self.path = path
        self.gate_count = gate_count
        self._gates = {}
        try:
            with open(path, 'rb') as source:
                contents = source.read(MAX_BYTES + 1)
        except FileNotFoundError:
            return
        if len(contents) > MAX_BYTES:
            raise ValueError('file exceeds %d bytes' % MAX_BYTES)
        record = json.loads(contents.decode('utf-8'), object_pairs_hook=_unique_pairs)
        self._gates = _validate(record, gate_count)

    def status(self):
        # A fresh nested copy prevents callers from editing the retained map.
        return {key: dict(fields) for key, fields in self._gates.items()}

    def gate_status(self, gate):
        """Happy Hare's three states for one gate, as this file stores them:
        None (GATE_UNKNOWN, no note recorded yet), True (GATE_AVAILABLE) or
        False (GATE_EMPTY). See extras/mmu/mmu_constants.py:79-82 in the
        Happy Hare oracle."""
        if type(gate) is not int or not 0 <= gate < self.gate_count:
            raise ValueError('invalid gate index')
        return self._gates.get(str(gate), {}).get('available')

    def edit(self, gate, updates):
        if type(gate) is not int or not 0 <= gate < self.gate_count:
            raise ValueError('invalid gate index')
        if type(updates) is not dict or not updates or set(updates) - set(FIELDS):
            raise ValueError('invalid gate fields')
        gates = self.status()
        fields = gates.setdefault(str(gate), {})
        fields.update(updates)
        _validate({'gates': gates}, self.gate_count)
        contents = (json.dumps({'gates': gates}, sort_keys=True,
                               separators=(',', ':')) + '\n').encode('utf-8')
        if len(contents) > MAX_BYTES:
            raise ValueError('metadata exceeds %d bytes' % MAX_BYTES)
        directory = os.path.dirname(self.path)
        fd, temporary = tempfile.mkstemp(prefix='.gate-metadata-', dir=directory)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(contents)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self._gates = gates
