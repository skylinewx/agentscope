"""面向模型的有界结果投影；冻结产物与页面数据集始终保存完整内容。"""
from copy import deepcopy
import json

from .models import digest


def project_results(values, compact_values, analysis_ref, previous_values=()):
    previous = {value['derivedId']: digest(value) for value in previous_values}
    changes = {'new': [], 'changed': [], 'unchanged': []}
    projected = []
    preview_budget = 12000
    for value, compact in zip(values, compact_values):
        ref = value['derivedId']
        state = 'new' if ref not in previous else 'unchanged' if previous[ref] == digest(value) else 'changed'
        changes[state].append(ref)
        item = deepcopy(compact)
        # 宿主按分析版本保存真实依赖；模型用结果 ID 继续引用，不反复抄依赖清单。
        for key in ('sourceFactIds', 'orderedFactIds', 'unavailableFactIds', 'selectedCoverageRefs'):
            if key in item:
                item[key + 'Count'] = len(value.get(key, []))
                item.pop(key)
        if len(item.get('inputRefs', [])) > 4:
            item['inputCount'] = len(value['inputRefs'])
            item.pop('inputRefs')
        if state == 'unchanged' and value.get('calculationValidation') == 'PASS':
            item = {key: item[key] for key in ('derivedId', 'operator', 'value', 'unit', 'executionStatus',
                    'calculationValidation', 'valueState', 'statistics', 'counts', 'outputKind', 'rowCount',
                    'checkScope', 'sourceConsistency', 'completionReason') if key in item}
            item['unchanged'] = True
            if 'formulaTrace' in value:
                trace = value['formulaTrace']
                item['formulaTrace'] = {key: deepcopy(trace[key]) for key in
                    ('targetFactRef', 'status', 'targetLocalCheck', 'completionReason') if key in trace}
        elif 'formulaTrace' in item:
            trace = item['formulaTrace']
            fields = ('targetFactRef', 'status', 'meaning', 'applicabilityStatus', 'targetLocalCheck', 'completionReason')
            concise = {key: trace[key] for key in fields if key in trace}
            concise['nodeCount'] = len(value['formulaTrace'].get('nodes', []))
            concise['nodes'] = []
            for node in trace.get('nodes', [])[:4]:
                record = {key: node[key] for key in ('factRef', 'headers', 'address', 'expression', 'status',
                          'observed', 'recalculated', 'difference') if key in node}
                check = node.get('localCheck') or {}
                record['localCheck'] = {key: check[key] for key in ('status', 'value', 'difference', 'completionReason', 'diagnostic') if key in check}
                # status 是逐层计算状态；显式标明含义，防止误作本层检查状态。
                if 'status' in record:
                    record['recursiveStatus'] = record.pop('status')
                concise['nodes'].append(record)
            item['formulaTrace'] = concise
        # 预算只限制可恢复的预览，不截断金额、核对状态、差额或错误根因，也不拒绝计算。
        preview = {key: item[key] for key in ('rows', 'data', 'text') if key in item}
        if 'formulaTrace' in item:
            preview['nodes'] = item['formulaTrace'].get('nodes', [])
        preview_size = len(json.dumps(preview, ensure_ascii=False, separators=(',', ':'))) if preview else 0
        if preview_size > preview_budget:
            for key in ('rows', 'data', 'text'):
                item.pop(key, None)
            if 'nodes' in preview:
                item['formulaTrace']['nodes'] = []
                item['formulaTrace']['nodesPreviewOmitted'] = True
            if value.get('outputKind') in ('TABLE', 'CHART_DATA', 'DIAGNOSTIC', 'FILE'):
                item['payloadComplete'] = False
            item['previewOmitted'] = True
        else:
            preview_budget -= preview_size
        item['detailRead'] = {'section': 'derived', 'analysisRef': analysis_ref, 'search': ref}
        if value.get('operator') == 'FORMULA_CHECK' and value.get('formulaTrace'):
            item['detailRead'] = {'section': 'formulas', 'analysisRef': analysis_ref,
                                  'factRef': value['formulaTrace']['targetFactRef']}
        elif value.get('operator') in ('SORT', 'RANK'):
            item['detailRead'] = {'section': 'order', 'analysisRef': analysis_ref, 'derivedId': ref}
        elif value.get('outputKind') in ('TABLE', 'CHART_DATA'):
            item['detailRead'] = {'section': 'products', 'analysisRef': analysis_ref, 'derivedId': ref}
            if state == 'unchanged':
                item['payloadComplete'] = False
        item.pop('nextRead', None)
        projected.append(item)
    failed = [value for value in values if value.get('executionStatus') == 'BLOCKED']
    summary = {'operationCount': len(values), 'completedCount': len(values) - len(failed), 'blockedCount': len(failed),
               'blockedOperations': [{'valueRef': value['derivedId'], 'codes': [item['code'] for item in value.get('diagnostics', [])]}
                                     for value in failed],
               'repeatedFailureCount': sum(any(item.get('details', {}).get('repeatedRequest') for item in value.get('diagnostics', [])) for value in failed)}
    return projected, changes, summary
