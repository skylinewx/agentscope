"""Declarative extraction from existing JSON MCP rows, without budget fields."""
from __future__ import annotations

from copy import deepcopy
import json

from ..models import AnalysisError, decimal_value, decimal_text, digest, exact_loads, json_values
from ..query_results import at_pointer
from ..sources import SourceAdapter, source_context, combined_context


class JsonRecordsAdapter(SourceAdapter):
    adapter_id = 'json-records'

    def __init__(self, options=None):
        super().__init__(options)
        allowed = {'rowsPointer', 'idField', 'labelField', 'values', 'unitField', 'periodField',
                   'dimensions', 'completePointer', 'contextPointer', 'memberDimension'}
        if (not isinstance(self.options, dict) or set(self.options) - allowed
                or not isinstance(self.options.get('rowsPointer'), str)
                or not isinstance(self.options.get('idField'), str) or not self.options['idField']
                or not isinstance(self.options.get('values'), dict) or not self.options['values']
                or any(not isinstance(k, str) or not k or not isinstance(v, str) or not v for k, v in self.options['values'].items())):
            raise AnalysisError('INVALID_SOURCE_CONFIG', 'JSON 行适配需要 rowsPointer、idField 和 values 指标到来源字段映射。')
        if (not isinstance(self.options.get('dimensions', {}), dict)
                or any(not isinstance(k, str) or not k or not isinstance(v, str) or not v for k, v in self.options.get('dimensions', {}).items())
                or any(not isinstance(self.options[k], str) or not self.options[k] for k in
                       ('labelField', 'unitField', 'periodField', 'completePointer', 'contextPointer', 'memberDimension') if k in self.options)):
            raise AnalysisError('INVALID_SOURCE_CONFIG', 'JSON 来源字段映射必须是非空字符串。')

    def raw_receipt(self, response):
        candidates = []
        for block in response.get('content', []):
            if block.get('type') != 'text':
                continue
            try:
                parsed = exact_loads(block['text'])
            except AnalysisError:
                continue
            if isinstance(parsed, (dict, list)):
                candidates.append(parsed)
        if len(candidates) != 1:
            raise AnalysisError('RECEIPT_UNAVAILABLE', 'JSON 来源需要唯一完整的 JSON 对象或数组响应。')
        return candidates[0]

    def receipt(self, capture):
        if str(capture.get('captureState', '')).upper() not in ('SUCCESS', 'SUCCEEDED'):
            raise AnalysisError('CAPTURE_INCOMPLETE', '来源工具调用未成功完成，不能作为分析事实。')
        return self.raw_receipt(capture['response'])

    def rows(self, capture, step_id=None):
        if step_id not in (None, 'rows'):
            raise AnalysisError('UNKNOWN_STEP', '该来源的实际 stepId 为 rows。')
        rows = at_pointer(self.receipt(capture), self.options['rowsPointer'])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise AnalysisError('INVALID_SOURCE_ROWS', 'rowsPointer 必须指向来源对象数组。')
        ids = [row.get(self.options['idField']) for row in rows]
        if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
            raise AnalysisError('INVALID_SOURCE_IDENTITY', 'idField 必须提供非空且不重复的来源对象标识。')
        return rows

    def _context(self, capture):
        receipt = self.receipt(capture)
        declared = at_pointer(receipt, self.options['contextPointer']) if 'contextPointer' in self.options else {}
        if not isinstance(declared, dict):
            raise AnalysisError('INVALID_SOURCE_CONTEXT', 'contextPointer 必须指向来源上下文对象。')
        context = {'sourceId': capture['resourceId'], 'snapshotId': digest(receipt),
                   'schemaHash': digest(self.options), 'sourceContext': json_values(declared)}
        return source_context(capture, context)

    def _complete(self, capture):
        return ('completePointer' in self.options
                and at_pointer(self.receipt(capture), self.options['completePointer']) is True)

    def summary(self, receipt):
        return {'status': 'CAPTURED', 'rowsPointer': self.options['rowsPointer'], 'adapter': self.adapter_id}

    def catalog(self, capture):
        context = self._context(capture)
        member_dimension = self.options.get('memberDimension', 'recordId')
        binding = 'dataset_' + digest({'source': context['source'], 'options': self.options})[:24]
        facts = []
        for index, row in enumerate(self.rows(capture)):
            member = row[self.options['idField']]
            dimensions = {member_dimension: member, **{name: row.get(field) for name, field in self.options.get('dimensions', {}).items()}}
            if any(value is not None and not isinstance(value, str) for value in dimensions.values()):
                raise AnalysisError('INVALID_SOURCE_DIMENSIONS', '来源维度必须是字符串，不能由模型改写。')
            period = row.get(self.options.get('periodField'))
            if period is not None and not isinstance(period, str):
                raise AnalysisError('INVALID_SOURCE_PERIOD', '来源时期必须是字符串。')
            unit = row.get(self.options.get('unitField'))
            if unit is not None and not isinstance(unit, str):
                raise AnalysisError('INVALID_SOURCE_UNIT', '来源单位必须是字符串，缺失时保持空值。')
            label = row.get(self.options.get('labelField'))
            label = label if isinstance(label, str) else ''
            for column, (metric, field) in enumerate(self.options['values'].items(), 1):
                key = self.options['rowsPointer'] + '/' + str(index) + '/' + field.replace('~', '~0').replace('/', '~1')
                raw = row.get(field)
                value, state = None, 'NO_VALUE'
                if raw is not None:
                    try:
                        value, state = decimal_text(decimal_value(raw)), 'PRESENT'
                    except AnalysisError:
                        state = 'NOT_APPLICABLE'
                facts.append({'factId': 'fact_' + digest([context['sourceContextId'], key])[:24],
                              'captureRef': capture['captureRef'], 'captureRefs': [capture['captureRef']],
                              'sourceContextId': context['sourceContextId'], 'context': context['context'], 'querySessionId': None,
                              'stepId': 'rows', 'cellKey': key, 'metricKey': field, 'bindingRef': binding,
                              'observationId': context['sourceContextId'], 'contentDigest': context['context']['snapshotId'],
                              'executionId': capture.get('toolCallId'), 'objectId': member, 'objectLabel': label,
                              'objectLabelState': 'RESOLVED' if label else 'MISSING', 'objectModelCode': None,
                              'effectiveDimensions': dimensions, 'dimensions': dimensions,
                              'dimensionModels': {}, 'measureCode': metric, 'period': period,
                              'row': index + 1, 'column': column, 'cellAddress': None, 'headers': [metric],
                              'value': value, 'valueState': state, 'precision': 'raw_json',
                              'rawValueJson': json.dumps(json_values(raw), ensure_ascii=False),
                              'displayValue': value, 'unit': unit, 'scale': '1' if unit else None,
                              'scaleBasis': 'source_unit_label' if unit else None, 'formula': None, 'evidenceIds': []})
        return facts

    def selectors(self, capture):
        rows = self.rows(capture)
        pointer = '/' + self.options['idField'].replace('~', '~0').replace('/', '~1')
        selector = {'captureRef': capture['captureRef'], 'stepId': 'rows'}
        return [{**selector, 'rowCount': len(rows), 'memberPointers': [pointer],
                 'populationSelector': {**selector, 'memberPointer': pointer},
                 'enumerationComplete': self._complete(capture), 'nextSection': 'rows'}]

    def entities(self, capture):
        return [{'key': row[self.options['idField']], 'label': row.get(self.options.get('labelField'), ''),
                 'modelCode': None} for row in self.rows(capture)]

    def population(self, capture, selector, members):
        actual = [at_pointer(row, selector['memberPointer']) for row in self.rows(capture, selector.get('stepId'))]
        if any(not isinstance(value, str) for value in actual) or len(set(actual)) != len(actual) or set(actual) != set(members):
            raise AnalysisError('POPULATION_MISMATCH', '主体 members 必须与来源枚举一致。')
        return self._complete(capture)

    def receipt_info(self, capture):
        return {'sourceAdapter': self.snapshot(), 'context': self._context(capture)['context'], 'querySessionId': None,
                'completeness': {'enumerationExhausted': self._complete(capture)}, 'evidenceIds': []}

    def member_namespace(self, capture, dimension):
        return {**super().member_namespace(capture, dimension), 'tool': capture.get('originalToolName'),
                'rowsPointer': self.options['rowsPointer'], 'idField': self.options['idField']}

    def adapt(self, captures, selections, scope, max_facts):
        if not selections:
            raise AnalysisError('EMPTY_SELECTION', '需要选择实际捕获的来源值。')
        members, references = scope.get('members', []), scope.get('referenceMembers', [])
        if (not isinstance(members, list) or not members or not isinstance(references, list)
                or any(not isinstance(value, str) or not value for value in members + references)
                or len(set(members + references)) != len(members + references)):
            raise AnalysisError('INVALID_SCOPE', '主体与比较对象必须是互不重叠、无重复的来源标识。')
        if scope.get('memberDimension', 'recordId') != self.options.get('memberDimension', 'recordId'):
            raise AnalysisError('INVALID_SCOPE', 'memberDimension 必须与来源适配登记一致。')
        contexts = {ref: self._context(capture) for ref, capture in captures.items()}
        if scope.get('contextMode', 'SINGLE_SNAPSHOT') not in ('SINGLE_SNAPSHOT', 'COMPOSITE'):
            raise AnalysisError('INVALID_SCOPE', 'contextMode 仅支持 SINGLE_SNAPSHOT 或 COMPOSITE。')
        if scope.get('contextMode') != 'COMPOSITE' and len({value['sourceContextId'] for value in contexts.values()}) != 1:
            raise AnalysisError('CONTEXT_CONFLICT', '不同 JSON 来源快照须显式以 COMPOSITE 分组汇编。')
        facts = {}
        catalogs = {ref: {row['cellKey']: row for row in self.catalog(capture)} for ref, capture in captures.items()}
        for selection in selections:
            ref = selection.get('captureRef')
            if ref not in captures or selection.get('stepId') != 'rows':
                raise AnalysisError('UNKNOWN_CAPTURE', '选择器必须使用已捕获来源及实际 stepId。')
            keys = selection.get('cellKeys', [])
            if not keys or len(keys) != len(set(keys)):
                raise AnalysisError('INVALID_SELECTOR', '选择器需要无重复的实际 cellKeys。')
            for key in keys:
                if key not in catalogs[ref]:
                    raise AnalysisError('CELL_NOT_CAPTURED', '所选来源位置未被捕获。')
                fact = deepcopy(catalogs[ref][key])
                if selection.get('bindingRef', fact['bindingRef']) != fact['bindingRef']:
                    raise AnalysisError('REQUEST_TARGET_MISMATCH', '选择器的绑定与来源不一致。')
                if (fact['objectId'] not in members + references
                        or any(fact['dimensions'].get(k) != value for k, value in scope.get('expectedDimensions', {}).items())):
                    raise AnalysisError('SCOPE_MISMATCH', '所选来源值与声明的对象或维度范围不一致。')
                previous = facts.get(fact['factId'])
                if previous:
                    previous['captureRefs'] = sorted(set(previous['captureRefs'] + [ref]))
                else:
                    facts[fact['factId']] = fact
        observed = {fact['objectId'] for fact in facts.values()}
        for member in set(members + references) - observed:
            facts['missing_' + digest(member)[:24]] = {'factId': 'missing_' + digest(member)[:24], 'objectId': member,
                'value': None, 'valueState': 'NOT_QUERIED', 'captureRefs': [], 'unit': None, 'scale': None}
        if len(facts) > max_facts:
            raise AnalysisError('FACT_LIMIT', '来源事实数量超过本次上限。')
        population, complete = scope.get('population'), False
        if population:
            if population.get('captureRef') not in captures:
                raise AnalysisError('UNKNOWN_POPULATION', '范围来源未纳入本次捕获。')
            complete = self.population(captures[population['captureRef']], population, members)
        selected_complete = observed == set(members + references)
        coverage = 'COMPLETE' if complete and selected_complete else 'UNKNOWN' if selected_complete else 'PARTIAL'
        return {'facts': list(facts.values()), 'context': combined_context(list(contexts.values())),
                'sourceContexts': list({item['sourceContextId']: item for item in contexts.values()}.values()),
                'scope': {**scope, 'scopeId': 'scope_' + digest(scope)[:24], 'coverage': coverage,
                          'excludedMembers': scope.get('excludedMembers', []),
                          'populationComplete': complete, 'selectedDataComplete': selected_complete, 'sourcePagesComplete': True,
                          'observedMembers': sorted(set(members) & observed), 'pendingMembers': sorted(set(members) - observed),
                          'observedReferenceMembers': sorted(set(references) & observed),
                          'pendingReferenceMembers': sorted(set(references) - observed), 'semanticCompleteness': 'UNVERIFIED'}}
