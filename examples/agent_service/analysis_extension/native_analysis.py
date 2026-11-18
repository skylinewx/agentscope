"""通用来源数值统计与可直接绑定的结果目录，不选择业务分析流程。"""
from copy import deepcopy
from decimal import Decimal, Inexact, Rounded, ROUND_HALF_EVEN, localcontext

from .models import AnalysisError, decimal_text, decimal_value, digest, precise_sum

CAPABILITY_VERSION = 'native-analysis-20261102-formula-work'
STATISTIC_FIELDS = ('sum', 'positiveSum', 'negativeSum', 'minimum', 'maximum', 'mean', 'topSum', 'topShare')


def capabilities():
    """让模型一次了解能力与边界，无需试调用来发现是否支持。"""
    return {
        'version': CAPABILITY_VERSION,
        'preferredExecution': 'AnalysisBuild.operations',
        'operations': {
            'DESCRIBE': '同一指标的来源值分布：正负零及缺值计数、数值合计、均值、极值、排序、占比和可选前N项。直接返回表格和标量，不证明业务合并关系。',
            'SORT/COUNT/RANK': '排序与计数；RANK 另需明确可比条件。',
            'FORMULA_CHECK': '按来源公式计算，本层与逐层状态分开；直接提供公式数据集、明细、差额和可用瀑布区间。',
            'ARITHMETIC/DIFFERENCE/RATIO': '精确算术、差额与比率；支持引用前序结果。ARITHMETIC 的 inputRefs 可为 {valueRef,field,nodeRef?}，按顺序用 v0/v1；不传 variables。',
            'SUM/LINEAR/CONVERT/RECONCILE': '基于来源或已登记规则的业务加总、线性计算、换算及汇总核对。',
        },
        'reuse': '优先使用已返回的 derivedValues 与 documentInputs；一次提交必要计算，不重算来源聚合，不重写来源公式。',
        'customCode': '现有能力不能表达本题必要的文件扫描、连接、透视或自定义算法时使用 AnalysisPython；填写 fallbackReason 与 purpose，不要求先试错。',
    }


def describe(items, scope, operation):
    """统计当前显式集合；空值不补零，不把观测数值之和当成业务合并数。"""
    from .comparability import member_dimensions
    if not items or any('factId' not in item for item in items):
        raise AnalysisError('FACTS_REQUIRED', 'DESCRIBE 需要非空的逐对象来源事实。')
    if len({item['objectId'] for item in items}) != len(items):
        raise AnalysisError('DUPLICATE_MEMBER', 'DESCRIBE 每个对象仅选同一指标的一条事实。')
    varying = member_dimensions(items, scope)
    signatures = {digest({
        **{key: item.get(key) for key in ('period', 'bindingRef', 'measureCode', 'unit', 'scale', 'sourceOrigins')},
        'metricKey': item.get('metricKey', item.get('cellKey')),
        'context': item.get('dataContext', item.get('context')),
        'grain': sorted(item.get('grain', [])),
        'dimensions': {key: value for key, value in item.get('dimensions', {}).items() if key not in varying},
    }) for item in items}
    if len(signatures) != 1:
        raise AnalysisError('METRIC_CONFLICT', 'DESCRIBE 不能混合指标、时期、单位、粒度或来源口径；请按实际输入分别统计。')
    places = operation.get('decimalPlaces', 2)
    top = operation.get('top')
    direction = operation.get('direction', 'desc')
    if type(places) is not int or not 0 <= places <= 16:
        raise AnalysisError('INVALID_ROUNDING', '统计精度必须在零到十六位之间。')
    if top is not None and (type(top) is not int or not 1 <= top <= 1000):
        raise AnalysisError('INVALID_TOP', 'top 为需要统计的前 1 至 1000 项。')
    if direction not in ('asc', 'desc'):
        raise AnalysisError('INVALID_DIRECTION', '排序方向必须为 asc 或 desc。')
    valid, missing = [], []
    for item in items:
        if item.get('valueState') != 'PRESENT':
            missing.append(item)
            continue
        if item.get('precision') == 'display_only' or item.get('calculationValidation', 'PASS') != 'PASS':
            raise AnalysisError('PRECISION_UNPROVEN', '统计需要原始精确数值，不能使用显示舍入值或失败的计算。')
        valid.append((item, decimal_value(item['value'])))
    valid.sort(key=lambda pair: (pair[0]['objectId'], pair[0]['factId']))
    valid.sort(key=lambda pair: pair[1], reverse=direction == 'desc')
    numbers = [number for _, number in valid]
    total = precise_sum(numbers) if numbers else None
    positive = [number for number in numbers if number > 0]
    negative = [number for number in numbers if number < 0]
    counts = {'total': len(items), 'observed': len(valid), 'unavailable': len(missing),
              'positive': len(positive), 'negative': len(negative), 'zero': sum(number == 0 for number in numbers)}

    def rounded_ratio(numerator, denominator, factor=Decimal(1)):
        if numerator is None or denominator in (None, Decimal(0)):
            return None
        with localcontext() as context:
            context.prec = 512
            context.traps[Inexact] = False
            context.traps[Rounded] = False
            return decimal_text((numerator / denominator * factor).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN))

    top_sum = precise_sum(numbers[:top]) if top is not None and numbers else None
    statistics = {
        'sum': decimal_text(total) if total is not None else None,
        'positiveSum': decimal_text(precise_sum(positive)) if numbers else None,
        'negativeSum': decimal_text(precise_sum(negative)) if numbers else None,
        'minimum': decimal_text(min(numbers)) if numbers else None,
        'maximum': decimal_text(max(numbers)) if numbers else None,
        'mean': rounded_ratio(total, Decimal(len(numbers))),
        'topSum': decimal_text(top_sum) if top_sum is not None else None,
        'topShare': rounded_ratio(top_sum, total, Decimal(100)),
    }
    rows = []
    for position, (item, number) in enumerate(valid, 1):
        rows.append([item['factId'], item.get('objectLabel') or item['objectId'], str(position),
                     item['value'], '正值' if number > 0 else '负值' if number < 0 else '零值',
                     rounded_ratio(number, total, Decimal(100))])
    for item in missing:
        rows.append([item['factId'], item.get('objectLabel') or item['objectId'], None, None, '无数值', None])
    unit = items[0].get('unit')
    return {
        'value': statistics['sum'], 'unit': unit, 'counts': counts, 'statistics': statistics,
        'top': top, 'direction': direction, 'outputKind': 'TABLE', 'rowCount': len(rows),
        'columns': [
            {'name': 'factRef', 'label': '来源引用', 'type': 'STRING'},
            {'name': 'object', 'label': '对象', 'type': 'STRING'},
            {'name': 'position', 'label': '序号', 'type': 'DECIMAL_STRING', 'decimalPlaces': 0},
            {'name': 'value', 'label': '来源值', 'type': 'DECIMAL_STRING', 'unit': unit, 'decimalPlaces': places},
            {'name': 'sign', 'label': '数值分类', 'type': 'STRING'},
            {'name': 'share', 'label': '占所选非空数值合计比例', 'type': 'DECIMAL_STRING', 'unit': '%', 'decimalPlaces': places},
        ], 'rows': rows,
        'orderedFactIds': [item['factId'] for item, _ in valid],
        'unavailableFactIds': [item['factId'] for item in missing],
        'meaning': 'selected_source_values_statistics_not_consolidated_total',
        'denominator': {'value': statistics['sum'], 'meaning': '所选非空原值的有符号合计；零或全空时比例为 null'},
        'boundary': '这是所选来源值的描述统计；不执行机构树去重，不替代预算服务的 CalcType 或业务口径聚合。缺值保留，不能宣称缺值对象已有金额。',
    }


