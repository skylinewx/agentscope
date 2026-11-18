"""Lossless, shallow review tables. Evidence and execution snapshots stay immutable."""
from copy import deepcopy
import json

from .business_tables import encode, escape
from .models import digest
from .business_records import record_identity


def business_record_tables(facts, *, input_context=False):
    """Separate record coordinates from its metric observations, without merging values."""
    groups = {}
    for position, fact in enumerate(facts):
        if input_context:
            record = fact.get('record', fact.get('overlay', fact))
            identity = {key: record[key] for key in ('dimensions', 'sourceContextId', 'bindingRef', 'batchId', 'stepId') if key in record}
        else:
            identity = record_identity(fact)
            if identity is None and fact.get('recordTable') and fact.get('dimensions') and not fact.get('aggregation'):
                identity = {'table': fact['recordTable'], 'dimensions': fact['dimensions']}
        key = encode([fact.get('sourceContextId'), identity]) if identity else str(position)
        groups.setdefault(key, []).append((position, fact))
    records, observations = [], [None] * len(facts)
    for index, entries in enumerate(groups.values(), 1):
        flat = [_flatten(fact) for _, fact in entries]
        common = {key: value for key, value in flat[0].items()
                  if key not in (('factId',), ('value',), ('valueState',))
                  and all(key in row and encode(row[key]) == encode(value) for row in flat)}
        row = {'recordRow': index}
        for path, value in common.items():
            target = row
            for key in path[:-1]: target = target.setdefault(key, {})
            target[path[-1]] = value
        records.append(row)
        for (position, _), source in zip(entries, flat):
            row = {'recordRow': index}
            for path, value in source.items():
                if path in common: continue
                target = row
                for key in path[:-1]: target = target.setdefault(key, {})
                target[path[-1]] = value
            observations[position] = row
    definitions, definition_keys = [], {}
    # These are exact descriptions, never inferred by splitting a title.
    # Frozen input overlays can repeat the same metric description across many
    # periods just as source facts do. Sharing it must not merge observations.
    column_fields = ('headers', 'measureCode', 'sourceMeasureCode', 'unit', 'scale', 'unitState', 'columnName')
    for row in observations:
        if input_context:
            definition = {prefix: {key: row[prefix][key] for key in (*column_fields, 'label', 'scaleBasis')
                                   if key in row.get(prefix, {})}
                          for prefix in ('overlay', 'record') if isinstance(row.get(prefix), dict)}
            definition = {key: value for key, value in definition.items() if value}
        else:
            definition = {key: row[key] for key in column_fields if key in row}
        if not definition: continue
        key = encode(definition)
        if key not in definition_keys:
            definition_keys[key] = len(definitions) + 1
            definitions.append({'columnRow': definition_keys[key], **definition})
        row['columnRow'] = definition_keys[key]
        if input_context:
            for prefix, fields in definition.items():
                for key in fields: row[prefix].pop(key)
                if not row[prefix]: row.pop(prefix)
        else:
            for key in definition: row.pop(key)
    return {'format': 'INPUT_CONTEXT_OBSERVATIONS_V1' if input_context else 'BUDGET_RECORD_OBSERVATIONS_V1', 'recordCount': len(records),
            'recordCoordinates': record_table(records), 'observations': record_table(observations),
            **({'columnDefinitions': record_table(definitions)} if definitions else {}),
            'meaning': ('recordRow 只关联逐字段相同的输入背景，不合并输入或判断业务记录相同；' if input_context else 'recordRow 关联同表、完整维度的记录字段；') +
                       '每个 observations 行是一个原始值，按原序保留。将记录字段、columnRow 对应的真实列定义与观测字段按路径合并得到完整项。不同值、指标、快照和用途均未合并。'}


def _flatten(record, path=()):
    result = {}
    for key, value in record.items():
        target = path + (key,)
        if isinstance(value, dict) and value:
            result.update(_flatten(value, target))
        else:
            result[target] = value
    return result


