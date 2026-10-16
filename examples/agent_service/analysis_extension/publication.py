"""Targeted publication repair, independent of business scenarios and model prose."""
from copy import deepcopy

from .models import AnalysisError


REPAIR_FIELDS = {'document', 'claims', 'viewPlan', 'presentation', 'workRefs', 'partialReason',
                 'technicalDetails', 'findingPlan', 'actionPlan', 'thresholds', 'hypotheses', 'renderCheckUnavailableReason'}
REPAIR_REASONS = {
    'invalid edit': '每项须提供 op、绝对 path；add/replace 须有 value，remove 不传 value。',
    'invalid pointer escape': '路径仅支持 JSON Pointer 的 ~0 和 ~1 转义。',
    'read-only field': '只能修改发布内容，不能修改事实、分析身份或基础版本。',
    'invalid array index': '数组位置须为无前导零的非负整数；仅 add 的末段可用 - 追加。',
    'append requires add': '数组末段 - 仅用于 add 追加；replace/remove 须指定已有数字下标。',
    'index out of range': '数组下标越界；add 最多使用当前长度，其余操作须指向已有项。',
    'not a container': '路径的中间节点必须是已有对象或数组。',
    'missing field': '路径指向的字段不存在；添加新字段使用 add，且父路径必须已经存在。',
    'use replace for an existing field': '字段已经存在，请使用 replace。',
}


def repair_fields(request):
    """Describe the saved shape, including explicit nulls; never guess edits."""
    return {key: {'path': '/' + key, 'op': 'replace' if key in request else 'add',
                  'exists': key in request}
            for key in sorted(REPAIR_FIELDS)}


def repair_request(saved, analysis_ref, repairs):
    if saved['analysisRef'] != analysis_ref:
        raise AnalysisError('REPAIR_SCOPE', '修复草案只能用于原分析版本；重新构建后请重新提交完整发布参数。')
    if not isinstance(repairs, list) or not 0 <= len(repairs) <= 100:
        raise AnalysisError('INVALID_REPAIR', 'repairs 最多 100 项；空列表表示原样提交已校验的预览。')
    request = deepcopy(saved)
    for index, edit in enumerate(repairs):
        try:
            if (not isinstance(edit, dict) or set(edit) - {'op', 'path', 'value'}
                    or edit.get('op') not in ('replace', 'remove', 'add')
                    or not isinstance(edit.get('path'), str) or not edit['path'].startswith('/')
                    or len(edit['path']) > 1024
                    or (edit['op'] == 'remove' and 'value' in edit)
                    or (edit['op'] != 'remove' and 'value' not in edit)):
                raise ValueError('invalid edit')
            parts = edit['path'][1:].split('/')
            if any('~' in part.replace('~0', '').replace('~1', '') for part in parts):
                raise ValueError('invalid pointer escape')
            parts = [p.replace('~1', '/').replace('~0', '~') for p in parts]
            if parts[0] not in REPAIR_FIELDS:
                raise ValueError('read-only field')
            parent = request
            for part in parts[:-1]:
                if isinstance(parent, list):
                    if not part.isdecimal() or str(int(part)) != part: raise ValueError('invalid array index')
                    if int(part) >= len(parent): raise ValueError('index out of range')
                    parent = parent[int(part)]
                elif isinstance(parent, dict):
                    if part not in parent: raise ValueError('missing field')
                    parent = parent[part]
                else: raise ValueError('not a container')
            key = parts[-1]
            if isinstance(parent, list):
                if key == '-':
                    if edit['op'] != 'add': raise ValueError('append requires add')
                    parent.append(deepcopy(edit['value'])); continue
                if not key.isdecimal() or str(int(key)) != key: raise ValueError('invalid array index')
                key = int(key)
                if edit['op'] == 'add':
                    if key > len(parent): raise ValueError('index out of range')
                    parent.insert(key, deepcopy(edit['value'])); continue
                if key >= len(parent): raise ValueError('index out of range')
            elif not isinstance(parent, dict):
                raise ValueError('not a container')
            elif edit['op'] != 'add' and key not in parent:
                raise ValueError('missing field')
            elif edit['op'] == 'add' and key in parent:
                raise ValueError('use replace for an existing field')
            if edit['op'] == 'remove': del parent[key]
            else: parent[key] = deepcopy(edit['value'])
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            reason = str(exc)
            hint = REPAIR_REASONS.get(reason, '路径须指向已有父对象或数组，并使用有效字段或下标。')
            corrected = ([{**edit, 'op': 'replace'}] if reason == 'use replace for an existing field' else None)
            raise AnalysisError('INVALID_REPAIR', '修复路径或操作无效：' + hint,
                                {'path': f'/repairs/{index}', 'targetPath': edit.get('path') if isinstance(edit, dict) else None,
                                 'reason': reason if reason in REPAIR_REASONS else 'invalid path', 'hint': hint,
                                 **({'suggestedEdit': corrected[0]} if corrected else {})}) from exc
    if not {'analysisRef', 'expectedBaseRevision'} <= request.keys() or (not request.get('document') and not {'claims','viewPlan'} <= request.keys()):
        raise AnalysisError('INVALID_REPAIR', '不能移除发布身份或全部展示内容。')
    return request


