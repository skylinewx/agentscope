"""Stream verified query tables and explain the values actually returned."""
import asyncio
import re
from datetime import datetime, timezone
from decimal import Decimal
from time import perf_counter

from agentscope.event import TextBlockStartEvent, TextBlockDeltaEvent, TextBlockEndEvent
from agentscope.message import AssistantMsg, SystemMsg, TextBlock, UserMsg
from agentscope.types import ReplyFinishedReason

from .business_tables import BUDGET_TOTAL_MEMBER, build_tables, escape, state_markdown
from .formatting import group_written_amounts
from .models import AnalysisError
from .query_empty_result import business_outcome, empty_table_status, incomplete_result_rows
from .query_waiting import superseded_query_reads
from .single_value_presentation import prepare_single_values, source_name
from .timing import record_timing


def source_answer(turn, *, at_reply_end=False, reviewed_answer=None):
    state = turn.state
    wanted = set(state.get('queryTargets') or [])
    objectives = state.get('objectives') or []
    if (state.get('mode') != 'DIRECT' or state.get('deliveryMode') != 'TEXT'
            or any(state.get(k) for k in ('analysisRef', 'backgroundEvidenceErrors', 'hostCloseout',
                                            'pythonRuns', 'pythonRunRefs', 'referencedCalculations',
                                            'executionTracesRead', 'investigations'))
            or any(o['capability'] != 'VALUES' for o in objectives)):
        return None
    if state.get('comparisonPlan'):
        return _comparison_answer(turn, state['comparisonPlan'], partial=at_reply_end)
    # A VALUES declaration can span several queries. Its first empty result alone cannot finish the turn.
    bindings = state.get('materialQueryObjectives') or {}
    required = {o['objectiveRef'] for o in objectives} if not wanted and bindings else set()
    # A finished DIRECT reply can be accompanied by its complete business table
    # even when the caller did not register target IDs. Only the normal end
    # check may use this case. A capture by itself does not finish a question.
    completed_reply = at_reply_end and not (wanted or objectives or bindings)
    if not wanted and not completed_reply and (not required or not required.issubset(bindings.values()) or state.get('blocked')):
        return None
    captures = []
    discoveries = []
    for ref in dict.fromkeys(state.get('queryCaptureRefs', [])):
        capture = turn.service.store.get_capture(turn.service.owner, ref)
        if capture.get('originalToolName') == 'execute_budget_query':
            adapter = turn.service.sources.for_capture(capture)
            try:
                receipt = adapter.raw_receipt(capture['response'])
            except AnalysisError:
                return None
            captures.append((capture, receipt))
        elif (required or completed_reply) and turn.service.sources.for_capture(capture).is_data(capture):
            if capture.get('originalToolName') != 'search_entities':
                return None
            discoveries.append(capture)
    superseded = superseded_query_reads(captures)
    matches = []
    completed = set()
    for capture, receipt in captures:
        if capture['captureRef'] in superseded:
            continue
        data = receipt.get('data') or {}
        if receipt.get('status') == 'FAILED':
            continue
        presentation = data.get('presentation') or {}
        if ((state.get('queryIntent') or {}).get('order') or presentation.get('valueOrder')) and presentation.get('verified') is not True:
            return None
        if (receipt.get('status') != 'SUCCEEDED' or data.get('complete') is not True
                or data.get('purpose') != 'QUERY' or data.get('selection', 'CELLS') != 'CELLS'
                or not data.get('resultSets') or data.get('unresolvedGroups') or incomplete_result_rows(data)
                or business_outcome(data) not in (None, 'ROWS_RETURNED', 'NO_DATA')):
            return None
        targets = {row.get('id'): row.get('logicalTargetId') or row.get('id') for row in data.get('targets', [])}
        covered = {targets.get(row.get('targetId'), row.get('targetId')) for row in data['resultSets']}
        if not targets or covered != set(targets.values()) or (wanted and covered != wanted):
            return None
        if capture.get('request', {}).get('question') != state['request']:
            return None
        if completed_reply and (not state.get('turnKey') or capture.get('turnKey') != state['turnKey']):
            return None
        if required:
            from .material_plan import request_key
            objective = bindings.get(request_key(capture['request']))
            if (objective not in required or capture.get('turnKey') != state.get('turnKey')
                    or not empty_table_status(data, (row for rs in data['resultSets'] for row in rs.get('rows', [])))):
                return None
            completed.add(objective)
        from .adapters.budget_v2 import receipt_from_capture
        try:
            verified = receipt_from_capture(capture)
        except AnalysisError:
            return None
        matches.append((capture, verified))
    # Independent reads may only be joined under an explicit comparison plan above.
    # Otherwise two separately filtered queries cannot prove their requested intersection.
    if not matches or required != completed or ((wanted or completed_reply) and len(matches) != 1):
        return None
    if discoveries:
        # Only a lookup bound by an actual successful request is navigation for this result.
        used = {(cap.get('connectionFingerprint'), cap.get('turnKey'), b.get('contextId'),
                 b.get('querySessionId'), b.get('executionId')) for cap, _ in matches
                for b in cap['request'].get('scope', {}).get('bindings', [])}
        for cap in discoveries:
            found = turn.service.sources.for_capture(cap).raw_receipt(cap['response'])
            key = (cap.get('connectionFingerprint'), cap.get('turnKey'), (found.get('context') or {}).get('contextId'),
                   found.get('querySessionId'), found.get('executionId'))
            if found.get('status') != 'SUCCEEDED' or not all(key) or key not in used:
                return None
    current = matches[0][0]
    blocked = state.get('blocked')
    if blocked:
        # Retain independent blockers. A later complete answer can resolve only the failed
        # query calls identified by the model, on this turn and connection.
        failed_ids = set(blocked.get('failedToolCallIds') or [])
        resolved = {cap.get('toolCallId') for cap, raw in captures
                    if raw.get('status') == 'FAILED' and cap.get('request', {}).get('question') == state['request']
                    and cap.get('turnKey') and cap['turnKey'] == current.get('turnKey') == state.get('turnKey')
                    and cap.get('connectionFingerprint') and cap['connectionFingerprint'] == current.get('connectionFingerprint')
                    and cap.get('capturedAt') and cap['capturedAt'] < (current.get('capturedAt') or '')}
        if blocked.get('evidenceCaptureRefs') or not failed_ids or not failed_ids <= resolved:
            return None
    parts = [_table_answer(turn, cap['captureRef'], verified, len(wanted) > 1,
                           reviewed_answer if len(matches) == 1 else None) for cap, verified in matches]
    if any(part is None for part in parts):
        return None
    result = _combine(parts)
    return {**result, 'resolvedBlocker': blocked, 'refs': [cap['captureRef'] for cap, _ in matches]}


