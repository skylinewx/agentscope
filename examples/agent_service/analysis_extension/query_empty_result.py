"""Successful empty queries retain their complete business presentation."""


def business_answer(sources):
    views = [source.get('source', {}).get('businessTableView') for source in sources]
    if not views or any(not view or not view.get('allFactsIncluded') or not view.get('markdown') for view in views):
        return None  # Missing presentation evidence must not be replaced by a generic empty sentence.
    return '\n\n'.join(view['markdown'] for view in views)


def empty_scenario_analysis_answer(evidence):
    """A completed scenario override with no data cannot gain a formula by querying the budget again."""
    if not evidence or evidence.get('pythonRuns'):
        return None
    sources = evidence.get('sources', [])
    if not sources or any(source.get('diagnostic') for source in sources):
        return None
    queries = [source.get('source', {}).get('businessQuery', {}) for source in sources]
    facts = [fact for source in sources for fact in source.get('facts', [])]
    if (not facts or any(query.get('purpose') not in ('QUERY', 'PREPARE_MATERIALS') or query.get('complete') is not True
                        or query.get('failureSummary', {}).get('outcome') != 'NO_DATA' for query in queries)
            or any(fact.get('valueState') != 'NO_DATA' or fact.get('formulaStatus') != 'NO_TARGET_CELL' for fact in facts)):
        return None
    answer = business_answer(sources)
    if answer is None:
        return None
    return {'status': 'DIRECT', 'semanticReview': 'NOT_REVIEWED', 'deliveryStatus': 'NO_DATA',
            'reason': 'NO_DATA', 'issues': [], 'answer': answer}


def _unmatched_answer(query):
    from .business_tables import escape

    count = query.get('summary', {}).get('candidateCount', 0)
    labels = dict.fromkeys(target['label'] for target in query.get('requestedTargets', []) if target.get('label'))
    subject = '、'.join(labels) or query.get('question')
    prefix = f'查询“{escape(subject)}”时，' if subject else '本次'
    answer = f'{prefix}已检查 {count} 个任务表样候选，未找到满足本次条件的运行单元格，因此没有取得数值。'
    names = {'OUTSIDE_REQUESTED_PERIOD': '数据时期不在所问范围内',
             'DIFFERENT_MODEL': '运行格与设计格所属业务模型不一致',
             'DIFFERENT_REQUIRED_DIMENSION': '实际维值不满足查询条件',
             'DIFFERENT_DESIGN_DIMENSION': '运行格不符合设计条件',
             'DIFFERENT_DESIGN_PERIOD': '运行格不符合设计时期',
             'RUNTIME_TITLE_MISMATCH': '运行表头不满足查询条件'}
    reasons = []
    for diagnostic in query['failureSummary'].get('matchDiagnostics', []):
        for code in diagnostic.get('rejectionCounts', {}):
            if code in names and names[code] not in reasons:
                reasons.append(names[code])
    if reasons:
        answer += '\n\n运行匹配记录的排除原因包括：' + '；'.join(reasons) + '。'
    return answer


def _confirmed_empty_filter(query):
    """Older receipts used NO_MATCH even when their native filter returned zero rows."""
    summary = query.get('summary') or {}
    filtering = query.get('valueFilter') or {}
    batches = filtering.get('batches') or []
    return (summary.get('rowCount') == 0 and (summary.get('resultSetCount') or 0) > 0
            and filtering.get('basis') in ('BUDGET_NATIVE_FILTER', 'BUDGET_NATIVE_RESULT_WHERE')
            and filtering.get('executionStatus') == 'EXECUTED' and bool(batches)
            and all(batch.get('status') == 'SUCCEEDED' and batch.get('rowCount') == 0 for batch in batches))


def empty_query_answer(evidence):
    if not evidence or evidence.get('pythonRuns'):
        return None
    empty_sources, unmatched = [], []
    for source in evidence.get('sources', []):
        if source.get('diagnostic') or any(fact.get('valueState') != 'NO_DATA' for fact in source.get('facts', [])):
            return None
        info = source.get('source', {})
        query = info.get('businessQuery')
        if not query:
            if source.get('facts') or info.get('batchQueries') or info.get('gridFiles') or info.get('gridFileErrors'):
                return None
            continue
        if query.get('purpose') != 'QUERY' or query.get('complete') is not True or info.get('status') not in (None, 'SUCCEEDED'):
            return None
        outcome = (query.get('failureSummary') or {}).get('outcome')
        if outcome == 'NO_DATA' or (outcome == 'NO_MATCH' and _confirmed_empty_filter(query)):
            empty_sources.append(source)
        elif outcome == 'NO_MATCH':
            unmatched.append(_unmatched_answer(query))
        else:
            return None

    if empty_sources:
        tables = business_answer(empty_sources)
        if tables is None:
            return None
        answer, outcome = '\n\n'.join([tables, *unmatched]), 'NO_DATA'
    elif unmatched:
        answer, outcome = '\n\n'.join(unmatched), 'NO_MATCH'
    else:
        return None
    return {'status': 'DIRECT', 'semanticReview': 'NOT_REVIEWED', 'deliveryStatus': outcome,
            'reason': outcome, 'issues': [], 'answer': answer}