def located(exc, root, index=None, identity=None):
    details = dict(exc.details or {})
    field = details.pop('field', None)
    relative_path = details.pop('relativePath', None)
    path = root + (f'/{index}' if index is not None else '')
    if relative_path: path += '/' + relative_path
    elif field: path += '/' + field
    return {'code': exc.code, 'message': str(exc), **details,
            'path': details.get('path', path), **(identity or {})}


def raise_issues(issues):
    if not issues: return
    first = issues[0]
    raise AnalysisError(first['code'], first['message'], {**first, 'issues': issues})


def validate_content(artifact, claims, views, presentation):
    """Collect independent item failures before asking the model to repair."""
    from .validation import validate_claims, validate_views
    from .presentation_options import validate_presentation
    from .tools import CLAIM, VIEW, PRESENTATION
    from jsonschema import Draft202012Validator
    issues, result = [], {}
    for name, entries, schema, limit, validator, identity in (
        ('claims', claims, CLAIM, 50, validate_claims, 'claimId'),
        ('viewPlan', views, VIEW, 20, validate_views, 'viewId'),
    ):
        if not isinstance(entries, list) or len(entries) > limit:
            issues.append({'code': 'INVALID_PUBLICATION', 'message': f'{name} 必须为最多 {limit} 项的列表。', 'path': '/'+name})
            continue
        seen, valid = set(), True
        for index, item in enumerate(entries):
            schema_errors = list(Draft202012Validator(schema).iter_errors(item))
            if schema_errors:
                valid = False
                for error in schema_errors[:10]:
                    issues.append({'code': 'INVALID_PUBLICATION', 'message': error.message[:2000],
                                   'path': '/'+name+'/'+str(index)+''.join('/'+str(k) for k in error.path),
                                   identity: item.get(identity) if isinstance(item, dict) else None})
                continue
            if item[identity] in seen:
                valid = False
                issues.append({'code': 'DUPLICATE_ID', 'message': '同一发布计划中的标识不能重复。', 'path': f'/{name}/{index}/{identity}', identity: item[identity]})
                continue
            seen.add(item[identity])
            probe = deepcopy(item)
            while True:
                try:
                    validator(artifact, [probe])
                    break
                except AnalysisError as exc:
                    valid = False
                    issues.append(located(exc, '/'+name, index, {identity: item[identity]}))
                    # Continue independent optional-field checks on a throwaway copy.
                    # Nothing is removed from the saved proposal or accepted output.
                    field = (exc.details or {}).get('field')
                    if name == 'viewPlan' and field in probe and field not in VIEW['required']:
                        del probe[field]
                    else: break
        if valid:
            try: result[name] = validator(artifact, entries)
            except AnalysisError as exc: issues.append(located(exc, '/'+name))
    schema_errors = list(Draft202012Validator(PRESENTATION).iter_errors(presentation or {}))
    if schema_errors:
        issues.extend({'code': 'INVALID_PRESENTATION', 'message': e.message[:2000],
                       'path': '/presentation'+''.join('/'+str(k) for k in e.path)} for e in schema_errors[:10])
    elif 'viewPlan' in result and 'claims' in result:
        try: result['presentation'] = validate_presentation(artifact, presentation, views, result['claims'])
        except AnalysisError as exc: issues.append(located(exc, '/presentation'))
    raise_issues(issues)
    return result


def schema_issues(request):
    from jsonschema import Draft202012Validator
    from .tools import AnalysisPublish
    if isinstance(request, dict) and 'arguments' in request:
        return [{'code': 'INVALID_PUBLICATION', 'path': '/arguments', 'validator': 'additionalProperties',
                 'message': '工具参数直接放在顶层 JSON 对象中，不再包一层 arguments；不能传字符串化的整份参数。',
                 'requiredFields': ['analysisRef', 'expectedBaseRevision', 'requestKey'],
                 'hint': '首次用 document（自由页面）或 claims+viewPlan（显式组件）；局部修复用 repairRef+repairs。检查阶段均带 previewOnly=true。'}]
    issues = []
    if isinstance(request, dict) and 'repairRef' in request:
        mixed = sorted(set(request) & REPAIR_FIELDS)
        if mixed:
            return [{'code': 'INVALID_PUBLICATION', 'path': '/' + key, 'validator': 'oneOf',
                     'message': 'repairRef 已保存完整内容；删除此顶层字段，需更新内容时通过 repairs 修改。',
                     'unexpectedFields': mixed, 'requiresBuild': False} for key in mixed]
    for error in Draft202012Validator(AnalysisPublish.input_schema).iter_errors(request):
        path = ''.join('/' + str(k).replace('~', '~0').replace('/', '~1') for k in error.absolute_path)
        message = error.message[:500]
        extra = {}
        if error.validator == 'oneOf':
            message = ('首次提交 document（自由页面）或 claims+viewPlan（显式组件），修复提交 repairRef+repairs；不能混传。'
                       if not path else '该项须符合一种完整参数形式；核对该字段的必填项和可用字段。')
        elif error.validator == 'additionalProperties' and isinstance(error.instance, dict):
            extra['unknownFields'] = sorted(set(error.instance) - set(error.schema.get('properties', {})))
            extra['availableFields'] = sorted(error.schema.get('properties', {}))
            message = '包含不支持的字段；按 availableFields 选择。'
        elif error.validator == 'required':
            extra['missingFields'] = [key for key in error.validator_value if key not in error.instance]
        issues.append({'code': 'INVALID_PUBLICATION', 'message': message, 'path': path,
                       'validator': error.validator, **extra})
        if len(issues) == 30: break
    return issues
