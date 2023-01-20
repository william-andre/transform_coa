#!/usr/bin/env python3
# pylint: skip-file

import re
import sys

sys.path.insert(0, "../odoo")
from odoo import Command
from odoo.tools.safe_eval import safe_eval
from mapping import MAPPING

if sys.version_info >= (3, 11):
    from odoo.tools.safe_eval import _SAFE_OPCODES, to_opcodes
    _SAFE_OPCODES.update(to_opcodes(['CALL', 'PUSH_NULL', 'PRECALL', 'RESUME', 'BINARY_OP', 'KW_NAMES']))

class Node(dict):
    def __init__(self, el):
        super().__init__({'id': el.get('id', el.get('name'))})

    def append(self, child):
        children = self.get('children') or []
        children.append(child)
        self['children'] = children

class Field(Node):
    def __init__(self, el):
        super().__init__(el)
        text = (el.get('text') or (hasattr(el, 'text') and el.text) or '').strip()
        ref = el.get('ref', '').strip()
        _eval = el.get('eval', '')
        if el.get('unquoted', ''):
            text = Unquoted(text)
        if isinstance(_eval, str):
            _eval = _eval.strip()

        if text:
            self._value = text
            self.value_type = 'text'
        elif ref:
            self._value = Ref(ref)
            self.value_type = 'ref'
        elif _eval:
            if 'time.' in _eval or 'obj().' in _eval or 'DateTime.' in _eval:
                self._value = Unquoted(_eval)
            else:
                self._value = safe_eval(_eval, globals_dict={'ref': Ref, 'Command': Command})
            self.value_type = 'eval'
        else:
            self._value = None
            self.value_type = None
        self._original_value = self._value


# Records -----------------------------------------------

class Record(Node):
    _from = None
    def __init__(self, el, tag, module):
        super().__init__(el)
        self['tag'] = tag
        self['_model'] = el.get('model')
        if self['_model'] == 'account.chart.template' and el.get('id'):
            self['_template'] = ref_module(el.get('id'), module)
        self['_module'] = module
        def get_all_subclasses(cls):
            subclass_list = []
            def recurse(klass):
                for subclass in klass.__subclasses__():
                    subclass_list.append(subclass)
                    recurse(subclass)
            recurse(cls)
            return subclass_list
        subclasses = get_all_subclasses(Record)
        mapping = {cls._from: cls for cls in subclasses if cls._from}
        target_cls = mapping.get(self['_model'])
        if target_cls:
            self.__class__ = target_cls

    def append(self, child):
        if not isinstance(child, Field):
            raise ValueError(f"Wrong child type {type(child)}, {child.get('_model')}")
        children = self.get('children') or {}
        child = self.cleanup(child)
        if not child.get('delete'):
            children[child.get('id')] = child
        self['children'] = children

    def cleanup(self, child):
        value = child._value
        record_id = child.get('id')
        if isinstance(value, str) and value.upper() in ('TRUE', 'FALSE'):
            child._value = {'TRUE': True, 'FALSE': False}.get(value.upper())
        if isinstance(value, str) and value == "None":
            child._value = None
        elif record_id == 'sequence':
            child._value = int(child._value or 0)
        elif record_id == 'amount':
            child._value = float(child._value)
        elif record_id == 'default_pos_receivable_account_id':
            child['id'] = 'account_default_pos_receivable_account_id'
        elif record_id == 'chart_template_id':
            self['_template'] = child._value
            child['delete'] = True
        elif record_id == 'note':
            child['delete'] = True
        elif record_id == 'nocreate':
            child['delete'] = True
        elif record_id == 'id':
            child['delete'] = True
        return child

    def cleanup_o2m(self, child, cls=None):
        value = child._value

        def cleanup_sub(fields, cls):
            sub = cls({'id': None, 'model': cls._from}, cls.__name__, self['_module'])
            for key, value in fields.items():
                sub.append(Field({'id': key, 'eval': repr(value)}))
            return sub

        if isinstance(value, (tuple, list)) and value and isinstance(value[0], str):
            value = [Command.set([
                ref_module(v, self['_module'])
                for v in value
                if v
            ])]
        elif isinstance(value, str):
            value = ','.join(
                ref_module(v, self['_module'])
                for v in value.split(',')
                if v
            )
        elif isinstance(value, (tuple, list)):
            value = [v for v in value if v[0] != Command.CLEAR]
            for i, sub in enumerate(value):
                if (isinstance(sub, (list, tuple)) and
                    len(sub) == 3 and
                    sub[0] == Command.CREATE and
                    sub[1] == 0 and
                    isinstance(sub[2], dict)):
                    if cls:
                        sub = [sub[0], sub[1], cleanup_sub(sub[2], cls)]
                        value[i] = sub
                elif (isinstance(sub, (list, tuple)) and
                      len(sub) == 3 and
                      sub[0] == Command.SET and
                      sub[1] == 0 and
                      isinstance(sub[2], (list, tuple))):
                    if cls:
                        sub = [sub[0], sub[1], [unquote_ref(x) for x in sub[2]]]
                        value[i] = sub
                    else:
                        value[i] = (Command.SET, 0, [ref_module(x, self['_module']) for x in sub[2]])
                elif (isinstance(sub, (list, tuple)) and
                      len(sub) in (2, 3) and
                      sub[0] == Command.LINK):
                      value[i] = (Command.LINK, ref_module(sub[1], self['_module']))
        return value

