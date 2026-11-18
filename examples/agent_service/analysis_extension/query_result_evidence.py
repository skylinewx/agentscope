"""普通查询的最终结果选择与执行证据；原始资料始终保留在宿主存储中。"""
from copy import deepcopy
import json

from .models import AnalysisError, digest, json_values


def select_results(turn, selections):
    """仅接受同一会话已经成功执行的产物，不按最新运行或相同数字猜依赖。"""
    if not isinstance(selections, list) or not 1 <= len(selections) <= 50:
        raise AnalysisError('QUERY_RESULT_REFS', 'resultRefs 指定 1—50 组实际 pythonRunRef 和 outputRefs。')
    resolved = []
    for selection in selections:
        if (not isinstance(selection, dict) or set(selection) != {'pythonRunRef', 'outputRefs'}
                or not isinstance(selection['pythonRunRef'], str)
                or not isinstance(selection['outputRefs'], list) or not selection['outputRefs']
                or any(not isinstance(ref, str) for ref in selection['outputRefs'])):
            raise AnalysisError('QUERY_RESULT_REFS', '每组需要 pythonRunRef 及非空 outputRefs，引用返回的 derivedId。')
        run = turn.service.store.get_python_run(turn.service.owner, selection['pythonRunRef'])
        known = {value['derivedId'] for value in run.get('outputs', [])}
        if run.get('status') != 'COMPLETED' or not set(selection['outputRefs']) <= known:
            raise AnalysisError('QUERY_RESULT_REFS', '只能选择成功运行中真实存在的产物；不得将失败运行登记为答复依据。')
        resolved.append({'pythonRunRef': run['runRef'], 'outputRefs': list(dict.fromkeys(selection['outputRefs']))})
        for source in run.get('fileInputs', []):
            if source.get('captureRef'):
                turn._add_query_capture(source['captureRef'])
        if run.get('sourceAnalysisRef'):
            base = turn.service.store.get_analysis(turn.service.owner, run['sourceAnalysisRef'])
            for capture_ref in base.get('captureRefs', []):
                turn._add_query_capture(capture_ref)
    turn.state['queryResultRefs'] = resolved
    return {'resultRefs': deepcopy(resolved), 'status': 'SELECTED',
            'boundary': '已登记答复依据；执行成功不代表业务核对通过。原始文件及探索记录保留，必要依赖由宿主追溯。'}


def bind_answer_outputs(service, run, names):
    """在同一次计算中选定最终产物，避免为登记引用再增加一次模型往返。"""
    if names is None:
        return {}
    if (not isinstance(names, list) or not names or any(not isinstance(name, str) for name in names)
            or len(names) != len(set(names))):
        raise AnalysisError('QUERY_RESULT_REFS', 'answerOutputs 使用本次 result 中不重复的 name。')
    by_name = {value['derivedId'].removeprefix('derived_' + run['runRef'] + '_'): value['derivedId']
               for value in run['outputs']}
    if not set(names) <= set(by_name):
        raise AnalysisError('QUERY_RESULT_REFS', 'answerOutputs 包含本次执行不存在的产物名称。',
                            {'pythonRunRef': run['runRef'], 'availableNames': list(by_name), 'requiresSourceQuery': False})
    selections = [{'pythonRunRef': run['runRef'], 'outputRefs': [by_name[name] for name in names]}]
    if service.turn is not None:
        select_results(service.turn, selections)
    return {'resultRefs': selections}