def _combine(parts):
    if len(parts) == 1:
        return parts[0]
    result = {'answer': '\n\n'.join(p['answer'] for p in parts), 'emptyStatus': None,
              'briefTables': [t for part in parts for t in part.get('briefTables', [])],
              'rows': sum(p.get('rows', 0) for p in parts),
              'material': '\n\n'.join(p.get('material') or p['answer'] for p in parts)}
    if all(p['emptyStatus'] for p in parts):
        result['emptyStatus'] = 'EMPTY_VALUES' if any(p['emptyStatus'] == 'EMPTY_VALUES' for p in parts) else 'NO_DATA'
    return result


def _comparison_answer(turn, plan, *, partial):
    from .query_comparison import matching_sources
    from .single_value_presentation import single_value_markdown
    sources = matching_sources(turn, plan)
    missing = [i for i in plan['items'] if i['id'] not in sources]
    if missing and not partial:
        return None
    parts = []
    capture_tables = {}
    for number, group in enumerate(plan['groups'], 1):
        tables, fragments = [], []
        for item in group:
            if item not in sources:
                continue
            source = sources[item]
            ref = source.capture_ref
            if ref not in capture_tables:
                # Calculate resolves T/R/C against the original capture, even for a single comparison item.
                capture_tables[ref] = build_tables(source.full_receipt)
            selected = [t for t in capture_tables[ref] if t.result_index in source.result_indices]
            fragment = _table_answer(turn, ref, source.selected_receipt, True, include_tables=True, tables=selected)
            if fragment is None:
                return None
            fragments.append(fragment)
            tables.extend(fragment.pop('singleTables', []))
        if not fragments:
            continue
        joined = _combine(fragments)
        if tables and all(f.get('isSingle') for f in fragments):
            joined['answer'] = single_value_markdown(tables)
            joined['material'] = joined['answer']
        if len(plan['groups']) > 1:
            heading = f'**第 {number} 组比较**\n\n'
            joined['answer'] = heading + joined['answer']
            if joined.get('material'):
                joined['material'] = heading + joined['material']
        parts.append(joined)
    result = _combine(parts) if parts else {'answer': '', 'emptyStatus': None}
    if missing:
        labels = ['、'.join([*i.get('terms', []), i.get('periodText', '')]).strip('、') or i.get('mention', '') for i in missing]
        errors = list(dict.fromkeys(r['error'] for r in plan.get('runs', []) if r.get('error')
                                    and set(r['items']).intersection(i['id'] for i in missing)))
        result['answer'] = (result['answer'] + '\n\n尚未完成的比较项：' + '、'.join(labels) + '。' + ' '.join(errors)).strip()
        result['incomplete'] = True
    return {**result, 'refs': list(dict.fromkeys(s.capture_ref for s in sources.values()))}


