"""Review source context and complete Python executions without duplicating both copies."""
from copy import deepcopy


def calculation_review_parts(payload, encode, limit, max_parts):
    runs = payload.get('pythonRuns', [])
    if not runs or any(not run.get('runRef') or not isinstance(run.get('inputRecords'), list) or not run['inputRecords']
                       or run.get('fileReads') or run.get('inputDeliverySummary', {}).get('sources') for run in runs):
        return None
    # Each execution snapshot is indivisible. Source rows and their original
    # scope are checked separately; an arbitrary sample is never a substitute.
    common = {'allTargetsPreserved': True, 'allPartsRequired': True,
              'pythonRunRefs': [run['runRef'] for run in runs]}
    source = {**payload, 'pythonRuns': [], 'evidencePartition': {**common,
        'kind': 'SOURCE_AND_DELIVERY',
        'meaning': '本段保留全部来源事实、范围、计算结果和交付全文；Python 代码及完整执行输入在另段核对。'
                   '不要把本段未重复的代码视为缺失，也不能仅凭本段认定脚本逻辑通过。'}}
    execution = {**payload, 'facts': [], 'sourceScopes': [], 'structureEvidence': [],
        'evidencePartition': {**common, 'kind': 'COMPLETE_PYTHON_EXECUTIONS',
        'completeCalculationInputs': True,
        'meaning': '本段保留 Python 的完整执行输入、代码、结果及交付全文；inputRecords.ref 对应来源事实或计算引用。'
                   '全部来源事实、原查询范围及表结构在来源段核对，未在本段重复不表示缺失。'
                   '核对实际完整输入、空值、单位、代码逻辑及输出；执行完成本身不证明计算正确。'}}
    parts = [source, execution]
    if len(encode(source)) > limit - 500:
        return None
    if len(encode(execution)) > limit - 500:
        # Keep a run and all of its outputs together, including actual snapshots
        # of upstream derived inputs. Never split a global calculation's rows.
        parts = [source]
        for run in runs:
            part = {**execution, 'pythonRuns': [run], 'derived': [value for value in payload.get('derived', [])
                    if value.get('pythonRunRef') == run['runRef']],
                    'evidencePartition': {**execution['evidencePartition'], 'includedRunRef': run['runRef']}}
            if len(encode(part)) > limit - 500:
                return None
            parts.append(part)
    if len(parts) > max_parts:
        return None
    return [encode({**part, 'evidencePartition': {**deepcopy(part['evidencePartition']),
                    'index': index + 1, 'count': len(parts)}}) for index, part in enumerate(parts)]
