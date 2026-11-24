"""Targeted publication repair, independent of business scenarios and model prose."""
from copy import deepcopy

from .models import AnalysisError


REPAIR_FIELDS = {'document', 'claims', 'viewPlan', 'presentation', 'workRefs', 'unresolvedItems',
                 'technicalDetails', 'findingPlan', 'actionPlan', 'thresholds', 'hypotheses', 'renderCheckUnavailableReason'}
REPAIR_REASONS = {
    'invalid edit': '每项须提供 op、绝对 path；add/replace 须有 value，remove 不传 value。',
    'invalid pointer escape': '路径仅支持 JSON Pointer 的 ~0 和 ~1 转义。',
    'read-only field': '只能修改发布内容，不能修改事实或分析身份；版本由服务管理。',
    'invalid array index': '数组位置须为无前导零的非负整数；仅 add 的末段可用 - 追加。',
    'append requires add': '数组末段 - 仅用于 add 追加；replace/remove 须指定已有数字下标。',
    'index out of range': '数组下标越界；add 最多使用当前长度，其余操作须指向已有项。',
    'not a container': '路径的中间节点必须是已有对象或数组。',
    'missing field': '路径指向的字段不存在；添加新字段使用 add，且父路径必须已经存在。',
    'use replace for an existing field': '字段已经存在，请使用 replace。',
}


def recovery_details(code, details):
    """按失败阶段给出恢复动作；缺证据、版本冲突不指示模型改报告文字。"""
    details = dict(details)
    requires_build = details.get('requiresBuild') is True or any(
        item.get('requiresBuild') is True for item in details.get('issues', []))
    if code in ('FORMULA_DATA_REQUIRED', 'GROUPING_RESULT_REQUIRED', 'SELECTED_DATA_INCOMPLETE', 'EVIDENCE_INVALID'):
        requires_build = True
    details['requiresBuild'] = requires_build
    if code == 'VERSION_CONFLICT':
        if details.get('nextAction') == 'RETRY_PREVIEW':
            details['repairHint'] = '沿用已保存的页面重新预览，宿主接续同一分析的数据摘要版本；无需重新取数、计算或改写页面。'
        else:
            details['nextAction'] = 'REBUILD_FROM_LATEST_PUBLISHED'
            details['repairHint'] = '已有新的发布结果。基于 latestPublishedAnalysisRef 续建并保留仍适用的页面内容，再预览发布；不要修改旧版本号强行确认，也不要反复改写文字。'
    elif requires_build:
        details['nextAction'] = 'BUILD_WITH_REQUIRED_INPUTS'
        details['repairHint'] = '先按缺失对象和引用核对分析范围及已有捕获，补入必要证据或计算后重新 Build，再复用页面内容预览；仅修改报告文字不能解决此错误。'
    else:
        if code == 'INVALID_WORK_REF' and details.get('path') == '/workPlan':
            # Publishing can override an existing task's references, but cannot edit its frozen plan.
            details['path'] = '/workRefs'
            if 'issues' in details:
                details['issues'] = [{**item, 'path': '/workRefs'} if item.get('code') == code and item.get('path') == '/workPlan'
                                     else item for item in details['issues']]
        details.setdefault('nextAction', 'REPAIR_SAVED_PROPOSAL')
        details.setdefault('repairHint', '按 issues/path 局部修复：再次调用 AnalysisPublish，使用 repairRef 和 repairs；保留未修改的图表、数据与说明，无需重新取数。')
    return details


def repair_fields(request):
    """Describe the saved shape, including explicit nulls; never guess edits."""
    if 'markdown' in (request.get('document') or {}):
        return {'正文': {'path': '/document/markdown', 'op': 'replaceText'},
                '追加段落或图表': {'path': '/document/markdown', 'op': 'appendMarkdown'},
                '完整草稿': {'path': '/document/path', 'op': 'replace'}}
    return {key: {'path': '/' + key, 'op': 'replace' if key in request else 'add',
                  'exists': key in request}
            for key in sorted(REPAIR_FIELDS)}


