"""Model-authored names and grouping; no business-specific layout decisions."""
from copy import deepcopy

from .models import AnalysisError
from .validation import render_parts, view_description


def validate_presentation(artifact, options, views, claims=None):
    options = {} if options is None else options
    if not isinstance(options, dict) or set(options) - {'labels', 'groups', 'claimsCollapsed', 'summaryPosition', 'summaryTitle', 'reportTitle'}:
        raise AnalysisError('INVALID_PRESENTATION', '展示设置仅接受 labels、groups、claimsCollapsed、summaryPosition、summaryTitle、reportTitle。')
    if options.get('summaryPosition', 'end') not in ('start', 'end'):
        raise AnalysisError('INVALID_PRESENTATION', 'summaryPosition 仅支持 start/end。', {'path': '/presentation/summaryPosition'})
    for field in ('summaryTitle', 'reportTitle'):
        if field in options and (not isinstance(options[field], str) or not options[field].strip() or len(options[field]) > 200):
            raise AnalysisError('INVALID_PRESENTATION', '报告及总结标题须为 1 至 200 字。', {'path': '/presentation/' + field})
    if 'claimsCollapsed' in options and type(options['claimsCollapsed']) is not bool:
        raise AnalysisError('INVALID_PRESENTATION', 'claimsCollapsed 必须为布尔值。')
    known = {f['factId']: f for f in artifact['facts']}
    known.update({v['derivedId']: v for v in artifact['derivedValues']})
    entries, seen = options.get('labels', []), set()
    if not isinstance(entries, list) or len(entries) > 2000:
        raise AnalysisError('INVALID_PRESENTATION', '共享简称最多 2000 项。')
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) != {'valueRef', 'role', 'label'}
                or entry.get('valueRef') not in known or entry.get('role') not in ('object', 'measure')
                or not isinstance(entry.get('label'), str) or not entry['label'].strip() or len(entry['label']) > 120
                or (entry['valueRef'], entry['role']) in seen):
            raise AnalysisError('INVALID_PRESENTATION', '共享简称需要真实引用、object/measure 角色和唯一的业务名称。')
        seen.add((entry['valueRef'], entry['role']))
    groups, ids = options.get('groups', []), set()
    if not isinstance(groups, list) or len(groups) > 20:
        raise AnalysisError('INVALID_PRESENTATION', '展示分组最多 20 项。')
    claim_ids = {c['claimId'] for c in (claims if claims is not None else artifact.get('claims', []))}
    assigned = set()
    used_groups = {v.get('groupId') for v in views if v['type'] != 'ORDER'}
    for index, group in enumerate(groups):
        path = f'/presentation/groups/{index}'
        if (not isinstance(group, dict) or set(group) - {'groupId', 'title', 'description', 'descriptionParts', 'width', 'claimRefs', 'claimsPosition'}
                or any(not isinstance(group.get(k), str) or not group[k].strip() or len(group[k]) > 1024 for k in ('groupId', 'title'))
                or group['groupId'] in ids):
            raise AnalysisError('INVALID_PRESENTATION', '分组须有唯一 groupId 与业务标题。')
        ids.add(group['groupId'])
        for field, default, choices in [('width', 'full', ('full', 'half')), ('claimsPosition', 'after', ('before', 'after'))]:
            if group.get(field, default) not in choices:
                raise AnalysisError('INVALID_PRESENTATION', f'{field} 仅支持 {"/".join(choices)}。', {'path': path + '/' + field})
        refs = group.get('claimRefs', [])
        if not isinstance(refs, list) or len(refs) > 50:
            raise AnalysisError('INVALID_PRESENTATION', 'claimRefs 须为最多 50 项的结论标识列表。', {'path': path + '/claimRefs'})
        for ref_index, ref in enumerate(refs):
            if not isinstance(ref, str) or ref not in claim_ids or ref in assigned:
                raise AnalysisError('INVALID_PRESENTATION', '局部解读须引用本次 claims 中真实且未重复分配的 claimId。',
                                    {'path': f'{path}/claimRefs/{ref_index}', 'hint': '保留结论原文和图表；只修正结论归属，同一结论只展示一次。'})
            assigned.add(ref)
        if refs and group['groupId'] not in used_groups:
            raise AnalysisError('INVALID_PRESENTATION', '包含局部解读的分组须关联至少一个实际视图。', {'path': path + '/claimRefs'})
        if 'description' in group and (not isinstance(group['description'], str) or len(group['description']) > 2000):
            raise AnalysisError('INVALID_PRESENTATION', '分组说明最多 2000 字。')
        if 'descriptionParts' in group:
            if 'description' in group: raise AnalysisError('INVALID_PRESENTATION', '分组说明与数值片段二选一。')
            render_parts(known, group['descriptionParts'], f'groups/{index}/descriptionParts')
    closed, previous = set(), None
    for view in views:
        group = view.get('groupId')
        if group is not None and group not in ids:
            raise AnalysisError('INVALID_PRESENTATION', 'groupId 必须引用已声明的展示分组。')
        if view['type'] == 'ORDER': continue
        if group != previous:
            if previous is not None: closed.add(previous)
            if group in closed: raise AnalysisError('INVALID_PRESENTATION', '同组展示项须在 viewPlan 中相邻，避免重排模型的叙事顺序。')
        previous = group
    return deepcopy(options)


