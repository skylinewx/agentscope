"""Field descriptions generated from the exact runtime input projection."""
from copy import deepcopy

CONTRACT_VERSION = 'ANALYSIS_INPUTS_V3'
DESCRIPTIONS = {
    'ref': '当前冻结输入引用；产物 inputRefs 使用此值。',
    'value': '精确原值；数值字符串用 Decimal，null 保留来源空值。',
    'valueState': '来源取值状态；未取得、失败与已取得空值不同。',
    'headers': '真实来源行列标题数组；不假设存在 label。',
    'sourceMeasureCode': '来源指标列编码；不是完整业务记录身份。',
    'dimensions': '维度标识映射到成员编码，按编码筛选和分组。预算维度的合计成员编码是字符串 00000000。',
    'dimensionNames': '维度标识映射到该条记录的成员显示名称。维度本身的名称来自表元数据。',
    'period': '该值的实际数据时期。',
    'executionPeriod': '执行／开表时期，可能不同于该值的实际时期。',
    'inputRole': '主体、对照或结构用途；重叠群体分别统计。',
    'inputMemberships': '同一事实属于各输入群体的用途和粒度，不能合并人数。',
    'columns': '本表实际列定义；业务列可随查询或自由分析变化。',
    'rows': '完整业务表格行：row.values[column.name] 读取，row.valueStates 保留空值和缺字段；row.metadata 保留来源上下文。原始网格单元格仍由 grid_rows 读取，不按表格列下标访问。',
    'formulaTrace': '来源公式节点和依赖，不转换为固定业务列。',
    'data': '当前文件／诊断的实际结构；读取方式由其 kind 决定。',
}


def _kind(value):
    if value is None: return 'null'
    if isinstance(value, bool): return 'boolean'
    if isinstance(value, str): return 'string'
    if isinstance(value, (int, float)): return 'number'
    if isinstance(value, list): return 'array'
    if isinstance(value, dict): return 'object'
    return type(value).__name__


def sample_records(records, *, limit=2, offset=0):
    omitted = []
    def sample(value, path, depth=0):
        if isinstance(value, list):
            if len(value) > 3: omitted.append({'path': path, 'totalCount': len(value), 'shownCount': 3})
            return [sample(item, f'{path}/{index}', depth + 1) for index, item in enumerate(value[:3])]
        if isinstance(value, dict):
            keys = list(value)
            if depth > 6 or len(keys) > 24:
                shown = 0 if depth > 6 else 24
                omitted.append({'path': path, 'totalCount': len(keys), 'shownCount': shown})
                keys = keys[:shown]
            return {key: sample(value[key], path + '/' + key, depth + 1) for key in keys}
        if isinstance(value, str) and len(value) > 500:
            omitted.append({'path': path, 'totalCharacters': len(value), 'shownCharacters': 500})
            return value[:500]
        return deepcopy(value)
    selected = [sample(record, '/examples/' + str(index)) for index, record in enumerate(records[offset:offset + limit])]
    return {'examples': selected, 'exampleOmissions': omitted,
            'examplesAreCompleteData': offset == 0 and len(records) <= limit and not omitted}


def field_catalog(records, *, examples=2, offset=0):
    fields = {}
    for record in records:
        for key, value in record.items():
            entry = fields.setdefault(key, {'name': key, 'types': set(), 'presentCount': 0})
            entry['types'].add(_kind(value)); entry['presentCount'] += 1
            if isinstance(value, dict):
                entry.setdefault('keys', set()).update(value)
    return {'contractVersion': CONTRACT_VERSION, 'recordCount': len(records),
            'outputContract': '产物列表赋给 result，或作为最后一个表达式；显式 result 优先。每项 name/label/inputRefs，产物遵循实际工具的 SCALAR/TABLE/CHART_DATA/DIAGNOSTIC/FILE 契约；inputRefs 要覆盖实际使用的数据，不用示例引用代替全部依据。',
            'fields': [{**{key: sorted(value) if isinstance(value, set) else value for key, value in entry.items()},
                        **({'meaning': DESCRIPTIONS[name]} if name in DESCRIPTIONS else {})}
                       for name, entry in fields.items()],
            **sample_records(records, limit=examples, offset=offset),
            'meaning': '目录来自本次 inputs 的真实字段；示例仅说明结构。实际计算由宿主注入全部已选择输入，不按示例条数截断。自由脚本可以创建任意内部字段和多种产物。'}
