"""Small business projections; canonical execution evidence remains unchanged."""
import ast
from copy import deepcopy
import json


def report_source_tables(facts):
    """List the reported task and form names once per distinct pair of source labels."""
    pairs = dict.fromkeys((fact.get('taskName'), fact.get('formName')) for fact in facts
                          if fact.get('taskName') or fact.get('formName'))
    return [{'taskName': task, 'formName': form} for task, form in pairs]


def business_memberships(items):
    """Keep distinct input groups and roles without per-cell host receipts."""
    return [{key: item[key] for key in ('inputRef', 'inputRole', 'grain') if key in item}
            for item in items or []]


def row_ranges(numbers):
    """Compact consecutive positions only; preserve order and repetitions."""
    groups = []
    for number in numbers:
        if groups and number == groups[-1][-1] + 1:
            groups[-1].append(number)
        else:
            groups.append([number])
    return '、'.join(str(group[0]) if len(group) == 1 else f'{group[0]}—{group[-1]}' for group in groups)


def repeated_input_block(signatures, prior_inputs, read_fields):
    """Locate one complete earlier input in this run, preserving order, types and multiplicity."""
    for previous, fields, run_index in sorted(prior_inputs, key=lambda entry: -len(entry[0])):
        if len(previous) > len(signatures) or not read_fields <= fields:
            continue
        try:
            start = signatures.index(previous[0])
        except ValueError:
            continue
        end = start + len(previous)
        if signatures[start:end] == previous:
            return run_index, start, end
    return None


def direct_input_fields(code):
    """Return proven literal record accesses, or None for dynamic/escaped inputs.

    This only decides whether redundant field-code columns may be omitted. It
    does not certify a calculation or change the executed Python input.
    """
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, TypeError):
        return None
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    records = set()
    fields = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ('eval', 'exec', 'globals', 'locals'):
            return None
        if isinstance(node, ast.Name) and node.id == 'inputs' and isinstance(node.ctx, ast.Load):
            parent = parents.get(node)
            if not isinstance(parent, (ast.For, ast.comprehension)) or parent.iter is not node or not isinstance(parent.target, ast.Name):
                return None
            records.add(parent.target.id)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Name) or node.id not in records or not isinstance(node.ctx, ast.Load):
            continue
        parent = parents.get(node)
        if isinstance(parent, ast.Subscript) and parent.value is node:
            key = parent.slice
        elif isinstance(parent, ast.Attribute) and parent.value is node and parent.attr == 'get':
            call = parents.get(parent)
            if not isinstance(call, ast.Call) or call.func is not parent or not call.args:
                return None
            key = call.args[0]
        else:
            return None
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            return None
        fields.add(key.value)
    # Absence of directly tracked inputs is not proof that a helper ignored them.
    return fields if records else None


def compact_business_scopes(scopes):
    """Deduplicate identical scope descriptions, never observations or members."""
    result, seen = [], set()
    transport = {'metrics', 'querySessionId', 'executionId', 'bindingRef', 'budTaskId'}
    for original in scopes:
        scope = deepcopy(original)
        if isinstance(scope.get('request'), dict):
            scope['request'] = {key: child for key, child in scope['request'].items() if key not in transport}
        if isinstance(scope.get('rangeSummary'), dict):
            # Retain actual exclusions, not batch alignment / matching counters.
            scope['rangeSummary'] = {key: child for key, child in scope['rangeSummary'].items()
                if key not in ('matchedTargetCount', 'alignedRowCount', 'dimensions')
                and not (key == 'filteredOutRowCount' and child == 0)}
        if scope.get('returnedRowCount') == scope.get('selectedRowCount'):
            scope.pop('returnedRowCount', None)
            scope.pop('selectedRowCount', None)
        for key in ('missingSelectedCellCount', 'excludedRowCount'):
            if scope.get(key) == 0:
                scope.pop(key, None)
        scope = {key: child for key, child in scope.items() if child not in ({}, [])}
        identity = json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        if scope and identity not in seen:
            seen.add(identity)
            result.append(scope)
    return result
