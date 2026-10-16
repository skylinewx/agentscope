"""Publication guards and model-authored presentation."""

from __future__ import annotations

from .models import AnalysisError, decimal_value, digest
from collections import Counter
from .formatting import number_text, validate_number_format


def value_reference_details(value, field=None, node_ref=None):
    fields = ['value', 'unit']
    details = {'valueRef': value.get('derivedId') or value.get('factId'),
               'valueField': field or 'value', 'nodeRef': node_ref, 'availableFields': fields}
    if 'counts' in value:
        fields.extend(value['counts'])
    if 'formulaTrace' in value:
        fields.extend(['sourceValue', 'localValue', 'localDifference', 'difference', 'absoluteDifference', 'localAbsoluteDifference'])
        details['allowedNodeRefs'] = [n['factRef'] for n in value['formulaTrace']['nodes']]
        details['hint'] = '公式数值使用本 FORMULA_CHECK 的 derivedId，nodeRef 选择实际节点；localValue 为本层，value 为逐层。'
    elif 'reconciliation' in value:
        fields.extend(['sourceValue', 'childSum', 'difference', 'absoluteDifference'])
    elif 'factId' in value:
        details['hint'] = '来源 factId 仅提供 value/unit；公式计算字段需引用已有 FORMULA_CHECK derivedId，并用 nodeRef 指向该事实。'
    return details


def selected_value(value, field=None, node_ref=None):
    field = field or 'value'
    if node_ref is not None and not any(n['factRef'] == node_ref for n in value.get('formulaTrace', {}).get('nodes', [])):
        raise AnalysisError('INVALID_VALUE_REF', 'nodeRef 仅能指定当前公式追溯中的实际节点。', value_reference_details(value, field, node_ref))
    if field == 'unit': return value.get('unit') or ''
    if field == 'value' and node_ref is None: return value.get('value')
    if field in ('positive', 'negative', 'zero', 'observed', 'unavailable') and 'counts' in value:
        return str(value['counts'][field])
    if 'formulaTrace' in value:
        nodes = value['formulaTrace']['nodes']
        node = next((n for n in nodes if n['factRef'] == node_ref), None) if node_ref else nodes[0]
        if node is None: raise AnalysisError('INVALID_VALUE_REF', 'nodeRef 不属于该公式追溯。', value_reference_details(value, field, node_ref))
        fields = {'sourceValue': node['observed'], 'localValue': node['localCheck']['value'],
                  'localDifference': node['localCheck']['difference'], 'difference': node['difference'], 'value': node['recalculated']}
        fields['absoluteDifference'] = (node['comparisons']['recursive'] or {}).get('absoluteDifference')
        fields['localAbsoluteDifference'] = (node['comparisons']['local'] or {}).get('absoluteDifference')
        if field in fields: return fields[field]
    if 'reconciliation' in value:
        fields = {'sourceValue': value['reconciliation']['parentValue'], 'childSum': value['reconciliation']['childSum'],
                  'difference': value['reconciliation']['difference']}
        fields['absoluteDifference'] = (value['reconciliation'].get('comparison') or {}).get('absoluteDifference')
        if field in fields: return fields[field]
    raise AnalysisError('INVALID_VALUE_REF', '该结果不存在所选数值字段。', value_reference_details(value, field, node_ref))


def bound_value(value, field=None, options=None, node_ref=None):
    raw = selected_value(value, field, node_ref)
    return number_text(raw, options) if options is not None else raw