def source_reads(service, run, budget=None):
    """恢复运行实际读到的单元格。读集属于整个运行，不能伪称逐产物精确血缘。"""
    from .grid_files import resolve_input, records
    from .python_worker import grid_matches, grid_value
    budget = {'remaining': 4 * 1024 * 1024} if budget is None else budget
    result = []
    for source in run.get('fileInputs', []):
        if source.get('provenance') != 'BUDGET_FORM':
            continue
        saved = resolve_input(service, {'captureRef': source['captureRef'], 'fileRef': source['ref']})
        if saved['sha256'] != source.get('sha256'):
            raise AnalysisError('GRID_FILE_INTEGRITY', '计算所用网格与已保存来源摘要不一致。')
        reads = [read for read in run.get('fileReads', []) if read['ref'] == source['ref']]
        columns = ['key', 'row', 'column', 'measureCode', 'headers', 'dimensions', 'value', 'unit', 'formula', 'mapType', 'issue']
        rows, seen = [], set()
        matched = [0] * len(reads)
        for position, cell in enumerate(records(saved), 1):
            cell = dict(cell)
            cell['dimensions'] = {**saved.get('context', {}).get('effectiveDimensions', {}), **cell.get('dimensions', {})}
            included = False
            for index, read in enumerate(reads):
                if position <= read['scannedCount'] and grid_matches(cell, cell['dimensions'], read['where']):
                    matched[index] += 1
                    included = True
            if not included or cell['key'] in seen:
                continue
            seen.add(cell['key'])
            cell['value'] = grid_value(cell, saved.get('numberFormat'))
            row = [cell.get(key) for key in columns]
            size = len(json.dumps(row, ensure_ascii=False))
            # 超过核对工作内存时显式标记未完成；不把截取后的明细当完整输入。
            if size <= budget['remaining']:
                rows.append(row)
                budget['remaining'] -= size
        if matched != [read['matchedCount'] for read in reads]:
            raise AnalysisError('GRID_READ_INTEGRITY', '已保存的运行读集与来源记录不一致。')
        data = {'ref': source['ref'], 'captureRef': source['captureRef'], 'sha256': source['sha256'],
                       'context': deepcopy(source.get('context', {})), 'coverage': source.get('coverage'),
                       'entryType': source.get('entryType'), 'columns': columns, 'rows': rows,
                       'rowCount': len(seen), 'complete': len(rows) == len(seen),
                       'lineage': 'CONSERVATIVE_RUN_READ_SET'}
        pack_source_rows(data)
        result.append(data)
    return result


def pack_source_rows(data):
    """无损提取共同条件和标题字典，保留每个单元格、空值及不同维度组合。"""
    cells = [dict(zip(data['columns'], row)) for row in data['rows']]
    if not cells:
        return
    fixed = {key: value for key, value in cells[0].items()
             if key not in ('key', 'row', 'column', 'dimensions', 'headers', 'value')
             and all(cell.get(key) == value for cell in cells)}
    fixed_dimensions = {key: value for key, value in cells[0]['dimensions'].items()
                        if all(cell['dimensions'].get(key) == value and key in cell['dimensions'] for cell in cells)}
    dimension_codes = sorted({key for cell in cells for key in cell['dimensions']} - set(fixed_dimensions))
    # 表头经常缺少数据行的维度，使用专用缺失下标，避免一行稀疏数据迫使整表重复维度名。
    # null 是字典中的真实值，与没有此维度的 -1 下标严格区分。
    headers, header_refs, dimension_values, dimension_refs = [], {}, [], {}
    columns = [key for key in data['columns'] if key not in fixed]
    packed = []
    for cell in cells:
        codes = []
        for title in cell['headers'] or []:
            fingerprint = digest(json_values(title))
            if fingerprint not in header_refs:
                header_refs[fingerprint] = len(headers)
                headers.append(title)
            codes.append(header_refs[fingerprint])
        dimensions = {key: value for key, value in cell['dimensions'].items() if key not in fixed_dimensions}
        encoded_dimensions = {}
        for key, value in dimensions.items():
            fingerprint = digest(json_values(value))
            if fingerprint not in dimension_refs:
                dimension_refs[fingerprint] = len(dimension_values)
                dimension_values.append(value)
            encoded_dimensions[key] = dimension_refs[fingerprint]
        item = {**cell, 'headers': codes if cell['headers'] is not None else None,
                'dimensions': [encoded_dimensions.get(key, -1) for key in dimension_codes]}
        packed.append([item[key] for key in columns])
    column_dictionaries = {}
    for index, column in enumerate(columns):
        if column in ('dimensions', 'headers'):
            continue
        values, references, encoded = [], {}, []
        for row in packed:
            fingerprint = digest(json_values(row[index]))
            if fingerprint not in references:
                references[fingerprint] = len(values)
                values.append(row[index])
            encoded.append(references[fingerprint])
        # 仅在确实节省空间时使用字典。唯一金额通常原样保留，表头与数据混排也不会丢失类型。
        original_size = len(json.dumps([row[index] for row in packed], ensure_ascii=False))
        encoded_size = len(json.dumps([values, encoded], ensure_ascii=False)) + len(column) + 20
        if encoded_size < original_size:
            column_dictionaries[column] = values
            for row, reference in zip(packed, encoded):
                row[index] = reference
    data.update(columns=columns, rows=packed, fixedCellFields=fixed, fixedDimensions=fixed_dimensions,
                dimensionColumns=dimension_codes, missingDimensionRef=-1,
                dimensionValueDictionary=dimension_values, headerDictionary=headers,
                columnDictionaries=column_dictionaries, rowEncoding='SHARED_GRID_FIELDS_V1')