def formula_check_summary(trace):
    """分开目标本层与全部表内计算层，避免把一个 MATCH 概括为整条公式闭合。"""
    nodes = [node for node in trace.get('nodes', []) if node.get('expression') and not node.get('boundary')]
    matched = [node for node in nodes if node.get('localCheck', {}).get('status') == 'MATCH']
    mismatched = [node for node in nodes if node.get('localCheck', {}).get('status') == 'MISMATCH']
    blocked = [node for node in nodes if node.get('localCheck', {}).get('status') not in ('MATCH', 'MISMATCH')]
    exceptions = [{'factRef': node['factRef'], 'headers': node.get('headers', []),
                   'sourceValue': node.get('observed'),
                   'localStatus': node.get('localCheck', {}).get('status'),
                   'localValue': node.get('localCheck', {}).get('value'),
                   'localDifference': node.get('localCheck', {}).get('difference'),
                   'recursiveStatus': node.get('status')}
                  for node in [*mismatched, *blocked]]
    return {'formulaNodeCount': len(nodes), 'localMatchedCount': len(matched),
            'localMismatchedCount': len(mismatched), 'localUnavailableCount': len(blocked),
            'allLocalChecksCompleted': bool(nodes) and not blocked,
            'allLocalChecksMatch': bool(nodes) and len(matched) == len(nodes),
            'exceptions': exceptions[:20], 'exceptionCount': len(exceptions),
            'differenceMeaning': '来源值减本层复算值；反向展示须同时变更符号与标题',
            'boundary': '每项结论只适用于当前对象和公式节点。本层相符、差异及缺值分别说明；下层缺值不抹去上层本层结果，局部相符不代表所有对象或整条公式相符。'}


def document_inputs(artifact):
    """提供小型结果绑定目录；不重复返回完整公式树或另行计算。"""
    from .scalar_references import field_catalog
    results = []
    facts = {fact['factId']: fact for fact in artifact.get('facts', [])}
    for value in artifact['derivedValues']:
        operator = value['operator']
        item = {'valueRef': value['derivedId'], 'operator': operator}
        if operator == 'FORMULA_CHECK' and any(node.get('expression') and not node.get('boundary')
                                              for node in value.get('formulaTrace', {}).get('nodes', [])):
            trace = value['formulaTrace']
            target = facts.get(trace['targetFactRef'], {})
            item.update(dataset={'valueRef': value['derivedId']}, kind='FORMULA',
                        targetFactRef=trace['targetFactRef'],
                        targetContext={'headers': list(target.get('headers', [])),
                                       'dimensions': {**target.get('effectiveDimensions', {}), **target.get('dimensions', {})}},
                        localStatus=trace['targetLocalCheck']['status'], recursiveStatus=trace['status'],
                        checkSummary=formula_check_summary(trace),
                        usage='绑定 document.datasets 后直接使用 nodes/operands/values/contributions/waterfall；无数值的计算字段保留 null，不用 Python 重写公式。')
        elif operator == 'DESCRIBE' and value.get('statistics'):
            item.update(dataset={'valueRef': value['derivedId']}, kind='TABULAR',
                        scalarFields=['value', *value['counts'], *value['statistics']],
                        fields=field_catalog(value),
                        usage='金额见 derivedValues.statistics，数量见 counts；直接用 {valueRef,field} 绑定或计算，不重算。字段 memberCount 是当前粒度的对象/分组数，不推断为机构户数。')
        elif operator in ('SORT', 'RANK') and value.get('calculationValidation') == 'PASS':
            item.update(dataset={'valueRef': value['derivedId']}, kind='ORDERED')
        else:
            continue
        results.append(item)
    return results
