"""Present captured relationships by subject without choosing a business explanation."""
import json

from .native_analysis import formula_check_summary


def composition_materials(artifact, limit=8, node_limit=24, character_limit=16000):
    facts = {fact['factId']: fact for fact in artifact.get('facts', [])}
    subjects, seen = [], set()
    for value in artifact.get('derivedValues', []):
        trace = value.get('formulaTrace') or {}
        target = facts.get(trace.get('targetFactRef'))
        if value.get('operator') != 'FORMULA_CHECK' or not target or target.get('inputRole', 'PRIMARY') != 'PRIMARY':
            continue
        if target['factId'] in seen:
            continue
        seen.add(target['factId'])
        nodes, limitations = [], []
        for node in trace.get('nodes', []):
            fact = facts.get(node['factRef'], {})
            local = node.get('localCheck') or {}
            dependencies = [dep['factRef'] for dep in node.get('dependencies', []) if dep.get('factRef')]
            nodes.append({'factRef': node['factRef'], 'headers': fact.get('headers', node.get('headers', [])),
                          'sourceValue': fact.get('value'), 'valueState': fact.get('valueState'), 'unit': fact.get('unit'),
                          'formulaStatus': fact.get('formulaStatus'), 'expression': node.get('expression'),
                          'dependencyRefs': dependencies, 'localCheck': local,
                          'boundary': node.get('boundary')})
            if local.get('status') in ('MISMATCH', 'BLOCKED') or fact.get('valueState') == 'NO_VALUE' or node.get('boundary'):
                limitations.append({'affectedFactRef': node['factRef'], 'localStatus': local.get('status'),
                                    'valueState': fact.get('valueState'), 'boundary': node.get('boundary'),
                                    'availableDependencyRefs': [ref for ref in dependencies if facts.get(ref, {}).get('valueState') == 'PRESENT']})
        subjects.append({'targetFactRef': target['factId'], 'calculationRef': value['derivedId'],
                         'objectId': target.get('objectId'), 'objectLabel': target.get('objectLabel'),
                         'context': {key: target.get(key) for key in ('bindingRef', 'period', 'observationId', 'dimensions')},
                         'checkSummary': formula_check_summary(trace), 'items': nodes[:node_limit],
                         'itemCount': len(nodes), 'complete': len(nodes) <= node_limit,
                         'limitations': limitations[:node_limit], 'limitationCount': len(limitations),
                         'detailRead': {'section': 'formulas', 'analysisRef': artifact['analysisRef'],
                                        'factRef': target['factId'], 'detail': 'full'}})
    result = {'subjects': subjects[:limit], 'subjectCount': len(subjects), 'complete': len(subjects) <= limit,
            'subjectReads': [item['detailRead'] for item in subjects[limit:limit + 20]],
            'nextRead': {'section': 'derived', 'analysisRef': artifact['analysisRef']},
            'boundary': '按真实公式关系组织已取得材料，不规定必写科目、章节或唯一成因。来源值、复算值和假设分开；缺口只限制受影响的节点，其他已知构成仍可分析。不要将相邻科目自动视为加总项，也不要为局部缺口重复查询已取得的数据。'}
    # Keep complete scalar values and references. If the brief is too large,
    # omit whole entries and offer the existing paginated inspection path.
    while len(json.dumps(result, ensure_ascii=False)) > character_limit:
        candidates = [(subject, key) for subject in result['subjects'] for key in ('items', 'limitations') if subject[key]]
        if candidates:
            subject, key = max(candidates, key=lambda pair: len(json.dumps(pair[0][pair[1]], ensure_ascii=False)))
            subject[key].pop()
            subject['complete'] = False
        elif result['subjectReads']:
            result['subjectReads'].pop()
        elif result['subjects']:
            result['subjects'].pop()
        else:
            break
        result['complete'] = False
    result['complete'] = result['complete'] and all(subject['complete'] for subject in result['subjects'])
    return result
