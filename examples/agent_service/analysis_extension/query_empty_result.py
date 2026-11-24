"""Successful empty queries retain their complete business presentation."""


def business_answer(sources):
    views = [source.get('source', {}).get('businessTableView') for source in sources]
    if not views or any(not view or not view.get('allFactsIncluded') or not view.get('markdown') for view in views):
        return None  # Missing presentation evidence must not be replaced by a generic empty sentence.
    return '\n\n'.join(view['markdown'] for view in views)


def _delivered_captures(sources):
    # business_answer has verified that the host printed the complete tables for these receipts.
    return list(dict.fromkeys(source['captureRef'] for source in sources)) if all(
        source.get('captureRef') for source in sources) else []


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
            'reason': 'NO_DATA', 'issues': [], 'answer': answer, 'deliveredCaptureRefs': _delivered_captures(sources)}


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


def business_outcome(query):
    outcome = (query.get('failureSummary') or {}).get('outcome')
    if outcome == 'NO_MATCH' and _confirmed_empty_filter(query):
        return 'NO_DATA'
    return outcome


def incomplete_result_rows(query):
    return (any(result.get('rowsComplete') is False or not isinstance(result.get('rows'), list)
                for result in query.get('resultSets', []))
            or any(result.get('rowsComplete') is False for result in query.get('resultSetCatalog', [])))


def completed_result_set(receipt, result):
    """Use the returned table's execution boundary when another named binding failed."""
    data = receipt.get('data') or {}
    if incomplete_result_rows({'resultSets': [result], 'resultSetCatalog': [
            entry for entry in data.get('resultSetCatalog', [])
            if entry.get('resultSetId') == result.get('resultSetId')]}):
        return False
    if receipt.get('status') == 'SUCCEEDED' and data.get('complete') is True:
        return True
    issues = data.get('unresolvedGroups') or []
    binding = (result.get('context', {}).get('target') or {}).get('formBindingKey')
    if (receipt.get('status') != 'PARTIAL' or data.get('resultType') != 'BUDGET_QUERY_RESULT'
            or not binding or not issues or receipt.get('resumeRef') or receipt.get('error')
            or any(not g.get('binding') or g['binding'] == binding for g in issues)
            or data.get('valueFilter') and data['valueFilter'].get('executionStatus') != 'EXECUTED'):
        return False
    rows = result['rows']
    if rows:
        return all(row.get('valueState') in ('PRESENT', 'NULL', 'NO_DATA') and row.get('originRefs') for row in rows)
    # An empty placeholder alone cannot establish a successful filter. Its native batch
    # must have run the specified target on this form and returned exactly zero rows.
    from .adapters.budget_batch import tables
    logical = str(result.get('targetId') or '').split(':', 1)[0]
    for item in (data.get('valueFilter') or {}).get('batches', []):
        column = (item.get('logicalTargetColumns') or {}).get(logical)
        if item.get('status') != 'SUCCEEDED' or item.get('rowCount') != 0 or not column:
            continue
        for _, table, _ in tables(receipt):
            if (table.get('batchId') == item.get('batchId') and table.get('disposition') == 'SUCCEEDED'
                    and table.get('data', {}).get('rowCount') == 0
                    and any(metric.get('name') == column and metric.get('context', {}).get('target', {}).get('formBindingKey') == binding
                            and metric['context'].get('period') == result['context'].get('period') for metric in table.get('metrics', []))):
                return True
    return False


def empty_table_status(query, facts):
    """Classify completed business tables without changing any source cell or its display."""
    if (query.get('purpose') != 'QUERY' or query.get('complete') is not True
            or query.get('unresolvedGroups') or incomplete_result_rows(query)):
        return None
    outcome = business_outcome(query)
    if outcome not in (None, 'ROWS_RETURNED', 'NO_DATA'):
        return None
    seen = set()
    for fact in facts:
        state = fact.get('valueState')
        if state not in ('NULL', 'NO_DATA') or fact.get('value') is not None:
            return None
        seen.add(state)
    if 'NULL' in seen:
        return 'EMPTY_VALUES' if outcome != 'NO_DATA' else None
    if seen or outcome == 'NO_DATA':
        return 'NO_DATA'
    summary = query.get('summary') or {}
    if (query.get('resultSets') and summary.get('rowCount', 0) == 0
            or summary.get('resultSetCount') and summary.get('rowCount') == 0):
        return 'NO_DATA'
    return None


def empty_query_answer(evidence):
    if not evidence or evidence.get('pythonRuns') or evidence.get('referencedCalculations'):
        return None
    empty_sources, unmatched = [], []
    has_null_values = False
    for source in evidence.get('sources', []):
        if source.get('diagnostic'):
            return None
        facts = source.get('facts', [])
        info = source.get('source', {})
        query = info.get('businessQuery')
        if not query:
            if facts or info.get('batchQueries') or info.get('gridFiles') or info.get('gridFileErrors'):
                return None
            continue
        if query.get('purpose') != 'QUERY' or query.get('complete') is not True or info.get('status') not in (None, 'SUCCEEDED'):
            return None
        table_status = empty_table_status(query, facts)
        if table_status:
            empty_sources.append(source)
            has_null_values |= table_status == 'EMPTY_VALUES'
        elif business_outcome(query) == 'NO_MATCH' and all(f.get('valueState') == 'NO_DATA' for f in facts):
            unmatched.append(_unmatched_answer(query))
        else:
            return None

    if empty_sources:
        tables = business_answer(empty_sources)
        if tables is None:
            return None
        answer = '\n\n'.join([tables, *unmatched])
        outcome = 'EMPTY_VALUES' if has_null_values else 'NO_DATA'
    elif unmatched:
        answer, outcome = '\n\n'.join(unmatched), 'NO_MATCH'
    else:
        return None
    return {'status': 'DIRECT', 'semanticReview': 'NOT_REVIEWED', 'deliveryStatus': outcome,
            'reason': outcome, 'issues': [], 'answer': answer, 'deliveredCaptureRefs': _delivered_captures(empty_sources)}