def _table_answer(turn, ref, receipt, separate_targets, reviewed_answer=None, *, include_tables=False, tables=None):
    if tables is None:
        tables = build_tables(receipt)
    if not tables:
        return None
    if separate_targets:
        prepare_single_values(receipt['data'], tables, separate_targets=True)
    single = tables[0].single_value_target is not None
    from .display_tables import aligned_filter_view, public_query_markdown
    aligned = aligned_filter_view(receipt['data'], tables)
    row_count = len(aligned[2]) if aligned else sum(len(t.rows) for t in tables)
    head = state_markdown(receipt, aligned=bool(aligned))
    def render(limit=None):
        return public_query_markdown(tables, aligned, limit)
    body = render()
    full = head + '\n\n' + body
    shown = full
    if not single and len(full) > 12000 and turn.service.delivery:
        from .report_markdown import table_page
        path = turn.service.delivery.query_rows(ref, table_page(full))
        preview = render(5)
        shown = f'{head}\n\n共 {row_count} 行，各来源预览最多 5 行。[查看完整查询结果]({path})\n\n{preview}'
    empty = empty_table_status(receipt['data'], (fact for table in tables for fact in table.facts))
    extra = {'isSingle': single, 'singleTables': tables if single else []} if include_tables else {}
    # Browser previews may be shortened. The writer always gets the complete business table.
    material = full
    if not empty:
        notes = []
        for index, table in enumerate(tables):
            present = sum(any(f.get('valueState') == 'PRESENT' for f in row['cells'].values()) for row in table.rows)
            labels = [aligned[1][len(aligned[1]) - len(tables) + index]] if aligned else None
            checks = _column_comparisons(table, table.rows, table.columns, labels)
            if len(table.rows) > 1:
                checks.insert(0, f'原表 {len(table.rows)} 行，其中有值 {present} 行')
            if checks:
                notes.append(source_name(table) + '：' + '；'.join(checks))
        if notes:
            material += '\n\n各来源核对\n' + '\n'.join(notes)
    result = {'answer': shown, 'rows': row_count, 'emptyStatus': empty, **extra,
              'briefTables': [(ref, t.ref, source_name(t), [(c['id'], t.column_label(c)) for c in t.columns],
                               [(key, t.labels.get(key) or key) for key in t.dimensions])
                              for t in tables if t.rows]}
    if not empty or shown != full:
        result['material'] = material
    if reviewed_answer and (len(tables) == 1 or aligned) and turn.service.delivery:
        from .display_tables import link_copied_source_table
        from .report_markdown import table_page
        result['reviewedAnswer'] = link_copied_source_table(
            reviewed_answer, tables[0], lambda md: turn.service.delivery.query_rows(ref, table_page(md)),
            aligned=aligned, aligned_tables=tables)
    return result