def source_cells(data):
    """恢复无损网格投影，供来源明细交付和回归校验共用。"""
    for row in data['rows']:
        cell = {**data.get('fixedCellFields', {}), **dict(zip(data['columns'], row))}
        if data.get('rowEncoding') == 'SHARED_GRID_FIELDS_V1':
            for column, dictionary in data.get('columnDictionaries', {}).items():
                cell[column] = dictionary[cell[column]]
            dimensions = (dict(zip(data['dimensionColumns'], cell['dimensions']))
                          if data['dimensionColumns'] is not None else cell['dimensions'])
            dimensions = {key: data['dimensionValueDictionary'][ref] for key, ref in dimensions.items()
                          if ref != data.get('missingDimensionRef')}
            cell['dimensions'] = {**data['fixedDimensions'], **dimensions}
            cell['headers'] = [data['headerDictionary'][ref] for ref in cell['headers']] if cell['headers'] is not None else None
        yield cell


def execution_evidence(turn):
    """选择答复根节点并递归恢复依赖；未选择时保留原有全运行语义。"""
    selections = turn.state.get('queryResultRefs')
    if selections is None:
        selections = []
        for ref in turn.state.get('pythonRunRefs', []):
            run = turn.service.store.get_python_run(turn.service.owner, ref)
            if run.get('status') == 'COMPLETED' and any(item.get('provenance') == 'BUDGET_FORM' for item in run.get('fileInputs', [])):
                selections.append({'pythonRunRef': ref, 'outputRefs': [item['derivedId'] for item in run['outputs']]})
    runs, roots, visiting = {}, [], set()
    read_budget = {'remaining': 4 * 1024 * 1024}

    def visit(run_ref, output_refs):
        if run_ref in visiting:
            raise AnalysisError('QUERY_RESULT_DEPENDENCY', '计算结果依赖存在循环。')
        run = turn.service.store.get_python_run(turn.service.owner, run_ref)
        outputs = {value['derivedId']: value for value in run.get('outputs', [])}
        if run.get('status') != 'COMPLETED' or not set(output_refs) <= set(outputs):
            raise AnalysisError('QUERY_RESULT_DEPENDENCY', '答复依赖的执行产物不存在或尚未成功。')
        if run_ref in runs:
            selected = {value['derivedId']: value for value in runs[run_ref]['outputs']}
            selected.update({ref: outputs[ref] for ref in output_refs})
            runs[run_ref]['outputs'] = list(selected.values())
            return
        visiting.add(run_ref)
        inputs = run.get('inputRecords')
        if inputs is None:
            # 较早的运行可从其固定分析版本恢复输入，不能用当前版本替代。
            base = (turn.service.store.get_analysis(turn.service.owner, run['sourceAnalysisRef'])
                    if run.get('sourceAnalysisRef') else {})
            known = {item.get('factId', item.get('derivedId')): item
                     for item in base.get('facts', []) + base.get('derivedValues', [])}
            file_refs = {item['ref'] for item in run.get('fileInputs', [])}
            missing = [ref for ref in run['inputRefs'] if ref not in known and ref not in file_refs]
            if missing:
                raise AnalysisError('QUERY_RESULT_DEPENDENCY', '无法恢复计算时实际输入，不能省略依赖后核对通过。')
            inputs = [{'ref': ref, **known[ref]} for ref in run['inputRefs'] if ref in known]
        dependencies = []
        for item in inputs:
            if item.get('pythonRunRef'):
                visit(item['pythonRunRef'], [item['ref']])
                dependencies.append(item['pythonRunRef'])
        from .grid_files import read_summary
        runs[run_ref] = {key: deepcopy(run[key]) for key in ('runRef', 'code', 'purpose', 'inputRefs', 'inputDigest', 'fileInputs', 'status') if key in run}
        for source in runs[run_ref].get('fileInputs', []):
            if source.get('provenance') == 'BUDGET_FORM':
                source.pop('preview', None)  # 实际读集已完整附上，结构预览不再重复传入核对。
        runs[run_ref].update(outputs=[deepcopy(outputs[ref]) for ref in output_refs],
                             allOutputsDigest=digest(json_values(run['outputs'])),
                             inputs=deepcopy(inputs), dependencies=list(dict.fromkeys(dependencies)),
                             fileReads=read_summary(run.get('fileReads', [])), sourceData=source_reads(turn.service, run, read_budget))
        visiting.remove(run_ref)

    for selection in selections:
        visit(selection['pythonRunRef'], selection['outputRefs'])
        roots.extend(selection['outputRefs'])
    return list(runs.values()), list(dict.fromkeys(roots))


