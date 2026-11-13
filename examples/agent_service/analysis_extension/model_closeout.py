"""在既有材料上生成一次可读收尾；不重新查数，不把原始 JSON 当成分析交付。"""
import json
from collections import Counter, defaultdict, deque
from copy import deepcopy
from time import perf_counter
from uuid import uuid4


from .models import AnalysisError, json_values, digest
from .review_control import progress
from .review_encoding import pack_review_records


def artifact_closeout_records(artifact):
    """Keep all business facts and local checks; omit repeated transport/provenance details."""
    from .review_revision import formula_review_trace
    from .formula_check_summary import summarize
    fields = ('factId', 'sourceContextId', 'objectId', 'objectLabel', 'objectLabelState', 'sourceObjectId',
              'objectModelCode', 'inputRole', 'headers', 'measureCode', 'period', 'dataPeriod', 'executionPeriod',
              'dimensions', 'effectiveDimensions', 'dimensionNames', 'value', 'valueType', 'valueState',
              'unit', 'unitBasis', 'scale', 'scaleBasis', 'precision', 'formula', 'formulaStatus', 'expression',
              'taskName', 'formName', 'cellAddress')
    facts = [{key: deepcopy(fact[key]) for key in fields if key in fact} for fact in artifact.get('facts', [])]
    computed = deepcopy(artifact.get('derivedValues', []))
    for value in computed:
        if value.get('operator') == 'FORMULA_CHECK' and value.get('formulaTrace'):
            value['checkSummary'] = summarize(value['formulaTrace'])
            value['formulaTrace'] = formula_review_trace(value['formulaTrace'])
    return facts, computed


def query_closeout_facts(sources):
    """Repeated identical business observations are evidence, not extra members."""
    facts, seen = [], {}
    for source in sources:
        if source.get('diagnostic'):
            continue
        for fact in source.get('facts', []):
            context = source.get('contexts', {}).get(fact.get('contextRef'), {})
            if not context or not fact.get('dimensions'):
                facts.append(deepcopy(fact))
                continue
            # Actual dimensions are already resolved on each fact. Opening
            # defaults and transport IDs cannot create a second business row.
            business_context = {key: context[key] for key in
                ('taskName', 'formName', 'target', 'period', 'executionPeriod') if key in context}
            pinned = (source.get('source') or {}).get('context') or {}
            source_boundary = {key: pinned[key] for key in ('sourceId', 'snapshotId', 'asOf', 'schemaHash') if key in pinned}
            identity = digest([source.get('sourceConnection'), source_boundary, business_context,
                {key: value for key, value in fact.items() if key not in ('factId', 'contextRef', 'originRefs', 'captureRefs')}])
            observation = {'captureRef': source.get('captureRef'), 'factId': fact.get('factId')}
            if identity in seen:
                seen[identity]['observations'].append(observation)
            else:
                item = {**deepcopy(fact), 'observations': [observation]}
                seen[identity] = item
                facts.append(item)
    return facts