class TemplateData(Record):
    _from = 'account.chart.template'
    def cleanup(self, child):
        child = super().cleanup(child)
        record_id = child.get('id')
        if record_id in ('spoken_languages', 'currency_id'):
            child['delete'] = True
        if record_id == 'parent_id':
            child['id'] = 'parent'
            child._value = MAPPING[f"{self['_module']}.{child._value}"]
        if record_id == 'name' and len(MAPPING[self.get('_template')]) == 2:
            child['delete'] = True
        return child

class AccountReconcileModel(Record):
    _from = 'account.reconcile.model.template'

class AccountReconcileModelLine(Record):
    _from = 'account.reconcile.model.line.template'

    def cleanup(self, child):
        child = super().cleanup(child)
        record_id = child.get('id')
        if record_id == 'tax_ids':
            child._value = self.cleanup_o2m(child, AccountTax)
        return child

class ResCompany(Record):
    _from = 'res.company'

class ResCountryGroup(Record):
    _from = 'res.country.group'
    def cleanup(self, child):
        child = super().cleanup(child)
        record_id = child.get('id')
        if record_id in ('country_ids'):
            child._value = self.cleanup_o2m(child)
        return child

class AccountTax(Record):
    _from = 'account.tax.template'
    def cleanup(self, child):
        child = super().cleanup(child)
        record_id = child.get('id')
        if record_id.endswith('_id'):
            # TODO: lazy that converts only if xmlid in known data
            child._value = unquote_ref(str(child._value).split('.')[-1])
        elif record_id in ('invoice_repartition_line_ids', 'refund_repartition_line_ids'):
            child._value = self.cleanup_o2m(child, AccountTaxRepartitionLine)
            child['id'] = 'repartition_line_ids'
            for _, _, rep_line in child._value:
                rep_line['children']['document_type'] = Field({'id': 'document_type', 'text': record_id.split('_')[0]})
        elif record_id == 'children_tax_ids':
            child._value = self.cleanup_o2m(child, AccountTax)
        elif record_id == 'price_include':
            child._value = bool(child._value)
        return child

    def get_repartition_lines(self):
        for name, child in self['children'].items():
            if 'repartition_line_ids' in name:
                yield child._value

    def append(self, child):
        children = self.get('children') or {}
        previous_rep_lines = children['repartition_line_ids'] if 'repartition_line_ids' in children else False
        super().append(child)
        if previous_rep_lines and child.get('id') == 'repartition_line_ids':
            children['repartition_line_ids']._value = previous_rep_lines._value + children['repartition_line_ids']._value