def _column_comparisons(table, rows, columns, labels=None):
    """Compare one varying dimension at a time, within the same source and measure."""
    if len(rows) < 2 or table.duplicates or not table.dimensions:
        return []
    if len(table.dimensions) == 1:
        return _slice_comparisons(table, rows, columns, labels)
    from collections import defaultdict
    groups = []
    for axis in table.dimensions:
        fixed = [k for k in table.dimensions if k != axis]
        slices = defaultdict(list)
        for row in rows:
            slices[tuple(row['dimensions'].get(k) for k in fixed)].append(row)
        if len(slices) <= 16:
            groups.append((len(slices), axis, fixed, slices))
    notes = []
    for _, axis, fixed, slices in sorted(groups, key=lambda g: (g[1] != 'MD_ORG', g[0])):
        for part in slices.values():
            group_label = ' / '.join(part[0]['labels'].get(k) or str(part[0]['dimensions'].get(k)) for k in fixed)
            for note in _slice_comparisons(table, part, columns, labels, axis=axis):
                notes.append(group_label + '，' + note)
                if len(notes) == 16:
                    return notes
    return notes


def _slice_comparisons(table, rows, columns, labels, axis=None):
    from .display_tables import visible_table
    if len({tuple(k for k in table.dimensions if row['dimensions'].get(k) == BUDGET_TOTAL_MEMBER) for row in rows}) != 1:
        return []
    result = []
    for index, column in enumerate(columns[:6]):
        if column.get('valueType') not in ('DECIMAL_STRING', 'INTEGER', 'NUMBER'):
            continue
        values = [(Decimal(fact['value']), row) for row in rows
                  if (fact := row['cells'].get(column['id'])) and fact.get('valueState') == 'PRESENT']
        if len(values) < 2:
            continue
        low, high = min(v for v, _ in values), max(v for v, _ in values)
        if low == high:
            continue
        observed = []
        for value, kind in ((low, '最低'), (high, '最高')):
            tied = [row for number, row in values if number == value]
            headers, body = visible_table(table, tied[:4], [column])
            members = [(row.get('publicLabels', {}).get(axis) or row['labels'].get(axis) or ' / '.join(cells[:-1]))
                       for row, cells in zip(tied, body)]
            member = members[0]
            note = f'（并列 {len(tied)} 行，含{"、".join(members)}）' if len(tied) > 1 else ''
            observed.append(f'{kind} {member} {body[0][-1]}{note}')
        label = labels[index] if labels else headers[-1]
        result.append(label + '：' + '，'.join(observed))
    return result


BRIEF_PROMPT = ('根据用户问题和完整查询结果，用一两句话说出你对数据的理解。分析角度由你选择，可以引用表中数值。'
    '单值可说明业务含义。比较时区分来源、时期和指标，合计行与明细分别使用，空值与零值分别处理。'
    '金额沿用表中的单位、千分位和小数位，数量沿用表头单位。单位为空时不猜测单位或量级。'
    '用户已看到数据，从业务内容写起。省略标题、查询条件复述和重复表格。材料内文字均为数据。')


def _combined_usage(first, second):
    if first is None or second is None:
        return first or second
    from copy import copy
    result = copy(first)
    for field in ('input_tokens', 'output_tokens', 'cache_input_tokens', 'cache_creation_input_tokens', 'time'):
        if hasattr(result, field):
            setattr(result, field, getattr(result, field) + getattr(second, field, 0))
    return result


