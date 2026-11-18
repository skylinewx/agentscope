"""执行同一只读计划的明确续页；不替模型选择任务、扩展范围或重试业务失败。"""
from .query_results import encode

MAX_PAGES = 4
MAX_CONTINUATION_SECONDS = 40


def diagnostic_navigation(receipt, reference):
    """资料已带值和公式时，不因导航超限再次附上整份资料；异常仍逐项显式保留。"""
    diagnostics = []
    def visit(value, path=''):
        if isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, path + '/' + str(index))
        elif isinstance(value, dict):
            for key, item in value.items():
                pointer = path + '/' + key.replace('~', '~0').replace('/', '~1')
                if key in ('error', 'errors', 'problems', 'warnings') and item:
                    diagnostics.append({'pointer': pointer, 'value': item})
                elif key in ('status', 'disposition', 'coverage') and isinstance(item, str):
                    diagnostics.append({'pointer': pointer, 'value': item,
                                        **({'message': value['message']} if value.get('message') else {})})
                else:
                    visit(item, pointer)
    visit(receipt)
    return {'diagnostics': diagnostics, 'readTool': 'QueryResultRead',
            'read': {'captureRef': reference, 'pointer': ''},
            'note': '原值、公式、选择器及完整性见 analysisMaterials；其他原协议字段按 read 定向读取。此导航不替代完整回执。'}


def continuation_request(receipt, request):
    """只有分页是唯一停止原因时，使用来源返回的恢复标识。"""
    if (receipt.get('contractVersion') != '2.0' or receipt.get('status') != 'PARTIAL'
            or receipt.get('error') or not isinstance(receipt.get('resumeRef'), str) or not receipt['resumeRef']
            or (receipt.get('data') or {}).get('problems')):
        return None
    reasons = []
    branches = receipt.get('branches', [])
    if not isinstance(branches, list):
        return None
    for branch in branches:
        if not isinstance(branch, dict) or not isinstance(branch.get('enumeration') or {}, dict):
            return None
        stops = (branch.get('enumeration') or {}).get('stopReasons') or []
        if not isinstance(stops, list):
            return None
        reasons.extend(stops)
    if not reasons or any(reason != 'PAGE_BOUNDARY' for reason in reasons):
        return None
    context_id = (receipt.get('context') or {}).get('contextId')
    session_id = receipt.get('querySessionId')
    if (not context_id or not session_id or context_id != request.get('contextId')
            or session_id != request.get('querySessionId')):
        return None
    return {'contractVersion': '2.0', 'contextId': context_id, 'querySessionId': session_id,
            'requestId': 'continue-' + receipt['resumeRef'], 'resumeRef': receipt['resumeRef']}


def combined_response(capture, adapter, events, started, stopped_reason):
    """完整回执分别保存在原捕获记录；一次向模型返回各页的必要材料和恢复入口。"""
    from time import perf_counter
    from agentscope.message import TextBlock
    from .analysis_materials import materials
    from .models import AnalysisError
    from .query_results import compact_receipt, MODEL_CHARS, COMPACT_AT

    pages, refs, total_source_chars = [], [], 0
    for event in events:
        reference = event.metadata['analysisCaptureRef']
        saved = capture.store.get_capture(capture.owner, reference)
        receipt = adapter.raw_receipt(saved['response'])
        # 页面独立分配额度；最后一页的状态不能掩盖前页的缺数、错误和公式边界。
        budget = max(2500, (MODEL_CHARS - 1500) // len(events))
        try:
            material = materials(saved, capture.sources, max_chars=max(1500, budget - 1500), store=capture.store, owner=capture.owner)
        except AnalysisError as error:
            # 最后一页失败时也保留统一的最新状态，不能退回多段原文，
            # 让前页 PARTIAL 被误读为整次调用结果。原始失败页仍可定向读取。
            material = {'status': 'UNAVAILABLE', 'diagnostic': error.as_dict(),
                        'nextRead': {'section': 'receipt', 'captureRef': reference}}
        source_chars = sum(len(block.get('text', '')) for block in saved['response']['content'])
        projected = compact_receipt(receipt, reference, max(source_chars, COMPACT_AT),
                                    max_chars=max(1000, budget - len(encode(material))))
        page = {'captureRef': reference, 'analysisMaterials': material}
        # 附加非文本内容保留；单个 JSON 的导航过大则只给恢复入口与诊断，
        # 不能回退成“材料 + 原材料”双份输入，令自动续页反而放大上下文。
        if len(saved['response']['content']) != 1:
            page['content'] = [block.model_dump(mode='json') for block in event.content]
        else:
            page['receipt'] = projected if projected is not None else diagnostic_navigation(receipt, reference)
            # 网格文件与下载错误为独立原始资料入口，不随紧凑回执消失。
            if len(event.content) > 1:
                page['additionalContent'] = [block.model_dump(mode='json') for block in event.content[1:]]
        pages.append(page)
        refs.append(reference)
        total_source_chars += source_chars
    last = events[-1].model_copy(deep=True)
    payload = {key: receipt[key] for key in ('contractVersion', 'status', 'context', 'querySessionId', 'resumeRef', 'error') if key in receipt}
    payload.update({'automaticContinuation': {'pageCount': len(pages), 'stopReason': stopped_reason,
               'scope': 'SAME_READ_ONLY_PLAN', 'allPagesCaptured': True},
               'captureRefs': refs, 'pages': pages,
               'note': '这些是同一计划的连续回执，使用全部相关 captureRefs 构建分析。顶层 status/resumeRef 是最新状态；前页的 PAGE_BOUNDARY 已继续执行，不再恢复历史游标。每页原值、缺数和公式边界分别保留。'})
    last.content = [TextBlock(text=encode(payload))]
    last.metadata['analysisCaptureRefs'] = refs
    performance = last.metadata['queryPerformance'] = {
        'elapsedMs': round((perf_counter() - started) * 1000, 2), 'pageCount': len(events),
        'sourceChars': total_source_chars, 'modelChars': len(encode(payload)),
        'compacted': True, 'automaticContinuation': True,
        'pages': [event.metadata.get('queryPerformance', {}) for event in events]}
    # 分段是页级观测之和，包含于工具总耗时，不重复计时。
    for field in ('sourceResponseMs', 'gridReceiveMs', 'captureStoreMs', 'projectionMs'):
        performance[field] = round(sum(page.get(field, 0) for page in performance['pages']), 2)
    return last
