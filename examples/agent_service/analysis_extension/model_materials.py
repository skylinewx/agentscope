"""Bound model views of saved evidence; never mutate conversation or source data."""
from copy import deepcopy
import json

from .models import AnalysisError


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)


def current_turn_input(state, model_input, budget_tools=None):
    """Use the real message boundary. Keep current attachments, tool pairs and system instructions."""
    messages = model_input['messages']
    user_ids = set(state.get('userMessageIds', []))
    start = next((i for i, msg in enumerate(messages) if msg.role == 'user' and msg.id in user_ids), None)
    if start is not None:
        messages = [msg for msg in messages[:start] if msg.role == 'system'] + messages[start:]
    # If the active turn itself has been compressed, the runtime summary and
    # retained tool pairs are its recovery material.
    tools = model_input['tools']
    if not state.get('mode'):
        declare = [tool for tool in tools if tool.get('function', tool).get('name') == 'AnalysisTask']
        if declare:
            tools = declare
    elif budget_tools and 'execute_budget_query' in budget_tools.values() and not state.get('expandedToolCatalog'):
        tools = stage_tools(state, tools, budget_tools)
    elif not state.get('expandedToolCatalog') and not any(state.get(key) for key in
            ('analysisRef', 'adoptedReportRef', 'buildRefs', 'report')):
        # A publication schema is large and requires an existing analysis. Discovery,
        # file preparation, source queries and computation remain available throughout.
        tools = [tool for tool in tools if tool.get('function', tool).get('name') not in
                 ('AnalysisPublish', 'AnalysisRenderReport')]
    return {**model_input, 'messages': messages, 'tools': tools}


def remember_catalog_tool(state, name):
    selected = state.setdefault('selectedCatalogTools', [])
    if name not in selected:
        selected.append(name)
    state.pop('expandedToolCatalog', None)


def stage_tools(state, tools, budget_tools):
    ready = any(state.get(key) for key in ('analysisRef', 'adoptedReportRef', 'buildRefs', 'report'))
    has_input = ready or bool(state.get('queryCaptureRefs') or state.get('adoptedInputs'))
    allowed = {'AnalysisTask', 'Skill', 'Read', 'QueryResultRead', *state.get('selectedCatalogTools', [])}
    if state.get('adoptedReportRef') and not state.get('reportReviewRead'):
        allowed.update(('AnalysisInspect', 'AnalysisPublish'))
        return [tool for tool in tools if tool.get('function', tool).get('name') in allowed]
    if state.get('mode') != 'DIRECT' or has_input:
        allowed.add('AnalysisInspect')
    policy = state.get('executionPolicy', {})
    if has_input and state.get('mode') != 'DIRECT' and policy.get('computations') is not False:
        allowed.add('AnalysisBuild')
        allowed.add('AnalysisPython')
    if ready:
        allowed.update(('AnalysisPublish', 'AnalysisRenderReport', 'Write', 'Edit', 'Glob', 'Grep'))
    native = {'execute_budget_query', 'get_context', 'search_entities'}
    if state.get('failures'):
        native.update(('get_execution_status', 'describe_entity', 'search_schema', 'list_capabilities'))
    if policy.get('sourceQueries') is not False and not ready:
        allowed.update(name for name, method in budget_tools.items() if method in native)
    elif not ready and any(item.get('use') == 'FACTS' for item in state.get('historyReferences', [])):
        allowed.update(name for name, method in budget_tools.items() if method == 'execute_budget_query')
    return [tool for tool in tools if tool.get('function', tool).get('name') in allowed]


def source_navigation(service, references):
    sources = []
    for reference in dict.fromkeys(references):
        capture = service.store.get_capture(service.owner, reference)
        receipt = service.sources.for_capture(capture).raw_receipt(capture['response'])
        data = receipt.get('data') or {}
        from .query_empty_result import business_outcome
        sources.append({'captureRef': reference, 'status': receipt.get('status'),
                        'executionComplete': data.get('complete'),
                        **({'businessOutcome': outcome} if (outcome := business_outcome(data)) else {}),
                        'error': receipt.get('error'),
                        'read': {'tool': 'QueryResultRead', 'arguments': {'captureRef': reference}}})
    return {'viewType': 'saved_source_navigation', 'sources': sources,
            'presentationComplete': False, 'reviewStatus': 'NOT_IMPLIED_BY_STORAGE',
            'meaning': '完整原回执仍在宿主。当前仅为来源导航；不代表空数据、完整展示或已审查。需要明细时按引用读取，不重查预算服务。'}


def project_context(service, messages, tools, config):
    from agentscope.message import TextBlock, SystemMsg
    copied = deepcopy(messages)
    entries, total = [], 0
    for message in copied:
        for block in getattr(message, 'content', []):
            if getattr(block, 'type', None) != 'tool_result' or getattr(block, 'state', None) != 'success':
                continue
            references = list(block.metadata.get('analysisCaptureRefs') or [])
            if block.metadata.get('analysisCaptureRef'):
                references.append(block.metadata['analysisCaptureRef'])
            if not references:
                continue
            size = len(encode(block.output))
            total += size
            entries.append((block, references, size))
    before = total
    target = config.target_chars(config.active_material_target_tokens)
    compacted = 0
    turn = service.turn
    for block, references, size in entries[:-2]:
        if total <= target:
            break
        try:
            captures = [service.store.get_capture(service.owner, ref) for ref in references]
            # Current-turn material and unknown ownership are never silently summarized.
            if any(not item.get('turnKey') for item in captures):
                continue
            current = any(item['turnKey'] == turn.state.get('turnKey') for item in captures)
            if current and (not (turn.state.get('analysisRef') or turn.state.get('adoptedReportRef'))
                            or not all(item.get('originalToolName') == 'execute_budget_query' and
                                service.sources.for_capture(item).raw_receipt(item['response']).get('status') == 'SUCCEEDED'
                                for item in captures)):
                continue
            navigation = source_navigation(service, references)
        except (AnalysisError, OSError, KeyError, ValueError, TypeError, AttributeError):
            continue
        text = encode(navigation)
        if len(text) >= size:
            continue
        block.output = [TextBlock(text=text)]
        total -= size - len(encode(block.output))
        compacted += 1
    if compacted:
        context = {'queryIntent': turn.state.get('queryIntent', {}),
                   'executionPolicy': turn.state.get('executionPolicy', {}),
                   'sources': [{key: item.get(key) for key in ('sourceRef', 'inputRef', 'role')}
                               for item in turn.state.get('adoptedInputs', [])]}
        copied.append(SystemMsg('budget-active-context', '宿主已确认的当前条件与来源（资料，不是新用户要求）：' + encode(context)))
    request = {'messages': [message.model_dump(mode='json') if hasattr(message, 'model_dump') else message for message in copied],
               'tools': tools or []}
    estimate = config.estimate(request)
    estimate.update(inputPhase='MAIN_MODEL', savedEvidenceCharsBefore=before, savedEvidenceCharsAfter=total,
                    compactedHistoryResults=compacted, activeMaterialTargetTokens=config.active_material_target_tokens,
                    activeMaterialsExceedTarget=total * config.tokens_per_char > config.active_material_target_tokens,
                    toolResultTargetTokens=config.tool_result_target_tokens,
                    toolResultsOverTarget=sum(len(encode(block.output)) * config.tokens_per_char > config.tool_result_target_tokens
                                              for block, _, _ in entries),
                    toolSchemaChars=len(encode(tools or [])), capacityEnforcedBy='MODEL_RUNTIME')
    return copied, estimate