def repair_request(saved, analysis_ref, repairs):
    if saved['analysisRef'] != analysis_ref:
        raise AnalysisError('REPAIR_SCOPE', '修复草案只能用于原分析版本；重新构建后请重新提交完整发布参数。')
    if not isinstance(repairs, list) or not 0 <= len(repairs) <= 100:
        raise AnalysisError('INVALID_REPAIR', 'repairs 最多 100 项；空列表表示原样提交已校验的预览。')
    request = deepcopy(saved)
    text_edits = [(index, edit) for index, edit in enumerate(repairs)
                  if isinstance(edit, dict) and edit.get('op') == 'replaceText']
    if text_edits:
        # 文字补丁与整页替换不能混用，避免顺序改变匹配基准。其他字段仍按原契约处理。
        if any(isinstance(edit, dict) and edit.get('op') not in ('replaceText', 'appendMarkdown')
               and edit.get('path') in ('/document', '/document/html', '/document/markdown', '/document/path') for edit in repairs):
            raise AnalysisError('INVALID_REPAIR', '同批不能同时替换整页和修正文本文字。')
        from .document_repair import apply_text_repairs
        document = request.get('document') or {}
        field = 'markdown' if 'markdown' in document else 'html'
        source = document.get(field)
        appended = []
    for index, edit in enumerate(repairs):
        if isinstance(edit, dict) and edit.get('op') == 'replaceText':
            continue
        if isinstance(edit, dict) and edit.get('op') == 'appendMarkdown':
            document = request.get('document') or {}
            if (set(edit) != {'op', 'path', 'value'} or edit['path'] != '/document/markdown'
                    or 'markdown' not in document or not isinstance(edit['value'], str) or not edit['value'].strip()):
                raise AnalysisError('INVALID_REPAIR', 'appendMarkdown 需要已保存的 Markdown 正文、/document/markdown 路径和非空的段落或图表引用。')
            if text_edits:
                appended.append(edit['value'].strip())
            else:
                document['markdown'] = document['markdown'].rstrip() + '\n\n' + edit['value'].strip()
            continue
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
            if parts == ['document'] and edit['op'] == 'replace' and isinstance(edit.get('value'), dict):
                # 页面内容更新沿用未修改的数据绑定。调用者显式传空数组仍可删除绑定。
                replacement = deepcopy(edit['value'])
                previous = request.get('document') or {}
                for field in ('bindings', 'datasets'):
                    if field not in replacement and field in previous:
                        replacement[field] = deepcopy(previous[field])
                edit = {**edit, 'value': replacement}
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
    if text_edits:
        bindings = request['document'].get('bindings', [])
        if not isinstance(bindings, list) or any(not isinstance(b, dict) or not isinstance(b.get('key'), str) for b in bindings):
            raise AnalysisError('INVALID_REPAIR', '修订后的数据绑定需要可引用的 key。')
        repaired = apply_text_repairs(source, text_edits, {b['key'] for b in bindings}, field=field)
        request['document'][field] = '\n\n'.join([repaired, *appended]) if appended else repaired
    if 'analysisRef' not in request or (not request.get('document') and not {'claims','viewPlan'} <= request.keys()):
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


def _publication_shape_errors(error):
    """Explain an identifiable form's missing fields, without choosing a form for the caller."""
    if error.validator != 'oneOf' or not isinstance(error.instance, dict):
        return [error]
    candidates = []
    for index, branch in enumerate(error.validator_value):
        errors = [child for child in error.context if child.schema_path[0] == index]
        # A prohibited field means this is not the requested form. In particular,
        # do not report the required fields of every rejected oneOf alternative.
        if errors and all(child.validator == 'required' for child in errors):
            matched = len(set(branch.get('required', [])) & set(error.instance))
            if matched:
                candidates.append((matched, errors))
    if candidates:
        best = max(score for score, _ in candidates)
        selected = [errors for score, errors in candidates if score == best]
        if len(selected) == 1:
            return selected[0]
    return [error]