class AccountTaxRepartitionLine(Record):
    _from = 'account.tax.repartition.line'
    def cleanup(self, child):
        child = super().cleanup(child)
        record_id = child.get('id')
        if record_id == 'account_id':
            child._value = child._value and unquote_ref(child._value)
        elif record_id in ('plus_report_expression_ids', 'minus_report_expression_ids'):
            values = [f"'{x}'" for x in child._value]
            child._value = Unquoted(', '.join(values))
        elif record_id == 'tag_ids':
            child._value = self.cleanup_o2m(child)
        return child

    def cleanup_tags(self, tags):
        tokens = []
        to_be_removed = []
        for name, child in self.get('children', {}).items():
            if name in ('plus_report_expression_ids', 'minus_report_expression_ids'):
                sign = '+' if name == 'plus_report_expression_ids' else '-'
                unformatted = [tags[ref_module(x, self['_module'])] for x in re.findall("'([^']+)'", child._value)]
                tokens += [f"{sign}{t}" for t in unformatted]
                to_be_removed.append(name)
        for name in to_be_removed:
            del self['children'][name]
        if tokens:
            self['children']['tag_ids'] = Field({'id': 'tag_ids', 'text': "||".join(tokens), 'unquoted': True})

class AccountFiscalPosition(Record):
    _from = 'account.fiscal.position'
    def cleanup(self, child):
        child = super().cleanup(child)
        record_id = child.get('id')
        if child._value is None:
            child['delete'] = True
        elif record_id in ('country_id', 'country_group_id'):
            child._value = Ref(child._value)
        elif record_id in ('vat_required', 'auto_apply'):
            child._value = int(child._value)
        return child

class AccountFiscalPositionTemplate(AccountFiscalPosition):
    _from = 'account.fiscal.position.template'

class AccountAccount(Record):
    _from = 'account.account'
    def cleanup(self, child):
        child = super().cleanup(child)
        record_id = child.get('id')
        if record_id == 'tax_ids':
            child._value = self.cleanup_o2m(child, AccountTax)
        elif record_id == 'tag_ids':
            child._value = self.cleanup_o2m(child)
        return child

class AccountAccountTemplate(AccountAccount):
    _from = 'account.account.template'

class AccountGroup(Record):
    _from = 'account.group'

class AccountGroupTemplate(AccountAccount):
    _from = 'account.group.template'

class AccountTaxGroup(Record):
    _from = 'account.tax.group'

class AccountFiscalPositionTaxTemplate(Record):
    _from = 'account.fiscal.position.tax.template'
    def cleanup(self, child):
        child = super().cleanup(child)
        record_id = child.get('id')
        if record_id in ('position_id', 'tax_src_id', 'tax_dest_id'):
            child._value = unquote_ref(child._value)
        return child

class AccountFiscalPositionAccountTemplate(Record):
    _from = 'account.fiscal.position.account.template'
    def cleanup(self, child):
        child = super().cleanup(child)
        record_id = child.get('id')
        if record_id in ('position_id', 'account_src_id', 'account_dest_id'):
            child._value = unquote_ref(child._value)
        return child

class AccountReport(Record):
    _from = 'account.report'
    def cleanup(self, child):
        child = super().cleanup(child)
        record_id = child.get('id')
        if record_id == 'line_ids':
            child._value = self.cleanup_o2m(child)
        return child

    def get_lines(self):
        for name, child in self['children'].items():
            if name == 'line_ids':
                yield from child['children']
                break

    def get_tags(self):
        tags = {}
        for line in self.get_lines():
            tags.update(line.get_tags())
        return tags

class AccountReportLine(Record):
    _from = 'account.report.line'
    def cleanup(self, child):
        child = super().cleanup(child)
        record_id = child.get('id')
        if record_id == 'sequence':
            child._value = int(child._value)
        return child

    def get_lines(self):
        for name, child in self.get('children', {}).items():
            if name == 'children_ids':
                yield from child['children']
                break

    def get_expressions(self):
        for name, child in self.get('children', {}).items():
            if name == 'expression_ids':
                yield from child['children']
                break

    def get_tags(self):
        tags = {}
        expressions = self.get_expressions()
        for expression in expressions:
            tags[ref_module(expression['id'], self['_module'])] = expression.get_tag_name()
        for line in self.get_lines():
            subtags = line.get_tags()
            tags.update(subtags)
        return tags

class AccountReportExpression(Record):
    _from = 'account.report.expression'

    def get_tag_name(self):
        for name, child in self.get('children', {}).items():
            if name == 'formula':
                return child._value


from transform_tools import Unquoted, Ref, unquote_ref, ref_module
