"""裁剪回执是完整观察的派生范围；不把局部材料解释成整表或重新发现业务口径。"""
import re
from urllib.parse import urlsplit

from .models import AnalysisError


def resolved_form_query(query, receipt, step_id):
    """本体负责恢复已选候选条件；消费已捕获回执中的规范化参数，不让模型重新抄写维值。"""
    if not query.get('targetRefs'):
        return query
    steps = receipt.get('data', {}).get('normalizedPlan', {}).get('steps', [])
    resolved = next((step.get('formQuery') for step in steps if step.get('id') == step_id), None)
    if not isinstance(resolved, dict) or resolved.get('targetRefs') != query['targetRefs']:
        raise AnalysisError('GRID_TARGET_REFERENCE_MISMATCH', '本体未返回本次已选目标的解析结果。')
    # 只允许本体补齐目标条件，时期、表外维度及调用方明确的条件均须保持。
    for key, value in query.items():
        if key == 'targetDimensions':
            if any(resolved.get(key, {}).get(dim) != member for dim, member in value.items()):
                raise AnalysisError('GRID_TARGET_REFERENCE_MISMATCH', '已选目标改变了调用方明确的筛选条件。')
        elif resolved.get(key) != value:
            raise AnalysisError('GRID_TARGET_REFERENCE_MISMATCH', '已选目标改变了原查询参数。')
    for key, value in resolved.items():
        if key not in query and key not in {'targetDimensions', 'measureCode', 'headerText'} and value not in (None, False, {}, []):
            raise AnalysisError('GRID_TARGET_REFERENCE_MISMATCH', '目标解析添加了无关运行条件。')
    if not any(resolved.get(key) for key in ('targetDimensions', 'measureCode', 'headerText')):
        raise AnalysisError('GRID_TARGET_REFERENCE_MISMATCH', '已选目标没有恢复可用于裁剪的条件。')
    return resolved


def validate_selection(form, query=None, upgraded=False):
    selection = form.get('selection')
    if selection is None:
        return None
    if not isinstance(selection, dict) or not isinstance(selection.get('filter'), dict):
        raise AnalysisError('GRID_SELECTION_INVALID', '裁剪材料缺少实际筛选条件。')
    counts = [selection.get(key) for key in ('sourceCellCount', 'targetCellCount', 'returnedCellCount')]
    if (any(type(value) is not int or value < 0 for value in counts)
            or not counts[1] <= counts[2] <= counts[0]
            or type(selection.get('complete')) is not bool
            or not re.fullmatch('[0-9a-f]{64}', str(selection.get('contentDigest', '')))):
        raise AnalysisError('GRID_SELECTION_INVALID', '裁剪范围计数或摘要不完整。')
    if query is not None:
        expected = {key: query.get(key) for key in ('measureCode', 'expression', 'headerText', 'cellKey')}
        expected.update(targetDimensions=query.get('targetDimensions') or {},
                        includeFormulaDependencies=query.get('includeFormulaDependencies', False))
        if query.get('modelCode') is not None or 'modelCode' in selection['filter']:
            expected['modelCode'] = query.get('modelCode')
        # FOR_GRID 升级到 INPUT_VO 时，来源服务用旧坐标的表达式重新定位，不能复用旧坐标。
        if upgraded and expected['cellKey'] and form.get('matchedExpression'):
            expected['cellKey'] = None
            expected['expression'] = form['matchedExpression']
        if expected != selection['filter']:
            raise AnalysisError('GRID_SELECTION_MISMATCH', '裁剪条件与本次查询的已确认目标不一致。')
    page, file = form.get('page'), form.get('file')
    if file:
        if file.get('cellCount') != counts[2] or file.get('sha256') != selection['contentDigest']:
            raise AnalysisError('GRID_SELECTION_MISMATCH', '裁剪附件的计数或摘要不匹配。')
    elif page:
        if (page.get('startCell') != 0 or page.get('endCellExclusive') != counts[2]
                or page.get('totalCells') != counts[2] or len(page.get('cells', [])) != counts[2]
                or page.get('observationExhausted') is not True or page.get('nextCursor') is not None):
            raise AnalysisError('GRID_SELECTION_MISMATCH', '裁剪材料没有完整返回声明的范围。')
    else:
        raise AnalysisError('GRID_SELECTION_INVALID', '裁剪结果没有数据页或附件。')
    return selection


def download_request(form, expected_path):
    """只允许当前观察的同源只读端点；复杂条件通过 JSON body 传递。"""
    manifest = form['file']
    path = urlsplit(manifest.get('downloadPath', ''))
    selection = validate_selection(form)
    method = 'POST' if selection else 'GET'
    expected = expected_path + ('/selection' if selection else '')
    if (path.scheme or path.netloc or path.fragment or path.query or path.path != expected
            or manifest.get('method', 'GET') != method):
        raise AnalysisError('GRID_FILE_MANIFEST', '网格下载方式或路径与来源观察不一致。')
    return method, expected, ({'json': selection['filter']} if selection else {})
