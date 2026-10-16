"""Bounded browser observations, checked against immutable document datasets.

These are DOM diagnostics, not proof of visual meaning or business semantics.
"""
from .models import AnalysisError, now


def invalid(message):
    raise AnalysisError('INVALID_PREVIEW_CHECK', message)


def numeric_coverage(observations):
    checks = observations.get('numericChecks')
    fields = {'counts': {'declared', 'measured', 'failed'},
              'linearBars': {'declared', 'measured', 'invalid', 'scaleGroups', 'checkedGroups', 'failedGroups'}}
    if not isinstance(checks, dict) or set(checks) != set(fields):
        invalid('数值图形检查须区分未声明、未测量与实际检查。')
    result = {}
    for name, keys in fields.items():
        item = checks[name]
        if (not isinstance(item, dict) or set(item) != keys
                or any(type(v) is not int or not 0 <= v <= 100000 for v in item.values())
                or item['measured'] > min(item['declared'], 50 if name == 'counts' else 1000)):
            invalid('数值图形检查计数无效。')
        if name == 'counts':
            if item['failed'] > item['measured']: invalid('计数检查失败数无效。')
            failed = item['failed'] > 0
            partial = item['measured'] < item['declared']
        else:
            if (item['invalid'] > item['measured']
                    or (item['measured'] > item['invalid'] and item['scaleGroups'] == 0)
                    or not 0 <= item['failedGroups'] <= item['checkedGroups'] <= item['scaleGroups'] <= item['measured'] - item['invalid']):
                invalid('线性条形检查分组无效。')
            failed = item['invalid'] > 0 or item['failedGroups'] > 0
            partial = item['measured'] < item['declared'] or item['checkedGroups'] < item['scaleGroups']
        status = ('NOT_DECLARED' if item['declared'] == 0 else 'NOT_MEASURED' if item['measured'] == 0
                  else 'FAIL' if failed else 'PARTIAL' if partial else 'PASS')
        result[name] = {**item, 'status': status}
    return result


def evaluate(document, observations):
    if not isinstance(observations, dict) or set(observations) != {'version', 'viewport', 'pageOverflow', 'overflow', 'overlaps', 'smallText', 'clippedText', 'orders', 'errors', 'truncated', 'numericChecks'}:
        invalid('页面检查字段不完整或包含未知字段。')
    viewport = observations['viewport']
    if (type(observations['version']) is not int or observations['version'] != 3 or not isinstance(viewport, dict) or set(viewport) != {'width', 'height'}
            or any(type(viewport[k]) is not int or not 1 <= viewport[k] <= 24000 for k in viewport)):
        invalid('页面检查需要有效的当前视口。')
    if any(type(observations[k]) is not bool for k in ('pageOverflow', 'truncated')):
        invalid('页面检查标志必须为布尔值。')
    for key, limit in (('overflow', 30), ('overlaps', 30), ('smallText', 30), ('clippedText', 30), ('orders', 50), ('errors', 20)):
        if not isinstance(observations[key], list) or len(observations[key]) > limit: invalid('页面检查条目超过上限。')
    text = lambda x, maximum: isinstance(x, str) and len(x) <= maximum
    for item in observations['overflow']:
        if not isinstance(item, dict) or set(item) != {'text', 'left', 'right'} or not text(item['text'], 100) or any(type(item[k]) is not int or abs(item[k]) > 1000000 for k in ('left', 'right')): invalid('越界诊断格式无效。')
    for item in observations['overlaps']:
        if not isinstance(item, dict) or set(item) != {'first', 'second'} or not all(text(v, 100) for v in item.values()): invalid('标签诊断格式无效。')
    for item in observations['smallText']:
        if (not isinstance(item, dict) or set(item) != {'text', 'fontPx'} or not text(item['text'], 100)
                or type(item['fontPx']) not in (int, float) or not 0 < item['fontPx'] < 12):
            invalid('字号诊断需要有效的实际显示像素。')
    for item in observations['clippedText']:
        if (not isinstance(item, dict) or set(item) != {'text', 'container', 'edges'}
                or not text(item['text'], 100) or not text(item['container'], 100)
                or not isinstance(item['edges'], list) or not 1 <= len(item['edges']) <= 4
                or not all(edge in ('left', 'right', 'top', 'bottom') for edge in item['edges'])
                or len(set(item['edges'])) != len(item['edges'])):
            invalid('文本裁切诊断格式无效。')
    if not all(text(item, 300) for item in observations['errors']): invalid('脚本诊断格式无效。')
    numeric_checks = numeric_coverage(observations)
    sets, orders, seen = document.get('datasets', {}), [], set()
    for item in observations['orders']:
        if (not isinstance(item, dict) or set(item) != {'key', 'partial', 'refs', 'truncated'}
                or not text(item['key'], 64) or type(item['partial']) is not bool or type(item['truncated']) is not bool
                or not isinstance(item['refs'], list) or len(item['refs']) > 2000 or not all(text(ref, 200) for ref in item['refs'])):
            invalid('集合顺序诊断格式无效。')
        dataset = sets.get(item['key'])
        if not dataset or dataset['kind'] != 'ORDERED':
            orders.append({'key': item['key'], 'status': 'UNSUPPORTED_MARKER', 'partial': item['partial']})
            continue
        seen.add(item['key'])
        expected = [row['factRef'] for row in dataset['rows']]
        selected = set(item['refs'])
        subsequence = [ref for ref in expected if ref in selected]
        correct = item['refs'] == (subsequence if item['partial'] else expected)
        status = 'TRUNCATED' if item['truncated'] else 'EMPTY' if not item['refs'] and expected else 'PASS' if correct else 'MISMATCH'
        orders.append({'key': item['key'], 'status': status, 'partial': item['partial'], 'observedCount': len(item['refs']), 'totalCount': len(expected)})
    unmeasured = [key for key, data in sets.items() if data['kind'] == 'ORDERED' and key not in seen]
    blocking = bool(observations['errors']) or any(o['status'] == 'MISMATCH' for o in orders) or any(c['status'] == 'FAIL' for c in numeric_checks.values())
    warnings = bool(observations['pageOverflow'] or observations['overflow'] or observations['overlaps'] or observations['smallText'] or observations['clippedText'] or observations['truncated'] or unmeasured or any(o['status'] != 'PASS' for o in orders) or any(c['status'] in ('PARTIAL', 'NOT_MEASURED') for c in numeric_checks.values()))
    return {'checkedAt': now(), 'contentDigest': document['contentDigest'], 'viewport': viewport,
            'status': 'ERRORS' if blocking else 'REVIEW_REQUIRED' if warnings else 'MEASURED',
            'blocking': blocking, 'orders': orders, 'unmeasuredDatasets': unmeasured, 'numericChecks': numeric_checks,
            **{key: observations[key] for key in ('pageOverflow', 'overflow', 'overlaps', 'smallText', 'clippedText', 'errors', 'truncated')},
            'readability': {'minimumFontPx': 12, 'advisoryOnly': True},
            'visualSemanticsValidated': False,
            'limitations': '仅记录当前宽度、交互状态及可见 DOM/SVG 文字；字号与矩形裁切为提示，不指定布局。未标记图形、Canvas 像素、隐藏分支、复杂遮挡、刻度含义及业务解读仍需实际查看。'}


