"""Bound model views of saved evidence; never mutate conversation or source data."""
from copy import deepcopy
import json

from .models import AnalysisError


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)


def source_navigation(service, references):
    sources = []
    for reference in dict.fromkeys(references):
        capture = service.store.get_capture(service.owner, reference)
        receipt = service.sources.for_capture(capture).raw_receipt(capture['response'])
        data = receipt.get('data') or {}
        sources.append({'captureRef': reference, 'status': receipt.get('status'),
                        'executionComplete': data.get('complete'),
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
            if any(not item.get('turnKey') or item['turnKey'] == turn.state.get('turnKey') for item in captures):
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
