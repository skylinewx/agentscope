"""Layout-neutral, exact datasets for model-authored documents."""
from copy import deepcopy
from decimal import Decimal, DecimalException, Inexact, localcontext
import re

from .comparison_scenes import affine
from .formulas import Expression
from .formatting import number_text, validate_number_format
from .models import AnalysisError, decimal_value, decimal_text


def compile_datasets(artifact, specifications):
    if not isinstance(specifications, list) or len(specifications) > 50:
        raise AnalysisError('INVALID_DOCUMENT_DATASET', 'datasets 最多 50 个集合。', {'path': '/document/datasets'})
    facts = {f['factId']: f for f in artifact['facts']}
    derived = {v['derivedId']: v for v in artifact['derivedValues']}
    result, refs = {}, set()
    for index, spec in enumerate(specifications):
        path = f'/document/datasets/{index}'
        try:
            if not isinstance(spec, dict) or set(spec) - {'key', 'valueRef', 'nodeRefs', 'format'}:
                raise AnalysisError('INVALID_DOCUMENT_DATASET', '集合仅接受 key/valueRef/nodeRefs/format。')
            key = spec.get('key')
            if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', key) or key in result:
                raise AnalysisError('INVALID_DOCUMENT_DATASET', '集合 key 必须唯一，使用字母开头的 1～64 位标识。')
            ref = spec.get('valueRef')
            value = derived.get(ref) if isinstance(ref, str) else None
            if value is None:
                raise AnalysisError('INVALID_VALUE_REF', '集合 valueRef 引用当前版本的 SORT/RANK、FORMULA_CHECK 或 RECONCILE 结果。')
            if value.get('calculationValidation') != 'PASS':
                raise AnalysisError('UNSUPPORTED_CLAIM', '该计算未通过执行条件检查，不能绑定集合。')
            fmt = spec.get('format')
            if fmt is not None: validate_number_format(fmt)
            if 'nodeRefs' in spec and value['operator'] != 'FORMULA_CHECK':
                raise AnalysisError('INVALID_DOCUMENT_DATASET', 'nodeRefs 仅用于 FORMULA_CHECK。')
            if value['operator'] in ('SORT', 'RANK'):
                data = {'kind': 'ORDERED', 'direction': value.get('direction', 'desc'),
                        'rows': [fact_record(facts[ref], fmt) for ref in value['orderedFactIds']],
                        'unavailable': [fact_record(facts[ref], fmt) for ref in value.get('unavailableFactIds', [])]}
            elif value['operator'] == 'FORMULA_CHECK':
                data = formula_dataset(value, facts, spec.get('nodeRefs'), fmt)
            elif value['operator'] == 'RECONCILE':
                data = {'kind': 'RECONCILIATION', 'parent': fact_record(facts[value['inputRefs'][0]], fmt),
                        'children': [fact_record(facts[ref], fmt) for ref in value['inputRefs'][1:]],
                        'relationRef': value.get('relationRef') or value.get('reconciliation', {}).get('relationRef'),
                        'status': value['reconciliation']['status'], 'expectation': value['reconciliation']['expectation'],
                        'values': {key: amount(value['reconciliation'].get(source), fmt)
                                   for key, source in (('sourceValue', 'parentValue'), ('childSum', 'childSum'), ('difference', 'difference'))}}
            else:
                raise AnalysisError('INVALID_DOCUMENT_DATASET', '集合只接受排序、公式追溯或汇总核对结果；标量使用 bindings。')
            data['valueRef'] = value['derivedId']
            result[key] = data
            refs.add(value['derivedId'])
        except AnalysisError as exc:
            raise AnalysisError(exc.code, str(exc), {**(exc.details or {}), 'path': path}) from exc
    return result, refs


def amount(raw, fmt):
    return {'value': raw, 'display': '' if raw is None else number_text(raw, fmt, grouping=True)}


