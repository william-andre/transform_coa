#!/usr/bin/env python3
# pylint: skip-file

import ast
from collections import defaultdict
from copy import deepcopy
import io
import logging
import os
from pathlib import Path
import re

from lxml import etree
import polib

import transform_models
from transform_tools import unquote_ref, Unquoted, indent, pformat, save_new_file, ref_module
from transform_csv import convert_csv_to_records, convert_records_to_csv
from mapping import MAPPING

PYTHON_HEADER = "# Part of Odoo. See LICENSE file for full copyright and licensing details.\n"

_logger = logging.getLogger(__name__)

self = locals().get('self') or {}
env = locals().get('env') or {}


# -----------------------------------------------

def parse_file(filename):
    module = str(filename).split('/')[-3]
    if not module.startswith('l10n_'): return {}
    with open(filename, 'rb') as file:
        file_read = file.read().split(b'\n')
        try:
            root = etree.fromstring(file_read[0] + b'&#xA;'.join(file_read[1:-1]) + file_read[-1])
            original_root = etree.fromstring(file_read[0] + b'&#xA;'.join(file_read[1:-1]) + file_read[-1])
        except etree.XMLSyntaxError:
            root = etree.fromstring(b'\n'.join(file_read))
            original_root = etree.fromstring(b'\n'.join(file_read))


    nodes_tree = []
    stack = [(nodes_tree, root)]
    parent = nodes_tree
    while stack:

        # Pop an element out of the stack
        parent, el = stack.pop(0)

        # Create a new node and attach it to the parent
        if el.tag in ('record', 'function'):
            node = transform_models.Record(el, el.tag, module)
            node._filename = filename
            parent.append(node)
        elif el.tag == 'delete':
            node = transform_models.Record(el, el.tag, module)
        elif el.tag == 'field':
            node = transform_models.Field(el)
            parent.append(node)
        else:
            node = parent

        # Cleanup files
        if el.tag == 'function' and el.attrib.get('name') == 'try_loading':
            chart_id = el[0].get('eval').split('(')[1][1:].split(')')[0][:-1]
            if len(el.getchildren()) == 2:
                el.addnext(etree.XML(f"""
    <function model="account.chart.template" name="try_loading">
        <value eval="[]"/>
        <value>{MAPPING[ref_module(chart_id, module)]}</value>
        {etree.tostring(el.getchildren()[1]).decode().strip()}
    </function>
"""))

        if (
            el.getparent()
            and isinstance(node, transform_models.Record)
            and node._from
            and (node._from.endswith('.template') or node._from == 'account.tax.group')
        ):
            el.getparent().remove(el)

        # Populate the stack with the node's children
        stack = [(node, child) for child in el] + stack

    is_empty = lambda node: node.tag in ('odoo', 'data') and all(is_empty(sub) or sub.tag == etree.Comment for sub in node)
    if not is_empty(root):
        if etree.tostring(original_root) != etree.tostring(root):
            with open(filename, 'w') as file:
                file.write('<?xml version="1.0" encoding="utf-8"?>\n' + etree.tostring(
                    root,
                    encoding='utf-8',
                ).decode().replace('&#10;', '\n') + '\n')
    else:
        os.remove(filename)

    for record in nodes_tree:
        yield record['id'], record

def get_xml_records():
    records = defaultdict(dict)
    for filename in Path.cwd().glob(f'../odoo/addons/*/data/*.xml'):
        module = str(filename).split('/')[-3]
        try:
            for key, value in parse_file(filename):
                template = value.get('_template')
                if value['tag'] == 'function':
                    continue
                if key not in records[(module, template)]:
                    records[(module, template)][key] = value
                elif 'children' in value:
                    # if the id is already present, merge the fields
                    for _id, field in value['children'].items():
                        records[(module, template)][key]['children'][_id] = field
        except etree.ParseError as e:
            _logger.warning("Invalid XML file %s, %s", filename, e)
    for filename in Path.cwd().glob(f'../odoo/addons/*/demo/*.xml'):
        try:
            all(parse_file(filename))
        except etree.ParseError as e:
            _logger.warning("Invalid XML file %s, %s", filename, e)
    return records

# -----------------------------------------------------------

