"""Business-first reviewer text; storage codecs never become reading protocols."""
from collections import OrderedDict
from copy import deepcopy
from itertools import groupby
import json
import re

from .budget_dimensions import dimension_labels
from .business_tables import STATES, escape, measure_label, member_label, period_label
from .review_aliases import replace_references
from .review_periods import period_labels
from .review_business_evidence import source_facts, source_entities, review_runs, output_business_fields
from .review_business_projection import direct_input_fields, compact_business_scopes, business_memberships, repeated_input_block, row_ranges

LABELS = {
    'userRequest': '用户要求', 'queryIntent': '已确认查询条件', 'scope': '分析范围',
    'facts': '完整来源数据', 'derived': '计算结果', 'pythonRuns': 'Python 专项核对',
    'sourceContexts': '来源环境', 'sourceScopes': '实际取数范围', 'structureEvidence': '业务表结构',
    'documentText': '报告展示正文', 'documentSourceText': '报告折叠及静态文字',
    'candidateAnswer': '本次答复', 'calculationReview': '计算审查结论',
    'evidenceCoverage': '需要核对的覆盖范围', 'computationIntent': '计算目标',
    'groupingRequirements': '分组要求', 'stageInstruction': '本次核对要求',
    'reviewStage': '审查阶段', 'sourceAddressProjection': '来源地址核对',
    'businessTables': '查询业务表', 'businessTableView': '查询业务表', 'evidence': '查询依据',
    'inputRecords': '完整实际执行输入（字段名与代码一致）', 'code': '编码',
    'purpose': '计算目的', 'inputRefs': '按实际顺序采用的数据引用', 'sourceFactIds': '来源数据引用',
    'sourceData': '数据使用说明', 'precisionPolicy': '精度规则', 'nullPolicy': '空值规则',
    'businessValidation': '业务审核状态', 'supersedesRunRefs': '替代的旧计算',
    'columns': '结果列定义', 'rows': '结果明细', 'diagnostics': '执行诊断',
    'referenceAliases': '代码中使用的原始数据标识', 'inputRole': '资料用途',
    'executionStatus': '执行状态', 'sourceConsistency': '与来源核对结果',
    'formula': '来源公式', 'formulaStatus': '公式状态', 'formulaTrace': '公式展开记录',
    'operator': '运算方式', 'unit': '单位', 'value': '数值', 'label': '名称',
    'metric': '指标', 'period': '时期', 'scenario': '情景', 'task': '任务',
    'population': '对象范围', 'grouping': '分组口径', 'businessQuery': '预算查询条件',
    'executionInputSets': '完整 Python 输入及实际附加字段', 'executionInputsRef': '采用的输入表',
    'fields': '实际存在的输入字段', 'factRef': '对应上表数据引用', 'overlay': '实际附加或不同字段',
    'record': '完整输入记录', 'sharedContexts': '共同取数背景', 'sharedContext': '取数背景',
    'referenceSelections': '采用的数据范围', 'orderedSelection': '数据范围', 'count': '数量',
    'periodGlossary': '时期中文说明', 'sourceColumnDefinitions': '来源列定义',
    'taskName': '预算任务', 'formName': '业务表', 'executionPeriod': '数据加载时期',
    'dataPeriod': '数据时期', 'effectiveDimensions': '固定维度', 'dataPeriodSelection': '时期选择',
    'selectionExecutionPeriod': '所选表的数据加载时期', 'selectionDataPeriod': '所选单元格的实际数据时期',
    'title': '指标／科目', 'status': '状态', 'message': '说明', 'data': '完整计算输出',
    'outputKind': '结果类型', 'name': '名称', 'description': '说明', 'fields': '字段及取值',
    'values': '原始取值', 'entityKind': '资料类型', 'key': '业务编码',
    'formulaArithmetic': '公式运算规则', 'formulaTrace': '完整公式构成',
    'sourceValue': '来源值', 'recomputedValue': '复算值', 'difference': '复算差额',
    'fixedDimValMap': '固定维度条件', 'grain': '数据粒度', 'rangeSummary': '实际查询范围',
    'request': '执行的业务条件', 'batchDimensions': '查询维度范围', 'budTaskId': '预算任务编码',
    'dimCode': '维度', 'all': '全部成员', 'level': '层级', 'members': '指定成员',
    'returnedRowCount': '来源返回行数', 'selectedRowCount': '实际采用行数',
    'missingSelectedCellCount': '选定但未取得的单元格数', 'selectionReason': '选择依据',
    'targetCombinationCount': '查询目标组合数', 'matchedTargetCount': '匹配的目标数',
    'alignedRowCount': '对齐后行数', 'filteredOutRowCount': '被条件排除行数',
    'dimensions': '维度', 'memberCount': '成员数', 'keyword': '分组口径',
    'required': '本轮要求', 'requested': '已选用', 'candidates': '来源可用字段',
    'omittedRows': '未采用的来源行', 'excludedMembers': '排除的成员',
    'numericPolicyVersion': '数值处理规则', 'providedToRun': '已提供给本次脚本',
    'scale': '来源缩放因子', 'tool': '执行工具', 'state': '执行状态',
    'error': '异常信息', 'details': '补充说明', 'requiresSourceQuery': '是否需要重新取数',
}

# Standard budget dimension vocabulary; source metadata always wins. Unknown
# configured dimensions retain their actual code, never inferred from values.

# Fields added by the host for ownership, storage and tool execution. Custom
# input fields are not discarded merely because their names are unfamiliar.
HOST_INPUT_FIELDS = set('ref factId aggregation batchId bindingRef calcType captureRef captureRefs cellAddress cellKey '
    'columnName context dataContext dimensionModels effectiveDimensions evidenceIds executionId executionPeriod '
    'formContext formName formulaArithmetic formulaStatus grain inputRole label measureCode memberRef metricKey '
    'objectId objectLabel objectModelCode observationId period precision propertyDimensions queryMetricName '
    'querySessionId rawValueJson readAt recordTable rowIdentity rowIndex scale scaleBasis scenarioFallback '
    'selectionCellKey selectionDataPeriod selectionExecutionPeriod selectionObservationId sourceContextId sourceMeasureCode '
    'sourceObjectId sourcePointer stepId tableDigest targetRef taskName unit value valueState valueType '
    'inputRefs sourceFactIds derivedId pythonRunRef reviewInputRefs'.split())


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _flat(value, path=''):
    result = {}
    for key, child in value.items():
        key = path + '.' + key if path else key
        if isinstance(child, dict) and child:
            result.update(_flat(child, key))
        else:
            result[key] = child
    return result


