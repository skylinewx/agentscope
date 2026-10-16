"""Bounded inspection and arithmetic checks of captured, local cell formulas.

No query, expression evaluation, coordinate guessing or business rule inference.
Cell addresses must come from source row/column index labels (or a registered
coordinate contract supplied by the adapter). Missing metadata stays a gap.
"""
from __future__ import annotations

from decimal import Decimal, Inexact, localcontext, DecimalException
import re

from .models import AnalysisError, decimal_text, decimal_value, exact_loads, precise_sum

TOKEN = re.compile(r'\s*(\$?[A-Za-z]{1,4}\$?[1-9][0-9]{0,6}|[0-9]+(?:\.[0-9]+)?|SUM|[()+*/,\-:])', re.I)
ADDRESS = re.compile(r'[A-Z]{1,4}[1-9][0-9]{0,6}')
MAX_NODES = 1000


def source_expression(raw):
    if raw in (None, ''):
        return None
    if not isinstance(raw, str) or len(raw) > 16000:
        raise AnalysisError('FORMULA_UNSUPPORTED', '来源公式超出支持的文本范围。')
    expression = raw.strip()
    if expression.startswith(('[', '{', '"')):
        try:
            metadata = exact_loads(expression)
        except AnalysisError as exc:
            raise AnalysisError('FORMULA_UNSUPPORTED', '来源公式元数据无法解析。') from exc
        if isinstance(metadata, list):
            if len(metadata) != 1:
                raise AnalysisError('FORMULA_AMBIGUOUS', '来源存在多个候选公式，尚未证明实际适用公式。')
            metadata = metadata[0]
        if isinstance(metadata, dict):
            if set(metadata) - {'f', 'p', 'cr', 'ct', 'c'} or not isinstance(metadata.get('f'), str):
                raise AnalysisError('FORMULA_AMBIGUOUS', '来源公式包含未确认的条件或未知元数据。')
            expression = metadata['f']
        elif isinstance(metadata, str):
            expression = metadata
        else:
            raise AnalysisError('FORMULA_UNSUPPORTED', '来源公式元数据不是可识别的表达式。')
    return expression.removeprefix('=').strip()