def split_template_from_company(all_records, module):
    company_record = transform_models.ResCompany({'model': 'res.company'}, 'Record', module)
    company_fields = (
        'country_id',
        'bank_account_code_prefix',
        'cash_account_code_prefix',
        'transfer_account_code_prefix',
        'default_pos_receivable_account_id',
        'account_default_pos_receivable_account_id',
        'income_currency_exchange_account_id',
        'expense_currency_exchange_account_id',
        'account_journal_suspense_account_id',
        'account_journal_early_pay_discount_loss_account_id',
        'account_journal_early_pay_discount_gain_account_id',
        'account_journal_payment_debit_account_id',
        'account_journal_payment_credit_account_id',
        'default_cash_difference_income_account_id',
        'default_cash_difference_expense_account_id',
    )
    chart_templates = all_records.get('account.chart.template', {})
    for _record_id, record in chart_templates.items():
        record['children'].pop('id', None)
        for field_id, field in record.get('children', {}).items():
            if re.match('.*account_.*id.*', field_id):
                record['children'][field_id]._value = unquote_ref(field._value)
        for field_id in company_fields:
            if field_id in record.get('children', {}):
                child = record['children'].pop(field_id)
                # Convert the account_fiscal_country_id which was still named country_id in the ACT
                if field_id == 'country_id':
                    child['id'] = 'account_fiscal_country_id'
                company_record.append(child)
    if company_record.get('children'):
        company_record['id'] = Unquoted("self.env.company.id")
        all_records['res.company'] = {company_record['id']: company_record}
    return all_records

def cleanup_tax_tags(all_records):
    tags = {
        k: v
        for records in all_records.values()
        for report in records.get('account.report', {}).values()
        for k, v in report.get_tags().items()
    }
    for records in all_records.values():
        taxes = records.get('account.tax', {}).values()
        many_fields = [line for x in taxes for lines in x.get_repartition_lines() for line in lines]
        for token in many_fields:
            if len(token) == 3 and token[0] == 0:
                token[2].cleanup_tags(tags)

def merge_fpos(all_records):
    all_fpos = {
        id: fpos
        for records in all_records.values()
        for id, fpos in {
            **records.get('account.fiscal.position', {}),
            **records.get('account.fiscal.position.template', {}),
        }.items()
    }
    for (module, template), records in all_records.items():
        for model in ['account.fiscal.position.tax', 'account.fiscal.position.tax.template']:
            for record in records.pop(model, {}).values():
                _id = ref_module(str(record['children'].pop('position_id')._original_value), module)
                if 'tax_ids' not in all_fpos[_id]['children']:
                    all_fpos[_id].append(transform_models.Field({'id': 'tax_ids', 'eval': '[]'}))
                all_fpos[_id]['children']['tax_ids']._value.append((0, 0, record))
        for model in ['account.fiscal.position.account', 'account.fiscal.position.account.template']:
            for record in records.pop(model, {}).values():
                _id = ref_module(str(record['children'].pop('position_id')._original_value), module)
                if 'account_ids' not in all_fpos[_id]['children']:
                    all_fpos[_id].append(transform_models.Field({'id': 'account_ids', 'eval': '[]'}))
                all_fpos[_id]['children']['account_ids']._value.append((0, 0, record))

def merge_reco_model(all_records):
    all_models = {
        id: model
        for records in all_records.values()
        for id, model in {
            **records.get('account.reconcile.model', {}),
            **records.get('account.reconcile.model.template', {}),
        }.items()
    }

    for (module, template), records in all_records.items():
        for model in ['account.reconcile.model.line', 'account.reconcile.model.line.template']:
            for record in records.pop(model, {}).values():
                _id = ref_module(str(record['children'].pop('model_id')._original_value), module)
                if 'line_ids' not in all_models[_id]['children']:
                    all_models[_id].append(transform_models.Field({'id': 'line_ids', 'eval': '[]'}))
                all_models[_id]['children']['line_ids']._value.append((0, 0, record))



def load_translations(module):
    paths = Path.cwd().glob(f'../odoo/addons/{module}/i18n*/*.po*')
    translations = defaultdict(dict)
    for path in paths:
        pofile = polib.pofile(path)
        original_pofile = polib.pofile(path)
        for entry in pofile:
            if entry.msgstr:
                translations[entry.msgid][path.stem] = entry.msgstr
            entry.occurrences = [
                occurrence
                for occurrence in entry.occurrences
                if 'model:account.account,' not in occurrence[0]
                and 'model:account.account.template,' not in occurrence[0]
                and 'model:account.group,' not in occurrence[0]
                and 'model:account.group.template,' not in occurrence[0]
                and 'model:account.tax,' not in occurrence[0]
                and 'model:account.tax.template,' not in occurrence[0]
                and 'model:account.tax.group,' not in occurrence[0]
                and 'model:account.tax.group.template,' not in occurrence[0]
                and 'model:account.fiscal.position,' not in occurrence[0]
                and 'model:account.fiscal.position.template,' not in occurrence[0]
                and 'model:account.chart.template,' not in occurrence[0]
            ]
            if not entry.occurrences:
                entry.obsolete = True
        for entry in pofile.obsolete_entries():
            pofile.remove(entry)
        if pofile != original_pofile:
            with open(path, 'w') as file:
                file.write(str(pofile))
    return translations

