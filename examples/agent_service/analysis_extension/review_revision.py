"""仅在数据与范围未变时提供局部修改定位；不根据文字相似度宣布业务等价。"""
from copy import deepcopy
from difflib import SequenceMatcher
import json

from .models import digest
from .review_control import progress


def revision_basis(material):
    source = {key: value for key, value in material.items()
              if key not in ('documentText', 'documentSourceText', 'candidateAnswer', 'deliveryEvidence', 'reviewHistory', 'failures')}
    return digest(source)


def focus_revision(turn, material):
    previous = progress(turn).get('lastContentReview')
    text = material.get('documentText', '')
    source = material.get('documentSourceText', '')
    if (not previous or material.get('candidateAnswer') or previous['basis'] != revision_basis(material)
            or (previous['text'] == text and previous.get('sourceText', '') == source)):
        return material
    changes = []
    for field, old, new in (('documentText', previous['text'], text),
                            ('documentSourceText', previous.get('sourceText', ''), source)):
        before, after = old.splitlines(), new.splitlines()
        for operation, old_start, old_end, new_start, new_end in SequenceMatcher(a=before, b=after, autojunk=False).get_opcodes():
            if operation != 'equal':
                changes.append({'field': field, 'before': before[old_start:old_end], 'after': after[new_start:new_end]})
    result = deepcopy(material)
    # 上一次内容审查已完成，且来源、范围、绑定、计算与失败信息逐项摘要相同。
    # 保留全部事实、派生数值、空值、公式差额及当前全文，只省去未变化的执行背景。
    unchanged_background = ('sourceScopes', 'structureEvidence', 'previousMessages', 'pythonRuns', 'relations')
    omitted = [field for field in unchanged_background if field in result]
    for field in omitted:
        result.pop(field)
    result['revisionFocus'] = {'changes': changes, 'previousIssues': previous['issues'],
                               'sameDataAndScope': True, 'unchangedEvidenceDigest': previous['basis'],
                               'omittedUnchangedBackground': omitted,
                               'meaning': '前次审查已完成；以上执行背景完全未变且已核对。全部主体事实、计算结果、缺数和公式差额仍在当前材料中。结合当前全文核对实际修改和原实质问题，不重新提出未改变部分的润色或可选分析要求。'}
    return result


def remember_review(turn, material, verdict):
    text = material.get('documentText', '')
    source = material.get('documentSourceText', '')
    if len(text) + len(source) <= 48000:
        progress(turn, lambda state: state.update(lastContentReview={
            'basis': revision_basis(material), 'text': text, 'sourceText': source, 'issues': verdict['issues']}))



def formula_review_trace(trace):
    """仅整理宿主已有核对结果；本层与逐层明确命名，不从缺数推断父层状态。"""
    result = {key: deepcopy(trace[key]) for key in (
        'value', 'targetLocalCheck', 'differenceOriginRefs', 'pendingDependencies',
        'externalBoundaries', 'meaning', 'boundaryMeaning', 'applicability',
        'executionStatus', 'completionReason') if key in trace}
    result['recursiveStatus'] = trace.get('status')
    result['nodes'] = [{
        'factRef': node['factRef'], 'observed': node.get('observed'),
        'localCheck': deepcopy(node.get('localCheck')),
        'recursiveCheck': {'status': node.get('status'), 'value': node.get('recalculated'),
                           'difference': node.get('difference')},
    } for node in trace['nodes']]
    return result


def repair_evidence(material, issues):
    """把已核对引用的原始事实及计算依赖直接交给修正模型，不额外调用模型找证据。"""
    facts = {item['factId']: item for item in material.get('facts', [])}
    facts.update({item['factId']: item for item in material.get('supportingEvidence', {}).get('facts', [])})
    derived = {item['derivedId']: item for item in material.get('derived', [])}
    selected = {reference for issue in issues for reference in issue.get('evidenceRefs', [])}
    pending = list(selected)
    while pending:
        record = derived.get(pending.pop(), {})
        dependencies = set(record.get('inputRefs', []) + record.get('sourceFactIds', []))
        dependencies.update(node.get('factRef') for node in record.get('formulaTrace', {}).get('nodes', []))
        dependencies &= facts.keys() | derived.keys()
        pending.extend(dependencies - selected)
        selected.update(dependencies)
    result = {'facts': [deepcopy(item) for key, item in facts.items() if key in selected],
              'derived': [deepcopy(item) for key, item in derived.items() if key in selected],
              'scope': '仅为原问题的修正依据，不新增分析主体；数值、空值和公式差额保持原样。'}
    # 大依赖仍完整留存，不截断后伪装成完整证明；原分析引用可按需定向读取。
    if len(json.dumps(result, ensure_ascii=False)) > 16000:
        return {'included': False, 'evidenceRefs': sorted(selected),
                'reason': '相关依赖较大，沿用已取得的分析材料；缺少具体字段时按 evidenceRefs 定向读取。'}
    return {'included': True, **result}


def compact_document_text(material):
    """实测全文完整保留，源码重复段落以可逆差异表示，隐藏文字和限定条件不丢失。"""
    text, source = material.get('documentText', ''), material.get('documentSourceText', '')
    if material.get('documentTextBasis') != 'HOST_RENDERED' or not text or not source:
        return material
    rendered_lines, source_lines = text.splitlines(), source.splitlines()
    changes = [{'renderedStartLine': start, 'renderedEndLine': end, 'sourceLines': source_lines[new_start:new_end]}
               for operation, start, end, new_start, new_end in
               SequenceMatcher(a=rendered_lines, b=source_lines, autojunk=False).get_opcodes()
               if operation != 'equal']
    projection = {'basis': 'documentText', 'changes': changes,
                  'meaning': '当前实测全文完整保留。源码其他行与实测一致；以下是全部源码差异（零基行区间）。源码占位符不代表实际页面缺值。'}
    if len(json.dumps(projection, ensure_ascii=False)) >= len(json.dumps(source, ensure_ascii=False)) * .85:
        return material
    result = deepcopy(material)
    result.pop('documentSourceText')
    result['documentSourceTextProjection'] = projection
    return result


def compact_query_facts(evidence):
    """大批量事实转为列行结构，保留缺字段与 null 的区别，避免重复字段占满核对材料。"""
    result = deepcopy(evidence)
    for source in result.get('sources', []):
        facts = source.get('facts', [])
        if len(facts) < 20:
            continue
        columns = list(dict.fromkeys(key for fact in facts for key in fact))
        fixed = {key: facts[0][key] for key in columns if key in facts[0]
                 and all(key in fact and fact[key] == facts[0][key] for fact in facts)}
        columns = [key for key in columns if key not in fixed]
        source['fixedFactFields'] = fixed
        source['factColumns'] = columns
        source['factRows'] = [[fact.get(key) for key in columns] for fact in facts]
        source['missingFactFields'] = {str(index): [key for key in columns if key not in fact]
                                      for index, fact in enumerate(facts) if any(key not in fact for key in columns)}
        del source['facts']
    return result