class Expression:
    def __init__(self, source):
        if not isinstance(source, str) or len(source) > 16000:
            raise AnalysisError('FORMULA_LIMIT', '公式文本超过支持范围。')
        self.source, self.tokens, self.position, self.references = source, [], 0, []
        offset = 0
        while offset < len(source):
            match = TOKEN.match(source, offset)
            if not match:
                if source[offset:].strip() == '': break
                code = 'FORMULA_EXTERNAL_REFERENCE' if any(c in source for c in ('!', '[', ']')) else 'FORMULA_UNSUPPORTED'
                raise AnalysisError(code, '公式含跨表引用或不支持的表达式；保留来源公式，不执行。')
            self.tokens.append(match[1].replace('$', '').upper())
            offset = match.end()
            if len(self.tokens) > 2000:
                raise AnalysisError('FORMULA_LIMIT', '公式项数超过支持范围。')
        self.tree = self.expression(0)
        if self.position != len(self.tokens):
            raise AnalysisError('FORMULA_UNSUPPORTED', '公式表达式未完整解析。')

    def peek(self):
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def take(self):
        value = self.peek()
        self.position += 1
        return value

    def expression(self, depth, minimum=0):
        if depth > 64:
            raise AnalysisError('FORMULA_LIMIT', '公式嵌套超过支持范围。')
        token = self.take()
        if token in ('+', '-'):
            left = ('unary', token, self.expression(depth + 1, 3))
        elif token == '(':
            left = self.expression(depth + 1)
            if self.take() != ')': raise AnalysisError('FORMULA_UNSUPPORTED', '公式括号不完整。')
        elif token == 'SUM':
            if self.take() != '(': raise AnalysisError('FORMULA_UNSUPPORTED', 'SUM 需要括号。')
            args = []
            while True:
                start = self.peek()
                if start and ADDRESS.fullmatch(start) and self.position + 1 < len(self.tokens) and self.tokens[self.position + 1] == ':':
                    self.take(); self.take()
                    end = self.take()
                    if not end or not ADDRESS.fullmatch(end): raise AnalysisError('FORMULA_UNSUPPORTED', '公式范围不完整。')
                    refs = expand_range(start, end)
                    self.references.extend(refs)
                    if len(self.references) > MAX_NODES:
                        raise AnalysisError('FORMULA_LIMIT', '公式引用总数超过支持范围。')
                    args.extend(('ref', ref) for ref in refs)
                else:
                    args.append(self.expression(depth + 1))
                next_token = self.take()
                if next_token == ')': break
                if next_token != ',': raise AnalysisError('FORMULA_UNSUPPORTED', 'SUM 参数不完整。')
            left = ('sum', args)
        elif token and ADDRESS.fullmatch(token):
            self.references.append(token)
            if len(self.references) > MAX_NODES:
                raise AnalysisError('FORMULA_LIMIT', '公式引用总数超过支持范围。')
            left = ('ref', token)
        elif token and re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', token):
            left = ('number', token)
        else:
            raise AnalysisError('FORMULA_UNSUPPORTED', '公式含不支持的语法。')
        while self.peek() in ('+', '-', '*', '/'):
            operator = self.peek()
            level = 1 if operator in ('+', '-') else 2
            if level < minimum: break
            self.take()
            left = ('binary', operator, left, self.expression(depth + 1, level + 1))
        return left

    def evaluate(self, resolve):
        def visit(node, depth=0):
            if depth > 128:
                raise AnalysisError('FORMULA_LIMIT', '公式运算层数超过支持范围。')
            kind = node[0]
            if kind == 'number': return decimal_value(node[1])
            if kind == 'ref': return resolve(node[1])
            if kind == 'sum': return sum((visit(arg, depth + 1) for arg in node[1]), Decimal(0))
            if kind == 'unary':
                value = visit(node[2], depth + 1); return value.copy_negate() if node[1] == '-' else value
            left, right = visit(node[2], depth + 1), visit(node[3], depth + 1)
            if node[1] == '+': return left + right
            if node[1] == '-': return left - right
            if node[1] == '*': return left * right
            if right == 0: raise AnalysisError('FORMULA_ZERO_DIVISOR', '来源公式的除数为零。')
            value = left / right
            if value * right != left:
                raise AnalysisError('FORMULA_ROUNDING_UNPROVEN', '公式除法需要明确舍入规则，不能自行决定精度。')
            return value
        try:
            with localcontext() as context:
                context.prec = 512
                result = visit(self.tree)
                if context.flags[Inexact]:
                    raise AnalysisError('FORMULA_PRECISION_LIMIT', '公式计算超出精确数值支持范围。')
                return decimal_value(result)
        except DecimalException as exc:
            raise AnalysisError('FORMULA_NUMERIC_ERROR', '来源公式不能完成精确运算。') from exc


def expand_range(start, end):
    def split(address):
        column, row = re.fullmatch(r'([A-Z]+)([0-9]+)', address).groups()
        number = 0
        for char in column: number = number * 26 + ord(char) - 64
        return number, int(row)
    c1, r1 = split(start); c2, r2 = split(end)
    if c2 < c1 or r2 < r1 or (c2 - c1 + 1) * (r2 - r1 + 1) > MAX_NODES:
        raise AnalysisError('FORMULA_LIMIT', '公式范围无效或超过支持数量。')
    def column(number):
        value = ''
        while number:
            number, rem = divmod(number - 1, 26); value = chr(65 + rem) + value
        return value
    return [column(c) + str(r) for r in range(r1, r2 + 1) for c in range(c1, c2 + 1)]