def dependency_units(evidence):
    """一个执行及其完整输入链是不可拆的核对单元，不能把 SUM/排序拆成局部通过。"""
    runs = {run['runRef']: run for run in evidence.get('pythonRuns', [])}
    units = []
    for run in runs.values():
        if not any(value['derivedId'] in evidence.get('resultRoots', []) for value in run['outputs']):
            continue
        selected = {}

        def include(ref):
            if ref in selected:
                return
            selected[ref] = runs[ref]
            for parent in runs[ref].get('dependencies', []):
                include(parent)
        include(run['runRef'])
        units.append({'unitRef': digest(json_values(list(selected.values()))), 'rootRunRef': run['runRef'],
                      'pythonRuns': list(selected.values())})
    return units


def dependency_cache_keys(unit, policy):
    """独立于问题和答复，绑定代码、实际输入/读集、全部输出摘要及规则版本。"""
    runs = sorted(unit['pythonRuns'], key=lambda run: run['runRef'])
    material = [{**{key: value for key, value in run.items() if key != 'outputs'},
                 'allOutputsDigest': run.get('allOutputsDigest') or digest(json_values(run['outputs']))}
                for run in runs]
    root = next(run for run in runs if run['runRef'] == unit['rootRunRef'])
    # 每个确实审过的产物单独登记；同次运行中未选入审查的探索产物不能搭便车。
    return {value['derivedId']: digest(['QUERY_EXECUTION_DEPENDENCY_V1', policy, json_values(material),
                                      json_values(value)]) for value in root['outputs']}


def cached_dependencies(turn, units, policy):
    """只复用当前会话内完整、无阻断的执行核对，不复用答案通过状态。"""
    verified = []
    for unit in units:
        keys = dependency_cache_keys(unit, policy)
        records = [turn.service.store.get_semantic_review(turn.service.owner, key) for key in keys.values()]
        if records and all(record and record.get('status') == 'PASS' and not record.get('issues') for record in records):
            verified.append(unit['unitRef'])
    return verified


