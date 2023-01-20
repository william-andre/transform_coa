#!/usr/bin/env python3
# pylint: skip-file

import io
from pathlib import Path


def get_command(x):
    return ['create', 'update', 'delete', 'unlink', 'link', 'clear', 'set'][x]


def pformat(item, level=0, stream=None):

    def pformat_field_record(value, stream):
        stream.write(pformat(value.get('children', {}), level=level))

    def pformat_tuple_list(value, stream):
        start, end = '[]' if isinstance(value, list) else '()'
        stream.write(indent(level, start + '\n'))
        is_o2m = all([isinstance(sub, (tuple, list))
                      and len(sub) in (2, 3)
                      and isinstance(sub[0], int)
                      for sub in value])
        for i, subitem in enumerate(value):
            if is_o2m:
                subitem = list(subitem)
                if subitem == [5, 0, 0]:
                    value_str = Unquoted("Command.clear()")
                elif len(subitem) == 3:
                    subvalue_str = pformat(subitem[2], level+1).strip()
                    name = get_command(subitem[0])
                    value_str = Unquoted(f"Command.{name}({subvalue_str})")
                elif len(subitem) in (2, 3) and subitem[0] == 4:
                    subvalue_str = pformat(subitem[1], level+1).strip()
                    name = get_command(subitem[0])
                    value_str = Unquoted(f"Command.{name}({subvalue_str})")
            else:
                value_str = repr(subitem)
            comma = ',' if i < len(value) else ''
            stream.write(indent(level + 1, f"{value_str}{comma}\n"))
        stream.write(indent(level, end))

    def pformat_dict(value, stream):
        stream.write(indent(level, '{\n'))
        for i, (key, subitem) in enumerate(value.items()):
            if isinstance(subitem, Field):
                subvalue = subitem._value
            else:
                subvalue = subitem
            value_str = pformat(subvalue, level + 1).lstrip()
            comma = ',' if i < len(value) else ''
            stream.write(indent(level + 1, f"{repr(key)}: {value_str}{comma}\n"))
        stream.write(indent(level, '}\n'))

    def pformat_str(value, stream):
        if '\n' in value:
            stream.write(f'''"""{value}"""''')
        else:
            stream.write(repr(value))

    stream = stream or io.StringIO()
    mapping = [
        ((Field, Record), pformat_field_record),
        ((tuple, list), pformat_tuple_list),
        ((dict,), pformat_dict),
        ((str,), pformat_str),
    ]
    for types, formatter in mapping:
        if isinstance(item, types):
            formatter(item, stream)
            break
    else:
        stream.write(indent(level, repr(item)))

    return stream.getvalue()


def unquote_ref(value):
    return f"{str(value).split('.')[-1]}"

def ref_module(value, module):
    return value if '.' in str(value) else f"{module}.{value}"

class Unquoted(str):
    def __init__(self, value):
        super().__init__()
        self._value = value
    def __repr__(self):
        return self._value

class Ref():
    def __init__(self, value):
        self.value = str(value)
    def __repr__(self):
        return f"'{self.value}'"
    def __str__(self):
        return self.value

def indent(level=0, content="", indent_size=4):
    return f"{' ' * level * indent_size}{content}"

def save_new_file(path, filename, content):
    path = Path.cwd() / path
    if not path.is_dir():
        path.mkdir()
    with open(str(path / filename), 'w', encoding="utf-8") as outfile:
        outfile.write(content)

from transform_models import Field, Record
