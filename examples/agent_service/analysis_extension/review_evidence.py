"""从已有捕获补充正文涉及的同次表观察，不改变分析主体或计算输入。"""
from copy import deepcopy
import json

from .models import AnalysisError, digest


def supplement(turn, artifact, document_text):
    anchors = [fact for fact in artifact['facts'] if fact.get('observationId')
               and fact.get('bindingRef') and fact.get('inputRole') != 'STRUCTURE']
    batch_anchors = [fact for fact in artifact['facts'] if fact.get('batchId') and fact.get('aggregation')
                     and fact.get('inputRole') != 'STRUCTURE']
    if not (anchors or batch_anchors) or not document_text:
        return {'facts': [], 'complete': True}
    cache = getattr(turn.service, '_review_evidence_cache', None)
    if cache is None:
        cache = turn.service._review_evidence_cache = {}
    cache_key = digest([anchors, batch_anchors, document_text])
    if cache_key in cache:
        return deepcopy(cache[cache_key])
    dimension = artifact['scope'].get('memberDimension')
    selected = {fact['factId'] for fact in artifact['facts']}
    result = {'facts': [], 'complete': True,
              'meaning': '同次表观察或同一捕获中正文明确提及的原生分组证据；不扩大主体、分母或已执行计算'}
    seen, size = set(), 0
    capture_refs = sorted({ref for fact in [*anchors, *batch_anchors]
                           for ref in fact.get('captureRefs', [fact.get('captureRef')]) if ref})
    for capture_ref in capture_refs:
        capture = turn.service.store.get_capture(turn.service.owner, capture_ref)
        try:
            catalog = turn.service.sources.catalog(capture, dimension)
        except AnalysisError as error:
            result['complete'] = False
            result.setdefault('diagnostics', []).append({'captureRef': capture_ref, 'code': error.code})
            continue
        for item in catalog:
            if item.get('factId') in selected or item.get('valueState') != 'PRESENT':
                continue
            labels = [str(label).strip() for label in item.get('headers', []) if isinstance(label, str)]
            # 同一个捕获仍可包含多个单位、表样、期间；必须逐项对齐真实观察。
            matches = [fact for fact in anchors
                       if any(2 <= len(label) <= 120 and label in document_text for label in labels)
                       and capture_ref in fact.get('captureRefs', [fact.get('captureRef')])
                       and all(item.get(field) == fact.get(field) for field in
                               ('observationId', 'bindingRef', 'sourceContextId', 'context', 'period'))
                       and {**item.get('effectiveDimensions', {}), **item.get('dimensions', {})}.get(dimension)
                       == fact.get('sourceObjectId', fact.get('dimensions', {}).get(dimension))]
            identities = {(fact['objectId'], fact.get('objectModelCode')) for fact in matches}
            grouped = [fact for fact in batch_anchors
                       if capture_ref in fact.get('captureRefs', [fact.get('captureRef')])
                       and item.get('batchId') and item.get('aggregation')
                       and 2 <= len(str(item.get('formName') or '')) <= 120
                       and item['formName'] in document_text
                       and all(item.get(key) == fact.get(key) for key in ('period', 'taskName', 'dataContext'))
                       and item['aggregation'].get('groupBy') == fact['aggregation'].get('groupBy')]
            if len(identities) != 1 and not grouped:
                continue
            candidate = {key: item[key] for key in ('factId', 'captureRef', 'sourceContextId', 'bindingRef',
                'observationId', 'cellKey', 'headers', 'period', 'dimensions', 'dimensionNames', 'value', 'unit', 'valueState',
                'formName', 'taskName', 'batchId', 'aggregation') if key in item}
            if len(identities) == 1:
                candidate.update(objectId=matches[0]['objectId'], objectLabel=matches[0].get('objectLabel'),
                                 matchedFactRef=matches[0]['factId'])
            candidate.update(captureRef=capture_ref, inputRole='REVIEW_SUPPORT')
            identity = digest([capture_ref, item.get('factId')])
            if identity in seen:
                continue
            encoded_size = len(json.dumps(candidate, ensure_ascii=False))
            if len(result['facts']) >= 40 or size + encoded_size > 12000:
                result['complete'] = False
                continue
            seen.add(identity)
            size += encoded_size
            result['facts'].append(candidate)
    cache[cache_key] = deepcopy(result)
    while len(cache) > 8:
        cache.pop(next(iter(cache)))
    return result