def schema_issues(request):
    from jsonschema import Draft202012Validator
    from .tools import AnalysisPublish
    if isinstance(request, dict) and 'arguments' in request:
        return [{'code': 'INVALID_PUBLICATION', 'path': '/arguments', 'validator': 'additionalProperties',
                 'message': '工具参数直接放在顶层 JSON 对象中，不再包一层 arguments；不能传字符串化的整份参数。',
                 'requiredFields': ['requestKey'],
                 'hint': '首次提供 analysisRef 和 document 或 claims+viewPlan；局部修复用 repairRef+repairs；审核已有报告用 analysisRef、requestKey 和 reviewAction。仅预览才带 previewOnly=true。'}]
    issues = []
    if isinstance(request, dict) and 'repairRef' in request:
        mixed = sorted(set(request) & REPAIR_FIELDS)
        if mixed:
            return [{'code': 'INVALID_PUBLICATION', 'path': '/' + key, 'validator': 'oneOf',
                     'message': 'repairRef 已保存完整内容；删除此顶层字段，需更新内容时通过 repairs 修改。',
                     'unexpectedFields': mixed, 'requiresBuild': False} for key in mixed]
    errors = [detail for error in Draft202012Validator(AnalysisPublish.input_schema).iter_errors(request)
              for detail in _publication_shape_errors(error)]
    seen = set()
    for error in errors:
        path = ''.join('/' + str(k).replace('~', '~0').replace('/', '~1') for k in error.absolute_path)
        message = error.message[:500]
        extra = {}
        if path == '/document' and error.validator == 'anyOf':
            message = 'document.bindings 或 document.datasets 至少一项须为非空数组。'
            misplaced = [key for key in ('bindings', 'datasets') if key in request]
            if misplaced:
                message += '将已有的 ' + '、'.join(misplaced) + ' 从请求顶层移入 document 对象，保留数组内容、analysisRef 和报告文件。'
            extra.update(requiredAlternatives=['/document/bindings','/document/datasets'], requiresBuild=False)
        elif error.validator == 'oneOf':
            forms = [branch.get('required', []) for branch in error.validator_value]
            extra['parameterForms'] = forms
            message = '参数形式不完整或存在互斥字段；请选择一种形式并补齐必填项：' + '；'.join(
                ' + '.join(fields) for fields in forms) + '。'
            if path:
                message = path + '：' + message
        elif error.validator == 'additionalProperties' and isinstance(error.instance, dict):
            extra['unknownFields'] = sorted(set(error.instance) - set(error.schema.get('properties', {})))
            extra['availableFields'] = sorted(error.schema.get('properties', {}))
            message = '包含不支持的字段；按 availableFields 选择。'
            misplaced = [key for key in ('bindings', 'datasets') if key in extra['unknownFields']]
            if not path and misplaced and isinstance(request.get('document'), dict):
                message = '将请求顶层的 ' + '、'.join(misplaced) + ' 并入 document 内的同名数组，保留已有条目，移除顶层对应字段。'
                extra['requiresBuild'] = False
        elif error.validator == 'required':
            missing = [key for key in error.validator_value if key not in error.instance]
            for field in missing:
                field_path = path + '/' + field.replace('~', '~0').replace('/', '~1')
                identity = (field_path, 'required')
                if identity in seen:
                    continue
                seen.add(identity)
                issue = {'code': 'INVALID_PUBLICATION', 'validator': 'required', 'path': field_path,
                         'missingFields': [field],
                         'message': (path + ' ' if path else '') + '缺少必填字段：' + field + '。'}
                if not error.absolute_path and field == 'analysisRef':
                    issue['hint'] = '补上本次分析已返回的 analysisRef，保留现有报告内容后重新调用 AnalysisPublish；无需重新取数或计算，不要自行编造引用。'
                elif not error.absolute_path and field == 'repairRef':
                    issue['hint'] = '使用之前预览或失败响应返回的 repairRef，与 repairs 一起提交。'
                issues.append(issue)
                if len(issues) >= 30:
                    return issues
            continue
        elif error.validator == 'not' and not path and request.get('finalize') is True and request.get('previewOnly') is True:
            path = '/finalize'
            extra['conflictingFields'] = ['finalize', 'previewOnly']
            message = 'finalize=true（检查通过后发布）与 previewOnly=true（仅预览）不能同时使用，请保留一个。'
        identity = (path, error.validator, message)
        if identity in seen:
            continue
        seen.add(identity)
        issues.append({'code': 'INVALID_PUBLICATION', 'message': message, 'path': path,
                       'validator': error.validator, **extra})
        if len(issues) >= 30: break
    return issues
