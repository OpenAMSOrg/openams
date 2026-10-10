#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read explicit local Klipper cfg files and print a conservative JSON migration report."""
import argparse
import json
import re
from pathlib import Path

SECTION = re.compile(r'^\s*\[([^\]]+)\]\s*(?:[#;].*)?$')
KEY = re.compile(r'^\s*([^\s:=#;]+)\s*[:=]\s*(.*?)\s*$')
PLACEHOLDER = re.compile(r'\[\[.*?\]\]|\{%.*?%\}|\[%.*?%\]|<[^>]+>')


def report(paths, source):
    result = {'source': source, 'files': [], 'sections': [], 'includes_unresolved': [],
              'placeholders_unresolved': [], 'manual_review': [], 'errors': [],
              'recommendation': None}
    seen_sections = set()
    for path in paths:
        file_name = str(path)
        result['files'].append(file_name)
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except (OSError, UnicodeError) as exc:
            result['errors'].append('%s: %s' % (file_name, exc))
            continue
        section = None
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith(('#', ';')):
                continue
            match = SECTION.match(line)
            if match:
                name = match.group(1).strip()
                if name.lower().startswith('include '):
                    result['includes_unresolved'].append({'file': file_name, 'line': number,
                                                          'directive': name})
                    section = None
                    continue
                if PLACEHOLDER.search(name):
                    result['placeholders_unresolved'].append({'file': file_name, 'line': number,
                                                              'section': name, 'key': None, 'value': name})
                identity = name.lower()
                if identity in seen_sections:
                    result['errors'].append('%s:%d: duplicate section [%s]' % (file_name, number, name))
                seen_sections.add(identity)
                section = {'file': file_name, 'line': number, 'name': name, 'entries': []}
                result['sections'].append(section)
                continue
            match = KEY.match(line)
            if match and section is not None:
                key = match.group(1)
                value = re.split(r'\s+[#;]', match.group(2), maxsplit=1)[0].strip()
                if any(entry['key'].lower() == key.lower() for entry in section['entries']):
                    result['errors'].append('%s:%d: duplicate key %s in [%s]' %
                                            (file_name, number, key, section['name']))
                section['entries'].append({'line': number, 'key': key, 'value': value})
                if PLACEHOLDER.search(value):
                    result['placeholders_unresolved'].append({'file': file_name, 'line': number,
                                                              'section': section['name'], 'key': key,
                                                              'value': value})
                continue
            result['errors'].append('%s:%d: malformed or out-of-section line' % (file_name, number))
    if source == 'happy-hare':
        units = [s for s in result['sections'] if s['name'].lower().startswith('mmu_unit ')]
        if len(units) != 1:
            result['errors'].append('Happy Hare requires exactly one [mmu_unit name] in supplied files')
        else:
            values = {e['key'].lower(): e['value'] for e in units[0]['entries']}
            vendor = values.get('vendor', '').lower().replace(' ', '').replace('-', '')
            version = values.get('version', '')
            count = values.get('num_gates', '')
            device = {'boxturtle': 'BoxTurtle', 'nightowl': 'NightOwl'}.get(vendor)
            if not device:
                result['errors'].append('unsupported or missing Happy Hare vendor')
            if not version or PLACEHOLDER.search(version):
                result['errors'].append('missing or unresolved Happy Hare version')
            if not count.isdecimal() or not 1 <= int(count) <= 8:
                result['errors'].append('Happy Hare num_gates must be an integer from 1 to 8')
            if device and version and not PLACEHOLDER.search(version) and count.isdecimal() and 1 <= int(count) <= 8:
                result['recommendation'] = {'device': device, 'lane_count': int(count)}
    else:
        devices = [s for s in result['sections'] if s['name'].split(' ', 1)[0].lower() in
                   ('afc_boxturtle', 'afc_nightowl')]
        if len(devices) != 1:
            result['errors'].append('AFC requires exactly one named [AFC_BoxTurtle name] or [AFC_NightOwl name]')
        else:
            name_parts = devices[0]['name'].split(' ', 1)
            if len(name_parts) != 2 or not name_parts[1].strip():
                result['errors'].append('AFC device section needs a name')
            else:
                unit_name = name_parts[1].strip()
                steppers = [s for s in result['sections'] if s['name'].lower().startswith('afc_stepper ')]
                units = []
                if not steppers:
                    result['errors'].append('AFC has no [AFC_stepper name] declarations')
                for stepper in steppers:
                    values = {e['key'].lower(): e['value'] for e in stepper['entries']}
                    unit = values.get('unit', '')
                    if unit.startswith(unit_name + ':') and unit[len(unit_name) + 1:].isdecimal():
                        units.append(int(unit[len(unit_name) + 1:]))
                    else:
                        result['errors'].append('AFC stepper has missing or foreign unit: [%s]' % stepper['name'])
                if not units or len(units) != len(set(units)) or len(units) > 8 or sorted(units) != list(range(1, len(units) + 1)):
                    result['errors'].append('AFC requires unique contiguous stepper units 1..8 for the named device')
                else:
                    result['recommendation'] = {'device': 'BoxTurtle' if name_parts[0].lower() == 'afc_boxturtle' else 'NightOwl',
                                                'lane_count': len(units)}
    if result['includes_unresolved']:
        result['manual_review'].append('Includes were listed but not expanded; supply all relevant files explicitly.')
    if result['placeholders_unresolved']:
        result['errors'].append('Unresolved template placeholders')
    values_text = '\n'.join(e['value'] for s in result['sections'] for e in s['entries'])
    if re.search(r'\b(?:HUB\s*=\s*PC4|TRG1\s*=\s*PC5)\b', values_text, re.I):
        result['manual_review'].append('AFC Lite legacy HUB=PC4 and TRG1=PC5 aliases overlap SW1/SW2; resolve physical wiring.')
    result['manual_review'].append('Review pins, polarity, TMC current, buffer/DiffA/DiffB, and assist manually; no values are inferred.')
    if result['errors'] or result['includes_unresolved']:
        result['recommendation'] = None
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', choices=('happy-hare', 'afc'), required=True)
    parser.add_argument('cfg', nargs='+', type=Path, help='explicit local cfg files')
    args = parser.parse_args()
    result = report(args.cfg, args.source)
    print(json.dumps(result, indent=2))
    return 1 if result['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