def save_dependency_verdict(turn, unit, policy):
    """调用方只在完整依赖段或整份首次核对通过后登记这些产物。"""
    for ref, key in dependency_cache_keys(unit, policy).items():
        turn.service.store.save_semantic_review(turn.service.owner, key, {
            'status': 'PASS', 'issues': [], 'outputRef': ref, 'rootRunRef': unit['rootRunRef'],
            'scope': 'IMMUTABLE_EXECUTION_ONLY', 'policyDigest': policy})


def final_result_evidence(evidence, units, verified_refs):
    """最终阶段保留选定输出、来源范围与已核对依赖身份，不再次附整次读集。"""
    final = {key: value for key, value in evidence.items() if key != 'pythonRuns'}
    final['finalResults'] = [value for run in evidence.get('pythonRuns', []) for value in run['outputs']
                             if value['derivedId'] in evidence.get('resultRoots', [])]
    final['dependencyUnitRefs'] = [unit['unitRef'] for unit in units]
    final['verifiedDependencyUnitRefs'] = list(verified_refs)
    # 保留每个输出的实际执行范围；它不声称是逐产物精确血缘。
    final['executionScopes'] = [{key: value for key, value in run.items()
                                if key in ('runRef', 'purpose', 'inputRefs', 'fileReads', 'dependencies')}
                               for run in evidence.get('pythonRuns', [])]
    return final


def share_source_data(evidence):
    """多次运行读取完全相同的来源时共享完整读集，不重复占用核对容量。"""
    counts, tables = {}, {}
    for run in evidence.get('pythonRuns', []):
        for data in run.get('sourceData', []):
            key = digest(json_values(data))
            counts[key] = counts.get(key, 0) + 1
            tables[key] = data
    shared = {key: value for key, value in tables.items() if counts[key] > 1}
    if not shared:
        return evidence
    runs = []
    for run in evidence['pythonRuns']:
        sources = []
        for data in run.get('sourceData', []):
            key = digest(json_values(data))
            sources.append({'sourceDataRef': key} if key in shared else data)
        runs.append({**run, 'sourceData': sources})
    return {**evidence, 'pythonRuns': runs, 'sharedSourceData': shared}


def focus_saved_reads(turn, evidence):
    """大读集仅补做一次本地筛选；次数持久化，恢复会话也不能反复补做。"""
    from .review_control import progress
    roots = set(evidence.get('resultRoots', []))
    runs = [run for run in evidence.get('pythonRuns', [])
            if any(output['derivedId'] in roots for output in run['outputs'])]
    if not any(run.get('sourceData') for run in runs):
        return None
    saved = progress(turn)
    if saved.get('queryReadFocus'):
        return None
    basis = []
    for run in runs:
        outputs = [output for output in run['outputs'] if output['derivedId'] in roots]
        basis.extend(_output_basis(run['runRef'], output) for output in outputs)
    request = {'attempts': 1, 'originalResults': basis,
               'originalRuns': [{'runRef': run['runRef'], 'outputRefs': [
                   output['derivedId'] for output in run['outputs'] if output['derivedId'] in roots]}
                   for run in runs], 'files': [
        {'captureRef': source['captureRef'], 'fileRef': source['ref'], 'rowCount': source['rowCount'],
         'completeReadEvidence': source['complete']}
        for run in runs for source in run.get('sourceData', [])]}
    progress(turn, lambda state: state.setdefault('queryReadFocus', request))
    return {'status': 'INCOMPLETE', 'semanticReview': 'NOT_REVIEWED', 'recoveryAction': 'FOCUS_SAVED_GRID',
            'issues': [{'code': 'QUERY_EVIDENCE_FOCUS', 'message':
                '现有计算的原始读集太大。只允许一次本地整理：复用已保存的 gridInputs，把代码中已有的维度、指标或坐标筛选移入 grid_rows(ref, where)，'
                '多个真实条件可分次读取并保持原有全局计算；沿用原问题、成员、时期、空值处理及所有答复产物。'
                '保留产物 name，诊断结构等整理若发生变化，宿主会保留整理前后的差异，交给同一次核对判断；不能为通过核对改写数值或删掉必要结果。'
                '使用 value 原值计算，不能以 displayValue 推算精度；通过 answerOutputs 选择重算后的全部答复产物。'
                '不重查预算服务、不抽样替代全量、不删减范围或构成。若无法保持相同计算范围，停止补做并如实说明。',
                'details': {'files': request['files'], 'outputs': [item['name'] for item in basis],
                            'remainingAttempts': 1, 'requiresSourceQuery': False}}]}