def record_table(records):
    """JSON cells retain string/number/null; missing is distinct, order is exact."""
    flat = [_flatten(record) for record in records]
    fields = list(dict.fromkeys(key for row in flat for key in row))
    common = {key: flat[0][key] for key in fields if key in flat[0]
              and all(key in row and encode(row[key]) == encode(flat[0][key]) for row in flat)}
    changing = [key for key in fields if key not in common]
    def name(path):
        return '/' + '/'.join(key.replace('~', '~0').replace('/', '~1') for key in path)
    independent, aliases = [], {}
    for key in changing:
        same = next((prior for prior in independent if all((key in row) == (prior in row)
                    and (key not in row or encode(row[key]) == encode(row[prior])) for row in flat)), None)
        if same is not None: aliases[name(key)] = name(same)
        else: independent.append(key)
    changing = independent
    headings = [name(key) for key in changing]
    string_columns = [all(isinstance(row[key], str) for row in flat if key in row) for key in changing]
    lines = ['| ' + ' | '.join(escape(h) for h in headings) + ' |',
             '| ' + ' | '.join('---' for _ in headings) + ' |']
    for row in flat:
        cells = []
        for key, is_string in zip(changing, string_columns):
            if key not in row: cells.append('MISSING'); continue
            text = escape(row[key] if is_string else encode(row[key]))
            if is_string:
                import re
                text = re.sub(r'^\s+|\s+$', lambda match: ''.join('&#' + str(ord(c)) + ';' for c in match[0]), text)
            cells.append('&#77;ISSING' if is_string and text == 'MISSING' else text)
        lines.append('| ' + ' | '.join(cells) + ' |')
    common_record = {}
    for path, value in common.items():
        target = common_record
        for key in path[:-1]: target = target.setdefault(key, {})
        target[path[-1]] = deepcopy(value)
    return {'format': 'JSON_CELL_TABLE_V1', 'rowCount': len(records),
            'commonFields': {name((key,)): value for key, value in common_record.items()},
            **({'equivalentColumns': aliases} if aliases else {}),
            'columns': headings, 'stringColumns': [headings[i] for i, yes in enumerate(string_columns) if yes],
            'markdown': '\n'.join(lines)}


def restore_table(table):
    """Reference decoder used by invariant tests and audit tooling, never by AI."""
    import html
    import re
    result = []
    for line in table['markdown'].splitlines()[2:]:
        # Split on unescaped pipes; escape() doubles original backslashes.
        cells, buffer, escaped = [], '', False
        for char in line[2:-2]:
            if char == '|' and not escaped:
                cells.append(buffer.strip()); buffer = ''; continue
            buffer += char
            escaped = not escaped if char == '\\' else False
        cells.append(buffer.strip())
        values = deepcopy(table['commonFields'])
        for path, cell in zip(table['columns'], cells):
            if cell != 'MISSING':
                cell = re.sub(r'\\([\\|])', r'\1', cell)
                text = html.unescape(cell.replace('<br>', '\n'))
                values[path] = text if path in table.get('stringColumns', []) else json.loads(text)
        for path, source in table.get('equivalentColumns', {}).items():
            if source in values: values[path] = deepcopy(values[source])
        row = {}
        for path, value in values.items():
            keys = [key.replace('~1', '/').replace('~0', '~') for key in path[1:].split('/')]
            current = row
            for key in keys[:-1]:
                current = current.setdefault(key, {})
            current[keys[-1]] = value
        result.append(row)
    return result


def field_group_tables(records):
    groups = {}
    for index, record in enumerate(records):
        signature = tuple(sorted(_flatten(record)))
        group = groups.setdefault(signature, {'positions': [], 'records': []})
        group['positions'].append(index); group['records'].append(record)
    return {'format': 'FIELD_GROUP_TABLES_V1', 'rowCount': len(records),
            'groups': [{'positions': group['positions'], 'records': record_table(group['records'])} for group in groups.values()],
            'meaning': '不同结构的输入分别展示字段；positions 为原始零基顺序，按原位恢复，不删除任何项。'}