def read_data():
    def merge(module, template, model, id, values):
        id = ref_module(id, module)
        if model.endswith('.template') and model != 'account.chart.template':
            model = model[:-9]
        if template:
            template = ref_module(str(template), module)
            if model in all_records[(module, None)] and id in all_records[(module, None)][model]:
                merge(module, template, model, id, all_records[(module, None)][model].pop(id))
        else:
            for m, t in all_records:
                if m == module and model in all_records[(m, t)] and id in all_records[(m, t)][model]:
                    template = t
                    break

        if model not in all_records[(module, template)]:
            all_records[(module, template)][model] = {}
        if id not in all_records[(module, template)][model]:
            all_records[(module, template)][model][id] = values
        else:
            all_records[(module, template)][model][id]['children'].update(values['children'])

    all_records = defaultdict(dict)
    for model in [
        "account.fiscal.position",
        "account.fiscal.position.tax",
        "account.fiscal.position.account",
        "account.tax",
        "account.account",
        "account.group",
        "account.tax.group",
        "account.chart.template",
    ]:
        for (module, template), values in convert_csv_to_records(model).items():
            for value in values.values():
                merge(module, template, model, value['id'], value)
    for (module, template), values in get_xml_records().items():
        for value in values.values():
            merge(module, template, value['_model'], value['id'], value)

    for (module, template), records in all_records.items():
        split_template_from_company(records, module)
    cleanup_tax_tags(all_records)
    merge_fpos(all_records)
    merge_reco_model(all_records)

    return all_records


def do_translate():
    """
        Translate an old Chart Template from a module to a new set of files and a Python class.
    """
    all_records = read_data()
    for (module, old_template), records in all_records.items():
        if old_template is None:
            continue
        if not old_template:
            print('missing template on', [key for records in records.values() for key in records.keys()])
            continue
        assert 'account.tax.group' not in records
        records['account.tax.group'] = all_records.get((module, None), {}).get('account.tax.group', {})
        template = MAPPING[old_template]
        translations = load_translations(module)

        for model in ['account.account', 'account.group', 'account.tax.group', 'account.tax', 'account.fiscal.position', 'account.reconcile.model']:
            if model in records:
                for record in records[model].values():
                    if record['children'] and record['children']['name']._value in translations:
                        for lang, translated in translations[record['children']['name']._value].items():
                            lang_field = f"name@{lang}"
                            record['children'][lang_field] = transform_models.Field({
                                'id': lang_field,
                                'text': translated,
                            })

        # Move tax group properties on the tax group
        for tax_group in records['account.tax.group'].values():
            for field in [
                'property_tax_receivable_account_id',
                'property_tax_payable_account_id',
                'property_advance_tax_payment_account_id',
            ]:
                if field in tax_group['children']:
                    new_name = field[9:]
                    tax_group['children'][new_name] = tax_group['children'].pop(field)

        for chart in records['account.chart.template'].values():
            for field in [
                'property_tax_receivable_account_id',
                'property_tax_payable_account_id',
                'property_advance_tax_payment_account_id',
            ]:
                if field in chart['children']:
                    new_name = field[9:]
                    value = chart['children'][field]._value
                    for tax_group in records['account.tax.group'].values():
                        if new_name not in tax_group['children']:
                            tax_group['children'][new_name] = transform_models.Field({
                                'id': new_name,
                                'ref': value,
                            })
                    del chart['children'][field]

        # CSV files
        for model in ['account.account', 'account.group', 'account.tax.group', 'account.tax', 'account.fiscal.position']:
            content = convert_records_to_csv(records, model)
            if content:
                save_new_file(f"../odoo/addons/{module}/data/template/", f"{model}-{template}.csv", content)

        # XML files
        contents = {}
        mapping = {
            "account.chart.template":           f"_get_{template}_template_data",
            "res.company":                      f"_get_{template}_res_company",
            "account.reconcile.model":          f"_get_{template}_reconcile_model",
            "account.reconcile.model.line":     f"_get_{template}_reconcile_model_line",
            "account.fiscal.position.tax":      f"_get_{template}_fiscal_position_tax",
            "account.fiscal.position.account":  f"_get_{template}_fiscal_position_account",
        }
        for model, function_name in mapping.items():
            for model_name in (model, model + '.template'):
                one_level = model_name == 'account.chart.template'
                content = convert_records_to_function(
                    records,
                    model_name,
                    function_name,
                    template,
                    one_level=one_level)
                if content:
                    contents[function_name] = contents.get(function_name, "") + content

        content = ""
        if contents:
            content += "\n".join(contents.values())

        content = PYTHON_HEADER + (
            f"from odoo import models{', Command' if 'Command.' in content else ''}\n"
            "from odoo.addons.account.models.chart_template import template\n"
            "\n\n"
            "class AccountChartTemplate(models.AbstractModel):\n"
            "    _inherit = 'account.chart.template'\n\n"
        ) + content

        template_module_name = f"template_{template}"
        save_new_file(f"../odoo/addons/{module}/models/", f"{template_module_name}.py", content)
        ensure_import(f'../odoo/addons/{module}/__init__.py', 'models')
        ensure_import(f'../odoo/addons/{module}/models/__init__.py', template_module_name)

        cleanup_manifest(module)