def _output_basis(run_ref, output):
    from decimal import Decimal

    def exact_number(value):
        number = Decimal(value)
        if not number:
            return '0'
        text = format(number, 'f')
        return text.rstrip('0').rstrip('.') if '.' in text else text

    content = {key: deepcopy(output[key]) for key in
               ('outputKind', 'value', 'unit', 'columns', 'rows', 'data', 'text') if key in output}
    if output.get('outputKind') == 'SCALAR' and content.get('value') is not None:
        content['value'] = exact_number(content['value'])
    if 'rows' in content and 'columns' in content:
        for index, column in enumerate(content['columns']):
            if column.get('type') == 'DECIMAL_STRING':
                for row in content['rows']:
                    if row[index] is not None:
                        row[index] = exact_number(row[index])
        content['columns'] = [{key: column[key] for key in ('name', 'type', 'unit') if key in column}
                              for column in content['columns']]
    return {'name': output['derivedId'].removeprefix('derived_' + run_ref + '_'),
            'contentDigest': digest(json_values(content))}


def focused_results_preserved(turn, evidence):
    """容量整理只改变文件读法，不允许以遗漏产物或改写数值作为恢复成功。"""
    from .review_control import progress
    request = progress(turn).get('queryReadFocus')
    if request is None:
        return True
    roots = set(evidence.get('resultRoots', []))
    original = sorted(digest(json_values(item)) for item in request['originalResults'])
    current = sorted(digest(json_values(_output_basis(run['runRef'], output)))
                     for run in evidence.get('pythonRuns', []) for output in run['outputs'] if output['derivedId'] in roots)
    return original == current


def focused_review_context(turn, evidence):
    """整理后的结构变化交给当前核对判断，不将诊断字段变化直接当作金额错误。"""
    from collections import defaultdict
    from .grid_files import read_summary
    from .review_control import progress

    request = progress(turn).get('queryReadFocus')
    if request is None or focused_results_preserved(turn, evidence):
        return None
    roots = set(evidence.get('resultRoots', []))
    previous, current = defaultdict(list), defaultdict(list)
    for item in request['originalResults']:
        previous[item['name']].append(item['contentDigest'])
    for run in evidence.get('pythonRuns', []):
        for output in run['outputs']:
            if output['derivedId'] in roots:
                basis = _output_basis(run['runRef'], output)
                current[basis['name']].append(basis['contentDigest'])
    changed = {name for name in previous.keys() | current.keys()
               if sorted(previous[name]) != sorted(current[name])}
    calculations = []
    for selected in request['originalRuns']:
        run = turn.service.store.get_python_run(turn.service.owner, selected['runRef'])
        outputs = [output for output in run['outputs'] if output['derivedId'] in selected['outputRefs']]
        if not any(_output_basis(run['runRef'], output)['name'] in changed for output in outputs):
            continue
        calculations.append({'runRef': run['runRef'], 'purpose': run.get('purpose'), 'code': run['code'],
                             'inputRefs': run['inputRefs'], 'fileReads': read_summary(run.get('fileReads', [])),
                             'outputs': outputs})
    # 原始运行与文件仍在存储中；这里保存旧代码和输出作差异证据，当前完整读集仍由 pythonRuns 提供。
    return {'changedOutputNames': sorted(changed), 'previousCalculations': calculations,
            'comparisonStatus': 'REQUIRES_REVIEW'}