def fact_selection(selection, facts):
    """An exact, checked selection by existing business fields, never by amount."""
    if any(not isinstance(value, str) for value in selection): return None
    members = list(dict.fromkeys(fact.get('objectId') for fact in facts.values() if fact.get('objectId') is not None))
    if members and selection in (members, sorted(members)):
        return {'source': 'facts', 'field': 'objectId', 'distinct': True,
                'order': 'PRESENTATION' if selection == members else 'ASCENDING', 'all': True}
    if not selection or len(set(selection)) != len(selection) or any(ref not in facts for ref in selection): return None
    selected = set(selection)
    presentation = [ref for ref in facts if ref in selected]
    order = 'PRESENTATION' if selection == presentation else 'FACT_ID_ASCENDING' if selection == sorted(selected) else None
    if not order: return None
    flats = {ref: _flatten(fact) for ref, fact in facts.items()}
    allowed = ('sourceContextId', 'bindingRef', 'batchId', 'period', 'executionPeriod', 'dimensions', 'effectiveDimensions',
               'inputRole', 'stepId', 'captureRefs', 'recordTable', 'inputMemberships', 'sourceMeasureCode',
               'selectionDataPeriod', 'selectionExecutionPeriod')
    conditions = {key: value for key, value in flats[selection[0]].items() if key[0] in allowed
                  and all(key in flats[ref] and encode(flats[ref][key]) == encode(value) for ref in selected)}
    remaining, where = set(facts), []
    while remaining != selected and conditions:
        path, value = min(conditions.items(), key=lambda item: sum(
            item[0] in flats[ref] and encode(flats[ref][item[0]]) == encode(item[1]) for ref in remaining))
        conditions.pop(path)
        matching = {ref for ref in remaining if path in flats[ref] and encode(flats[ref][path]) == encode(value)}
        if matching == remaining: break
        remaining = matching
        where.append({'path': list(path), 'equals': value})
    if remaining != selected: return None
    result = {'source': 'facts', 'where': where, 'order': order}
    return result if len(encode(result)) < len(encode(selection)) else None