def trace_formula(facts, target_ref):
    known = {f['factId']: f for f in facts}
    if target_ref not in known:
        raise AnalysisError('UNKNOWN_VALUE', '公式目标必须引用本次分析的来源事实。')
    target = known[target_ref]
    same_table = [f for f in facts if all(f.get(k) == target.get(k) for k in ('observationId', 'bindingRef', 'objectId', 'sourceOrigins', 'context', 'sourceContextId'))]
    addresses = {}
    for fact in same_table:
        if fact.get('cellAddress'):
            addresses.setdefault(fact['cellAddress'], []).append(fact)
    nodes, pending, active, complete, checked = [], [], set(), {}, set()

    def visit(fact, depth=0):
        fid = fact['factId']
        if fid in complete: return complete[fid]
        if fid in active:
            raise AnalysisError('FORMULA_CYCLE', '表内公式存在循环引用。')
        if depth > 64 or len(nodes) >= MAX_NODES:
            raise AnalysisError('FORMULA_LIMIT', '表内公式依赖超过本次核对容量。')
        checked.add(fid); active.add(fid)
        node = {'factRef': fid, 'objectLabel': fact.get('objectLabel', ''), 'headers': fact.get('headers', []),
                'cellKey': fact.get('cellKey'), 'address': fact.get('cellAddress'), 'expression': None,
                'dependencies': [], 'observed': fact.get('value'), 'recalculated': None, 'difference': None,
                'status': 'BLOCKED', 'diagnostics': [],
                'localCheck': {'status': 'BLOCKED', 'value': None, 'difference': None},
                'propagatedDifference': None, 'differenceOriginRefs': []}
        nodes.append(node)
        try:
            if fact.get('valueState') != 'PRESENT' or fact.get('precision') == 'display_only':
                raise AnalysisError('FORMULA_VALUE_UNAVAILABLE', '公式引用包含缺值、冲突或仅有显示精度的单元格。')
            if (fact.get('unit'), fact.get('scale')) != (target.get('unit'), target.get('scale')):
                raise AnalysisError('FORMULA_UNIT_CONFLICT', '表内公式项的单位或缩放标注不一致。')
            observed = decimal_value(fact['value'])
            source = source_expression(fact.get('formula'))
            node['expression'] = source
            if not source:
                node['status'] = 'SOURCE_VALUE'
                node['localCheck']['status'] = 'NOT_APPLICABLE'
                complete[fid] = observed
                return observed
            try:
                expression = Expression(source)
            except AnalysisError as exc:
                if exc.code == 'FORMULA_EXTERNAL_REFERENCE' and depth > 0:
                    # This operator traces the current table. A dependency whose
                    # own formula leaves the table is a captured-value boundary,
                    # not a fabricated zero or an evaluated external formula.
                    node.update(status='SOURCE_VALUE', boundary='EXTERNAL_REFERENCE', numericRole='INCLUDED_SOURCE_VALUE')
                    node['localCheck']['status'] = 'NOT_APPLICABLE'
                    complete[fid] = observed
                    return observed
                raise
            missing = False
            for address in dict.fromkeys(expression.references):
                candidates = addresses.get(address, [])
                dependency = {'address': address, 'factRef': candidates[0]['factId'] if len(candidates) == 1 else None}
                node['dependencies'].append(dependency)
                if len(candidates) != 1:
                    pending.append({'targetFactRef': fid, 'address': address,
                                    'reason': 'AMBIGUOUS_CELL' if len(candidates) > 1 else 'DEPENDENCY_NOT_RESOLVED'})
                    missing = True
            # Local arithmetic uses immediate captured inputs; recursive arithmetic
            # substitutes in-table formulas. A child mismatch must not be labelled
            # as a mismatch in this node's own equation.
            if not missing:
                try:
                    local_values = {}
                    for dep in node['dependencies']:
                        child = known[dep['factRef']]
                        if child.get('valueState') != 'PRESENT' or child.get('precision') == 'display_only':
                            raise AnalysisError('FORMULA_VALUE_UNAVAILABLE', '本层公式输入数值不可用。')
                        if (child.get('unit'), child.get('scale')) != (target.get('unit'), target.get('scale')):
                            raise AnalysisError('FORMULA_UNIT_CONFLICT', '本层公式输入的单位或缩放不一致。')
                        local_values[dep['address']] = decimal_value(child['value'])
                    local_result = expression.evaluate(local_values.__getitem__)
                    local_difference = precise_sum([observed, local_result.copy_negate()])
                    node['localCheck'] = {'status': 'MATCH' if local_difference == 0 else 'MISMATCH',
                                          'value': decimal_text(local_result), 'difference': decimal_text(local_difference)}
                except AnalysisError as exc:
                    node['localCheck']['diagnostic'] = exc.as_dict()
            values = {}; child_failed = False
            for dependency in node['dependencies']:
                if dependency['factRef'] is None: continue
                try: values[dependency['address']] = visit(known[dependency['factRef']], depth + 1)
                except AnalysisError as exc:
                    child_failed = True
                    if exc.code in ('FORMULA_CYCLE', 'FORMULA_LIMIT'): node['diagnostics'].append(exc.as_dict())
            if missing:
                raise AnalysisError('FORMULA_DEPENDENCY_MISSING', '必要依赖尚未取得或缺少来源坐标映射；不能把缺项当作零。')
            if child_failed:
                raise AnalysisError('FORMULA_DEPENDENCY_BLOCKED', '下层公式仍有断点，本层不能视为完整复算。')
            result = expression.evaluate(values.__getitem__)
            difference = precise_sum([observed, result.copy_negate()])
            node.update(recalculated=decimal_text(result), difference=decimal_text(difference), status='MATCH' if difference == 0 else 'MISMATCH')
            if node['localCheck']['value'] is not None:
                node['propagatedDifference'] = decimal_text(precise_sum([
                    decimal_value(node['localCheck']['value']), result.copy_negate()]))
            complete[fid] = result
            return result
        except AnalysisError as exc:
            node['diagnostics'].append(exc.as_dict())
            raise
        finally:
            active.discard(fid)
    try:
        computed = visit(target)
        state = 'MISMATCH' if any(n['status'] == 'MISMATCH' for n in nodes) else 'MATCH' if target.get('formula') else 'NO_FORMULA'
    except AnalysisError:
        computed, state = None, 'BLOCKED'
    by_ref = {n['factRef']: n for n in nodes}
    def origins(ref, visited):
        if ref in visited: return set()
        visited.add(ref)
        n = by_ref.get(ref)
        if n is None: return set()
        result = {ref} if n['localCheck']['status'] == 'MISMATCH' else set()
        for dep in n['dependencies']: result.update(origins(dep['factRef'], visited))
        return result
    from .comparisons import comparison
    for n in nodes:
        n['differenceOriginRefs'] = sorted(origins(n['factRef'], set()))
        n['comparisons'] = {
            'local': comparison(n['observed'], n['localCheck']['value'], 'source', 'immediate_inputs_calculation'),
            'recursive': comparison(n['observed'], n['recalculated'], 'source', 'recursive_calculation')}
    return {'targetFactRef': target_ref, 'status': state,
            'executionStatus': 'BLOCKED' if state == 'BLOCKED' else 'COMPLETED',
            'sourceConsistency': state if state in ('MATCH', 'MISMATCH') else 'NOT_CHECKED',
            'targetLocalCheck': nodes[0]['localCheck'],
            'comparisons': nodes[0]['comparisons'],
            'differenceOriginRefs': nodes[0]['differenceOriginRefs'],
            'boundaryMeaning': 'external_formulas_not_expanded_but_captured_values_included', 'nodes': nodes, 'pendingDependencies': pending,
            'externalBoundaries': [n['factRef'] for n in nodes if n.get('boundary') == 'EXTERNAL_REFERENCE'],
            'sourceFactIds': sorted(checked), 'value': decimal_text(computed) if computed is not None else None,
            'meaning': 'captured_formula_arithmetic_not_business_cause',
            'applicability': '按捕获的唯一公式及来源坐标复算，不证明来源系统实际执行了该公式。'}