def render_parts(known, parts, path='parts'):
    if not isinstance(parts, list) or not 1 <= len(parts) <= 100:
        raise AnalysisError('INVALID_CLAIM', '文字需要有界的文字和值引用。')
    refs, rendered = [], []
    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            raise AnalysisError('INVALID_CLAIM', '文字和值引用片段必须为对象。')
        if set(part) == {'text'}:
            if not isinstance(part['text'], str) or len(part['text']) > 2000:
                raise AnalysisError('CLAIM_LIMIT', '文字片段过长。')
            rendered.append(part['text']); continue
        try:
            if not set(part) <= {'valueRef','field','format','nodeRef'} or 'valueRef' not in part:
                raise AnalysisError('INVALID_VALUE_REF', '文字片段不是受支持的值引用。')
            value = known.get(part['valueRef'])
            if not value or value.get('valueState') != 'PRESENT' or value.get('calculationValidation','PASS') != 'PASS':
                raise AnalysisError('UNSUPPORTED_CLAIM', '文字引用了不存在、缺值或未通过条件的数值。')
            if 'format' in part:
                validate_number_format(part['format'])
                if part.get('field') == 'unit': raise AnalysisError('INVALID_NUMBER_FORMAT', '单位文本不接受数值格式。')
            raw = selected_value(value, part.get('field'), part.get('nodeRef'))
            if raw is None: raise AnalysisError('INVALID_VALUE_REF', '该结果不是可用的标量数值。', value_reference_details(value, part.get('field'), part.get('nodeRef')))
            rendered.append(bound_value(value, part.get('field'), part.get('format'), part.get('nodeRef')))
        except AnalysisError as exc:
            details = {'valueRef': part.get('valueRef'), 'valueField': part.get('field', 'value'),
                       'nodeRef': part.get('nodeRef'), **(exc.details or {}), 'relativePath': f'{path}/{index}'}
            raise AnalysisError(exc.code, str(exc), details) from exc
        refs.append(part['valueRef'])
    text = ''.join(rendered)
    if len(text.encode('utf-16-le')) > 32000:
        raise AnalysisError('CLAIM_LIMIT', '文字超过展示容量，请拆分。')
    return text, refs


def view_description(artifact, view):
    if 'descriptionParts' not in view: return view.get('description', '')
    known = {f['factId']: f for f in artifact['facts']}
    known.update({v['derivedId']: v for v in artifact['derivedValues']})
    return render_parts(known, view['descriptionParts'])[0]


def public_states(artifact) -> dict:
    return {key: artifact[key] for key in ("coverage", "receiptVerification", "comparability", "calculationValidation", "publicationState")}


def validate_report_topics(artifact, published=False, document=None):
    """The model chooses report scope; the host checks its declared references."""
    topics = artifact['scope'].get('reportTopics')
    if topics is None: return
    if not isinstance(topics, list) or not 1 <= len(topics) <= 50:
        raise AnalysisError('INVALID_REPORT_TOPICS', '报告主题需要 1 至 50 项明确的问题与来源引用。')
    known = {f['factId'] for f in artifact['facts']}
    displayed = set()
    if published:
        from .coverage import represented
        displayed.update(represented(artifact, document)['factRefs'])
    for topic in topics:
        if (not isinstance(topic, dict) or set(topic) != {'title', 'question', 'factRefs'}
                or any(not isinstance(topic[k], str) or not topic[k].strip() or len(topic[k]) > 1024 for k in ('title','question'))
                or not isinstance(topic['factRefs'], list) or not 1 <= len(topic['factRefs']) <= 1000
                or any(not isinstance(ref,str) or ref not in known for ref in topic['factRefs'])
                or len(set(topic['factRefs'])) != len(topic['factRefs'])):
            raise AnalysisError('INVALID_REPORT_TOPICS', '每个报告主题必须关联本次构建中真实存在的关键事实。')
        if published and not set(topic['factRefs']) <= displayed:
            raise AnalysisError('REPORT_TOPIC_UNREPRESENTED', '报告遗漏了已声明主题的关键数据，请补充结论或展示计划。',
                                {'title': topic['title'], 'missingFactRefs': sorted(set(topic['factRefs'])-displayed)})


