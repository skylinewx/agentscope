"""为已捕获查询提供即用材料；来源范围、原值、公式与真实选择器保持不变。"""
from collections import defaultdict
from copy import deepcopy

from .models import AnalysisError, digest
from .query_results import encode

MATERIAL_CHARS = 14000


def share_item_fields(rows):
    """提取同页完全相同的字段和维度键；缺失字段与显式 null 仍可无损恢复。"""
    if len(rows) < 2:
        return {}, rows
    shared = {}
    for field, first in rows[0].items():
        # 每项原值和状态直接可见，不为压缩把金额隐藏到公共字段中。
        if field in ('factId', 'value', 'valueState') or any(field not in row for row in rows):
            continue
        if field in ('dimensions', 'dimensionNames') and isinstance(first, dict):
            common = {key: value for key, value in first.items()
                      if all(isinstance(row[field], dict) and key in row[field]
                             and digest(row[field][key]) == digest(value) for row in rows)}
            if common:
                shared[field] = common
        elif all(digest(row[field]) == digest(first) for row in rows):
            shared[field] = first
    compact = deepcopy(rows)
    for row in compact:
        for field, value in shared.items():
            if field in ('dimensions', 'dimensionNames') and isinstance(value, dict):
                row[field] = {key: item for key, item in row[field].items() if key not in value}
                if not row[field]:
                    row.pop(field)
            else:
                row.pop(field)
    return deepcopy(shared), compact


def selection_candidates(capture, sources, catalog=None):
    """补齐网格选择器，目录超限时也能沿真实入口恢复同一组单元格。"""
    adapter = sources.for_capture(capture)
    candidates = adapter.selectors(capture)
    from .prepared_inputs import summaries
    prepared = summaries(capture) if adapter.adapter_id == 'budget-v2' else []
    prepared_keys = {(item['stepId'], item['observationId']) for item in prepared if item.get('selection')}
    candidates.extend(item['selection'] for item in prepared if item.get('selection'))
    groups = defaultdict(list)
    targets = defaultdict(list)
    for item in adapter.catalog(capture) if catalog is None else catalog:
        if item.get('batchId') or not item.get('cellKey'):
            continue
        if item.get('measureCode') or item.get('formula'):
            groups[(item.get('stepId'), item.get('observationId'))].append(item['cellKey'])
        if item.get('formulaTarget'):
            targets[(item.get('stepId'), item.get('observationId'))].append(item['cellKey'])
    for (step, observation), keys in groups.items():
        if (step, observation) in prepared_keys:
            continue
        candidates.append({'captureRef': capture['captureRef'], 'stepId': step, 'observationId': observation,
                           'cellKeys': list(dict.fromkeys(targets.get((step, observation), keys))),
                           **({'includeFormulaDependencies': True} if (step, observation) in targets else {})})
    return candidates


def material_capture(capture, sources, store=None, owner=None):
    """续查沿已证明的当前会话链恢复原计划；不按表名或业务内容猜关联。"""
    if store is not None and sources.for_capture(capture).adapter_id == 'budget-v2':
        from .adapters.budget_v2 import stored_form_request
        return stored_form_request(store, owner, capture)
    return capture


