"""Deterministic source-only delivery after review/repair exhaustion."""
from .business_tables import escape, text, STATES, DEFAULT_LABELS


def summarize(material, reason):
    facts = material.get('facts', [])
    lines = ['已取得以下来源数据。本次分析或答复核对尚未完成，先列示已确认的来源值。',
             '', '| 对象 | 指标或标题 | 任务／表样 | 实际时期 | 其他维度 | 来源值 | 单位 |',
             '| --- | --- | --- | --- | --- | ---: | --- |']
    refs = []
    contexts = {}
    for item in material.get('contexts', []):
        if isinstance(item, dict):
            contexts.update(item)
    for fact in facts[:80]:
        context = contexts.get(fact.get('contextRef', fact.get('sourceContextId')), {})
        dims = {**context.get('effectiveDimensions', {}), **(fact.get('effectiveDimensions') or {}),
                **(fact.get('dimensions') or {})}
        names = fact.get('dimensionNames') or {}
        headers = fact.get('headers', [])
        title = ' / '.join(map(str, headers)) if isinstance(headers, list) else str(headers)
        values = [fact.get('objectLabel') or names.get('MD_ORG') or fact.get('objectId') or dims.get('MD_ORG', ''),
                  title or fact.get('title') or fact.get('measureCode', ''),
                  ' / '.join(str(fact.get(k) or context[k]) for k in ('taskName', 'formName') if fact.get(k) or context.get(k)),
                  fact.get('dataPeriod') or fact.get('period') or dims.get('DATATIME', ''),
                  '；'.join(f'{DEFAULT_LABELS.get(key, key)}={names.get(key) or value}' for key, value in dims.items() if key not in ('DATATIME', 'MD_ORG')),
                  text(fact.get('value')) if fact.get('value') is not None else STATES.get(fact.get('valueState'), '状态未确认'), fact.get('unit') or '']
        lines.append('| ' + ' | '.join(escape(text(value)) for value in values) + ' |')
        if fact.get('factId'):
            refs.append(fact['factId'])
    # Only deterministic formula checks are safe here. Unreviewed Python outputs,
    # draft paragraphs and reviewer claims are not promoted to verified results.
    checks = []
    by_ref = {fact.get('factId'): fact for fact in facts}
    for derived in material.get('computed', []):
        if derived.get('operator') != 'FORMULA_CHECK':
            continue
        from .formula_check_summary import summarize as formula_summary
        summary = derived.get('checkSummary') or formula_summary(derived.get('formulaTrace') or {})
        for item in summary.get('localResults', []):
            if item.get('status') not in ('MISMATCH', 'BLOCKED'):
                continue
            fact = by_ref.get(item['factRef'], {})
            label = ' / '.join(str(v) for v in [fact.get('objectLabel'), *(fact.get('headers') or [])] if v)
            checks.append((label or '已捕获公式', item['statement']))
        if checks and derived.get('derivedId'):
            refs.append(derived['derivedId'])
    if checks:
        lines += ['', '已完成的本层公式核对：', '', '| 对象或指标 | 核对结果 |', '| --- | --- |']
        lines += ['| ' + escape(label) + ' | ' + escape(statement) + ' |' for label, statement in checks[:30]]
    omitted = material.get('omittedCount', 0) + max(0, len(facts) - 80)
    if omitted:
        lines += ['', f'以上为展示片段，另有 {omitted} 条材料未在此展开；完整明细已保存在本次结果中。']
    if not facts:
        lines += ['', '本次整理材料没有可直接列示的来源行；已有计算与完整明细仍保留。']
    lines += ['', '尚未完成：本题分析结论与交付核对。以上数据不代表分析已经通过验收。']
    return {'status': 'PARTIAL', 'semanticReview': 'NOT_REVIEWED', 'deliveryStatus': 'FACT_SUMMARY',
            'reason': reason, 'issues': [], 'answer': '\n'.join(lines), 'usedRefs': list(dict.fromkeys(refs)),
            'evidenceSummary': {'displayedFactCount': min(len(facts), 80), 'omittedRecordCount': omitted,
                                'basis': 'SOURCE_FACTS_AND_HOST_FORMULA_CHECKS'}}