def validate_claims(artifact: dict, claims: list[dict]) -> list[dict]:
    known = {f["factId"]: f for f in artifact["facts"]}
    known.update({v["derivedId"]: v for v in artifact["derivedValues"]})
    if len(claims) > 50:
        raise AnalysisError("CLAIM_LIMIT", "发布结论过多。")
    result, seen = [], set()
    for claim in claims:
        cid = claim.get("claimId")
        if not isinstance(cid, str) or not cid or cid in seen:
            raise AnalysisError("INVALID_CLAIM", "结论需要唯一标识。")
        seen.add(cid)
        kind = claim.get("kind")
        if kind not in ("fact", "computed", "hypothesis"):
            raise AnalysisError("INVALID_CLAIM", "结论类型必须区分事实、计算与假设。")
        if claim.get("scopeId") != artifact["scope"]["scopeId"]:
            raise AnalysisError("CLAIM_SCOPE", "结论与分析范围不一致。")
        if kind == "hypothesis":
            missing = claim.get("missingEvidence")
            if not isinstance(missing, list) or not 1 <= len(missing) <= 20 or any(not isinstance(item, str) or not item.strip() or len(item) > 1024 for item in missing):
                raise AnalysisError("MISSING_EVIDENCE", "原因假设须列出具体缺少的证据。")
        elif claim.get("missingEvidence"):
            raise AnalysisError("INVALID_CLAIM", "缺证条件仅用于假设结论。")
        rendered, refs = render_parts(known, claim.get('parts'))
        if kind != "hypothesis" and not refs:
            raise AnalysisError("UNSUPPORTED_CLAIM", "事实与计算结论需要实际值引用。")
        if kind == "fact" and any(ref.startswith("derived_") for ref in refs):
            raise AnalysisError("CLAIM_KIND", "派生计算不能标成来源原始事实。", {'field': 'kind', 'suggestedKind': 'computed', 'valueRefs': refs})
        if kind == "computed" and not any(ref.startswith("derived_") for ref in refs):
            raise AnalysisError("CLAIM_KIND", "计算结论必须引用实际派生计算；若仅叙述来源值，使用 fact。", {'field': 'kind', 'valueRefs': refs, 'suggestedKind': 'fact', 'hint': '当前 parts 仅引用来源值；若这就是原意，保留 parts 并将 kind 改为 fact。若包含新计算，先取得并引用真实 derived 结果，不能只改类型或删除数字掩盖未核验的计算。'})
        if len(rendered.encode("utf-16-le")) > 32000:
            raise AnalysisError("CLAIM_LIMIT", "单条结论超过展示文字容量，请拆成多条有引用的结论。")
        result.append({**claim, "valueRefs": refs, "rendered": rendered,
                       "support": "NEEDS_EVIDENCE" if kind == "hypothesis" else "REFERENCES_VALID"})
    return result


def validate_source_chart(facts, scope):
    """Permit raw-source geometry without asserting business comparability."""
    if not facts or any(not f.get("factId") or f.get("valueState") != "PRESENT" for f in facts):
        raise AnalysisError("INVALID_VIEW_REF", "来源原值图只能引用已取得的来源事实。")
    if len({f["objectId"] for f in facts}) != len(facts):
        raise AnalysisError("DUPLICATE_MEMBER", "来源原值图中每个对象只能选择一个指标。")
    if not scope.get("memberDimension") or any(
        not f.get("sourceOrigins") or any(not f.get(k) for k in ("bindingRef", "cellKey", "measureCode"))
        for f in facts
    ):
        raise AnalysisError("SOURCE_CHART_UNCONFIRMED", "来源原值图需要明确的来源、指标与成员维度。")
    signatures = {digest({
        **{k: f.get(k) for k in ("sourceOrigins", "bindingRef", "period", "measureCode", "unit", "scale")},
        'context': f.get('dataContext', f.get('context')),
        "metricKey": f.get('metricKey', f.get('cellKey')),
        "dimensions": {k: v for k, v in f.get("dimensions", {}).items() if k != scope["memberDimension"]},
    }) for f in facts}
    if len(signatures) != 1:
        raise AnalysisError("SOURCE_CHART_CONFLICT", "来源原值图不能混合来源、指标、时期、口径或不同的单位与缩放标注。")
    if any(f.get("precision") == "display_only" for f in facts):
        raise AnalysisError("PRECISION_UNPROVEN", "来源原值图需要可核对的原始数值，不能仅依据显示文本。")
    for fact in facts:
        decimal_value(fact["value"])


