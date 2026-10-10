# SPDX-License-Identifier: GPL-3.0-or-later
"""Retained logical tool-to-gate assignments; no physical state or motion."""
import json
import os
import tempfile

MAX_BYTES = 1024


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key %s' % key)
        result[key] = value
    return result


def _validate(record, count):
    if type(record) is not dict or set(record) != {'tools'} or type(record['tools']) is not dict:
        raise ValueError('expected a tools object')
    tools = record['tools']
    if len(tools) > count:
        raise ValueError('too many tools')
    for key, gate in tools.items():
        if (type(key) is not str or not key.isascii() or not key.isdecimal()
                or str(int(key)) != key or not 0 <= int(key) < count
                or type(gate) is not int or not 0 <= gate < count):
            raise ValueError('invalid tool-to-gate assignment')
    return tools


class ToolMapping:
    def __init__(self, path, count):
        path = os.path.expanduser(path)
        if not os.path.isabs(path):
            raise ValueError('tool_mapping_file must be an absolute path')
        if type(count) is not int or not 1 <= count <= 12:
            raise ValueError('tool/gate count must be 1..8')
        self.path = path
        self.count = count
        self._tools = {}
        try:
            with open(path, 'rb') as source:
                contents = source.read(MAX_BYTES + 1)
        except FileNotFoundError:
            return
        if len(contents) > MAX_BYTES:
            raise ValueError('mapping file too large')
        self._tools = _validate(json.loads(contents.decode('utf-8'),
                                           object_pairs_hook=_unique_pairs), count)

    def status(self):
        return dict(self._tools)

    def resolve(self, tool):
        if type(tool) is not int or not 0 <= tool < self.count:
            raise ValueError('invalid tool index')
        return self._tools.get(str(tool))

    def _require_tool(self, tool):
        if type(tool) is not int or not 0 <= tool < self.count:
            raise ValueError('invalid tool index')

    def _store(self, tools):
        _validate({'tools': tools}, self.count)
        contents = (json.dumps({'tools': tools}, sort_keys=True,
                               separators=(',', ':')) + '\n').encode('utf-8')
        if len(contents) > MAX_BYTES:
            raise ValueError('mapping file too large')
        directory = os.path.dirname(self.path)
        fd, temporary = tempfile.mkstemp(prefix='.tool-mapping-', dir=directory)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(contents)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self._tools = tools

    def set(self, tool, gate):
        self._require_tool(tool)
        if type(gate) is not int or not 0 <= gate < self.count:
            raise ValueError('invalid gate index')
        tools = self.status()
        tools[str(tool)] = gate
        self._store(tools)

    def clear(self, tool):
        self._require_tool(tool)
        key = str(tool)
        if key not in self._tools:
            return
        tools = self.status()
        del tools[key]
        self._store(tools)
