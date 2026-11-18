"""普通查数核对暂不可用时交付来源明细，不释放未经核对的模型草稿或合计。"""
import json

from .models import AnalysisError


def source_only_answer(evidence, reason):
    """只使用已验证归属与完整性的来源记录；明示截取，空值与零分别保留。"""
    from .query_result_evidence import source_cells
    records, seen, diagnostics = [], set(), []
    for source in evidence.get('sources', []):
        if source.get('diagnostic') or source.get('source', {}).get('gridFileErrors'):
            # 保留失败请求的诊断，但不以先前失败否定另一份已经验证成功的来源。
            diagnostics.append({'captureRef': source.get('captureRef'),
                                'diagnostic': source.get('diagnostic') or source['source']['gridFileErrors']})
            continue
        for fact in source.get('facts', []):
            context = source.get('contexts', {}).get(fact.get('contextRef'), {})
            if fact.get('valueState') not in ('PRESENT', 'NO_VALUE'):
                continue
            records.append((context, fact.get('headers', []), fact.get('dimensions', {}), fact.get('value'), fact.get('unit')))
    for run in evidence.get('pythonRuns', []):
        for data in run.get('sourceData', []):
            if data.get('entryType') == 'FOR_GRID':
                continue
            for cell in source_cells(data):
                key = (data['captureRef'], data['ref'], cell['key'])
                if key in seen or not cell.get('measureCode') or cell.get('issue'):
                    continue
                seen.add(key)
                records.append((data['context'], cell.get('headers') or [cell['measureCode']],
                                cell.get('dimensions', {}), cell.get('value'), cell.get('unit')))
    if not records:
        raise AnalysisError('NO_AVAILABLE_FACTS', '已保存原始资料，但当前没有可独立交付的来源数值明细。')

    def text(value):
        if value is None:
            return '—'
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        return str(value).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('|', '\\|').replace('\n', ' ')

    shown = records[:40]
    lines = ['已取得来源数据，最终核对暂未完成。以下仅展示来源明细；尚未确认的合计、比较和分析结论本次未交付。',
             '', '| 来源条件 | 项目 | 单元格条件 | 原始值 | 来源单位 |', '| :--- | :--- | :--- | ---: | :--- |']
    for context, headers, dimensions, value, unit in shown:
        conditions = {key: context[key] for key in ('taskName', 'formName', 'period', 'effectiveDimensions') if context.get(key)}
        lines.append('| ' + ' | '.join((text(conditions), text(' / '.join(str(item) for item in headers)),
                                        text(dimensions), text(value), text(unit) if unit else '')) + ' |')
    truncated = len(records) > len(shown) or any(not data.get('complete', True)
        for run in evidence.get('pythonRuns', []) for data in run.get('sourceData', []))
    if truncated:
        lines.extend(['', '此处展示部分来源明细，不代表查询的全部范围；完整原始资料和运行结果已保留。'])
    if diagnostics:
        lines.extend(['', '部分查询尝试有错误记录；以上仅列出实际成功取得的来源明细。'])
    reasons = {'REVIEW_CAPACITY': '核对材料超过当前容量。',
               'RESULT_CHANGED_DURING_FOCUS': '本地整理前后的计算结果不一致，尚未确认最终结果。'}
    lines.extend(['', '暂未完成原因：' + reasons.get(reason, '核对服务暂不可用。')])
    return {'status': 'PARTIAL', 'semanticReview': 'UNAVAILABLE', 'deliveryStatus': 'SOURCE_ONLY',
            'reason': reason, 'issues': [], 'sourceDiagnostics': diagnostics, 'answer': '\n'.join(lines)}