def _rendered_column_gaps(text):
    """Report measured blank runs compactly. A blank source value remains valid."""
    notes = []
    for tabular, lines in groupby(text.splitlines(), key=lambda line: '\t' in line):
        if not tabular:
            continue
        header, *body = ([cell.strip() for cell in line.split('\t')] for line in lines)
        if not all(header):
            continue
        rows = [row for row in body if len(row) == len(header) and any(row)]
        if len(rows) < 2:
            continue
        for i, label in enumerate(header):
            gaps = sum(row[i] in ('', '—', '–') for row in rows)
            if gaps >= 2 and gaps >= len(rows) - 1:
                notes.append(f'{escape(label[:60])}（{gaps}/{len(rows)} 行为空或显示横线）')
                if len(notes) == 6:
                    return notes
    return notes


def render_business_markdown(material, aliases, readable_view=None, wire_references=None):
    """Review business evidence; the full execution audit stays frozen in host storage."""
    names = {original: alias for alias, original in aliases.items()}
    value = replace_references(deepcopy(material), names)
    periods = period_labels(material)
    if periods:
        value['periodGlossary'] = periods
    runs = review_runs(value)
    wire_references = {} if wire_references is None else wire_references
    locations = {}
    table_index = 0
    input_fields = set()
    for run in runs:
        fields = direct_input_fields(run.get('code', ''))
        if fields is None:
            input_fields = None
            break
        input_fields.update(fields)
    def labels(node):
        if isinstance(node, dict):
            name = node.get('dimensionLabel') or (node.get('label') if not node.get('field') and not node.get('objectCode') else None)
            if node.get('dimCode') and name:
                yield {'dimCode': node['dimCode'], 'label': name}
            if isinstance(node.get('dimensionLabels'), dict):
                for code, label in node['dimensionLabels'].items():
                    yield {'dimCode': code, 'label': label}
            for child in node.values():
                yield from labels(child)
        elif isinstance(node, list):
            for child in node:
                yield from labels(child)
    dimensions = dimension_labels(labels(value))
    def field_label(key):
        parts=str(key).split('.')
        if not any(part in LABELS or part in dimensions for part in parts): return str(key)
        return '／'.join(LABELS.get(part, dimensions.get(part,part)) for part in parts)
    # A provenance identifier is not a business title. Long values stay in the
    # frozen owner-scoped map; literals used by the actual code are listed below.
    all_facts = list(value.get('facts') or [])
    fact_dimensions = {code for fact in all_facts if isinstance(fact.get('dimensions'), dict) for code in fact['dimensions']}
    evidence = value.get('evidence') or {}
    review_targets=set(value.get('reviewTargets') or evidence.get('resultRoots') or [])
    for source in evidence.get('sources', []):
        all_facts.extend(source_facts(source))
    derived=value.get('derived') or evidence.get('finalResults') or []
    if not isinstance(derived,list): derived=[derived]
    outputs=derived or [output for run in runs for output in run.get('outputs',[])]
    # Human locations must cover computations as well as source cells. They
    # resolve to the same frozen identities; display labels are never keys.
    for prefix, items, key in (('Python脚本', runs, 'runRef'), ('结果', outputs, 'derivedId')):
        for index, item in enumerate(items, 1):
            if isinstance(item, dict) and item.get(key):
                reference=item[key]
                location=f'{prefix}{index}'
                wire_references[location]=aliases.get(reference,reference)
                locations[reference]=location
    fact_refs = [fact['factId'] for fact in all_facts if isinstance(fact,dict) and 'factId' in fact]
    lines = ['# ' + ('计算专项审查材料' if value.get('reviewStage') == 'CALCULATION' else '业务成果审查材料'),
             '', '以下为本次审核所需的业务资料。来源值按预算服务返回保留；相同金额或名称不代表同一条数据。']
    lines.extend(['', '审查意见的数据定位使用下文的“表1第1行”“Python脚本1”“结果1”等实际位置；'
                  '填写 evidenceRefs 时逐项使用这些位置。章节、行号范围和解释写在 location；没有对应引用时 evidenceRefs 填空数组。'])

    def heading(key, depth):
        lines.extend(['', '#' * min(depth, 6) + ' ' + LABELS.get(str(key), str(key)), ''])

    def scalar(item, typed=False):
        if item is None: return 'null（来源空值）'
        if isinstance(item, str):
            if item in locations:
                return escape(locations[item])
            display = escape(_json(item) if typed else item)
            period = period_label(item)
            return display + ('（' + escape(period) + '）' if period != item else '')
        return escape(_json(item))

    def table(headings, rows):
        lines.extend(['', '| ' + ' | '.join(escape(h) for h in headings) + ' |',
                      '| ' + ' | '.join('---' for _ in headings) + ' |'])
        lines.extend('| ' + ' | '.join(row) + ' |' for row in rows)
        lines.append('')

    def records(items, depth, offset=0, group_context=True):
        """Ordinary field tables, with common context once above the rows."""
        flat = [_flat(item) for item in items]
        fields = list(dict.fromkeys(key for row in flat for key in row))
        common = {key: flat[0][key] for key in fields if key in flat[0]
                  and all(key in row and _json(row[key]) == _json(flat[0][key]) for row in flat)}
        changing = [key for key in fields if key not in common]
        lines.append(f'共 {len(items)} 条，保留原顺序。')
        if common:
            heading('这些记录共同的条件', depth)
            table(['字段', '实际内容'], [[escape(field_label(key)), scalar(child,True)] for key, child in common.items()])
        # Consecutive records from the same captured execution share their
        # context above their own table. Never sort, combine or omit records.
        anchors = [key for key in changing if key in ('overlay.captureRef', 'overlay.batchId')]
        if group_context and anchors:
            groups = []
            for index, row in enumerate(flat):
                identity = _json([[key, row.get(key), key in row] for key in anchors])
                if not groups or groups[-1][0] != identity:
                    groups.append((identity, index, []))
                groups[-1][2].append({k:v for k,v in row.items() if k not in common})
            if 1 < len(groups) <= len(items) // 4:
                for _, start, group in groups:
                    heading(f'实际输入第 {offset + start + 1}—{offset + start + len(group)} 条', depth)
                    records(group, depth + 1, offset + start, False)
                return
        # Put equivalent field names in the SAME column heading. A reader sees
        # the actual value here, without consulting an index or another table.
        independent, equivalents = [], {}
        for key in changing:
            prior = next((prior for prior in independent if all((key in row) == (prior in row)
                         and (key not in row or _json(row[key]) == _json(row[prior])) for row in flat)), None)
            if prior is None: independent.append(key)
            else: equivalents.setdefault(prior, []).append(key)
        changing = independent
        lines.append('原始字段表中，双引号表示字符串；数字、布尔值、空值保留原类型，字段不存在单独标明。')
        # Extra-wide code inputs are split by fields, never by sampled rows.
        # Each row retains its original sequence number in every field block.
        for start in range(0, len(changing), 8):
            columns = changing[start:start + 8]
            table(['原序号', *['、'.join(field_label(k) for k in [key,*equivalents.get(key,[])]) for key in columns]],
                  [[str(offset + i + 1), *[scalar(row[key],True) if key in row else '（字段不存在）' for key in columns]]
                   for i, row in enumerate(flat)])

    def facts(items, depth):
        nonlocal table_index
        def mapping(fact,key):
            return fact.get(key) if isinstance(fact.get(key),dict) else {}
        def dimension_display(fact, key, include_code=False):
            code = mapping(fact,'dimensions').get(key)
            name = mapping(fact,'dimensionNames').get(key)
            display = member_label({**fact,'dimensions':mapping(fact,'dimensions'),'dimensionNames':mapping(fact,'dimensionNames')}, key)
            if include_code and key == 'DATATIME' and name and name != display:
                return display + '（' + str(code) + '；来源名称：' + str(name) + '）'
            return display + ('（' + str(code) + '）' if include_code and name and name != str(code) else '')
        groups = OrderedDict()
        for fact in items:
            key = (fact.get('recordTable'), fact.get('sourceContextId', fact.get('contextRef')), fact.get('inputRole'))
            groups.setdefault(key, []).append(fact)
        for (model, context, role), group in groups.items():
            table_index += 1
            heading(f'业务数据表 {table_index}', depth)
            for row_index, fact in enumerate(group, 1):
                reference = fact.get('factId') or fact.get('key')
                if reference:
                    location = f'表{table_index}第{row_index}行'
                    wire_references[location] = aliases.get(reference, reference)
                    locations.setdefault(reference, location)
            lines.append(f'行号用于核对定位，例如“表{table_index}第1行”；原始来源引用由程序保存。')
            form_names=list(dict.fromkeys(record.get('formName') for run in runs for record in run.get('inputRecords',[])
                if record.get('recordTable') == model and record.get('formName')))
            if model: lines.append('来源业务表：' + ('、'.join(form_names)+'（'+escape(model)+'）' if form_names else escape(model)))
            if role: lines.append('用途：' + {'PRIMARY':'主体', 'REFERENCE':'对照', 'STRUCTURE':'结构资料'}.get(role, role))
            keys = list(dict.fromkeys(key for row in group for key in mapping(row,'dimensions')))
            keys.sort(key=lambda key: (key != 'DATATIME', key))
            fixed = {key for key in keys if key in mapping(group[0],'dimensions') and
                     all(key in mapping(row,'dimensions') and _json([row['dimensions'][key],mapping(row,'dimensionNames').get(key)])
                         == _json([group[0]['dimensions'][key],mapping(group[0],'dimensionNames').get(key)]) for row in group)}
            if fixed:
                lines.append('共同条件：' + '；'.join(dimensions.get(key, key) + '＝' + dimension_display(group[0], key, True) for key in keys if key in fixed))
            if keys:
                lines.append('维度字段：' + '；'.join(dimensions.get(key,key) + '＝' + key for key in keys))
            value_types = list(dict.fromkeys(type(row.get('value')).__name__ for row in group))
            if len(value_types) == 1:
                lines.append('来源值原始类型：' + {'str':'字符串（保留原始数值文本及精度）', 'int':'整数',
                             'float':'数值', 'bool':'布尔值', 'NoneType':'空值'}.get(value_types[0],value_types[0]))
            changing = [key for key in keys if key not in fixed]
            dictionary, collisions = [], set()
            for key in changing:
                members = list({(_json(mapping(row,'dimensions').get(key)), mapping(row,'dimensionNames').get(key),
                                 dimension_display(row,key)) for row in group})
                labels_to_codes = {}
                for code, original_name, shown in sorted(members, key=lambda item:item[0]):
                    dictionary.append([escape(dimensions.get(key,key)), escape(code), escape(shown), escape(original_name or '')])
                    labels_to_codes.setdefault(shown,set()).add(code)
                if any(len(codes)>1 for codes in labels_to_codes.values()): collisions.add(key)
            if dictionary:
                heading('维度名称与原始编码', depth + 1)
                table(['维度','原始编码','业务名称','来源名称'], dictionary)
            metric_fields=[]
            by_title={}
            for fact in group:
                title=tuple(fact.get('headers') or [fact.get('title') or fact.get('label','')])
                by_title.setdefault(title,set()).add((fact.get('measureCode'),fact.get('sourceMeasureCode')))
            ambiguous=any(len(codes)>1 for codes in by_title.values())
            for field in ('measureCode','sourceMeasureCode'):
                if any(f.get(field) for f in group) and (ambiguous or input_fields is None or field in input_fields):
                    metric_fields.append(field)
            headings = ['行号', *[dimensions.get(key, key) for key in changing], '指标／科目',
                        *[{'measureCode':'结果指标编码','sourceMeasureCode':'来源指标编码'}[field] for field in metric_fields], '来源值', '单位']
            has_scale=any('scale' in fact for fact in group)
            if has_scale: headings.append('来源缩放因子')
            rows = []
            for fact in group:
                amount = fact.get('value')
                shown = (STATES.get(fact.get('valueState'), '空值') if amount is None else
                         (_json(amount) if len(value_types) > 1 else str(amount)))
                if 'value' not in fact: shown='（字段不存在）'
                display_fact={**fact,'dimensions':mapping(fact,'dimensions'),'dimensionNames':mapping(fact,'dimensionNames')}
                title = ' / '.join(fact.get('headers') or []) or fact.get('title') or measure_label(display_fact) or fact.get('label', '')
                rows.append([str(len(rows)+1), *[escape(dimension_display(fact, key, key in collisions)) for key in changing],
                             escape(title), *[scalar(fact[field],True) if field in fact else '（字段不存在）' for field in metric_fields],
                             escape(shown), escape(fact.get('unit') or '')])
                if has_scale: rows[-1].append(scalar(fact['scale'],True) if 'scale' in fact else '（字段不存在）')
            # Hoist exact common display fields without changing row numbers, values or source types.
            if len(rows) > 2:
                shared = [i for i, title in enumerate(headings) if title in
                          ('来源缩放因子', '单位', '指标／科目', '结果指标编码', '来源指标编码')
                          and all(row[i] == rows[0][i] for row in rows)]
                if shared:
                    lines.append('本表各行相同：' + '，'.join(headings[i] + '＝' + (rows[0][i] or '（空）') for i in shared) + '。')
                    headings = [title for i, title in enumerate(headings) if i not in shared]
                    rows = [[value for i, value in enumerate(row) if i not in shared] for row in rows]
            table(headings, rows)
            memberships=OrderedDict()
            for row_index,fact in enumerate(group,1):
                for membership in business_memberships(fact.get('inputMemberships')):
                    identity=_json(membership)
                    memberships.setdefault(identity,(membership,[]))[1].append(row_index)
            if memberships:
                heading('业务数据的群体用途',depth+1)
                membership_rows=[]
                for group_index,(membership,positions) in enumerate(memberships.values(),1):
                    membership_rows.append(['本表全部数据' if positions==list(range(1,len(group)+1)) else '第'+row_ranges(positions)+'行',
                        str(group_index),escape({'PRIMARY':'主体','REFERENCE':'对照','STRUCTURE':'结构资料'}.get(membership.get('inputRole'),membership.get('inputRole',''))),
                        escape('、'.join(dimensions.get(k,k) for k in membership.get('grain',[])))])
                table(['适用范围','来源组','用途','统计粒度'],membership_rows)
                lines.extend(['同一数据可以同时属于主体和对照；各组分别计算，不能合并重复计数，也不能擅自从对照中剔除主体。',''])
            extra = []
            for fact in group:
                fields = {k:fact[k] for k in ('dataPeriod','period','executionPeriod','selectionExecutionPeriod',
                    'selectionDataPeriod','valueType','providedToRun','sourceValueState','issue') if k in fact
                    and not (k in ('period','dataPeriod') and fact[k] == mapping(fact,'dimensions').get('DATATIME'))}
                if fields: extra.append({'数据行':fact.get('factId',fact.get('key')),**fields})
            if extra:
                # Repeated field semantics belong above the table once. An
                # actual row-specific period/flag still retains its data row.
                payloads=[{k:v for k,v in row.items() if k != '数据行'} for row in extra]
                if len(extra)==len(group) and all(_json(row)==_json(payloads[0]) for row in payloads):
                    section('共同的数据属性',payloads[0],depth+1)
                else: section('数据时期、类型及实际使用范围',extra,depth+1)
            for fact in group:
                formula = {k:fact[k] for k in ('formula','formulaTrace','expression','formulaArithmetic') if fact.get(k) is not None}
                if fact.get('formula') and fact.get('cellAddress'):
                    formula['来源单元格'] = fact['cellAddress']
                if formula: section('来源公式：' + (measure_label({**fact,'dimensions':mapping(fact,'dimensions'),
                    'dimensionNames':mapping(fact,'dimensionNames')}) or fact.get('label','')), formula, depth+1)

    def section(key, item, depth=2, python_source=False):
        heading('实际执行的 Python 代码' if python_source else key, depth)
        if key == 'facts' and isinstance(item, list) and all(isinstance(row, dict) and 'factId' in row for row in item):
            facts(item, depth + 1)
        elif python_source and isinstance(item, str):
            fence = '`' * max(3, 1 + max((len(m[0]) for m in re.finditer(r'`+', item)), default=0))
            lines.extend([fence + 'python', item, fence, ''])
        elif isinstance(item, dict):
            # Native calculation table rows already have a stable column schema.
            cols = item.get('columns')
            rows = item.get('rows')
            if isinstance(cols, list) and cols and isinstance(rows, list) and all(isinstance(c, dict) and c.get('name') for c in cols):
                if all(isinstance(row, dict) and isinstance(row.get('values'), dict) for row in rows):
                    def cell(row, name):
                        state = row.get('valueStates', {}).get(name)
                        if state in ('MISSING', 'FAILED', 'NOT_APPLICABLE'):
                            return {'MISSING': '字段缺失', 'FAILED': '取值失败', 'NOT_APPLICABLE': '不适用'}[state]
                        return scalar(row['values'][name]) if name in row['values'] else '字段缺失'
                    table([c.get('label', c['name']) for c in cols],
                          [[cell(row, c['name']) for c in cols] for row in rows])
                    item = {k:v for k,v in item.items() if k != 'rows'}
            simple = {k:v for k,v in item.items() if not isinstance(v,(dict,list)) and k != 'markdown' and '\n' not in str(v)}
            if simple: table(['事项', '内容'], [[escape(field_label(k)), scalar(v)] for k,v in simple.items()])
            for name, child in item.items():
                if name in simple: continue
                if name == 'markdown' and isinstance(child, str): lines.extend(['',child,''])
                else: section(name, child, depth + 1)
            if not item: lines.append('（无）')
        elif isinstance(item, list):
            if not item: lines.append('（无）')
            elif all(isinstance(row,dict) for row in item):
                if key == 'pythonRuns' or any('code' in row or 'rows' in row or
                        any(isinstance(v,str) and '\n' in v for v in _flat(row).values()) for row in item):
                    for i,row in enumerate(item,1): section(f'第 {i} 项', row, depth+1)
                else: records(item, depth+1)
            elif key in ('inputRefs','sourceFactIds','selectedFactIds') and fact_refs and item == fact_refs:
                lines.append(f'采用上方完整来源数据表的全部 {len(fact_refs)} 条数据，严格按照表中原序；没有筛选、去重或重新排序。')
            elif all(not isinstance(row,(dict,list)) for row in item):
                table(['原序区间','内容（依序）'], [[f'{start+1}—{min(start+8,len(item))}',
                      '，'.join(scalar(child) for child in item[start:start+8])] for start in range(0,len(item),8)])
            else:
                for i,row in enumerate(item,1): section(f'第 {i} 项', row, depth+1)
        elif isinstance(item,str): lines.extend([item,''])
        else: lines.append(scalar(item))

    def business_conditions(conditions):
        if not isinstance(conditions,dict): return conditions
        return {key:child for key,child in conditions.items() if key in
                ('metric','period','scenario','task','form','population','grouping','order','valueFilter','dimensionScopes','unit')
                and child is not None and child != '' and child != []}

    def business_scope(item):
        if not isinstance(item,dict): return item
        # Retain selected and excluded business rows and real aggregation rules;
        # omit repeated column schemas and transport receipts.
        return {key:child for key,child in item.items() if key in (
            'taskName','formName','period','executionPeriod','effectiveDimensions','dataPeriodSelection',
            'request','requestedScope','selectedRows','excludedRows','returnedRowCount','selectedRowCount',
            'excludedRowCount','exclusionReason','selectionReason','aggregationSummary','rangeSummary',
            'complete','status','error','diagnostic','selection','valueFilter','purpose','question',
            'omittedRows','excludedMembers','missingSelectedCellCount','fixedDimValMap','inputRole','grain')
                and child is not None and child != [] and child != {}}

    def scope_summary(scopes):
        """Read business conditions without repeating physical query batches."""
        scopes=compact_business_scopes(scopes)
        if not scopes: return
        heading('查询范围与选择条件',3)
        def condition(key, child):
            if isinstance(child,str):
                if key == 'inputRole':
                    return {'PRIMARY':'主体','REFERENCE':'对照','STRUCTURE':'结构资料'}.get(child,child)
                if child in dimensions: return dimensions[child]
                if key in fact_dimensions:
                    matches={row.get('dimensionNames',{}).get(key) for row in all_facts
                        if isinstance(row.get('dimensions'),dict) and isinstance(row.get('dimensionNames'),dict)
                        and row['dimensions'].get(key)==child and row['dimensionNames'].get(key)}
                    if len(matches)==1: return scalar(next(iter(matches)))+'（'+escape(child)+'）'
                return scalar(child)
            if isinstance(child,list): return '、'.join(condition(key,item) for item in child)
            if isinstance(child,dict):
                if child.get('dimCode'):
                    dimension=child['dimCode']
                    parts=[dimensions.get(dimension,dimension)+'：'+condition(dimension,child['values'])] if 'values' in child else [dimensions.get(dimension,dimension)]
                    parts.extend(field_label(k)+'：'+condition(k,v) for k,v in child.items() if k not in ('dimCode','values'))
                    return '；'.join(parts)
                return '；'.join(field_label(k)+'：'+condition(k,v) for k,v in child.items())
            return scalar(child)
        flat=[_flat(scope) for scope in scopes]
        keys=list(dict.fromkeys(key for row in flat for key in row))
        common={key:flat[0][key] for key in keys if key in flat[0]
                and all(key in row and _json(row[key])==_json(flat[0][key]) for row in flat)}
        for key,child in common.items():
            lines.extend([field_label(key)+'：'+condition(key.split('.')[-1],child)+'。',''])
        varying=[key for key in keys if key not in common]
        if len(varying)==1 and all(varying[0] in row for row in flat):
            key=varying[0]
            lines.extend([field_label(key)+'：'+'、'.join(condition(key.split('.')[-1],row[key]) for row in flat)+'。',''])
        elif varying:
            # Multiple changing dimensions remain paired; no Cartesian expansion.
            table([field_label(key) for key in varying],
                  [[condition(key.split('.')[-1],row[key]) if key in row else '（字段不存在）' for key in varying] for row in flat])

    heading('一、用户要求与目标', 2)
    lines.extend([str(value.get('userRequest') or (value.get('computationIntent') or {}).get('userRequest') or '见本次用户问题'), ''])
    if value.get('businessReviewRequest'):
        heading('本次复核要求', 3)
        lines.extend([str(value['businessReviewRequest']), ''])
    support = value.get('supportingEvidence') or {}
    if support.get('groupedSources'):
        heading('同次分组查询各来源的采用情况', 3)
        def group_count(value):
            return '待核实' if value is None else str(value)
        rows = [row for row in support['groupedSources'] if '材料已选分组数' in row]
        if rows:
            table(['业务表', '返回分组数', '材料已选分组数', '各指标的已选组数 / 返回组数'],
                  [[escape(row['业务表']), group_count(row['返回分组数']), group_count(row['材料已选分组数']),
                    '、'.join(f"{escape(m['指标'])} {group_count(m['已选'])}/{group_count(m['返回'])}"
                             for m in row.get('指标采用', []))] for row in rows])
        legacy = [row for row in support['groupedSources'] if '纳入本报告的分组数' in row]
        if legacy:
            table(['业务表', '旧材料返回的数据格数', '旧材料已选的数据格数'],
                  [[escape(row['业务表']), str(row['返回分组数']), str(row['纳入本报告的分组数'])] for row in legacy])
        lines.extend(['已选表示进入分析材料。请对照正文和图表，核实实际展示的来源、分组与指标。'
                      '在现有 coverage 中用一项说明用户本次要哪些来源。承接此前多份结果且未缩小范围时，'
                      '把所需来源中遗漏的分组列为需要补齐的交付内容，复用本次查询结果即可。'
                      '用户限定来源或名次时按该范围检查。AI 自己选用的来源不能作为用户缩小范围的依据。', ''])
    # Show the model's actual choice, never infer a requirement or a source
    # failure from VALUES. The original request remains the review authority.
    material_choices = value.get('materials')
    if material_choices is None:
        material_choices = (value.get('analysisRequirement') or {}).get('materials')
    objectives = value.get('objectiveCoverage') or []
    if objectives:
        heading('本次分析对象', 3)
        table(['编号', '对象与问题', '用途', '业务能力', '可核对的材料'], [
            [str(o['number']), escape(o.get('question') or '见用户原问'),
             {'PRIMARY':'主体','REFERENCE':'对照','STRUCTURE':'结构'}.get(o['role'], o['role']),
             '数据构成' if o['capability'] == 'DATA_COMPOSITION' else '取值',
             '、'.join(locations[ref] for ref in o.get('formulaRefs', []) if ref in locations)
             or ('已采用空结果' if o.get('empty') else '来源边界' if o.get('boundaryRefs')
                 else '仅供探索' if o.get('exploratory') else '')] for o in objectives])
        lines.extend(['依据用户原问核对各对象。构成引用该对象的来源公式结果，确认的来源边界按实际材料说明。', ''])
    elif material_choices:
        heading('AI 本次选择的分析材料', 3)
        roles = {'PRIMARY': '主体', 'REFERENCE': '对照', 'STRUCTURE': '结构依据'}
        kinds = {'VALUES': '数值', 'FORMULAS': '公式构成'}
        table(['用途', '已选择材料'], [[roles.get(item['role'], item['role']),
              '、'.join(kinds.get(kind, kind) for kind in item['kinds'])] for item in material_choices])
        lines.extend(['这是执行计划，不代表用户要求已全部覆盖；只选数值不等于来源没有公式。', ''])
    if value.get('stageInstruction'): lines.extend([value['stageInstruction'],''])
    if review_targets:
        lines.extend(['本次审核以标注“本次采用”的结果为交付目标；其他结果属于前置计算或探索上下文。'
            '旧计算有误不自动说明当前结果错误：结合实际输入和代码，判断它是否影响本次采用结果。'
            '必要的依赖错误仍须指出；没有影响当前结果的旧问题不要求另行修复或重审。',''])
    partition = value.get('evidencePartition') or {}
    if partition.get('meaning'): lines.extend([partition['meaning'],''])
    if value.get('resultReviewPhase') == 'DEPENDENCY':
        lines.extend(['本次仅审核计算：核对下方目标、完整输入、实际代码与输出；此阶段没有待交付答复。',''])
    elif value.get('resultReviewPhase') == 'FINAL':
        lines.extend(['本次审核最终答复。计算代码已在独立阶段核对；继续核对下方完整结果、业务范围和本次答复。',''])
    if value.get('metadataPartition'):
        part = value['metadataPartition']
        lines.extend([f"本次为元数据审核第 {part['index']} / {part['count']} 段；完整数值结果仍在下方。其他段的定义未重复列出，不代表不存在。",''])
    confirmed = business_conditions(value.get('queryIntent') or (value.get('computationIntent') or {}).get('queryIntent'))
    if confirmed: section('已确认的业务条件',confirmed,3)
    prior = business_conditions(value.get('previousQueryIntent'))
    if prior and prior != confirmed: section('前轮已确认条件（是否继续适用以本轮要求为准）',prior,3)
    scope = value.get('scope',{})
    if isinstance(scope,dict):
        for key in ('label','selectionReason'):
            if scope.get(key): lines.extend([str(scope[key]),''])
    for key,label in (('selectedContextRequests','本轮承接的原始问题'),('executionPolicy','本轮执行约束')):
        selected=value.get(key)
        if selected: section(label,selected,3)
    selected_groups=[item for item in value.get('groupingRequirements') or [] if item.get('required')]
    if selected_groups:
        heading('用户选用的分组口径',3)
        for group in selected_groups:
            fields=[dimensions.get(field.get('dimCode'),field.get('label') or field.get('dimCode',''))
                    for field in group.get('candidates',[]) if isinstance(field,dict)]
            lines.extend([str(group.get('keyword',''))+('；来源可用字段：'+'、'.join(fields) if fields else ''),''])
            if group.get('status') not in (None,'SUPPORTED') and group.get('message'):
                lines.extend([group['message'],''])
    followup=value.get('followupScope') or {}
    inherited_system=followup.get('inheritedSystemConditions') or []
    latest=followup.get('latestSourceScope') or {}
    for title,conditions,original in (('最近交付的范围与本次选择不同', latest.get('conditions'), latest.get('request')),
            ('本次选取的历史范围' if latest else '本轮开始时已确认的系统维度范围', inherited_system, None)):
        if not conditions: continue
        heading(title,3)
        if original: lines.extend([original,''])
        for condition in conditions:
            selector=condition.get('selector') or {}
            parts=[]
            if selector.get('type') == 'ALL':
                parts.append(selector['rootText']+'及全部下属单位' if selector.get('rootText') else '全部成员')
            if selector.get('members'): parts.append('、'.join(selector['members']))
            if selector.get('rootText') and selector.get('type') != 'ALL': parts.append('根：'+selector['rootText'])
            if selector.get('level') is not None: parts.append('第'+str(selector['level'])+'层')
            if selector.get('where'): parts.append('筛选条件：'+scalar(selector['where']))
            table(['维度','已确认范围'],[[escape(condition['dimensionHint']),escape('；'.join(parts))]])
    if latest or inherited_system:
        lines.extend(['系统维度默认沿用。本轮明确指向机构时按本轮机构或层级取数，未指定时沿用前轮范围。',''])
    if followup.get('effectiveScopes'):
        scope_summary(followup['effectiveScopes'])
    for key,title in (('context','本轮承接方式与用户依据'),('intentSummary','本轮已确认条件'),
                      ('scopeEdits','模型声明的范围调整（须符合用户要求）'),('inheritedSubject','所承接查询的范围'),
                      ('resultSubject','所承接的已选计算结果'),('unavailable','承接范围的实际限制')):
        content=followup.get(key)
        if content: section(title,content,3)
    for selected in followup.get('selectedContexts',[]):
        # These are the actual user-selected earlier results, not a default to
        # the latest query. Preserve business intent and selected membership.
        context={k:v for k,v in selected.items() if k not in ('contextRef','analysisRef','turnKey','sourceRef','captureRefs')}
        if context: section('本轮继承的查询条件与结果范围',context,3)

    heading('二、查询结果与实际输入', 2)
    if value.get('reportSourceTables'):
        lines.append('下表是本报告来源记录中的名称，可用来核对正文标注的取数口径。')
        table(['预算任务', '业务表'], [[escape(row.get('taskName') or ''), escape(row.get('formName') or '')]
                                 for row in value['reportSourceTables']])
    if value.get('facts') is not None: facts(value['facts'],3)
    for source in evidence.get('sources',[]) if isinstance(evidence,dict) else []:
        if source.get('businessTables'): section('查询结果',source['businessTables'],3)
        else:
            for context, group in groupby(source_facts(source),lambda f:f.get('contextRef') or f.get('sourceContextId')):
                background=business_scope((source.get('contexts') or {}).get(context,{}))
                if background: section('本表查询口径',background,3)
                facts(list(group),3)
        for actual in source.get('executionScopes',[]):
            background=business_scope(actual)
            if isinstance(background, dict) and 'period' in background and 'executionPeriod' not in background:
                background['executionPeriod'] = background.pop('period')
            if background: section('数据加载条件',background,3)
        entities=source_entities(source,evidence)
        if entities:
            for entity in entities:
                entity={k:v for k,v in entity.items() if k not in ('resourceRef','rootResourceRef','fieldProjection','omittedFieldCount')}
                section('查询到的业务定义或成员',entity,3)
        query = (source.get('source') or {}).get('businessQuery',{})
        if query.get('unresolvedGroups'):
            failures = [{k:g[k] for k in ('binding','taskCode','formCode','code','message') if g.get(k)}
                        for g in query['unresolvedGroups']]
            section('未完成的来源（按实际任务和表样标识）', failures, 3)
        elif query.get('failureSummary') and query.get('complete') is not True:
            section('本次取数未完成的部分',query['failureSummary'],3)
        elif query.get('complete') is False:
            section('本次取数未完成的部分','查询未完整完成，不能按完整范围交付。',3)
        if source.get('diagnostic'): section('本次来源查询的实际错误',source['diagnostic'],3)
    for key in ('businessTables','businessTableView'):
        if value.get(key): section('查询结果',value[key],3)
    if value.get('structureEvidence'): section('来源表结构与公式',value['structureEvidence'],3)
    if value.get('relations'): section('来源给出的业务关系',value['relations'],3)
    scopes=[]
    for scope in value.get('sourceScopes') or []:
        background=business_scope(scope)
        if isinstance(background.get('request'),dict):
            background['request']={k:v for k,v in background['request'].items() if k not in
                ('metrics','querySessionId','executionId','bindingRef')}
        if background: scopes.append(background)
    scope_summary(scopes)
    if support.get('facts') or support.get('diagnostics') or support and not support.get('groupedSources'):
        section('同次查询取得的补充业务数据',{k: v for k, v in support.items() if k != 'groupedSources'},3)
    if value.get('subjectCandidates'):
        section('答复主体可选范围', value['subjectCandidates'],3)
        lines.extend(['审核最终答复时，用 candidateRef 记录答复实际使用的来源；rowIndices 为原表零基行号，全部行填 null。不要默认最后一次查询。',''])

    # The actual values, scope and business columns are the reviewer inputs.
    # Transport IDs, runtime bookkeeping and membership ledgers are checked by
    # the host and are retained in ReviewRequest.material, not narrated to AI.
    inputs_by_fact = {f.get('factId'):f for f in all_facts if isinstance(f,dict)}
    business_input_keys = {'value','valueState','unit','scale','period','dimensions','dimensionNames','headers',
                          'sourceMeasureCode','measureCode','inputRole','inputMemberships','sourceValueState',
                          'data','rows','columns','operator','formula'}
    shown_sources={}
    shown_inputs=[]
    for index, run in enumerate(runs,1):
        for source in run.get('sourceData',[]):
            identity=_json(source)
            if identity in shown_sources:
                lines.extend([f"第 {index} 段脚本读取与第 {shown_sources[identity]} 段相同的完整来源文件，数据见该段输入表。",''])
                continue
            shown_sources[identity]=index
            heading(f'第 {index} 段脚本的来源文件完整数据',3)
            background=business_scope(source.get('context',{}))
            if background: section('文件的业务范围',background,4)
            if source.get('complete') is False: lines.append('该来源文件未完整提供，不能据此认定全量计算通过。')
            from .query_result_evidence import source_cells
            cells=list(source_cells(source))
            facts([{**cell,'factId':cell.get('key',str(i+1))} for i,cell in enumerate(cells)],4)
            lines.extend(['providedToRun 表示程序是否实际发送该行；发送完成不证明代码使用了该行，应结合脚本筛选与计算核对。',''])
        records_in = run.get('inputRecords',[])
        positions = list(range(1, len(records_in) + 1))
        description = f'第 {index} 段脚本'
        code = run.get('code', '')
        read_fields = {key for key in HOST_INPUT_FIELDS - business_input_keys - {'ref', 'label'}
                       if repr(key) in code or '"'+key+'"' in code}
        if records_in and all(record.get('ref') for record in records_in):
            signatures = tuple(_json(record) for record in records_in)
            reused = repeated_input_block(signatures, shown_inputs, read_fields)
            shown_inputs.append((signatures, read_fields, index))
            if reused:
                previous, start, end = reused
                lines.extend([f'{description}的输入第 {start+1} 至 {end} 行与第 {previous} 段的全部输入逐行相同，字段、值及顺序见该段。', ''])
                records_in = records_in[:start] + records_in[end:]
                positions = positions[:start] + positions[end:]
                if not records_in: continue
                lines.extend(['下方按原顺序列出其余输入，原输入行号为 '+row_ranges(positions)+'。', ''])
                description += '的其余输入'
        conditions = [{key:record[key] for key in ('taskName','formName','executionPeriod') if key in record} for record in records_in]
        conditions = list({_json(condition):condition for condition in conditions if condition}.values())
        if conditions: section('实际取数的业务表与数据加载时期',conditions,3)
        same_rows = bool(records_in) and [r.get('ref') for r in records_in] == list(inputs_by_fact)
        def matches_source(record):
            source=inputs_by_fact.get(record.get('ref'))
            if source is None: return False
            for key in business_input_keys:
                if key not in record:
                    if key in source and key not in ('inputRole','inputMemberships','sourceValueState'):
                        return False
                    continue
                actual,expected=record[key],source.get(key)
                if key=='inputMemberships':
                    actual,expected=business_memberships(actual),business_memberships(expected)
                elif key=='sourceValueState' and key not in source:
                    expected=source.get('valueState')
                if _json(actual)!=_json(expected): return False
                if key not in source and key not in ('inputMemberships','sourceValueState'): return False
            return True
        same_values = bool(records_in) and all(matches_source(record) for record in records_in)
        if same_values:
            if same_rows:
                lines.extend([f'{description}使用上方全部 {len(records_in)} 条来源数据，按来源原顺序输入。',''])
            else:
                lines.extend([f'{description}复用上方来源值，实际输入次序如下（含重复使用）：',''])
                table(['输入次序','来源位置'],[[str(i),scalar(record['ref'])] for i,record in zip(positions,records_in)])
        elif records_in:
            heading(f'{description}对应的业务输入',3)
            projected=[]
            for original_position, record in zip(positions, records_in):
                selected={k:v for k,v in record.items() if k in business_input_keys}
                selected['factId']=record.get('ref','输入行'+str(original_position))
                if 'data' in selected or 'rows' in selected: section('实际输入',selected,4)
                elif any(key in selected for key in ('value','dimensions','headers')): projected.append(selected)
            if projected: facts(projected,4)
        if records_in:
            lines.extend(['脚本字段说明：value 为来源原值，数值字符串按原始精度保留；dimensions 为维度编码，dimensionNames 为名称；'
                          'period 为实际数据时期；headers 为来源标题，unit 为来源单位。缺失字段与来源空值按实际输入区分。',''])
        extras=[]
        extra_positions=[]
        label_separator = None
        if records_in and all(isinstance(record.get('label'), str) and record.get('headers')
                              and all(isinstance(part, str) for part in record['headers']) for record in records_in):
            label_separator = next((sep for sep in ('_', ' / ') if all(
                record['label'] == sep.join(record['headers']) for record in records_in)), None)
        if label_separator is not None:
            lines.extend([f'脚本收到的每行 label 均由该行原始 headers 按顺序用 `{label_separator}` 连接。', ''])
        for record_index,record in zip(positions,records_in):
            extra={key:child for key,child in record.items() if key not in HOST_INPUT_FIELDS | business_input_keys
                   or key in read_fields}
            # A distinct script label cannot silently be replaced by a source header.
            if label_separator is None and record.get('label') and record.get('label') != ' / '.join(record.get('headers',[])):
                extra['label']=record['label']
            for field in ('dimensions','dimensionNames'):
                if field in record and not record[field]: extra[field]=record[field]
            if extra:
                extras.append({'来源数据行':record.get('ref'),**extra})
                extra_positions.append(record_index)
        if extras:
            # Collapse repeated supplemental descriptions, never source rows.
            # Input positions keep correlation with the exact code input order.
            extra_groups=OrderedDict()
            for record_index,extra in zip(extra_positions,extras):
                fields={k:v for k,v in extra.items() if k!='来源数据行'}
                extra_groups.setdefault(_json(fields),(fields,[]))[1].append(record_index)
            if len(extra_groups)<len(extras):
                section('脚本输入的附加业务数据',[
                    {'适用输入行':row_ranges(positions),**fields} for fields,positions in extra_groups.values()],3)
            else: section('脚本输入的附加业务数据',extras,3)

    heading('三、计算过程',2)
    if runs:
        for index,run in enumerate(runs,1):
            heading(f'Python 脚本 {index}',3)
            if run.get('purpose'): lines.extend([run['purpose'],''])
            section('code',run.get('code',''),4,python_source=True)
            used={locations.get(alias,alias):original for alias,original in aliases.items() if original in run.get('code','')}
            used.update({location:original for location,original in wire_references.items() if original in run.get('code','')})
            if used: section('脚本直接使用的原始数据标识',used,4)
    else:
        lines.extend(['本阶段没有待审 Python 脚本。使用来源查询结果及已执行的计算结果。',''])
    for item in derived:
        if isinstance(item,dict) and item.get('operator') and item.get('operator') != 'PYTHON':
            rules={k:item[k] for k in ('label','operator','formula','expression','parameters','rule') if k in item}
            section('程序执行的计算规则',rules,3)
            if item.get('inputRefs'): section('inputRefs',item['inputRefs'],4)
    for calculation in evidence.get('referencedCalculations',[]):
        section('精确计算工具的实际输入、运算和结果',output_business_fields(calculation),3)
    for run in runs:
        policies={key:run[key] for key in ('numericPolicy','numericPolicyVersion','precisionPolicy','nullPolicy') if key in run}
        if policies: section('实际数值处理规则',policies,3)
    comparison=evidence.get('focusComparison')
    if comparison:
        section('本次计算修改的前后依据',comparison,3)

    heading('四、执行结果与交付内容',2)
    shown_output_tables = {}
    for index,item in enumerate(outputs,1):
        if not isinstance(item,dict): section(f'结果 {index}',item,3); continue
        result=output_business_fields(item)
        rows, columns = result.get('rows'), result.get('columns')
        if (result.get('outputKind') in ('TABLE', 'CHART_DATA') and columns and rows
                and all(isinstance(row, dict) and isinstance(row.get('values'), dict) for row in rows)):
            # Only share the printed cells. Each run, result identity, role and lineage stays independently addressable.
            body = _json([columns, [{k: v for k, v in row.items() if k != 'rowRef'} for row in rows]])
            previous = shown_output_tables.setdefault(body, index)
            if previous != index:
                del result['rows'], result['columns']
                result['明细位置'] = f'列定义、逐行取值和状态与结果{previous}相同，按相同顺序读取其明细。'
        if review_targets:
            result={'本次用途':('本次采用' if item.get('derivedId') in review_targets or item.get('pythonRunRef') in review_targets
                             else '前置计算或探索上下文；核对其对本次采用结果的影响'),**result}
        section(f'结果 {index}'+('：'+item['label'] if item.get('label') else ''),result,3)
    for run in runs:
        if run.get('status') in ('FAILED','CANCELLED'):
            section('未完成的计算',{'状态':run['status'],'原因':run.get('error') or run.get('message')},3)
    failures=value.get('toolFailures') or []
    if failures:
        heading('执行过程中记录的异常',3)
        lines.extend(['以下保留实际发生的异常；是否仍影响本次交付，应结合后续执行结果判断。',''])
        for index,failure in enumerate(failures,1):
            section(f'异常 {index}',{k:v for k,v in failure.items() if k != 'callId'},4)
    review=value.get('calculationReview')
    if isinstance(review,dict):
        summary={key:review[key] for key in ('status','summary','message') if key in review}
        if review.get('coverage'):
            summary['已核对的业务要求']=[{key:row[key] for key in ('requirement','status','location') if key in row}
                                       for row in review['coverage']]
        if review.get('issues'):
            summary['发现的问题']=[{key:row[key] for key in ('message','description','severity','location','suggestion') if key in row}
                                  for row in review['issues']]
        section('已有计算审查结论',summary,3)
    if value.get('documentTrends'):
        heading('时期折线图', 3)
        table(['指标', '时期', '期数', '序列数', '核对状态'], [
            [escape(item['metric'] + ('（'+item['unit']+'）' if item['unit'] else '')),
             escape(item['from']+'至'+item['to']), str(item['periods']), str(item['series']),
             '实际图形已核对' if item['measured'] else '已绑定，未实测']
            for item in value['documentTrends']])
    if value.get('documentTextBasis') == 'VERIFIED_MARKDOWN':
        section('宿主交付的图表与数据附录', {'实际绑定图表': value.get('documentFigures', []),
                    '附录业务行数': value.get('appendixRows'), '来源事实数': value.get('appendixFacts')}, 3)
    focus = value.get('revisionFocus') or {}
    if focus.get('scope') == 'MARKDOWN_DELTA':
        section('上次审查要求修正的内容', focus['previousIssues'], 3)
    if value.get('documentOrders'):
        heading('图表实际排列方向', 3)
        table(['图表数据集', '已核对的数值顺序'],
              [[escape(key), '从小到大' if direction == 'asc' else '从大到小']
               for key, direction in value['documentOrders'].items()])
    if value.get('documentTextBasis') == 'HOST_RENDERED' and value.get('documentText'):
        gaps = _rendered_column_gaps(value['documentText'])
        if gaps:
            heading('实测页面中的空列', 3)
            lines.append('、'.join(gaps) + '。请按本次来源核对这些列。来源有名称或数值时，检查明细能否逐行对应；来源确实为空时按空值接受。')
    for key in ('documentText','documentSourceText','candidateAnswer'):
        if value.get(key): section(key,value[key],3)
    if value.get('completeRankedListsAvailable'):
        section('本次排序答复的交付选择',
                '按用户原问选择 rankedDelivery。要求各成员完整排序，且上方来源业务表已回答整题时，选 SOURCE_TABLES；宿主将原样交付所有排序表，按这些表给出审核结论。'
                '要求限定名次、只看某个来源、另外解释或计算，或来源表不能独立回答整题时，选 CANDIDATE，审核本次答复。',3)
    projection=value.get('documentSourceTextProjection') or {}
    extra_text=['\n'.join(change.get('sourceLines',[])) for change in projection.get('changes',[])]
    if any(extra_text): section('报告折叠部分及其他展示文字','\n\n'.join(extra_text),3)
    if value.get('documentText') and value.get('documentTextBasis'):
        lines.append('正文取自：'+{'HOST_RENDERED':'程序读取的实际展示正文','VERIFIED_MARKDOWN':'已通过渲染检查的 Markdown 与来源绑定','STATIC_SOURCE':'静态页面文字（尚未验证浏览器实际展示）'}.get(value['documentTextBasis'],value['documentTextBasis']))
    return '\n'.join(lines) + '\n'
