"""Resolve a dimension/member pair within the actual input scope before calculation."""
from .budget_dimensions import SYSTEM_DIMENSION_LABELS
from .models import AnalysisError


def resolve(dimensions, members, labels, filters, *, strict=True):
    """Codes take precedence. A display name must identify exactly one code."""
    predicates = []
    for column, values in filters:
        keys = {column} if column in dimensions else {key for key in dimensions if column in labels.get(key, ())}
        if len(keys) != 1:
            raise AnalysisError('INVALID_TABLE_FILTER', '筛选维度未唯一定位，请使用当前来源中的维度标识。',
                                {'dimension': column, 'choices': sorted(keys or dimensions), 'requiresSourceQuery': False})
        key = next(iter(keys))
        domain = members.get(key, {})
        accepted = set()
        for value in values:
            matches = {value} if value in domain else {code for code, names in domain.items() if value in names}
            if len(matches) > 1:
                raise AnalysisError('AMBIGUOUS_TABLE_MEMBER', '该维度下存在同名成员，请选用成员编码。',
                                    {'dimension': key, 'member': value, 'codes': sorted(matches)})
            if not matches and strict:
                raise AnalysisError('UNKNOWN_FILTER_MEMBER', '当前输入中找不到这个维值。可按列举的名称或编码筛选同一份数据。',
                                    {'dimension': key, 'member': value, 'choices': [
                                        {'code': code, 'names': sorted(names)} for code, names in list(domain.items())[:12]],
                                     'requiresSourceQuery': False})
            accepted.update(matches)
        predicates.append((key, accepted))
    return predicates


def select_facts(rows, conditions, scopes):
    from .business_tables import member_label
    if not isinstance(conditions, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                for k, v in conditions.items()):
        raise AnalysisError('INVALID_SELECTOR', '维度筛选以维度名称或编码对应一个成员名称或编码。')
    if not conditions or not rows:
        return rows
    labels = {key: {title} for key, title in SYSTEM_DIMENSION_LABELS.items()}
    domains = {}
    sources = {(r.get('captureRef'), r.get('stepId'), r.get('batchId')) for r in rows}
    for scope in scopes:
        if any(all(not scope.get(k) or v == scope[k] for k, v in zip(('captureRef', 'stepId', 'batchId'), source))
               for source in sources):
            for key, name in (scope.get('dimensionLabels') or {}).items():
                labels.setdefault(key, set()).add(name)
    for row in rows:
        for key, code in (row.get('dimensions') or {}).items():
            names = domains.setdefault(key, {}).setdefault(code, set())
            name = member_label(row, key)
            if isinstance(name, str) and name:
                names.add(name)
    terms = resolve(set(domains), domains, labels, [(key, [v]) for key, v in conditions.items()])
    return [r for r in rows if all((r.get('dimensions') or {}).get(k) in values for k, values in terms)]