def convert_records_to_function(all_records, model, function_name, template, one_level=False):
    """Convert a set of Records to a Python function."""
    records = all_records.get(model, {})
    if not records and model != 'account.tax':
        return ''

    stream = io.StringIO()
    if one_level:
        record = None
        records.pop('try_loading', None)
        stream.write(pformat(list(records.items())[0][1], level=2).lstrip())
    else:
        stream.write("{\n")
        for record in records.values():
            if model != 'res.company':
                key = Unquoted(f"'{record['id'].replace('.', '_')}'")
            else:
                key = record['id']
            value = pformat(record, level=3).strip()
            stream.write(indent(3, f"{key}: {value},\n"))
        stream.write(indent(2, "}\n"))
    body = stream.getvalue()

    stream = io.StringIO()
    stream.write(
        indent(1, f"@template('{template}', '{model}')\n" if model != 'account.chart.template' else f"@template('{template}')\n")
        + indent(1, f"def {function_name}(self):\n")
        + indent(2, "return ")
        + body
    )
    return stream.getvalue()


def ensure_import(path: str, import_name: str):
    path = Path.cwd() / path
    if not path.exists():
        with open(path, 'w', encoding="utf-8") as init_file:
            init_file.write(PYTHON_HEADER + f"from . import {import_name}\n")
            return
    with open(path, encoding="utf-8") as init_file:
        init_tree = ast.parse(init_file.read())
    import_idx = next((i for i, n in enumerate(init_tree.body) if isinstance(n, ast.ImportFrom)), 0)
    try:
        next(i for i, n in enumerate(init_tree.body) if isinstance(n, ast.ImportFrom) and n.names[0].name == import_name)
        import_idx = -1
    except StopIteration:
        pass
    if import_idx != -1:
        init_tree.body.insert(import_idx, ast.ImportFrom('.', [ast.alias(name=import_name)]))
        with open(path, 'w', encoding="utf-8") as init_file:
            init_file.write(PYTHON_HEADER + ast.unparse(init_tree) + '\n')

def cleanup_manifest(module):
    manifest_path = Path.cwd() / f'../odoo/addons/{module}/__manifest__.py'
    if not manifest_path.exists():
        return
    with open(manifest_path, 'r') as manifest:
        vals = eval(manifest.read())
        original_vals = deepcopy(vals)
    if 'data' in vals:
        for value in list(vals['data']):
            data_path = Path.cwd() / f'../odoo/addons/{module}/{value}'
            if not data_path.exists():
                vals['data'].remove(value)
        if not vals['data']:
            del vals['data']
    if 'l10n_multilang' in vals['depends']:
        vals['depends'].remove('l10n_multilang')
        if 'account' not in vals['depends']:
            vals['depends'].append('account')
    if original_vals != vals:
        with open(manifest_path, 'w') as manifest:
            manifest.write(PYTHON_HEADER + pformat(vals))

# -----------------------------------------------------------

if __name__ == '__main__':
    do_translate()