def materials(capture, sources, *, offset=0, limit=100, max_chars=MATERIAL_CHARS, store=None, owner=None):
    """只投影这一份回执，不读取其他会话、不选业务口径、不发出远端请求。"""
    capture = material_capture(capture, sources, store, owner)
    adapter = sources.for_capture(capture)
    reference = capture['captureRef']
    info = adapter.receipt_info(capture)
    catalog = adapter.catalog(capture)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise AnalysisError('INVALID_QUERY_PAGE', '资料 offset 须非负，limit 须为 1—100。')
    source_state = {key: info[key] for key in ('status', 'context', 'querySessionId', 'error', 'branches', 'completeness', 'resumeRef') if key in info}
    selections = []
    # 批量表由适配器给出指标列和分组维度，不能把属性组还原为机构明细。
    for item in selection_candidates(capture, sources, catalog):
        if item.get('batchId') and item.get('columnNames'):
            selections.append({key: item[key] for key in ('captureRef', 'stepId', 'batchId', 'columnNames')})
        elif item.get('cellKeys') or item.get('preparedTargets'):
            selections.append(item)
    rows = []
    contexts = {}
    fields = ('factId', 'stepId', 'batchId', 'rowIndex', 'columnName', 'cellKey', 'cellAddress', 'headers',
              'modelCode', 'measureCode', 'value', 'valueState', 'unit', 'formula', 'formulaStructure', 'formulaTarget', 'dimensions', 'dimensionNames')
    for item in catalog[offset:offset + limit]:
        context = {key: item.get(key) for key in ('taskName', 'formName', 'bindingRef', 'observationId',
                                                 'period', 'executionPeriod', 'effectiveDimensions', 'dimensionModels')}
        context_ref = 'context_' + digest(context)[:24]
        contexts[context_ref] = context
        rows.append({**{key: item[key] for key in fields if key in item}, 'contextRef': context_ref})
    result = {'captureRef': reference, 'sourceState': source_state, 'selections': selections,
              'selectionMeaning': '这些选择器覆盖本回执已取得的数据；由模型按本题选用或缩小，不自动代表本题全部主体。',
              'items': rows, 'contexts': contexts, 'total': len(catalog), 'offset': offset,
              'selectionRead': {'section': 'selectors', 'captureRef': reference},
              'analysisRead': {'section': 'materials', 'captureRef': reference},
              'pythonInputFields': ['ref', 'value', 'valueState', 'unit', 'dimensions', 'headers', 'formula',
                                    'stepId', 'observationId', 'batchId', 'rowIndex', 'columnName', 'cellKey'],
              'note': '已展示的值和公式可直接分析；构建时使用真实 selections 与本题 scope。多个对象/部分用 requests 批量读取；相关计算一次提交，缺字段才补读。'}
    if adapter.adapter_id == 'budget-v2':
        from .prepared_inputs import summaries
        prepared = summaries(capture)
        if prepared:
            result['preparedTargets'] = prepared
            result['note'] += ' 唯一目标的构建可仅传 captureRefs、scope、operations、requestKey。多个候选用 preparedTargets 与已确认的 targetDimensions 选择，依赖和公式检查由宿主补齐。'
            result['fieldViews'] = {'analysis': 'AnalysisInspect 的 materials/inventory 使用 cellKey、value、factId',
                                    'source': 'QueryResultRead 的原始 cells 使用 key、rawValueJson、displayValue'}
    if any(item.get('cellKeys') or item.get('preparedTargets') for item in selections):
        result['gridSelectionContract'] = ('cellKeys 复用实际 selections，只缩小已确认范围，不按起止行号自行补齐空白行。'
            '网格与批量表混用时，各 selection 指定 role 与 grain；grain 从实际 dimensions/effectiveDimensions 选择，成员由所选数据生成。'
            '若不声明逐输入 role/grain，网格仍须提供 scope.memberDimension 与 inventory 中的真实 members。'
            '本回执只代表已捕获资料，失败分支不能当作已取得；追问原主体不因局部输入失败而缩小。')
    if adapter.adapter_id == 'budget-v2' and selections and not info.get('gridFiles'):
        result['buildInput'] = {'captureRefs': [reference]}
        result['buildInputMeaning'] = '使用所选回执与本题 scope、operations、requestKey 构建。多个来源分别承担主体或对照、需选子集时显式传 selections。'
    # 大网格不展开目录，直接给出可执行输入引用及运行时字段。
    if info.get('gridFiles') or info.get('gridFileErrors'):
        result['gridFiles'] = info.get('gridFiles', [])
        result['gridFileErrors'] = info.get('gridFileErrors', [])
        result['pythonInput'] = {'gridInputs': [{'captureRef': reference, 'fileRef': item['ref']}
                                               for item in info.get('gridFiles', [])]}
        result['note'] += ' 文件是已裁剪的数据集合。需要扫描多个候选时使用 pythonInput.gridInputs，目标个数不代表需要逐格复算。'
    if len(encode(result)) > max_chars and rows:
        shared, compact = share_item_fields(rows)
        if shared:
            candidate = {**result, 'items': compact, 'fixedItemFields': shared,
                         'itemProjection': '每项与 fixedItemFields 合并；dimensions、dimensionNames 按键合并，项内字段优先。仅提取完全相同字段，未省略数据行；value/valueState 保留在各项。'}
            if len(encode(candidate)) < len(encode(result)):
                result = candidate
    if len(encode(result)) > max_chars and selections:
        result.pop('selections')
        result['selectionRead'] = {'section': 'selectors', 'captureRef': reference}
    while len(encode(result)) > max_chars - 400 and result['items']:
        result['items'] = result['items'][:len(result['items']) // 2]
        used = {item.get('contextRef', result.get('fixedItemFields', {}).get('contextRef')) for item in result['items']}
        result['contexts'] = {key: value for key, value in contexts.items() if key in used}
    returned = len(result['items'])
    result.update(returnedCount=returned, catalogComplete=offset + returned >= len(catalog),
                  nextOffset=offset + returned if offset + returned < len(catalog) else None)
    if result['nextOffset'] is not None:
        # 单项超限不返回原地续页，交给已有原回执读取或文件处理入口。
        if returned:
            result['nextRead'] = {'section': 'materials', 'captureRef': reference, 'offset': result['nextOffset'], 'limit': limit}
        else:
            result['detailRead'] = {'section': 'inventory', 'captureRef': reference, 'offset': offset, 'limit': 1}
            result.pop('nextOffset')
    if len(encode(result)) > max_chars:
        return {'captureRef': reference, 'analysisRead': {'section': 'inventory', 'captureRef': reference},
                'selectionRead': {'section': 'selectors', 'captureRef': reference}, 'catalogComplete': False,
                'note': '资料元数据较大，按这两个入口定向读取；原始回执完整保留。'}
    return result


def response_materials(capture, sources, *, store=None, owner=None):
    """只有数据回执增加资料；发现和能力查询继续使用原协议。"""
    adapter = sources.for_capture(capture)
    try:
        if not adapter.is_data(capture):
            return None
        result = materials(capture, sources, store=store, owner=owner)
    except (AnalysisError, KeyError, TypeError, ValueError) as exc:
        # 资料投影不能把源系统失败或不完整回执伪装成空数据。
        import logging
        logging.getLogger(__name__).warning('分析资料投影失败：回执=%s 原因=%s',
                                           capture.get('captureRef'), getattr(exc, 'code', type(exc).__name__))
        return None
    if not result.get('total') and not result.get('gridFiles') and not result.get('gridFileErrors'):
        return None
    return result