def fact_record(fact, fmt):
    return {'factRef': fact['factId'], 'objectId': fact['objectId'], 'objectLabel': fact.get('objectLabel', ''),
            'headers': deepcopy(fact.get('headers', [])), 'unit': fact.get('unit') or '',
            'valueState': fact['valueState'], **amount(fact['value'] if fact['valueState'] == 'PRESENT' else None, fmt)}


def formula_dataset(value, facts, node_refs, fmt):
    trace = value['formulaTrace']
    known = {node['factRef']: node for node in trace['nodes']}
    if node_refs is None:
        node_refs = [ref for ref, node in known.items() if node.get('expression') and not node.get('boundary')]
    if (not isinstance(node_refs, list) or not node_refs or len(node_refs) > 200
            or any(not isinstance(ref, str) or ref not in known for ref in node_refs)
            or len(set(node_refs)) != len(node_refs)):
        raise AnalysisError('INVALID_DOCUMENT_DATASET', 'nodeRefs 选择实际 formulaTrace.nodes[].factRef，不能重复；每层保留全部直接操作数。')
    nodes = []
    for ref in node_refs:
        node = known[ref]
        if not node.get('expression') or node.get('boundary'):
            raise AnalysisError('INVALID_DOCUMENT_DATASET', 'nodeRefs 须选择可展开的公式节点，叶子/外部边界不会生成计算层。')
        operands = [{**fact_record(facts[dep['factRef']], fmt)} for dep in node['dependencies']]
        address_refs = {dep['address']: dep['factRef'] for dep in node['dependencies']}
        tree = Expression(node['expression']).tree
        item = {**fact_record(facts[ref], fmt), 'operands': operands,
                'expressionTree': reference_tree(tree, address_refs),
                'values': {key: amount(raw, fmt) for key, raw in {
                    'sourceValue': node.get('observed'), 'localValue': node.get('localCheck', {}).get('value'),
                    'localDifference': node.get('localCheck', {}).get('difference'),
                    'recursiveValue': node.get('recalculated'), 'recursiveDifference': node.get('difference'),
                    'propagatedDifference': node.get('propagatedDifference')}.items()},
                'differenceOriginRefs': deepcopy(node.get('differenceOriginRefs', [])),
                'additive': False}
        # Exact coefficients are an optional capability, never a business attribution.
        try:
            with localcontext() as context:
                context.prec = 512
                context.traps[Inexact] = True
                coefficients = {}
                for address, coefficient in affine(tree):
                    coefficients[address] = coefficients.get(address, Decimal(0)) + coefficient
                contributions = []
                for dep, operand in zip(node['dependencies'], operands):
                    coefficient = coefficients.get(dep['address'], Decimal(0))
                    raw = None if operand['value'] is None else decimal_text(coefficient * decimal_value(operand['value']))
                    contributions.append({'factRef': dep['factRef'], 'coefficient': decimal_text(coefficient), **amount(raw, fmt)})
                item.update(additive=True, contributions=contributions,
                            constant=amount(decimal_text(coefficients.get(None, Decimal(0))), fmt))
        except (AnalysisError, DecimalException):
            pass  # A nonlinear formula retains its tree and operands, without fake additive bars.
        nodes.append(item)
    return {'kind': 'FORMULA', 'targetFactRef': trace['targetFactRef'], 'nodes': nodes,
            'differenceConvention': 'source minus calculated; local uses direct source inputs; recursive expands internal formulas'}


def reference_tree(tree, references):
    """Keep operator precedence and fact identity, without cell expressions in UI data."""
    kind = tree[0]
    if kind == 'ref': return {'kind': 'ref', 'factRef': references[tree[1]]}
    if kind == 'number': return {'kind': 'number', 'value': tree[1]}
    if kind == 'sum': return {'kind': 'sum', 'operands': [reference_tree(t, references) for t in tree[1]]}
    if kind == 'unary': return {'kind': 'unary', 'operator': tree[1], 'operand': reference_tree(tree[2], references)}
    return {'kind': 'binary', 'operator': tree[1], 'left': reference_tree(tree[2], references),
            'right': reference_tree(tree[3], references)}