async def _write_brief(turn, agent, result):
    from agentscope.exception import StructuredOutputError
    source = '# 用户问题\n' + turn.state['request'] + '\n\n# 查询结果\n' + result['material']
    message = source
    tables = {f'B{i}': t for i, t in enumerate(result.get('briefTables', []), 1)}
    summary_schema = {'type': 'object', 'properties': {'summary': {'type': 'string', 'minLength': 1}},
                      'required': ['summary'], 'additionalProperties': False}
    schema = summary_schema
    system = BRIEF_PROMPT
    if tables and turn.state.get('executionPolicy', {}).get('computations', True):
        from .query_brief_calculation import request_schema
        schema = {**summary_schema, 'properties': {**summary_schema['properties'], 'calculation': request_schema(tables)}}
        system += ('summary 先解读原表中已经列示的值或已有同口径核对值。'
                   '需要推算新的合计、比例或差额时，用 calculation 提交计算，得到核对结果后再引用该数值。')
        message += '\n\n可引用的来源和列\n' + '\n'.join(
            key + ' ' + name + '，指标 ' + '、'.join(c + ' ' + n for c, n in columns)
            + ('，可筛选维度 ' + '、'.join(k + ' ' + n for k, n in dims) if dims else '')
            for key, (_, _, name, columns, dims) in tables.items())
    started = perf_counter()
    usage = None
    calculation_status = None
    calculation_error = None

    async def write(instructions, data, contract):
        return await asyncio.wait_for(agent.model.generate_structured_output(
            [SystemMsg('query-summary', instructions), UserMsg('query-data', data)],
            structured_model=contract, max_tokens=1024), timeout=turn.service.review_budget.call_timeout_seconds)

    try:
        try:
            response = await write(system, message, schema)
        except StructuredOutputError:
            if 'calculation' not in schema['properties']:
                raise
            calculation_status = 'SIMPLIFIED_RESPONSE'
            response = await write(BRIEF_PROMPT, source, summary_schema)
        usage = response.usage
        if 'calculation' in response.content and 'calculation' in schema['properties']:
            from .query_brief_calculation import compute
            instructions, followup = BRIEF_PROMPT, source
            try:
                calculated = await compute(turn, response.content['calculation'], tables)
            except AnalysisError as error:
                calculation_status, calculation_error = 'SOURCE_ONLY', error.code
                instructions += '原表已成功取得。补充运算未得到可引用数值，请从表中直接可见的业务差异写起，省略需另行推算的数值。'
            else:
                calculation_status = 'COMPLETED'
                followup += '\n\n# 附加计算结果\n' + calculated
            try:
                checked = await write(instructions, followup, summary_schema)
            except Exception as error:
                calculation_status, calculation_error = 'SOURCE_SUMMARY_USED', calculation_error or type(error).__name__
            else:
                response = checked
                usage = _combined_usage(usage, checked.usage)
        summary = re.sub(r'^(?:#{1,6}\s+)?(?:\*\*)?AI\s*简述(?=\s|[：:*]|$)(?:\*\*)?\s*[：:]?(?:\*\*)?\s*',
                         '', response.content['summary'].strip(), count=1)
        if not summary:
            raise ValueError('empty summary')
        status = 'DIRECT'
        suffix = '\n\n' + escape(group_written_amounts(summary))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        status = 'PARTIAL'
        suffix = '\n\n数据已返回，文字说明暂未生成。'
        failure = type(exc).__name__
    record_timing(turn, 'queryBrief', (perf_counter() - started) * 1000, materialChars=len(message),
                  rowCount=result['rows'], status=status, inputTokens=getattr(usage, 'input_tokens', 0),
                  outputTokens=getattr(usage, 'output_tokens', 0), errorType=failure if status == 'PARTIAL' else None,
                  calculationStatus=calculation_status, calculationError=calculation_error)
    return suffix, usage, status


async def deliver(turn, agent, result):
    from .review_control import progress
    if result.get('resolvedBlocker') and result['resolvedBlocker'] == turn.state.get('blocked'):
        turn.state.pop('blocked')
    block = TextBlock(text=result['answer'])
    turn.state['streamedQueryBlock'] = block.id
    reply_id = agent.state.reply_id
    yield TextBlockStartEvent(reply_id=reply_id, block_id=block.id)
    yield TextBlockDeltaEvent(reply_id=reply_id, block_id=block.id, delta=block.text)
    closeout = {'status': 'DIRECT', 'semanticReview': 'NOT_REVIEWED', 'issues': [],
                'deliveredCaptureRefs': result['refs'], 'tablesShownAt': datetime.now(timezone.utc).isoformat()}
    usage, suffix = None, ''
    if result['emptyStatus']:
        closeout.update(deliveryStatus=result['emptyStatus'], reason=result['emptyStatus'])
    else:
        suffix, usage, status = await _write_brief(turn, agent, result)
        block.text += suffix
        closeout.update(status=status, deliveryStatus='SOURCE_WITH_AI' if status == 'DIRECT' else 'SOURCE_ONLY',
                        summaryFinishedAt=datetime.now(timezone.utc).isoformat())
    closeout['answer'] = block.text
    progress(turn, lambda saved: saved.update(closeout=closeout))
    turn.state['hostCloseout'] = closeout
    if suffix:
        yield TextBlockDeltaEvent(reply_id=reply_id, block_id=block.id, delta=suffix)
    yield TextBlockEndEvent(reply_id=reply_id, block_id=block.id)
    agent._save_to_context([block], usage)
    yield AssistantMsg(id=reply_id, name=agent.name, content=[block], usage=agent._get_reply_usage(),
                       finished_reason=ReplyFinishedReason.COMPLETED)
