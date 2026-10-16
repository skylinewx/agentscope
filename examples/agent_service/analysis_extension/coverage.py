"""Data actually presented in explicit views; dependencies are not display."""


def represented(artifact, document=None):
    from .tables import ordered_facts
    from .presentation_options import ordered_for_view
    from .presentation import CHART_ROWS
    from .models import digest
    facts = {f['factId']: f for f in artifact['facts']}
    values = {v['derivedId']: v for v in artifact['derivedValues']}
    ordered = ordered_facts(artifact)
    shown, views = set(), []
    if artifact.get('freeDocument'):
        from .free_document import static_fact_refs
        shown.update(static_fact_refs(artifact))

    def parts_refs(parts):
        result = set()
        for part in parts:
            ref = part.get('valueRef')
            if ref in facts and part.get('field', 'value') == 'value':
                result.add(ref)
            if ref in values and part.get('field') == 'sourceValue':
                trace = values[ref].get('formulaTrace')
                if trace:
                    result.add(part.get('nodeRef', trace['targetFactRef']))
                elif values[ref].get('operator') == 'RECONCILE':
                    result.add(values[ref]['inputRefs'][0])
        return result

    # Free documents own their text; optional structured claims are not rendered
    # by that page unless the author actually binds the values in its HTML.
    for claim in ([] if artifact.get('freeDocument') else artifact.get('claims', [])):
        shown.update(parts_refs(claim.get('parts', [])))
    displayed_views = None
    displayed_groups = set()
    if document is not None:
        displayed_views = {m['viewId'] for m in document['metrics']}
        displayed_views.update(s['viewId'] for s in document['scenes'])
        table_keys = {t['tableKey'] for t in document['tables']}
        for view in artifact['viewPlan']:
            keys = {'table_' + digest(view['viewId'])[:24]}
            keys.update('table_' + digest([view['viewId'], ref])[:24] for ref in view['valueRefs'])
            if keys & table_keys:
                displayed_views.add(view['viewId'])
        displayed_groups = {s['groupId'] for s in document['sections']}
    for view in artifact.get('viewPlan', []):
        kind, refs = view['type'], view['valueRefs']
        if kind == 'ORDER':
            continue
        if displayed_views is not None and view['viewId'] not in displayed_views:
            continue
        local = parts_refs(view.get('descriptionParts', []))
        if kind in ('TABLE', 'METRIC', 'HIERARCHY'):
            local.update(ref for ref in refs if ref in facts)
        if kind in ('BAR_VALUES', 'BAR_RANK'):
            requested = set(refs) if kind == 'BAR_VALUES' else {f for ref in refs for f in values[ref].get('sourceFactIds', [])}
            candidates = [f for f in ordered_for_view(artifact, view, ordered) if f['factId'] in requested]
            local.update(f['factId'] for f in candidates[:CHART_ROWS])
        for ref in refs:
            value = values.get(ref, {})
            trace = value.get('formulaTrace')
            if trace:
                for node in trace['nodes']:
                    if view.get('nodeRefs') and node['factRef'] not in view['nodeRefs']:
                        continue
                    if kind == 'TABLE' or (kind == 'FORMULA' and view.get('includeDetails')):
                        local.add(node['factRef'])
                    if kind in ('FORMULA', 'WATERFALL') and node.get('expression') and not node.get('boundary'):
                        local.add(node['factRef'])
                        if kind == 'FORMULA':
                            local.update(d['factRef'] for d in node['dependencies'] if d.get('factRef') in facts)
                        for caption in view.get('nodeDescriptions', []):
                            if caption['nodeRef'] == node['factRef']:
                                local.update(parts_refs(caption['parts']))
            if kind == 'RECONCILIATION' and value.get('operator') == 'RECONCILE':
                # The chart displays the parent and an aggregate, not individual children.
                local.add(value['inputRefs'][0])
        local &= set(facts)
        shown.update(local)
        views.append({'viewId': view['viewId'], 'type': kind, 'title': view.get('title', ''),
                      'groupId': view.get('groupId'), 'collapsed': view.get('collapsed', False),
                      'factRefs': sorted(local), 'objectIds': sorted({facts[r]['objectId'] for r in local})})
    for group in artifact.get('presentation', {}).get('groups', []):
        if document is not None and 'group:' + group['groupId'] in displayed_groups:
            shown.update(parts_refs(group.get('descriptionParts', [])))
    topics = [{'title': t['title'], 'represented': set(t['factRefs']) <= shown,
               'missingFactRefs': sorted(set(t['factRefs']) - shown)} for t in artifact['scope'].get('reportTopics', [])]
    return {'factRefs': sorted(shown), 'objectIds': sorted({facts[r]['objectId'] for r in shown}),
            'views': views, 'topics': topics}