def validate_views(artifact, views):
    if len(views) > 20:
        raise AnalysisError("VIEW_LIMIT", "展示要求超过首版数量上限。")
    known = {f["factId"]: f for f in artifact["facts"]}
    known.update({v["derivedId"]: v for v in artifact["derivedValues"]})
    ids = set()
    for view in views:
        if set(view) - {"viewId", "scopeId", "type", "valueRefs", "title", "field", "relationRef", "showUnit", "description", "includeDetails", "collapsed", "pageSize", "nodeRefs", "numberFormat", "labels", "comparisonRef", "descriptionParts", "maxInputsPerScene", "groupId", "orderRef", "nodeDescriptions", "relationLayout", "showChecks", "emphasisRefs", "orientation"}:
            raise AnalysisError("INVALID_VIEW", "展示要求不接受任意代码或替代数值。")
        if "title" in view and (not isinstance(view["title"], str) or not view["title"].strip() or len(view["title"]) > 1024):
            raise AnalysisError("INVALID_VIEW", "展示标题必须是有界的业务说明。")
        for option in ('showUnit', 'includeDetails', 'collapsed', 'showChecks'):
            if option in view and type(view[option]) is not bool:
                raise AnalysisError('INVALID_VIEW', '展示开关必须为布尔值。')
        if 'description' in view and (not isinstance(view['description'], str) or len(view['description']) > 2000):
            raise AnalysisError('INVALID_VIEW', '图表说明必须是有界文字。')
        if 'descriptionParts' in view:
            if 'description' in view: raise AnalysisError('INVALID_VIEW', 'description 与 descriptionParts 二选一。')
            render_parts(known, view['descriptionParts'], 'descriptionParts')
        if 'maxInputsPerScene' in view and (view['type'] not in ('FORMULA','HIERARCHY')
                or type(view['maxInputsPerScene']) is not int or not 1 <= view['maxInputsPerScene'] <= 100):
            raise AnalysisError('INVALID_VIEW', '关系图每组输入数量须在 1 至 100 之间。')
        for field, kinds, choices in (
            ('relationLayout', ('FORMULA','HIERARCHY'), ('compact','flow')),
            ('orientation', ('BAR_VALUES','BAR_RANK','WATERFALL','RECONCILIATION'), ('horizontal','vertical')),
        ):
            if field in view and (view['type'] not in kinds or view[field] not in choices):
                raise AnalysisError('INVALID_VIEW', '布局选项不适用于此视图。', {'field':field,'allowedViewTypes':list(kinds)})
        if 'showChecks' in view and view['type'] not in ('FORMULA','HIERARCHY','WATERFALL'):
            raise AnalysisError('INVALID_VIEW','showChecks 仅用于公式、层级与瀑布图。',{'field':'showChecks'})
        if 'emphasisRefs' in view:
            allowed={ref for ref in view['valueRefs'] if 'factId' in known.get(ref,{})}
            if view['type'] == 'FORMULA':
                from .formula_selection import selected_nodes
                for ref in view['valueRefs']:
                    trace=known.get(ref,{}).get('formulaTrace')
                    if trace:
                        for node in selected_nodes(trace,view):
                            if not node.get('expression') or node.get('boundary'): continue
                            allowed.add(node['factRef'])
                            allowed.update(d['factRef'] for d in node['dependencies'] if d.get('factRef'))
            if (view['type'] not in ('FORMULA','HIERARCHY') or not isinstance(view['emphasisRefs'],list)
                    or len(view['emphasisRefs'])>100 or any(not isinstance(x,str) or x not in allowed for x in view['emphasisRefs'])):
                raise AnalysisError('INVALID_VIEW','重点须引用本图实际事实。',{'field':'emphasisRefs'})
        if 'pageSize' in view and (type(view['pageSize']) is not int or not 1 <= view['pageSize'] <= 100):
            raise AnalysisError('INVALID_VIEW', '明细每页行数须在 1 至 100 之间。')
        if 'includeDetails' in view and view['type'] not in ('FORMULA', 'HIERARCHY'):
            raise AnalysisError('INVALID_VIEW', 'includeDetails 仅适用于 FORMULA/HIERARCHY；TABLE 本身就是明细。',
                                {'field': 'includeDetails', 'allowedViewTypes': ['FORMULA', 'HIERARCHY'],
                                 'hint': '仅移除此字段，保留原视图及其 valueRefs、标题、简称和分组。'})
        if not view.get("viewId") or view["viewId"] in ids or view.get("scopeId") != artifact["scope"]["scopeId"]:
            raise AnalysisError("VIEW_SCOPE", "展示身份或范围不一致。")
        ids.add(view["viewId"])
        if view.get("type") not in ("METRIC", "TABLE", "BAR_VALUES", "BAR_RANK", "BRIDGE", "ORDER", "FORMULA", "HIERARCHY", "WATERFALL", "RECONCILIATION"):
            raise AnalysisError("UNSUPPORTED_VIEW", "不支持该视图类型。")
        if not view.get("valueRefs") or len(view["valueRefs"]) > 1000:
            raise AnalysisError("INVALID_VIEW_REF", "视图需要有界且非空的值引用。")
        if not isinstance(view["valueRefs"], list) or any(not isinstance(ref, str) for ref in view["valueRefs"]):
            raise AnalysisError("INVALID_VIEW_REF", "值引用必须为字符串列表。")
        if len(set(view["valueRefs"])) != len(view["valueRefs"]):
            raise AnalysisError("INVALID_VIEW_REF", "同一展示项不能重复引用同一个值。")
        if 'nodeRefs' in view:
            allowed = {n['factRef'] for ref in view.get('valueRefs', []) for n in known.get(ref, {}).get('formulaTrace', {}).get('nodes', [])}
            refs = view['nodeRefs']
            if (view['type'] not in ('FORMULA', 'WATERFALL') or not isinstance(refs, list) or not refs or len(refs) > 1000
                    or any(not isinstance(ref, str) or ref not in allowed for ref in refs) or len(set(refs)) != len(refs)):
                raise AnalysisError('INVALID_VIEW', 'nodeRefs 必须填写所选 FORMULA_CHECK 的 formulaTrace.nodes[].factRef，不能填写 derivedId。',
                                    {'field': 'nodeRefs', 'allowedViewTypes': ['FORMULA', 'WATERFALL'],
                                     'allowedNodeRefs': sorted(allowed)[:100], 'allowedNodeCount': len(allowed),
                                     'nextSection': 'formulas', 'valueRefs': view.get('valueRefs', []),
                                     'hint': 'valueRefs 保持 derived 引用；只修正 nodeRefs，或移除此可选字段展示全部公式节点。'})
        if 'groupId' in view and (not isinstance(view['groupId'], str) or not view['groupId'] or len(view['groupId']) > 1024):
            raise AnalysisError('INVALID_VIEW', 'groupId 必须为有界标识。')
        if 'orderRef' in view:
            order = known.get(view['orderRef'], {})
            if view['type'] not in ('TABLE', 'BAR_VALUES', 'BAR_RANK') or order.get('operator') not in ('SORT', 'RANK') or order.get('calculationValidation') != 'PASS':
                raise AnalysisError('INVALID_ORDER_REF', 'TABLE/BAR_VALUES/BAR_RANK 的 orderRef 须引用通过校验的 SORT/RANK；ORDER 把排序引用放入 valueRefs。',
                                    {'viewId': view['viewId'], 'allowedViewTypes': ['TABLE', 'BAR_VALUES', 'BAR_RANK'],
                                     'hint': '保留要展示的事实；不要删除表格或图形来绕过错误。'})
            refs = [f for ref in view['valueRefs'] for f in known.get(ref, {}).get('sourceFactIds', [ref])]
            members = {known[ref]['objectId'] for ref in refs if ref in known and 'factId' in known[ref]}
            ordered = order.get('orderedFactIds', []) + order.get('unavailableFactIds', [])
            anchors = [known[ref]['objectId'] for ref in ordered]
            if len(set(anchors)) != len(anchors) or not members <= set(anchors):
                raise AnalysisError('INVALID_ORDER_REF', '排序须覆盖所选对象，并且每对象只有一个排序指标。')
        if 'nodeDescriptions' in view:
            allowed = {n['factRef'] for ref in view['valueRefs'] for n in known.get(ref, {}).get('formulaTrace', {}).get('nodes', [])}
            entries = view['nodeDescriptions']
            if view['type'] not in ('FORMULA', 'WATERFALL') or not isinstance(entries, list) or len(entries) > 100:
                raise AnalysisError('INVALID_NODE_DESCRIPTION', '节点说明仅适用于所选公式的实际节点。')
            seen_nodes = set()
            for entry_index, entry in enumerate(entries):
                if (not isinstance(entry, dict) or set(entry) != {'nodeRef', 'parts'} or entry.get('nodeRef') not in allowed or entry['nodeRef'] in seen_nodes):
                    raise AnalysisError('INVALID_NODE_DESCRIPTION', '节点说明须绑定唯一的实际节点。')
                seen_nodes.add(entry['nodeRef'])
                render_parts(known, entry['parts'], f'nodeDescriptions/{entry_index}/parts')
                for part in entry['parts']:
                    if 'valueRef' not in part: continue
                    value = known[part['valueRef']]
                    if value.get('formulaTrace') and (part['valueRef'] not in view['valueRefs'] or part.get('nodeRef', value['formulaTrace']['targetFactRef']) != entry['nodeRef']):
                        raise AnalysisError('INVALID_NODE_DESCRIPTION', '节点说明的公式数值必须来自该节点，不能套用其他节点的复算结论。')
        if 'numberFormat' in view:
            validate_number_format(view['numberFormat'])
        if 'labels' in view:
            allowed = set(view['valueRefs'])
            for ref in view['valueRefs']:
                allowed.update(known.get(ref, {}).get('sourceFactIds', []))
            entries = view['labels']
            if (not isinstance(entries, list) or len(entries) > 1000 or any(
                    not isinstance(item, dict) or set(item) != {'valueRef', 'label'} or item.get('valueRef') not in allowed
                    or not isinstance(item.get('label'), str) or not item['label'].strip() or len(item['label']) > 120 for item in entries)
                    or len({item['valueRef'] for item in entries}) != len(entries)):
                raise AnalysisError('INVALID_VIEW_LABEL', '展示简称须绑定所选值或其真实来源，且不重复、不超过 120 字。')
        if 'comparisonRef' in view:
            check = known.get(view['comparisonRef'], {})
            if (view['type'] != 'HIERARCHY' or check.get('operator') != 'RECONCILE'
                    or check.get('calculationValidation') != 'PASS'
                    or check.get('reconciliation', {}).get('relationRef') != view.get('relationRef')
                    or check.get('inputRefs') != view['valueRefs']):
                raise AnalysisError('INVALID_COMPARISON_REF', 'comparisonRef 仅用于 HIERARCHY；RECONCILIATION 把 RECONCILE 的 derivedId 填入 valueRefs。层级差额须绑定同一父项和直属子项。',
                                    {'field': 'comparisonRef', 'allowedViewTypes': ['HIERARCHY']})
        field = view.get("field", "value")
        if ("field" in view and view["type"] != "METRIC") or field not in ("value", "positive", "negative", "zero", "observed", "unavailable"):
            raise AnalysisError("INVALID_VIEW_FIELD", "仅指标卡可以选择标量值或 COUNT 的具体统计字段。")
        for ref in view.get("valueRefs", []):
            if ref not in known or (view["type"] not in ("TABLE", "FORMULA") and known[ref].get("calculationValidation", "PASS") != "PASS"):
                raise AnalysisError("INVALID_VIEW_REF", "视图引用了不存在或受阻的计算。", {'field': 'valueRefs', 'invalidRef': ref,
                    'nextSection': 'derived' if str(ref).startswith('derived_') else 'facts',
                    'hint': '按原视图目的修正引用；公式核对即使有断点也可由 FORMULA 展示，不改成不可用标量卡或删除图形。'})
            if view["type"] not in ("TABLE", "ORDER", "FORMULA", "HIERARCHY") and known[ref].get("valueState") not in ("PRESENT", "NOT_APPLICABLE"):
                raise AnalysisError("INVALID_VIEW_REF", "图表和指标卡不能把冲突或缺值当作数值。")
            if view["type"] == "METRIC" and (known[ref].get("value") is None or known[ref].get("valueState") != "PRESENT"):
                raise AnalysisError("INVALID_VIEW_REF", "指标卡需要可用的标量数值。")
            if view["type"] == "METRIC" and field != "value" and (known[ref].get("operator") != "COUNT" or field not in known[ref].get("counts", {})):
                raise AnalysisError("INVALID_VIEW_FIELD", "所选值没有该统计字段。")
            if view["type"] == "ORDER" and "factId" not in known[ref] and known[ref].get("operator") not in ("SORT", "RANK"):
                raise AnalysisError("INVALID_VIEW_REF", "展示顺序只能引用来源事实的明确顺序，或一个通过校验的 SORT/RANK 结果。")
        if view["type"] == "ORDER" and any("derivedId" in known[ref] for ref in view["valueRefs"]) and len(view["valueRefs"]) != 1:
            raise AnalysisError("INVALID_VIEW_REF", "使用排序结果时只能选择一个依据；手工顺序请提供来源事实引用列表。")
        if 'relationRef' in view and view['type'] != 'HIERARCHY':
            raise AnalysisError('INVALID_VIEW', '只有层级图可以指定关系引用。', {'field': 'relationRef', 'allowedViewTypes': ['HIERARCHY']})
        if view['type'] == 'FORMULA' and any(known[ref].get('operator') != 'FORMULA_CHECK' or not known[ref].get('formulaTrace') for ref in view['valueRefs']):
            raise AnalysisError('INVALID_VIEW_REF', '计算关系图必须引用 FORMULA_CHECK；受阻的核对可展示实际断点，不伪造结果。')
        if view['type'] in ('WATERFALL', 'RECONCILIATION'):
            operator = 'FORMULA_CHECK' if view['type'] == 'WATERFALL' else 'RECONCILE'
            if any(known[ref].get('operator') != operator or known[ref].get('calculationValidation') != 'PASS' for ref in view['valueRefs']):
                raise AnalysisError('INVALID_VIEW_REF', '差额图需要已完成的对应派生计算，不能直接填写来源 factId。',
                                    {'field': 'valueRefs', 'requiredOperator': operator,
                                     'allowedValueRefs': [ref for ref, value in known.items() if value.get('operator') == operator and value.get('calculationValidation') == 'PASS'][:100]})
        if view['type'] == 'HIERARCHY':
            from .relations import hierarchy
            hierarchy(artifact, view)
        if view["type"] == "BAR_RANK" and (len(view['valueRefs']) != 1 or known[view['valueRefs'][0]].get('operator') != 'RANK'):
            raise AnalysisError("RANK_UNCONFIRMED", "每张 BAR_RANK 的 valueRefs 需要一个通过可比性校验的 RANK；不同排名分别声明视图。只比较来源原值时用 BAR_VALUES 加事实引用。",
                                {'viewId': view['viewId'], 'field': 'valueRefs', 'requiredOperator': 'RANK', 'sourceValueView': 'BAR_VALUES'})
        if view["type"] == "BAR_VALUES":
            validate_source_chart([known[ref] for ref in view["valueRefs"]], artifact["scope"])
        if view["type"] == "BRIDGE" and not any(known[ref].get("operator") == "LINEAR" for ref in view.get("valueRefs", [])):
            raise AnalysisError("INCOMPLETE_BRIDGE", "构成图需要通过条件的完整构成计算。")
    if sum(view["type"] == "ORDER" for view in views) > 1:
        raise AnalysisError("INVALID_VIEW", "一个发布版本只能指定一个全局展示顺序。")
    if sum(len(view["valueRefs"]) for view in views if view["type"] == "METRIC") > 200:
        raise AnalysisError("VIEW_LIMIT", "所选指标卡超过 200 项，请缩小展示计划。")
    return views


