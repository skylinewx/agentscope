"""按模型给定的范围合并只读操作；分页不改变筛选条件或来源完整性。"""
import asyncio
from copy import deepcopy

from .models import AnalysisError
from .query_results import encode

MAX_REQUESTS = 12
MAX_CHARS = 24000
MAX_PAGES = 100
CAPTURE_SECTIONS = ('materials', 'inventory', 'entities', 'selectors', 'capture', 'receipt', 'rows', 'gridFiles')
ANALYSIS_SECTIONS = ('facts', 'derived', 'formulas', 'summary', 'claims', 'verificationRecords', 'relations', 'order', 'products')


def validate_inspect_requests(requests):
    """只允许真正按给定引用读取的区段；不能用无效引用掩盖全会话扫描。"""
    for index, request in enumerate(requests if isinstance(requests, list) else []):
        if not isinstance(request, dict):
            continue
        section = request.get('section')
        required = ('captureRef',) if section in CAPTURE_SECTIONS else ('analysisRef',) if section in ANALYSIS_SECTIONS else (
            ('pythonRunRef', 'analysisRef') if section == 'python' else ())
        if not required or not any(request.get(key) for key in required):
            raise AnalysisError('READ_SCOPE_REQUIRED', '批量项须选择按实际引用读取的 section，并提供该区段需要的引用。',
                                {'requestIndex': index, 'section': section, 'requiredOneOf': list(required)})


def continuation(page, request):
    """兼容顶层分页和公式节点、表产物的嵌套分页。"""
    if isinstance(page.get('nextRead'), dict):
        return deepcopy(page['nextRead'])
    offsets = [page.get('nextOffset')]
    offsets.extend(value.get('nextOffset') for value in page.values()
                   if isinstance(value, dict) and 'items' in value and 'total' in value)
    offsets = [value for value in offsets if type(value) is int]
    return {**request, 'offset': min(offsets)} if offsets else None


async def read_batch(requests, all_pages, reader, *, allowed_fields):
    """批量返回成功、失败及未读部分；大资料继续引用已保存原件，不复制到上下文。"""
    if (not isinstance(requests, list) or not 1 <= len(requests) <= MAX_REQUESTS
            or type(all_pages) is not bool):
        raise AnalysisError('INVALID_READ_BATCH', 'requests 使用 1—12 个读取请求，allPages 使用布尔值。')
    for index, request in enumerate(requests):
        if not isinstance(request, dict) or not request or set(request) - allowed_fields:
            raise AnalysisError('INVALID_READ_BATCH', '批量项只接受本工具的单次读取参数。', {'requestIndex': index})
        if not request.get('captureRef') and not request.get('analysisRef') and not request.get('pythonRunRef'):
            raise AnalysisError('READ_SCOPE_REQUIRED', '批量读取须明确 captureRef、analysisRef 或 pythonRunRef，不自动扫描会话历史。',
                                {'requestIndex': index})
    # 为每项预留额度，避免第一项大资料挤掉后续对象；预算只控制模型投影。
    item_budget = (MAX_CHARS - 1200) // len(requests)

    async def read_one(index, original):
        current = deepcopy(original)
        pages = []
        result = {'requestIndex': index, 'request': deepcopy(original), 'pages': pages,
                  'status': 'COMPLETE', 'readComplete': False}
        if len(encode(result)) > item_budget - 400:
            raise AnalysisError('READ_REQUEST_TOO_LARGE', '批量读取参数过长，请缩小本次请求数量。')
        for _ in range(MAX_PAGES):
            try:
                page = await reader(**current)
            except AnalysisError as error:
                if error.code in ('ACCESS_DENIED', 'STORE_UNAVAILABLE', 'DIGEST_MISMATCH', 'MISSING_CONTENT'):
                    raise
                result.update(status='ERROR', error=error.as_dict())
                # 底层错误的可用字段可能很长；原请求仍可单独复现完整诊断。
                if len(encode(result)) > item_budget - 400:
                    result['error'] = {'code': error.code, 'message': str(error), 'detailRead': original}
                return result
            if len(encode({**result, 'pages': pages + [page]})) > item_budget - 400:
                # 缩页只影响读取，不修改原回执、过滤条件或业务范围。
                limit = current.get('limit', 20)
                if limit > 1:
                    current = {**current, 'limit': max(1, limit // 2)}
                    continue
                result.update(status='PARTIAL', nextRead=current, sourceAlreadyStored=True)
                break
            pages.append(page)
            following = continuation(page, current)
            if following is None:
                result['readComplete'] = page.get('catalogComplete') is not False
                if not result['readComplete'] and page.get('detailRead'):
                    result['nextRead'] = page['detailRead']
                break
            if following.get('offset', 0) <= current.get('offset', 0):
                raise AnalysisError('READ_CURSOR_INVALID', '读取续页没有前进，已停止而未扩大范围。')
            result.update(status='PARTIAL', nextRead=following)
            if not all_pages:
                break
            current = following
        if result['readComplete']:
            result['status'] = 'COMPLETE'
            result.pop('nextRead', None)
        elif result['status'] != 'ERROR':
            result.update(status='PARTIAL', nextRead=current if len(pages) == 0 else result.get('nextRead', current),
                          sourceAlreadyStored=True)
        result['pageCount'] = len(pages)
        return result

    results = await asyncio.gather(*(read_one(index, request) for index, request in enumerate(requests)))
    output = {'results': results, 'readComplete': all(item['readComplete'] for item in results),
              'sourceQueryExecuted': False,
              'note': 'readComplete 只表示指定已保存片段读完，不代表来源查询完整；来源状态按各页回执判断。大网格用已有 gridInputs，不重复查询。'}
    pending = [item['nextRead'] for item in results if item.get('nextRead')]
    if pending:
        output['nextBatch'] = {'requests': pending, 'allPages': all_pages}
        # 续读参数很长时仍保留各项 nextRead，不重复扩张回包。
        if len(encode(output)) > MAX_CHARS:
            output.pop('nextBatch')
    if len(encode(output)) > MAX_CHARS:
        raise AnalysisError('READ_BATCH_LIMIT', '批量读取投影超过容量，请减少同次请求数；原始资料仍已保存。')
    return output