def readable_analysis_material(material, *, all_identities=False):
    """Factor exact input copies and reference sets, then render ordinary tables.

    There is no global string dictionary or nested index encoding. Unknown
    fields survive. An execution input differing by even one value stays distinct.
    """
    from .review_aliases import compact_references
    result, aliases = compact_references(material, all_identities=all_identities)
    column_definitions, column_keys = {}, {}
    for scope in result.get('sourceScopes') or []:
        if not isinstance(scope.get('columns'), list): continue
        refs = []
        for column in scope.pop('columns'):
            identity = encode(column)
            if identity not in column_keys:
                ref = 'column' + str(len(column_keys) + 1)
                column_keys[identity] = ref; column_definitions[ref] = column
            refs.append(column_keys[identity])
        scope['columnDefinitionRefs'] = refs
    if column_definitions:
        result['sourceColumnDefinitions'] = [{'columnRef': ref, **value} for ref, value in column_definitions.items()]
    input_sets, identities = {}, {}
    facts = {fact['factId']: fact for fact in result.get('facts') or [] if 'factId' in fact}
    for run in result.get('pythonRuns') or []:
        if not run.get('inputRecords'):
            continue
        records = run.pop('inputRecords')
        fingerprint = digest(records)
        if fingerprint not in identities:
            reference = 'executionInputs' + str(len(input_sets) + 1)
            identities[fingerprint] = reference
            # Each input has one explicit fact link and a shallow field overlay.
            # Only exactly matching fields are shared; missing fields are listed.
            rows = []
            for record in records:
                fact = facts.get(record.get('ref'))
                if fact:
                    overlay = {key: value for key, value in record.items()
                               if key != 'ref' and (key not in fact or encode(value) != encode(fact[key]))}
                    rows.append({'factRef': fact['factId'], 'fields': list(record), 'overlay': overlay})
                else:
                    rows.append({'record': record})
            input_sets[reference] = rows
        # inputSetRef is the real immutable execution input identity. The
        # readable packet's local lookup must not overwrite that provenance.
        run['executionInputsRef'] = identities[fingerprint]
    if input_sets:
        result['executionInputSets'] = input_sets
    # The same complete ordered dependency list is stated once in business terms.
    selections, keys = {}, {}
    def references(value, key=None):
        if isinstance(value, dict):
            return {k: references(v, k) for k, v in value.items()}
        if isinstance(value, list):
            if key in ('inputRefs', 'sourceFactIds', 'factRefs', 'selectedFactIds', 'members', 'observedMembers', 'referenceMembers', 'observedReferenceMembers') and len(value) > 8:
                fingerprint = encode(value)
                if fingerprint not in keys:
                    ref = 'selection' + str(len(keys) + 1)
                    keys[fingerprint] = ref; selections[ref] = value
                return {'orderedSelection': keys[fingerprint], 'count': len(value)}
            return [references(child) for child in value]
        return value
    result = references(result)
    fact_order = list(facts)
    input_orders = {ref: [row.get('factRef', (row.get('record') or {}).get('ref')) for row in rows]
                    for ref, rows in input_sets.items()}
    for reference, selection in list(selections.items()):
        if selection == fact_order:
            selections[reference] = {'source': 'facts', 'order': 'PRESENTATION', 'all': True}
        elif selection == sorted(facts):
            selections[reference] = {'source': 'facts', 'order': 'FACT_ID_ASCENDING', 'all': True}
        else:
            exact = fact_selection(selection, facts)
            if exact:
                selections[reference] = exact
                continue
            for ref, order in input_orders.items():
                if selection == order:
                    selections[reference] = {'source': 'executionInputSets', 'inputSetRef': ref, 'order': 'ACTUAL_INPUT_ORDER', 'all': True}
                    break
    if selections:
        result['referenceSelections'] = selections
    # Share complete, byte-equivalent business contexts, never words or values.
    # Keep this one hop: the directory itself is not encoded into other refs.
    context_fields = {'context', 'dataContext', 'formContext', 'baseContext', 'dimensionModels'}
    occurrences = {}
    def count_contexts(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in context_fields and isinstance(child, dict) and len(encode(child)) > 160:
                    identity = encode(child)
                    occurrences.setdefault(identity, {'value': child, 'count': 0, 'name': key})['count'] += 1
                else:
                    count_contexts(child)
        elif isinstance(value, list):
            for child in value: count_contexts(child)
    count_contexts(result)
    contexts, context_refs = {}, {}
    for identity, entry in occurrences.items():
        if entry['count'] < 2: continue
        ref = entry['name'] + str(len(contexts) + 1)
        context_refs[identity] = ref
        contexts[ref] = deepcopy(entry['value'])
    def share_contexts(value):
        if isinstance(value, dict):
            return {key: {'sharedContext': context_refs[encode(child)]}
                    if key in context_fields and isinstance(child, dict) and encode(child) in context_refs
                    else share_contexts(child) for key, child in value.items()}
        if isinstance(value, list): return [share_contexts(child) for child in value]
        return value
    if contexts:
        result = share_contexts(result)
        result['sharedContexts'] = contexts
    def tables(value):
        if isinstance(value, dict):
            return {key: tables(child) for key, child in value.items()}
        if isinstance(value, list):
            children = [tables(child) for child in value]
            if len(value) >= 3 and all(isinstance(row, dict) and 'code' not in row for row in value):
                table = record_table(children)
                if len(encode(table)) < len(encode(value)):
                    return table
            return children
        return value
    if facts:
        grouped = business_record_tables(result['facts'])
        if len(encode(grouped)) < len(encode(record_table(result['facts']))):
            result['facts'] = grouped
    for reference, records in result.get('executionInputSets', {}).items():
        choices = [business_record_tables(records, input_context=True), field_group_tables(records)]
        grouped = min(choices, key=lambda item: len(encode(item)))
        if len(encode(grouped)) < len(encode(record_table(records))):
            result['executionInputSets'][reference] = grouped
    result = tables(result)
    result['materialFormat'] = {
        'version': 'READABLE_BUSINESS_V1',
        'tables': 'commonFields 为每行逐字段相同的背景；equivalentColumns 指定逐行完全相同的列，宿主已逐值校验。stringColumns 中各列是原始字符串（数值字符串仍为字符串），其余单元格使用 JSON 值保留类型。MISSING 表示字段不存在，null 是来源空值；行序与原始输入一致。字段路径采用 JSON Pointer。',
        'inputs': 'executionInputSets 按实际完整快照共享。factRef 指向本包来源事实，也就是实际 input.ref；fields 列出实际存在的字段，overlay 提供不同值和新增字段，其余从该事实同名字段取得。不含 factRef 时 record 就是完整输入。sharedContext 指向共同背景 sharedContexts 的完整对象，逐字段相同才复用；仅一层引用，不是抽样。',
        'references': 'orderedSelection 指向 referenceSelections 中完整且有序的引用列表；每份实际代码和产物均保留，原始证据未修改。'}
    if aliases:
        result['referenceAliases'] = aliases
        result['materialFormat']['references'] += ' @e 开头的短引用由宿主与原始标识逐一映射；没有合并事实，代码原文不变。正文仅列出代码实际使用的原始标识对照，完整映射由宿主保存。审查意见可使用短引用，由宿主还原。'
    from .review_periods import period_labels
    periods = period_labels(material)
    if periods:
        result['materialFormat']['periods'] = periods
    # Put the short reading guide before the full evidence, without duplicating
    # Chinese names in every input row or changing the program's actual values.
    return {'materialFormat': result.pop('materialFormat'), **result}


def business_review_material(material):
    """Address syntax is audited by the host; the model sees the resolved table.

    Values, dimensions, formulas and *actual* execution inputs are unchanged.
    The original address remains accessible by factId in the frozen artifact.
    """
    result = deepcopy(material)
    addressed = 0
    # If a historical script actually received an address, retain it also on the
    # fact to share that exact input without a second competing representation.
    address_inputs = {item.get('ref') for run in result.get('pythonRuns', [])
                      for item in run.get('inputRecords', []) if 'expression' in item}
    for fact in result.get('facts', []):
        identity = record_identity(fact)
        if identity and fact['factId'] not in address_inputs:
            fact['recordTable'] = identity['table']
            fact.pop('expression', None)
            addressed += 1
    if addressed:
        result['sourceAddressProjection'] = {'count': addressed, 'basis': 'RESOLVED_TABLE_AND_EFFECTIVE_DIMENSIONS',
            'meaning': '预算取数地址语法已由宿主验证，模型视图给出真实 recordTable 和完整生效维度；原始 expression 按 factId 在冻结事实中保留。值、来源公式和实际 Python 输入不改变。'}
    return result


def restore_material(material, *, tables_only=False):
    """Prove the readable projection retains all material, snapshots and types."""
    def expand(value):
        if isinstance(value, dict):
            if value.get('format') == 'JSON_CELL_TABLE_V1':
                return expand(restore_table(value))
            if value.get('format') == 'FIELD_GROUP_TABLES_V1':
                rows = [None] * value['rowCount']
                for group in value['groups']:
                    for index, row in zip(group['positions'], expand(group['records'])): rows[index] = row
                return rows
            if value.get('format') in ('BUDGET_RECORD_OBSERVATIONS_V1', 'INPUT_CONTEXT_OBSERVATIONS_V1'):
                coordinates = {row['recordRow']: row for row in expand(value['recordCoordinates'])}
                columns = {row['columnRow']: row for row in expand(value.get('columnDefinitions', []))}
                rows = []
                for observation in expand(value['observations']):
                    flat = {**_flatten(coordinates[observation['recordRow']]),
                            **_flatten(columns.get(observation.get('columnRow'), {})), **_flatten(observation)}
                    row = {}
                    for path, item in flat.items():
                        if path in (('recordRow',), ('columnRow',)): continue
                        target = row
                        for key in path[:-1]: target = target.setdefault(key, {})
                        target[path[-1]] = item
                    rows.append(row)
                return rows
            return {key: expand(child) for key, child in value.items()}
        if isinstance(value, list): return [expand(child) for child in value]
        return deepcopy(value)
    result = expand(material)
    if tables_only:
        return result
    result.pop('materialFormat', None)
    contexts = result.pop('sharedContexts', {})
    def restore_contexts(value):
        if isinstance(value, dict):
            if set(value) == {'sharedContext'} and value['sharedContext'] in contexts:
                return deepcopy(contexts[value['sharedContext']])
            return {key: restore_contexts(child) for key, child in value.items()}
        if isinstance(value, list): return [restore_contexts(child) for child in value]
        return value
    result = restore_contexts(result)
    facts = {fact['factId']: fact for fact in result.get('facts', [])}
    selections = result.pop('referenceSelections', {})
    def refs(value):
        if isinstance(value, dict):
            if 'orderedSelection' in value:
                selection = selections[value['orderedSelection']]
                if isinstance(selection, dict):
                    if selection['source'] == 'executionInputSets':
                        return [row.get('factRef', (row.get('record') or {}).get('ref'))
                                for row in result['executionInputSets'][selection['inputSetRef']]]
                    if selection.get('field') == 'objectId':
                        members = list(dict.fromkeys(fact['objectId'] for fact in facts.values() if fact.get('objectId') is not None))
                        return sorted(members) if selection['order'] == 'ASCENDING' else members
                    if 'where' in selection:
                        matches = []
                        for ref, fact in facts.items():
                            flat = _flatten(fact)
                            if all(tuple(condition['path']) in flat and encode(flat[tuple(condition['path'])]) == encode(condition['equals'])
                                   for condition in selection['where']): matches.append(ref)
                        return sorted(matches) if selection['order'] == 'FACT_ID_ASCENDING' else matches
                    return sorted(facts) if selection['order'] == 'FACT_ID_ASCENDING' else list(facts)
                return deepcopy(selection)
            return {key: refs(child) for key, child in value.items()}
        if isinstance(value, list): return [refs(child) for child in value]
        return value
    result = refs(result)
    definitions = {item['columnRef']: {key: value for key, value in item.items() if key != 'columnRef'}
                   for item in result.pop('sourceColumnDefinitions', [])}
    for scope in result.get('sourceScopes', []):
        if 'columnDefinitionRefs' in scope:
            scope['columns'] = [deepcopy(definitions[ref]) for ref in scope.pop('columnDefinitionRefs')]
    sets = result.pop('executionInputSets', {})
    for run in result.get('pythonRuns', []):
        lookup = 'executionInputsRef' if 'executionInputsRef' in run else 'inputSetRef'
        if run.get(lookup) not in sets: continue
        records = []
        for item in sets[run.pop(lookup)]:
            if 'record' in item: records.append(item['record']); continue
            source = {**facts[item['factRef']], 'ref': item['factRef'], **item['overlay']}
            records.append({key: deepcopy(source[key]) for key in item['fields']})
        run['inputRecords'] = records
    from .review_aliases import replace_references
    aliases = result.pop('referenceAliases', {})
    return replace_references(result, aliases)
