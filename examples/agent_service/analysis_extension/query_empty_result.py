"""An observed empty match has a useful terminal answer even without numerical facts."""


def empty_scenario_analysis_answer(evidence):
    """A completed scenario override with no data cannot gain a formula by querying the budget again."""
    if not evidence or evidence.get('pythonRuns'):
        return None
    sources = evidence.get('sources', [])
    if not sources or any(source.get('diagnostic') for source in sources):
        return None
    queries = [source.get('source', {}).get('businessQuery', {}) for source in sources]
    facts = [fact for source in sources for fact in source.get('facts', [])]
    if (not facts or any(query.get('purpose') not in ('QUERY', 'ANALYZE') or query.get('complete') is not True
                        or query.get('failureSummary', {}).get('outcome') != 'NO_DATA' for query in queries)
            or any(fact.get('valueState') != 'NO_DATA' or fact.get('formulaStatus') != 'NO_TARGET_CELL' for fact in facts)):
        return None
    scenes = sorted({fact.get('dimensionNames', {}).get('MD_SCENE') or fact.get('dimensions', {}).get('MD_SCENE', '') for fact in facts})
    label = '、'.join(scene for scene in scenes if scene) or '所请求的情景'
    answer = (f'本次已按{label}完成取数，数据源未返回对应记录，暂时无法进行数值构成分析。\n\n'
              '应用服务已用对应预算格定位，并仅替换情景条件取数，其他维度保持不变。'
              '由于未命中目标情景的运行格，本次没有可用的目标情景公式；未将预算格公式当作该情景的公式。\n\n'
              '无数据不代表数值为 0。')
    return {'status': 'DIRECT', 'semanticReview': 'NOT_REVIEWED', 'deliveryStatus': 'NO_DATA',
            'reason': 'NO_DATA', 'issues': [], 'answer': answer}


def empty_query_answer(evidence):
    sources = evidence.get('sources', [])
    if any(fact.get('valueState') != 'NO_DATA' for source in sources for fact in source.get('facts', [])) or evidence.get('pythonRuns'):
        return None
    queries = [source.get('source', {}).get('businessQuery', {}) for source in sources]
    queries = [query for query in queries if query]
    if not queries or any(query.get('purpose') != 'QUERY' or not query.get('complete')
                          or query.get('failureSummary', {}).get('outcome') not in ('NO_MATCH', 'NO_DATA') for query in queries):
        return None
    count = queries[-1].get('summary', {}).get('candidateCount', 0)
    names = {'OUTSIDE_REQUESTED_PERIOD': '数据时期不在所问范围内',
             'DIFFERENT_MODEL': '运行格与设计格所属业务模型不一致',
             'DIFFERENT_REQUIRED_DIMENSION': '实际维值不满足查询条件',
             'DIFFERENT_DESIGN_DIMENSION': '运行格不符合设计条件',
             'DIFFERENT_DESIGN_PERIOD': '运行格不符合设计时期',
             'RUNTIME_TITLE_MISMATCH': '运行表头不满足查询条件'}
    reasons = []
    for diagnostic in queries[-1].get('failureSummary', {}).get('matchDiagnostics', []):
        for code in diagnostic.get('rejectionCounts', {}):
            if code in names and names[code] not in reasons:
                reasons.append(names[code])
    no_data = queries[-1].get('failureSummary', {}).get('outcome') == 'NO_DATA'
    answer = ('本次已确认目标单元格并完成取数，数据源未返回这些目标的对应记录。' if no_data else
              f'本次已检查 {count} 个任务表样候选，未找到满足本次条件的运行单元格，因此没有取得数值。')
    if reasons and not no_data:
        answer += '\n\n运行匹配记录的排除原因包括：' + '；'.join(reasons) + '。'
    answer += '\n\n这不代表所查询的值为 0，也不能据此认定整个业务没有数据。'
    outcome = 'NO_DATA' if no_data else 'NO_MATCH'
    return {'status': 'DIRECT', 'semanticReview': 'NOT_REVIEWED', 'deliveryStatus': outcome,
            'reason': outcome, 'issues': [], 'answer': answer}
