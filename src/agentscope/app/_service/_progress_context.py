"""Public operation context: selected arguments and attributable result fields.

No model reasoning, system prompts, source code, or arbitrary recursive text
is admitted. Discovery candidates remain candidates, not confirmed selections.
"""
from __future__ import annotations

import json
import re

from ._progress_facts import _label, result_facts

_DECIMAL = re.compile(r'-?\d+(?:\.\d+)?')

_MODELS = {
    'BUDGET_TASK': '预算任务', 'BUDGET_TASK_FORM_BINDING': '任务与表样',
    'MD_ORG': '机构', 'MD_ORG_BUD': '预算机构', 'MD_MGRVER': '预算版本',
    'MD_STAGE': '任务阶段', 'MD_CURRENCY': '币种', 'MD_SCENE': '情景',
    'MD_BUDACCOUNT': '预算指标', 'DATATIME': '时期',
}


def tool_key(name):
    name = re.split(r'__|\.', name)[-1]
    return re.sub(r'(?<=[a-z0-9])(?=[A-Z])', '_', name).lower().replace('_', '')


def object_value(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def _items(value):
    return value if isinstance(value, list) else []


def _quoted(values, limit=3):
    labels = list(dict.fromkeys(label for value in values if (label := _label(value))))
    return '、'.join(f'“{label}”' for label in labels[:limit])


def _lookup(labels, model, key):
    return labels.get(f'{model}:{key}') or labels.get(str(key))


def _sum_inputs(request):
    variables = object_value(request.get('variables'))
    terms = str(request.get('expression', '')).replace(' ', '').split('+')
    if (1 < len(terms) <= 32 and len(set(terms)) == len(terms)
            and set(terms) == set(variables)
            and all(isinstance(v, str) and _DECIMAL.fullmatch(v) for v in variables.values())):
        return len(terms)
    return None


def operation(name, arguments, labels=None):
    """Describe an actually selected call; do not invent a future tool call."""
    request, labels = object_value(arguments), labels or {}
    key = tool_key(name)
    topic, title, plan = '', '', ''
    if key == 'getcontext':
        topic, title = 'source', '连接查询数据源'
        plan = ('接下来检查当前可查询的数据源，确认本次请求可以从哪里读取数据。'
                if request.get('action') == 'discover' else
                '接下来连接选定的数据源并建立查询上下文，让后续的对象查找和数据读取使用明确的数据范围。')
    elif key == 'searchentities':
        mention = _quoted([request.get('entityMention')])
        topic, title = 'entities', f'定位{mention or "查询对象"}'
        if mention:
            plan = f'接下来检索{mention}对应的对象，查看返回的名称和所属类别，避免仅凭相似名称就确定查询范围。'
    elif key == 'searchschema':
        keyword = _quoted([request.get('keyword')])
        topic, title = 'schema', '核对对象定义'
        if keyword:
            plan = f'接下来查找{keyword}的定义，确认这个名称在数据目录中的含义和适用范围。'
    elif key in ('discoverquerytargets', 'resolvegrouping'):
        group = _quoted([request.get('groupingKeyword')])
        if group:
            topic, title = 'grouping', f'核对{group}分组'
            plan = f'接下来检查所选表样是否支持按{group}分组，确认用于汇总的字段，避免把名称相近的分类混在一起。'
        else:
            metric = _quoted([request.get('keyword')])
            task = _quoted([request.get('taskKeyword')])
            year = request.get('year')
            period = f'{year}年' if isinstance(year, int) and 1900 <= year <= 2200 else ''
            topic, title = 'targets', f'定位{metric or "指标与表样"}'
            if metric or task:
                plan = f'接下来在{task or "可查询的预算任务"}中查找{period}{metric or "所需指标"}，比较候选表样与指标名称，确定读取金额前还需要哪些条件。'
    elif key == 'listcapabilities':
        names = [_MODELS.get(code) for code in _items(request.get('modelCodes'))]
        names = '、'.join(dict.fromkeys(n for n in names if n))
        topic, title = 'schema', '确认可读取字段'
        plan = f'接下来检查{names or "当前对象"}支持的字段和关系，确定如何读取所需条件。'
    elif key == 'executesemanticplan':
        steps = _items(object_value(request.get('plan')).get('steps'))
        names = [_MODELS.get(s.get('modelCode')) for s in steps if isinstance(s, dict)]
        names = list(dict.fromkeys(n for n in names if n))
        batch = next((object_value(s.get('batchQuery')) for s in steps
                      if isinstance(s, dict) and s.get('operation') == 'BATCH_DATA_QUERY'), None)
        if batch is not None:
            topic, title = 'query', '读取业务金额'
            periods, populations, groups, metrics = [], [], [], []
            for dim in _items(batch.get('batchDimensions')):
                if not isinstance(dim, dict):
                    continue
                if dim.get('dimCode') == 'MD_ORG' and dim.get('all') is True:
                    populations.append('该任务全部单位')
                start = object_value(object_value(dim.get('periodRequest')).get('start'))
                if isinstance(start.get('year'), int):
                    periods.append(f'{start["year"]}年')
            for group in _items(object_value(batch.get('aggregation')).get('groupBy')):
                if isinstance(group, dict):
                    label = _lookup(labels, group.get('dimCode'), group.get('field'))
                    if label:
                        groups.append(label)
            for metric in _items(batch.get('metrics')):
                selection = object_value(object_value(metric).get('selection'))
                for model, value in object_value(selection.get('targetDimensions')).items():
                    label = _lookup(labels, model, value)
                    if model == 'MD_BUDACCOUNT' and label:
                        metrics.append(label)
            subject = _quoted(metrics) or '所选指标'
            plan = f'接下来读取{"、".join(dict.fromkeys(periods))}的{subject}数据。' if periods else f'接下来读取{subject}的数据。'
            if populations:
                plan += '机构范围为' + '、'.join(populations) + '。'
            if groups:
                plan += '本次按' + _quoted(groups) + '汇总，取得对应分组的返回金额。'
            else:
                plan += '返回后才能说明实际金额、单位和有效数据范围。'
        else:
            topic, title = 'scope', '核对' + ('、'.join(names[:3]) or '查询条件')
            plan = f'接下来读取{"、".join(names[:3]) or "查询对象"}的具体取值，核对哪些条件可以用于本次查询；这一步还不会取得业务金额。'
            limits = list(dict.fromkeys(s['limit'] for s in steps if isinstance(s, dict)
                                       and isinstance(s.get('limit'), int) and not isinstance(s['limit'], bool)))
            if len(limits) == 1:
                plan += f'本次每项最多读取 {limits[0]} 个候选值。'
    elif key == 'queryresultread':
        topic, title = 'read', '补读已保存的查询结果'
        plan = '接下来从已保存的查询回执中补读选定字段，取得上一轮未展开的内容，避免只依据截断的预览作判断。'
    elif key == 'calculate':
        topic, title = 'calculation', '核对数值计算'
        count = _sum_inputs(request)
        plan = (f'接下来对选定的 {count} 个数值执行加法，取得这些输入的算术合计；是否可以用作业务总额，仍取决于输入范围和汇总口径。'
                if count else '接下来对选定的输入执行计算，检查算术结果；计算结果本身不能证明输入范围或业务口径完整。')
    elif key in ('read', 'query', 'check', 'analysisinspect', 'analysisreview', 'analysispython', 'analysispublish', 'analysisrenderreport'):
        topic, title, plan = {
            'read': ('read', '读取资料', '接下来读取所选资料，确认其中与本次问题有关的内容。'),
            'query': ('query', '查询数据', '接下来执行已选定的数据查询，等待实际返回值。'),
            'check': ('check', '核查数据', '接下来执行数据核查，检查当前结果和依据是否一致。'),
            'analysisinspect': ('read', '核对分析资料', '接下来读取已保存的分析资料，核对它的范围和依据。'),
            'analysisreview': ('check', '检查分析结果', '接下来检查分析结果，核对结论与数据依据是否一致。'),
            'analysispython': ('calculation', '运行分析计算', '接下来对已选择的输入运行计算；计算完成前不会把预期结果作为结论。'),
            'analysispublish': ('delivery', '检查交付条件', '接下来检查结果是否满足交付条件，再确定可供你使用的内容。'),
            'analysisrenderreport': ('delivery', '生成报告', '接下来生成已选择的报告，整理结果供你查阅。'),
        }[key]
    return {'topic': topic, 'title': title, 'nextAction': plan}


def public_observations(receipt, source_id, name='', request=None):
    """Bounded discovery/data facts, collected before projection or offloading."""
    receipt, request = object_value(receipt), object_value(request)
    key = tool_key(name)
    data = object_value(receipt.get('data'))
    facts, labels = [], {}
    profile = operation(name, request)

    def bind(key_, label):
        label = _label(label)
        if key_ and label and len(labels) < 128:
            labels[str(key_)] = label

    def add(text, pointer, value=None, **extra):
        if len(facts) < 4:
            facts.append({'text': text, 'sourceId': source_id, 'pointer': pointer,
                          'value': value, 'state': 'observed', **extra})

    if receipt.get('status') in ('FAILED', 'ERROR') or receipt.get('error'):
        error = object_value(receipt.get('error'))
        messages = [(f'/error/details/{i}/message', _label(item.get('message')))
                    for i, item in enumerate(_items(error.get('details'))[:2]) if isinstance(item, dict)]
        messages = [(p, m) for p, m in messages if m]
        if not messages and _label(error.get('message')):
            messages = [('/error/message', _label(error['message']))]
        if messages:
            pointer, message = messages[0]
            display = message.replace('INVALID_STEP_LIMIT: limit', '单次读取条数')
            add(f'{profile["title"] or "本次操作"}未成功，服务返回的原因是“{display}”。尚未取得这次请求的有效结果。', pointer, message, kind='failure')
        return {**profile, 'version': 1, 'facts': facts, 'labels': labels}

    if key == 'calculate' and isinstance(receipt.get('value'), str) and _DECIMAL.fullmatch(receipt['value']):
        count = _sum_inputs(request)
        subject = f'对选定的 {count} 个数值执行加法后，' if count else ''
        add(f'{subject}计算返回 {receipt["value"]}。这是所选输入的算术结果，尚不能据此确认业务总额；业务范围和单位仍需以原始输入为准。',
            '/value', receipt['value'], kind='calculation', state='calculated')
        return {**profile, 'version': 1, 'facts': facts, 'labels': labels}

    numeric = result_facts(receipt, source_id)
    if numeric:
        profile.update(topic='query', title='取得查询数据')
        # Amounts already include their observed period and group. Do not add
        # an unrelated generic claim of having verified these results.
        return {**profile, 'version': 1, 'facts': numeric, 'labels': labels}

    if key == 'analysistask' and isinstance(receipt.get('intent'), dict):
        intent = receipt['intent']
        subject = '、'.join(label for k in ('task', 'period', 'metric') if (label := _label(intent.get(k))))
        scope = _label(intent.get('population'))
        group = _label(intent.get('grouping'))
        if subject:
            profile.update(topic='request', title='记录本次查询范围')
            text = f'本次请求已登记为“{subject}”的查询'
            if scope:
                text += f'，机构范围是“{scope}”'
            if group:
                text += f'，按“{group}”分组'
            add(text + '。这只是本轮的处理范围，数据来源和可用条件还需通过实际查询确认。',
                '/intent', intent, kind='request')
    elif key == 'getcontext':
        entries = object_value(receipt.get('directory')).get('entries')
        if isinstance(entries, list):
            add(f'当前数据目录返回了 {len(entries)} 个来源条目，正在确认可用于本次查询的来源。这里还没有读取业务金额。',
                '/directory/entries', len(entries), kind='source', unit='个')
        if receipt.get('action') == 'open' and receipt.get('status') == 'SUCCEEDED' and receipt.get('session'):
            add('已连接选定的数据源并建立本次查询上下文。数据读取入口已经可用，具体的任务、指标和机构仍需从目录中定位。',
                '/status', receipt['status'], kind='source')
    elif data.get('resolutionKind') == 'GROUPING':
        profile.update(topic='grouping', title='确认分组字段')
        candidates = _items(data.get('candidates'))
        supported = [(i, c) for i, c in enumerate(candidates) if isinstance(c, dict) and c.get('supported') is True and _label(c.get('label'))]
        for i, candidate in supported[:2]:
            label = candidate['label']
            owner = _label(candidate.get('dimensionLabel')) or '当前表样'
            add(f'当前表样支持通过{owner}的“{label}”字段分组。分组字段已找到，但各组金额和是否存在空名称记录，要等查询结果返回后才能确认。',
                f'/data/candidates/{i}/label', label, kind='grouping')
            bind(f'{candidate.get("dimCode")}:{candidate.get("field")}', label)
        if not supported and data.get('status'):
            add('这次检查尚未找到可直接使用的分组字段，当前不能确认请求中的分组方式能够执行。',
                '/data/status', data['status'], kind='grouping')
    elif key == 'discoverquerytargets' and isinstance(data.get('candidates'), list):
        candidates = data['candidates']
        if not candidates:
            add('本次检索没有返回匹配的表样或指标候选，需要重新确认检索范围。它并不证明数据源中不存在所需数据。',
                '/data/candidates', 0, kind='candidate')
        for i, candidate in enumerate(candidates):
            if not isinstance(candidate, dict):
                continue
            task, form = _label(candidate.get('taskName')), _label(candidate.get('formName'))
            matches = [m for m in _items(candidate.get('matches')) if isinstance(m, dict) and _label(m.get('label'))]
            for match in matches:
                bind(match.get('resourceIri'), match['label'])
                for model, value in object_value(match.get('targetDimensions')).items():
                    bind(f'{model}:{value}', match['label'])
            bind(candidate.get('bindingIri'), f'{task}的{form}' if task and form else form)
            if i == 0 and task and form and matches:
                names = _quoted([m['label'] for m in matches], 2)
                caveat = '这些名称是候选匹配，尚未读取对应金额。'
                add(f'本次检索返回 {len(candidates)} 个候选表样。其中，“{task}”的“{form}”中包含{names}。{caveat}',
                    f'/data/candidates/{i}/matches', matches, kind='candidate', scope=f'{task} / {form}')
    elif key == 'searchentities' and isinstance(data.get('candidates'), list):
        candidates = data['candidates']
        names = _quoted([c.get('label') for c in candidates if isinstance(c, dict)])
        mention = _quoted([request.get('entityMention')]) or '本次检索'
        add(f'{mention}返回了 {len(candidates)} 个候选对象' + (f'，包括{names}。' if names else '。') +
            '候选名称仍需结合对象类别和任务范围确认，不能直接当作最终选定对象。',
            '/data/candidates', len(candidates), kind='candidate', unit='个')
        for candidate in candidates:
            if isinstance(candidate, dict):
                bind(candidate.get('resourceIri'), candidate.get('label'))
                bind(f'{candidate.get("modelCode")}:{candidate.get("key")}', candidate.get('label'))
    elif key == 'searchschema' and isinstance(data.get('definitions'), list):
        count = len(data['definitions'])
        keyword = _quoted([request.get('keyword')]) or '所选名称'
        add(f'关于{keyword}，本次定义检索返回 {count} 条记录。' +
            ('当前没有可以引用的定义，不能仅凭名称判断它对应哪个业务对象。' if count == 0 else '这些定义可用于区分同名对象，尚不是业务金额的查询结果。'),
            '/data/definitions', count, kind='schema', unit='条')
    elif key == 'listcapabilities' and isinstance(data.get('capabilities'), list):
        capabilities = data['capabilities']
        names = _quoted([c.get('label') for c in capabilities if isinstance(c, dict)], 4)
        add(f'当前对象可读取的字段或关系中包含{names}。这些信息用于确定查询入口和条件，尚未取得业务数值。' if names else '已返回当前对象的可读取字段及关系说明。',
            '/data/capabilities', len(capabilities), kind='schema')
        for capability in capabilities:
            if isinstance(capability, dict):
                bind(capability.get('capabilityId'), capability.get('label'))

    for index, step in enumerate(_items(data.get('stepResults'))):
        if not isinstance(step, dict) or not str(step.get('resultType', '')).startswith('ENTITY_SET'):
            continue
        rows = _items(step.get('rows'))
        model = next((r.get('modelCode') for r in rows if isinstance(r, dict)), '')
        category = _MODELS.get(model)
        for row in rows:
            if isinstance(row, dict):
                bind(row.get('resourceIri'), row.get('label'))
                bind(f'{row.get("modelCode")}:{row.get("key")}', row.get('label'))
        if category:
            row_labels = [r.get('label') for r in rows if isinstance(r, dict)]
            preferred = [n for n in row_labels if n in ('人民币', '上报版', '编制版', '预算数')]
            names = _quoted(preferred or row_labels)
            add(f'已读取{category}的 {len(rows)} 个候选值' + (f'，其中包括{names}。' if names else '。'),
                f'/data/stepResults/{index}/rows', len(rows), kind='scope', unit='个')

    return {**profile, 'version': 1, 'facts': facts, 'labels': labels}


def observations_from_output(text, metadata, tool_call_id, name='', request=None):
    public = metadata.get('public_progress')
    if not isinstance(public, dict) or public.get('version') != 1:
        public = public_observations(object_value(text), metadata.get('analysisCaptureRef') or tool_call_id, name, request)
        # A host schema rejection occurs before the tool/capture middleware.
        # Recognize this narrow diagnostic without exposing arbitrary stderr.
        if (not public['facts'] and isinstance(text, str)
                and text.startswith('Input validation failed for tool ')
                and "Additional properties are not allowed ('maximumRows' was unexpected)" in text):
            public['facts'] = [{'text': '查询请求包含当前入口不支持的最大行数参数，已在执行前被校验拒绝；本次尚未读取业务数据。',
                                'sourceId': tool_call_id, 'pointer': '', 'value': text,
                                'state': 'observed', 'kind': 'failure'}]
    facts = [dict(item, toolCallId=tool_call_id) for item in _items(public.get('facts'))[:4]
             if isinstance(item, dict) and isinstance(item.get('text'), str) and len(item['text']) <= 600
             and isinstance(item.get('sourceId'), str) and item.get('state') in ('observed', 'calculated', 'verified')]
    labels = {k: v for k, v in list(object_value(public.get('labels')).items())[:128]
              if isinstance(k, str) and len(k) < 600 and _label(v)}
    return {**operation(name, request, labels), **{k: public[k] for k in ('topic', 'title') if isinstance(public.get(k), str)},
            'facts': facts, 'labels': labels}
