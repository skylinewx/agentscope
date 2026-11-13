"""Explain native aggregation counts without recomputing money or inferring missing data."""


def explain(source):
    aggregation = source.get('aggregationSummary')
    if not isinstance(aggregation, dict) or not aggregation:
        return ''
    scope = source.get('rangeSummary') or {}
    counts = []
    for values, key, label in (
            (scope, 'targetCombinationCount', '原始范围目标组合'),
            (aggregation, 'leafTargetCombinationCount', '有效末级参与目标组合'),
            (scope, 'filteredOutRowCount', '明细条件筛除行'),
            (aggregation, 'detailRowCount', '汇总前明细行'),
            (aggregation, 'groupCountBeforeHaving', '分组条件筛选前组数'),
            (aggregation, 'filteredOutGroupCount', '分组条件筛除组数')):
        number = values.get(key)
        if type(number) is int and number >= 0 and (number or 'filteredOut' not in key):
            counts.append(f'{label} {number}')
    returned = source.get('rowCount', source.get('returnedRowCount'))
    if type(returned) is int and returned >= 0:
        counts.append(f'实际返回 {returned} 组')
    return (' → '.join(counts) + '。' if counts else '') + (
        '按当前时期及所选范围取有效末级，上级不重复参与；目标组合数不等于有值数或分组数，减少不等于缺数。'
        '按参与成员自身属性分组，不继承父级属性，保留空属性组。聚合值不等于合并报表值，不含已去重的上级行。')


def annotate(scopes):
    return [{**source, **({'aggregationExplanation': note} if (note := explain(source)) else {})}
            for source in scopes]


def receipt_markdown(receipt):
    lines = []
    for step in (receipt.get('data') or {}).get('stepResults', []):
        for batch in step.get('batchResults', []):
            note = explain(batch.get('data') or {})
            if note:
                # The batch identifier locates the exact receipt when multiple forms coexist.
                identity = str(batch.get('batchId') or step.get('stepId') or '')
                identity = identity.replace('\n', ' ').replace('\r', ' ').replace('`', '')[:160]
                lines.append(f'**聚合口径**（{identity}）：{note}')
    return lines