def summary(records, unavailable_reason=None):
    widths = {r['viewport']['width'] for r in records}
    return {'performed': bool(records), 'status': 'NOT_CHECKED' if not records else
            'ERRORS' if any(r['blocking'] for r in records) else
            'REVIEW_REQUIRED' if any(r['status'] == 'REVIEW_REQUIRED' for r in records) else 'MEASURED',
            'viewports': records, 'visualSemanticsValidated': False,
            'requiredViewportCount': 2, 'checkedViewportCount': len(widths),
            'measurementRequirementMet': len(widths) >= 2,
            **({'unavailableReason': unavailable_reason} if unavailable_reason else {})}


def require_publication_checks(document, records, unavailable_reason=None):
    checks = summary(records, unavailable_reason)
    if any(r['blocking'] for r in records):
        raise AnalysisError('DOCUMENT_RENDER_ERRORS', '当前页面存在已测到的脚本、排序、计数、图形尺寸或线性比例错误，修正后重新检查。',
                            {'path': '/document/html', 'renderChecks': checks, 'requiresBuild': False})
    if not checks['measurementRequirementMet'] and not unavailable_reason:
        raise AnalysisError('DOCUMENT_NOT_CHECKED', '当前页面还未完成聊天嵌入宽度与较窄宽度的实际检查。',
                            {'path': '/document', 'renderChecks': checks, 'requiresBuild': False,
                             'nextAction': 'RUN_RENDER_CHECKS',
                             'hint': '打开 previewPage，在两种实际宽度运行页面检查，再复用 confirmPublish。浏览器确实不可用时才在草案中记录 renderCheckUnavailableReason，保留未检查状态。'})
    if records:
        ordered = {key for key, data in document.get('datasets', {}).items() if data['kind'] == 'ORDERED'}
        measured = {order['key'] for record in records for order in record['orders'] if order['status'] == 'PASS'}
        if ordered - measured:
            raise AnalysisError('DOCUMENT_ORDER_NOT_CHECKED', '排序集合已绑定，但尚未在实际页面中检出有效的顺序标记。',
                                {'path': '/document/html', 'missingDatasetKeys': sorted(ordered - measured),
                                 'requiresBuild': False, 'hint': '容器标 data-analysis-set，逐项标 data-analysis-ref；展开实际列表后重新检查。不要仅在脚本中声明数据而不展示。'})
    return checks