def summary(artifact: dict, document=None) -> dict:
    """Structured tool data, never a prewritten user response or table."""
    from .presentation_options import ordered_for_view
    from .tables import ordered_facts
    from .coverage import represented
    from .completion import completion_summary
    facts = ordered_facts(artifact)
    orders = [{'viewId': view['viewId'], 'orderRef': view.get('orderRef'),
               'factRefs': [f['factId'] for f in ordered_for_view(artifact, view, facts) if f['factId'] in view['valueRefs']]}
              for view in artifact['viewPlan'] if view['type'] in ('TABLE','BAR_VALUES')]
    return {
        'document': ({k: artifact['freeDocument'][k] for k in ('version','title','bindingCount','contentDigest','valueRefs')} if artifact.get('freeDocument') else None),
        'represented': represented(artifact, document),
        'completion': completion_summary(artifact),
        'displayOrders': orders,
        'scope': artifact['scope'],
        'factCount': len(artifact['facts']),
        'valueStates': dict(Counter(f['valueState'] for f in artifact['facts'])),
        'derivedRefs': [v['derivedId'] for v in artifact['derivedValues']],
        'checks': [{'valueRef': v['derivedId'], 'executionStatus': v['executionStatus'],
                    'sourceConsistency': v.get('sourceConsistency', 'NOT_CHECKED'),
                    **({'targetLocalCheck': v['formulaTrace']['targetLocalCheck'],
                        'differenceOriginRefs': v['formulaTrace']['differenceOriginRefs']} if 'formulaTrace' in v else {}),
                    **({'reconciliation': v['reconciliation']} if 'reconciliation' in v else {})}
                   for v in artifact['derivedValues']],
        'claims': [c['rendered'] for c in artifact['claims']],
        'viewPlan': artifact['viewPlan'],
        'presentation': artifact.get('presentation', {}),
        'technicalDetails': artifact['technicalDetails'],
    }
