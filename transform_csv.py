# pylint: skip-file
import csv
from collections import defaultdict
import os
from pathlib import Path
import re

from transform_tools import Field, Ref, unquote_ref
from transform_models import Record



def load_old_csv(model):
    """
        Look for old Chart Template file and read it.
    """
    filenames = (
        f"{model}",
        f"{model}".replace('_', '.'),
        f"{model}_template",
        f"{model}_template".replace('_', '.'),
    )
    for name in filenames:
        paths = (
            list(Path.cwd().glob(f'../odoo/addons/*/data/{name}.csv'))
            + list(Path.cwd().glob(f'../odoo/addons/*/data/{name}-*.csv'))
        )
        for path in paths:
            module = str(path).split('/')[-3]
            if not module.startswith('l10n_'): continue
            with open(path, newline='', encoding='utf-8') as csvfile:
                yield module, csvfile
            os.remove(path)

def read_csv_lines(model):
    for module, csvfile in load_old_csv(model):
        csvcontent = (csvfile and csvfile.read() or '').split('\n')
        if not csvcontent:
            continue
        reader = csv.reader(csvcontent, delimiter=',')
        yield module, [line for line in reader if line]

def cleanup_csv(header, rows):
    for i, column in enumerate(header):
        header[i] = '"' + str(column).replace('"', '""') + '"'
    for i, row in enumerate(rows):
        for j, value in enumerate(row):
            if value in ('TRUE', 'FALSE'):
                value = {'TRUE': True, 'FALSE': False}.get(value)
            if value is None:
                value = ""
            rows[i][j] = '"' + str(value).replace('"', '""') + '"'
    return header, rows

def extract_template_column(header, rows, fields, remove=True):
    column = None
    templates = []
    for i, field in enumerate(header):
        if field in fields:
            column = i
        elif field.endswith(':id') or field.endswith('/id'):
            header[i] = header[i][:-3]
    if column is not None:
        getattr(header, 'pop' if remove else '__getitem__')(column)
        for row in rows:
            templates.append(getattr(row, 'pop' if remove else '__getitem__')(column))
    else:
        templates = [None] * len(rows)
    return header, rows, templates

def convert_records_to_csv(records, model):
    def hierarchy(records, path=(), root=None):
        root = root or {}
        sub_hierarchy = root
        for p in path:
            sub_hierarchy = sub_hierarchy.setdefault(p, {})
        for record in (records.values() if isinstance(records, dict) else records):
            for _id, values in record.get('children', {}).items():
                fname = _id.replace(":", "/")
                if(
                    isinstance(values, dict)
                    and isinstance(values._value, (list, tuple))
                    and isinstance(values._value[0], (list, tuple))
                    and len(values._value[0]) > 2
                    and isinstance(values._value[0][2], dict)
                ):
                    hierarchy([r[2] for r in record.get('children')[fname]._value], path + (fname,), root)
                else:
                    sub_hierarchy[fname] = True
        return root

    def header_getter(hierarchy):
        return [
            '/'.join(p for p in [fname, sub] if p)
            for fname, subs in hierarchy.items()
            for sub in (header_getter(subs) if isinstance(subs, dict) else [''])
        ]

    def line_getter(hierarchy, record):
        return [
            [(fname, i)] + line_getter(hierarchy[fname], sub)
            for fname, detail in hierarchy.items()
            for i, sub in (
                enumerate([r[2] for r in record.get('children')[fname]._value])
                if isinstance(detail, dict) and fname in record.get('children')
                else []
            )
        ]

    records = {
        **records.get(model, {}),
        **records.get(f"{model}.template", {})
    }
    header_hierarchy = hierarchy(records)
    header = list({fname: True for fname in header_getter(header_hierarchy)})
    header.sort(key=(lambda h: 2 if '@' in h else 1 if '/' in h else 0))
    if 'id' not in header:
        header.insert(0, "id")

    rows = []
    for record in records.values():
        children = record.get('children', {})
        for i, line in enumerate(line_getter(header_hierarchy, record) or [[('root', 0)]]):
            row = []
            for field in header:
                if '/'.join(field.split('/')[:-1]) == '/'.join(p[0] for p in line):
                    sub_rec = record
                    for el, j in line:
                        if el not in sub_rec.get('children', {}):
                            v = ''
                            break
                        sub_rec = sub_rec.get('children')[el]._value[j][2]
                    else:
                        v = sub_rec.get('children', {}).get(field.split('/')[-1], '')
                        v = v and v._value
                    if isinstance(v, list):
                        v = ','.join(
                            id_elem
                            for id_group in ((
                                [_id] if command == 4
                                else value[0] if command == 6
                                else 'UNSUPPORTED COMMAND'
                            ) for command, _id, *value in v)
                            for id_elem in id_group
                        )
                    row.append(v)
                elif i != 0:
                    row.append('')
                elif field == 'id':
                    row.append(unquote_ref(record['id']))
                elif field not in children:
                    row.append('')
                elif isinstance(children[field]._value, list):
                    row.append(','.join(
                        str(s)[1:-1] if str(s).startswith("'") else str(s)
                        for s in children[field]._value[0][2]
                    ))
                else:
                    row.append(children[field]._value)
            rows.append(row)

    header, rows = cleanup_csv(header, rows)
    if not header or not rows:
        return None
    return ('\n'.join(','.join([str(field) for field in row]) for row in [header] + rows)).strip() + '\n'

def convert_csv_to_records(model):
    """
        Convert old CSV to Records, so that it can be further be processed.
        For example, it can be turned into a Python list.
    """
    records = defaultdict(dict)
    for module, lines in read_csv_lines(model):
        header, *rows = lines
        if model == 'account.chart.template':
            header, rows, templates = extract_template_column(header, rows, ('id',), remove=False)
        else:
            header, rows, templates = extract_template_column(header, rows, ('chart_template_id/id', 'chart_template_id:id'))
        id_idx = header.index('id')
        for i, (row, template) in enumerate(zip(rows, templates)):
            _id = row[id_idx]
            records[(module, template)][_id] = Record({'id': _id, 'tag': 'record', 'model': model}, 'record', module)
            for i, field_header in enumerate(header[:len(row)]):
                is_ref = re.match(r'^ref\(.*\)$', row[i], re.I)
                records[(module, template)][_id].append(Field({
                    'id': field_header,
                    'text': row[i] if not is_ref else '',
                    'ref': Ref(row[i]) if is_ref else ''
                }))
    return records