def closeout_material(turn, evidence=None, artifact=None):
    """保留身份和条件；有容量边界时明确材料不全，禁止伪装成全量分析。"""
    material = {'userRequest': turn.state.get('request'), 'goal': turn.state.get('goal'),
                'queryIntent': turn.state.get('queryIntent'), 'inheritedScope': turn.state.get('inheritedScope'),
                'facts': [], 'computed': [], 'contexts': [], 'calculations': [], 'omittedCount': 0,
                'reviewIssues': [item for review in turn.state.get('reviews', [])[-1:] for item in review.get('issues', [])]}
    if artifact:
        material.update(scope=artifact.get('scope'), checks=artifact.get('checks'), partialReason=artifact.get('partialReason'))
        facts, computed = artifact_closeout_records(artifact)
        material['projection'] = 'All business facts and per-layer checks retained; repeated transport metadata omitted.'
        material['reviewIssues'].extend(deepcopy(progress(turn).get('lastContentReview', {}).get('issues', [])))
    else:
        evidence = evidence or {}
        facts = query_closeout_facts(evidence.get('sources', []))
        material['contexts'] = [source.get('contexts', {}) for source in evidence.get('sources', []) if source.get('facts')]
        roots = set(evidence.get('resultRoots', []))
        computed = [output for run in evidence.get('pythonRuns', []) for output in run.get('outputs', [])
                    if output.get('derivedId') in roots]
        material['calculations'] = [{key: run.get(key) for key in ('runRef', 'purpose', 'code', 'inputRefs', 'fileReads')}
                                    for run in evidence.get('pythonRuns', [])
                                    if any(output.get('derivedId') in roots for output in run.get('outputs', []))]
    # 先无损共享重复字段，让主体概览和构成同时进入材料，避免逐条加长字段导致只剩首张表。
    if not facts and not computed:
        raise AnalysisError('NO_AVAILABLE_FACTS', '当前尚无足够的已取得数据用于解释本题。')
    full = json_values({**material, 'facts': facts, 'computed': computed})
    if len(json.dumps(pack_review_records(full), ensure_ascii=False, separators=(',', ':'))) <= 60000:
        return full
    # Truly oversized material must represent every context and value state,
    # rather than filling the budget with the first (possibly empty) task.
    # Counts are from the full captured set; the rows remain an explicit sample.
    groups, coverage = defaultdict(deque), {}
    for fact in facts:
        context_ref = fact.get('contextRef', fact.get('sourceContextId', ''))
        state = fact.get('valueState', 'UNSPECIFIED')
        groups[(context_ref, state)].append(fact)
        group = coverage.setdefault(context_ref, {'contextRef': context_ref, 'availableCount': 0,
                                                  'includedCount': 0, 'valueStates': Counter()})
        group['availableCount'] += 1
        group['valueStates'][state] += 1
    material['factCoverage'] = list(coverage.values())
    material_size = len(json.dumps(json_values(material), ensure_ascii=False))
    def round_robin():
        pending = deque(groups.values())
        while pending:
            group = pending.popleft()
            yield group.popleft()
            if group:
                pending.append(group)

    for field, records in (('computed', computed), ('facts', round_robin())):
        for record in records:
            value = json_values(record)
            size = len(json.dumps(value, ensure_ascii=False)) + 2
            if material_size + size > 60000:
                material['omittedCount'] += 1
            else:
                material[field].append(value)
                material_size += size
                if field == 'facts':
                    coverage[record.get('contextRef', record.get('sourceContextId', ''))]['includedCount'] += 1
    if not material['facts'] and not material['computed']:
        raise AnalysisError('NO_AVAILABLE_FACTS', '当前尚无足够的已取得数据用于解释本题。')
    if len(json.dumps(material, ensure_ascii=False)) > 65000:
        raise AnalysisError('CLOSEOUT_CAPACITY', '当前问题的必要背景超过收尾容量，已有资料已保存。')
    return material


async def model_closeout(agent, turn, reason, *, evidence=None, artifact=None):
    """Persistent source-only closeout. Exhausted review never starts an unreviewed narrative."""
    state = progress(turn)
    if state.get('modelCloseout', {}).get('result'):
        return state['modelCloseout']['result']
    material = closeout_material(turn, evidence, artifact)
    attempt_id = uuid4().hex
    claimed = progress(turn, lambda saved: saved.setdefault('modelCloseout', {'started': True, 'attemptId': attempt_id}))
    if claimed['modelCloseout'].get('attemptId') != attempt_id:
        raise AnalysisError('CLOSEOUT_UNAVAILABLE', '来源数据摘要正在处理或已结束，不重复生成。')
    started = perf_counter()
    from .fact_closeout import summarize
    result = summarize(material, reason)
    if artifact and artifact.get('analysisRef'):
        result['detailsRef'] = artifact['analysisRef']
    progress(turn, lambda saved: saved['modelCloseout'].update(
        result=result, outcome='COMPLETED', elapsedMs=round((perf_counter() - started) * 1000, 2)))
    return result