def prepare_views(artifact):
    """Attach a private lookup only to the rendering copy, after validation."""
    artifact = deepcopy(artifact)
    from .tables import measure_key
    from .models import digest
    known = {f['factId']: f for f in artifact['facts']}
    identities = {}
    def identity(ref, role):
        if (ref, role) in identities: return identities[ref, role]
        fact = known.get(ref)
        if not fact: return (role, ref)
        key = (role, measure_key(fact, artifact['scope']) if role == 'measure' else
                digest([fact['objectId'], fact.get('sourceOrigins'), artifact['scope'].get('memberDimension')]))
        identities[ref, role] = key
        return key
    def expand(entries, root):
        if not entries: return {'object': {}, 'measure': {}}
        by_identity, paths = {}, {}
        for index, entry in enumerate(entries):
            key = identity(entry['valueRef'], entry['role'])
            if key in by_identity and by_identity[key] != entry['label']:
                raise AnalysisError('AMBIGUOUS_TABLE_LABEL', '同一来源口径的指标或对象被明确指定了不同简称，请统一这两处名称。',
                                    {'path': f'{root}/{index}/label', 'conflictingPath': paths[key],
                                     'valueRef': entry['valueRef'], 'role': entry['role'],
                                     'labels': [by_identity[key], entry['label']], 'hint': '仅修改冲突的简称，保留其余简称和图表。'})
            by_identity[key], paths[key] = entry['label'], f'{root}/{index}/label'
        refs = set(known) | {entry['valueRef'] for entry in entries}
        return {role: {ref: by_identity[identity(ref, role)] for ref in refs if identity(ref, role) in by_identity}
                for role in ('object', 'measure')}
    shared = expand(artifact.get('presentation', {}).get('labels', []), '/presentation/labels')
    for index, view in enumerate(artifact['viewPlan']):
        view['_sharedLabelLookup'] = shared
        role = 'object' if view['type'] in ('BAR_VALUES', 'BAR_RANK', 'HIERARCHY') else 'measure'
        view['_localLabelLookup'] = expand([{**entry, 'role': role} for entry in view.get('labels', [])], f'/viewPlan/{index}/labels')
    return artifact


def sections(artifact, metrics, scenes, tables):
    """References into the flat payload avoid duplicating charts or amounts."""
    groups = {g['groupId']: g for g in artifact.get('presentation', {}).get('groups', [])}
    claims = {c['claimId']: i for i, c in enumerate(artifact['claims'])}
    result = []
    for view in artifact['viewPlan']:
        if view['type'] == 'ORDER': continue
        indexes = [i for i, m in enumerate(metrics) if m['viewId'] == view['viewId']]
        charts = [i for i, s in enumerate(scenes) if s['viewId'] == view['viewId']]
        table_indexes = [i for i, t in enumerate(tables) if t['viewId'] == view['viewId']]
        group = groups.get(view.get('groupId'))
        if not indexes and not charts and not table_indexes and not (group and group.get('claimRefs')): continue
        gid = 'group:' + group['groupId'] if group else 'view:' + view['viewId']
        if not result or result[-1]['groupId'] != gid:
            result.append({'groupId': gid, 'title': group['title'] if group else '',
                           'description': view_description(artifact, group) if group else '',
                           'width': group.get('width', 'full') if group else 'full',
                           'claimsPosition': group.get('claimsPosition', 'after') if group else 'after',
                           'claimIndexes': [claims[ref] for ref in group.get('claimRefs', [])] if group else [], 'blocks': []})
        result[-1]['blocks'].append({'viewId': view['viewId'], 'title': view.get('title', ''),
            'description': view_description(artifact, view), 'metricIndexes': indexes, 'sceneIndexes': charts, 'tableIndexes': table_indexes})
    return result


def ordered_for_view(artifact, view, facts):
    """Explicit per-view SORT/RANK, otherwise global ORDER or selected reference order."""
    known = {v['derivedId']: v for v in artifact['derivedValues']}
    order = known.get(view.get('orderRef'))
    if order:
        by_ref = {f['factId']: f for f in artifact['facts']}
        members = [by_ref[ref]['objectId'] for ref in order['orderedFactIds'] + order['unavailableFactIds']]
        positions = {member: i for i, member in enumerate(members)}
        return sorted(facts, key=lambda f: positions.get(f['objectId'], len(positions)))
    if any(v['type'] == 'ORDER' for v in artifact['viewPlan']): return facts
    if view['type'] == 'BAR_RANK':
        ranking = known[view['valueRefs'][0]]
        positions = {ref: i for i, ref in enumerate(ranking['orderedFactIds'] + ranking['unavailableFactIds'])}
        return sorted(facts, key=lambda f: positions.get(f['factId'], len(positions)))
    positions = {ref: i for i, ref in enumerate(view['valueRefs'])}
    return sorted(facts, key=lambda f: positions.get(f['factId'], len(positions)))
