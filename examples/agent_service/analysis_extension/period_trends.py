"""Time series selected from frozen observations or successful tabular calculations."""
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal, Inexact, InvalidOperation, Rounded, localcontext
import re

from .analysis_inputs import fact_roles
from .business_dataset import row_values
from .business_tables import BUDGET_TOTAL_MEMBER, period_label
from .models import AnalysisError, digest


TREND_HELP = ('在现有分析段内放置 <div data-analysis-trend="所选 trendRef"></div>。'
              '选与问题最相关的指标和比较对象。宿主按实际时期绘制各对象的全部点；无需写绘图脚本、复制金额或重跑计算。')
KINDS = {'N': 1, 'H': 2, 'J': 4, 'Y': 12, 'X': 36, 'Z': 53, 'R': 366}
PERIOD = re.compile(r'(\d{4})([NHJYXZR])(\d{4})')
CHINESE_PERIOD = re.compile(r'(\d{4})年(?:(上半年|下半年)|第?([0-9一二三四]+)(季度|季|月|周|旬|天))?')
PERIOD_HEADER = re.compile(r'\{\s*[+-]?\d+[NHJYXZR](?:\s*,\s*[+-]?\d+[NHJYXZR])*\s*\}')


def period_code(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    match = PERIOD.fullmatch(value)
    if match:
        year, kind, n = int(match[1]), match[2], int(match[3])
    else:
        match = CHINESE_PERIOD.fullmatch(value)
        if not match:
            return None
        year = int(match[1])
        if match[2]:
            kind, n = 'H', (1 if match[2] == '上半年' else 2)
        elif match[3]:
            number = match[3]
            if not number.isascii() and number not in ('一', '二', '三', '四'):
                return None
            n = int(number) if number.isascii() else '一二三四'.index(number) + 1
            kind = {'季度': 'J', '季': 'J', '月': 'Y', '周': 'Z', '旬': 'X', '天': 'R'}[match[4]]
        else:
            kind, n = 'N', 1
    if not 1 <= year <= 9999 or not 1 <= n <= KINDS[kind]:
        return None
    if kind == 'R' and n > date(year, 12, 31).timetuple().tm_yday:
        return None
    return f'{year:04d}{kind}{n:04d}'


def period_position(code):
    year, kind, n = int(code[:4]), code[4], int(code[5:])
    if kind == 'R':
        return date(year, 1, 1).toordinal() + n - 1
    # A budget year can have a 53rd week. Within a year use its actual period index.
    return year * KINDS[kind] + n


def observed_period(fact):
    for raw in (fact.get('dataPeriod'), (fact.get('dimensions') or {}).get('DATATIME'), fact.get('selectionDataPeriod')):
        if raw:
            return period_code(raw)
    # Earlier artifacts sometimes store only period. A form-opening fallback on its
    # own does not establish that the data changed time between two observations.
    raw = fact.get('period')
    if raw and raw != fact.get('executionPeriod'):
        return period_code(raw)
    return None


def axis_label(code, supplied=None):
    label = period_label(code, supplied)
    if label != code:
        return label
    year, kind, n = int(code[:4]), code[4], int(code[5:])
    if kind == 'R':
        d = date(year, 1, 1) + timedelta(days=n - 1)
        return f'{d.year}年{d.month}月{d.day}日'
    return f'{year}年第{n}' + {'Z': '周', 'X': '旬'}[kind]


def numeric(raw):
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        return str(raw) if Decimal(str(raw)).is_finite() else None
    except InvalidOperation:
        return None


class Group:
    def __init__(self, identity, title, unit, source):
        self.ref = 'trend_' + digest(identity)[:14]
        self.title, self.unit, self.source = title, unit, source
        self.derived_ref = identity[1] if identity[0] == 'table' else None
        self.lanes = defaultdict(dict)
        self.names, self.labels = {}, {}
        self.ambiguous = False

    def add(self, coords, labels, period, label, value, ref):
        points = self.lanes[coords]
        if period in points and points[period] != {'ref': ref, 'value': value}:
            self.ambiguous = True  # Same coordinate with multiple records needs a real calculation.
        self.names[coords] = labels
        self.labels[period] = axis_label(period, label)
        points[period] = {'ref': ref, 'value': value}

    def chart(self):
        if self.ambiguous or not any(len(points) >= 2 for points in self.lanes.values()):
            return None
        if not any(p['value'] is not None for lane in self.lanes.values() for p in lane.values()):
            return None
        periods = sorted(self.labels, key=period_position)
        varying = [i for i in range(len(next(iter(self.lanes))))
                   if len({coords[i] for coords in self.lanes}) > 1]
        common = self.names[next(iter(self.lanes))]
        context = ' / '.join(label for i, label in enumerate(common) if i not in varying and label)
        indices = {code: i for i, code in enumerate(periods)}
        lines = []
        names = {coords: ' / '.join(self.names[coords][i] for i in varying) or self.title for coords in self.lanes}
        counts = Counter(names.values())
        for coords, points in sorted(self.lanes.items()):
            label = names[coords]
            if counts[label] > 1:
                label += '（' + ' / '.join(coords[i] for i in varying) + '）'
            # Large uneven grids must stay proportional to the actual observations.
            lines.append({'label': label, 'points': [dict(points[code], periodIndex=indices[code])
                                                      for code in sorted(points, key=indices.__getitem__)]})
        return {'trendRef': self.ref, 'metric': self.title, 'unit': self.unit, 'context': context,
                **({'derivedRef': self.derived_ref} if self.derived_ref else {}),
                'source': self.source, 'series': lines,
                'periods': [{'code': code, 'label': self.labels[code], 'order': period_position(code)} for code in periods]}


def source_groups(facts, groups, formula_targets):
    for fact in facts:
        if 'PRIMARY' not in fact_roles(fact):
            continue
        memberships = fact.get('inputMemberships') or [fact]
        if fact['factId'] not in formula_targets and not any(
                item.get('inputRole', 'PRIMARY') == 'PRIMARY' and item.get('inputPurpose', 'VALUES') != 'FORMULA'
                for item in memberships):
            continue
        dims = fact.get('dimensions') or {}
        code = observed_period(fact)
        metric = fact.get('sourceMeasureCode') or fact.get('measureCode')
        if not code or not metric:
            continue
        coords = {**(fact.get('rowIdentity') or {}), **dims}
        coords.pop('DATATIME', None)
        object_code = fact.get('sourceObjectId')
        if object_code and not period_code(object_code) and object_code not in coords.values():
            coords['object'] = object_code
        elif not coords and not period_code(object_code) and fact.get('objectId'):
            coords['object'] = fact['objectId']
        keys = sorted(coords)
        names = fact.get('dimensionNames') or {}
        labels = tuple(str(names.get(k) or (fact.get('objectLabel') if k == 'object' else None)
                           or ('合计' if coords[k] == BUDGET_TOTAL_MEMBER else coords[k])) for k in keys)
        # Separate facts by table, measure, unit, scenario, currency, version, period basis and aggregation level.
        scope = tuple((k, str(coords[k])) for k in keys if k in ('MD_SCENE', 'MD_CURRENCY', 'MD_MGRVER', 'MD_STAGE')
                      or coords[k] == BUDGET_TOTAL_MEMBER)
        # Header axes can distinguish periodic and cumulative columns even when their measure code is shared.
        dimension_labels = {str(v) for v in names.values()}
        headers = tuple(h for h in fact.get('headers') or [] if isinstance(h, str) and h and
                        h not in dimension_labels and not period_code(h) and not PERIOD_HEADER.fullmatch(h))
        basis = digest([fact.get(k) for k in ('periodBasis', 'periodMode', 'aggregation', 'calcType')])
        unit = fact.get('unit') or ''
        key = ('source', (fact.get('context') or {}).get('sourceId'), fact.get('recordTable'),
               fact.get('bindingRef'), metric, headers, code[4], unit, basis, tuple(keys), scope)
        if key not in groups:
            title = ' / '.join(headers) or next((s for s in reversed(fact.get('headers') or [])
                                                  if isinstance(s, str) and s in dimension_labels), str(metric))
            groups[key] = Group(key, title, unit, fact.get('formName') or fact.get('recordTable') or '')
        value = numeric(fact.get('value')) if fact.get('valueState') == 'PRESENT' else None
        groups[key].add(tuple(str(coords[k]) for k in keys), labels, code, names.get('DATATIME'), value, fact['factId'])


def calculated_groups(values, groups, facts):
    fact_roles_by_id = {f['factId']: fact_roles(f) for f in facts}
    aliases = defaultdict(set)
    for f in facts:
        code = observed_period(f)
        if code:
            supplied = (f.get('dimensionNames') or {}).get('DATATIME')
            if supplied:
                aliases[supplied].add(code)
            if code[4] == 'J':
                n = int(code[5:])
                for label in (f'Q{n}', f'第{n}季度', f'{n}季度', f'第{"一二三四"[n-1]}季度',
                              f'{"一二三四"[n-1]}季度', f'{code[:4]}Q{n}'):
                    aliases[label].add(code)
    def read_period(value):
        if isinstance(value, str) and len(matches := aliases.get(value, ())) == 1:
            return next(iter(matches))
        return period_code(value)
    for value in values:
        if value.get('calculationValidation') != 'PASS' or value.get('outputKind') not in ('TABLE', 'CHART_DATA'):
            continue
        refs = value.get('sourceFactIds') or value.get('inputRefs') or []
        if refs and all(isinstance(ref, str) and ref in fact_roles_by_id and
                        'PRIMARY' not in fact_roles_by_id[ref] for ref in refs):
            continue
        cols = value.get('columns') or []
        rows = [(row_values(row, cols), row.get('rowRef') if isinstance(row, dict) else None)
                for row in value.get('rows', [])]
        if len(rows) < 2:
            continue
        text_cols = [col['name'] for col in cols if col['type'] == 'STRING']
        clocks = {k: list(map(read_period, (row.get(k) for row, _ in rows))) for k in text_cols}
        clocks = {k: codes for k, codes in clocks.items() if all(codes)}
        if not clocks:
            continue
        clock, codes = next(iter(clocks.items()))
        if any(seq != codes for seq in clocks.values()):
            continue
        dimensions = [k for k in text_cols if k not in clocks and k not in ('factRef', 'valueRef', 'rowRef')]
        for col in cols:
            if col.get('type') not in ('DECIMAL_STRING', 'INTEGER', 'NUMBER'):
                continue
            field, unit = col['name'], col.get('unit') or ''
            for index, (row, ref) in enumerate(rows):
                period = codes[index]
                key = ('table', value['derivedId'], clock, field, period[4])
                if key not in groups:
                    groups[key] = Group(key, col.get('label') or field, unit, value.get('label') or '计算结果')
                coords = tuple(str(row.get(k) or '') for k in dimensions)
                groups[key].add(coords, coords, period, None, numeric(row.get(field)), ref or str(index))


def trend_catalog(artifact):
    groups = {}
    calculated_groups(artifact.get('derivedValues', []), groups, artifact.get('facts', []))
    targets = {v['formulaTrace']['targetFactRef'] for v in artifact.get('derivedValues', [])
               if v.get('formulaTrace', {}).get('targetFactRef')}
    source_groups(artifact.get('facts', []), groups, targets)
    charts = [chart for group in groups.values() if (chart := group.chart())]
    # Prefer a comparable aggregate over many detail curves, without choosing a business metric.
    charts.sort(key=lambda chart: (len(chart['series']), -len(chart['periods'])))
    return {chart['trendRef']: chart for chart in charts}


def choices(catalog):
    return [{'trendRef': key, '指标': chart['metric'], '单位': chart['unit'], '范围': chart['context'] or chart['source'],
             'Markdown': f'![时期趋势](charts/{key}.svg)',
             '对象': '、'.join(line['label'] for line in chart['series'][:3]), '序列数': len(chart['series']),
             '时期': [chart['periods'][0]['label'], chart['periods'][-1]['label']], '期数': len(chart['periods'])}
            for key, chart in catalog.items()]


def plot_geometry(chart):
    numbers = [Decimal(point['value']) for line in chart['series'] for point in line['points']
               if point and point['value'] is not None]
    lo, hi = min(numbers), max(numbers)
    chart['range'] = [str(lo), str(hi)]
    # Subtract while still decimal so large, close amounts remain distinguishable in the browser.
    with localcontext() as ctx:
        ctx.prec = max(40, max(len(n.as_tuple().digits) for n in numbers) + 8)
        ctx.traps[Inexact] = ctx.traps[Rounded] = False
        for line in chart['series']:
            for point in line['points']:
                if point and point['value'] is not None:
                    point['height'] = float((Decimal(point['value']) - lo) / (hi - lo)) if hi != lo else 0.5
    return chart


def selected_trends(catalog, requested):
    if not catalog and not requested:
        return {}
    if not requested or any(key not in catalog for key in requested):
        raise AnalysisError('DOCUMENT_TREND_REQUIRED', '本次分析有可比较的多个实际时期，请在正文绑定对应指标的时期折线图。'
                            if catalog else '当前冻结数据没有这条时期序列。',
                            {'path': '/document/html', 'trendChoices': choices(catalog)[:12], 'hint': TREND_HELP,
                             'requiresBuild': False, 'requiresSourceQuery': False})
    return {key: plot_geometry(catalog[key]) for key in dict.fromkeys(requested)}
