"""预算网格文件的完整接收、会话归属及按需读取；源文件不进入模型上下文。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import logging
from urllib.parse import urlsplit, urlunsplit, quote

from .models import AnalysisError, digest

MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_LINE_BYTES = 32 * 1024 * 1024


def manifest_forms(receipt):
    from .adapters.budget_v2 import form_rows
    return [(step, form) for step, form in form_rows(receipt) if isinstance(form.get('file'), dict)]


def file_summary(capture):
    return [{key: value[key] for key in ('ref', 'stepId', 'observationId', 'sha256', 'byteSize', 'cellCount',
                                       'coverage', 'entryType', 'context', 'preview', 'structure', 'selection') if key in value}
            for value in capture.get('gridFiles', [])]


def read_summary(reads):
    """完整选中坐标留在执行记录，模型只接收少量样例与真实扫描计数。"""
    return [{**{key: value for key, value in read.items() if key != 'selectedKeys'},
             'selectedKeysPreview': read.get('selectedKeys', [])[:12],
             'selectedKeysStoredCount': len(read.get('selectedKeys', []))} for read in reads]


def _verify_file(path, expected):
    total, count, actual = 0, 0, hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(65536), b''):
            total += len(block)
            if total > MAX_FILE_BYTES:
                raise AnalysisError('GRID_FILE_LIMIT', '网格文件超过当前文件处理上限。')
            count += block.count(b'\n')
            actual.update(block)
    if total != expected['byteSize'] or count != expected['cellCount'] or actual.hexdigest() != expected['sha256']:
        raise AnalysisError('GRID_FILE_INTEGRITY', '网格文件大小、记录数量或摘要与来源回执不一致。')


def records(value):
    """仅接收宿主登记的描述；脚本通过 ref 读取，不提供任意路径。"""
    path = Path(value['_path'])
    with path.open('rb') as stream:
        while line := stream.readline(MAX_LINE_BYTES + 1):
            if len(line) > MAX_LINE_BYTES:
                raise AnalysisError('GRID_FILE_LIMIT', '单条网格记录超过处理上限。')
            cell = json.loads(line, parse_float=str)
            if not isinstance(cell, dict) or not isinstance(cell.get('key'), str):
                raise AnalysisError('GRID_FILE_INVALID', '网格文件存在无效单元格。')
            yield cell


def structure_profile(value):
    """一次扫描生成可直接用于读取的实际字段目录；不猜指标含义或成员业务范围。"""
    measures, dimension_codes, map_types = {}, set(), set()
    omitted = False
    for cell in records(value):
        dimension_codes.update(cell.get('dimensions', {}))
        if cell.get('mapType'):
            map_types.add(cell['mapType'])
        code = cell.get('measureCode')
        if not code:
            continue
        if code not in measures and len(measures) >= 32:
            omitted = True
            continue
        item = measures.setdefault(code, {'measureCode': code, 'cellCount': 0, 'headerExamples': [],
                                         'where': {'measureCode': code}})
        item['cellCount'] += 1
        headers = cell.get('headers') or []
        example = headers[-1] if headers else None
        if isinstance(example, str) and len(example) <= 160 and example not in item['headerExamples'] and len(item['headerExamples']) < 3:
            item['headerExamples'].append(example)
    return {'measures': list(measures.values()), 'dimensionCodes': sorted(dimension_codes),
            'mapTypes': sorted(map_types), 'measureDirectoryComplete': not omitted,
            'meaning': ('由当前裁剪范围统计的字段目录，不代表整表；' if value.get('selection') else '由完整原始文件统计的字段目录；')
                       + 'where 可与已确认的 dimensions 合并后交给 grid_rows。标题只是实际样例，不能替代业务口径判断。'}


async def receive_files(receipt, tool, store, owner):
    """只从当前已授权 MCP 连接的同源地址下载，不使用模型传入的 URL 或凭据。"""
    import httpx
    forms = manifest_forms(receipt)
    if not forms:
        return []
    connection = tool._entry.client
    configuration = connection.mcp_config
    source_url = urlsplit(configuration.url)
    if source_url.scheme not in ('http', 'https') or source_url.username or source_url.password:
        raise AnalysisError('GRID_FILE_TRANSPORT', '当前来源连接不支持同源文件传输。')
    headers = dict(getattr(configuration, 'headers', None) or {})
    headers.update(dict(getattr(connection, '_runtime_headers', {}) or {}))
    # MCP 的会话协议头不能用于 REST 文件下载。
    headers = {key: value for key, value in headers.items() if key.lower() not in ('mcp-session-id', 'accept', 'content-type')}
    root = store.root / 'grid-files' / digest(owner.key)
    root.mkdir(parents=True, exist_ok=True)
    result = []
    async with httpx.AsyncClient(headers=headers, timeout=60, follow_redirects=False) as client:
        for step_id, form in forms:
            started = time.perf_counter()
            manifest = form['file']
            observation = form.get('runtime', {}).get('observation', {})
            oid = observation.get('observationId')
            context = receipt.get('context', {})
            expected_path = '/api/v1/ontology/sources/' + quote(str(context.get('sourceId')), safe='') + '/snapshots/' + quote(str(context.get('snapshotId')), safe='') + '/forms/observations/' + quote(str(oid), safe='') + '/cells'
            from .grid_selection import download_request
            method, expected_path, arguments = download_request(form, expected_path)
            if (manifest.get('format') != 'GRID_JSONL_V1'
                    or not isinstance(manifest.get('sha256'), str) or len(manifest['sha256']) != 64
                    or any(character not in '0123456789abcdef' for character in manifest['sha256'])
                    or type(manifest.get('byteSize')) is not int or not 0 <= manifest['byteSize'] <= MAX_FILE_BYTES
                    or type(manifest.get('cellCount')) is not int or not 0 <= manifest['cellCount'] <= 1_000_000):
                raise AnalysisError('GRID_FILE_MANIFEST', '网格文件描述与已捕获的来源观察不一致。')
            destination = root / (manifest['sha256'] + '.jsonl')
            if not destination.exists():
                handle, temporary = tempfile.mkstemp(prefix='grid-', suffix='.tmp', dir=root)
                try:
                    url = urlunsplit((source_url.scheme, source_url.netloc, expected_path, '', ''))
                    with os.fdopen(handle, 'wb') as output:
                        async with client.stream(method, url, **arguments) as response:
                            response.raise_for_status()
                            size = 0
                            async for block in response.aiter_bytes(65536):
                                size += len(block)
                                if size > manifest['byteSize'] or size > MAX_FILE_BYTES:
                                    raise AnalysisError('GRID_FILE_LIMIT', '下载内容超过来源声明的文件大小。')
                                output.write(block)
                    _verify_file(Path(temporary), manifest)
                    os.replace(temporary, destination)
                except httpx.HTTPError as failure:
                    raise AnalysisError('GRID_FILE_DOWNLOAD', '网格文件下载失败，原查询回执已保留；没有将失败解释为无数据。',
                                        {'exceptionType': type(failure).__name__, 'observationId': oid}) from failure
                finally:
                    Path(temporary).unlink(missing_ok=True)
            else:
                _verify_file(destination, manifest)
            value = {'ref': 'grid_' + digest([owner.key, step_id, oid, manifest['sha256'], form.get('selection')])[:24], '_path': str(destination),
                     'stepId': step_id, 'observationId': oid, 'sha256': manifest['sha256'], 'byteSize': manifest['byteSize'],
                     'cellCount': manifest['cellCount'], 'coverage': form.get('coverage'), 'entryType': observation.get('entryType'),
                     'context': form.get('runtime', {}).get('context', {}), 'sourceFactIds': [],
                     'kind': 'GRID_FILE', 'provenance': 'BUDGET_FORM', 'valueState': 'NOT_APPLICABLE'}
            if form.get('selection'):
                value['selection'] = form['selection']
            preview, chars = [], 0
            for cell in records(value):
                projected = {key: cell.get(key) for key in ('key', 'mapType', 'measureCode', 'dimensions', 'headers', 'displayValue', 'unit', 'formula')}
                size = len(json.dumps(projected, ensure_ascii=False))
                if chars + size > 12000 or len(preview) >= 12:
                    break
                preview.append(projected); chars += size
            value['preview'] = preview
            value['structure'] = structure_profile(value)
            logging.getLogger(__name__).info("网格文件接收：观察=%s，裁剪=%s，来源单元格=%s，返回单元格=%s，字节数=%s，耗时毫秒=%s",
                        oid, bool(form.get('selection')), (form.get('selection') or {}).get('sourceCellCount', manifest['cellCount']),
                        manifest['cellCount'], manifest['byteSize'], round((time.perf_counter()-started)*1000, 2))
            result.append(value)
    return result


def resolve_input(service, specification):
    capture = service.store.get_capture(service.owner, specification.get('captureRef'))
    from .adapters.budget_v2 import receipt_from_capture, validate_form_request, stored_form_request
    capture = stored_form_request(service.store, service.owner, capture)
    receipt = receipt_from_capture(capture)
    values = [value for value in capture.get('gridFiles', []) if value['ref'] == specification.get('fileRef')]
    if len(values) != 1:
        raise AnalysisError('GRID_FILE_NOT_FOUND', '当前回执未登记指定网格文件，请读取 captures 或原始回执的文件信息。',
                            {'files': file_summary(capture), 'downloadErrors': capture.get('gridFileErrors', [])})
    value = dict(values[0])
    forms = [(step, form) for step, form in manifest_forms(receipt)
             if step == value['stepId'] and form.get('runtime', {}).get('observation', {}).get('observationId') == value['observationId']
             and form['file'].get('sha256') == value['sha256']]
    if len(forms) != 1:
        raise AnalysisError('GRID_FILE_MANIFEST', '网格文件未匹配唯一来源观察。')
    validate_form_request(capture, receipt, *forms[0])
    expected_root = (service.store.root / 'grid-files' / digest(service.owner.key)).resolve()
    path = Path(value['_path'])
    if path.is_symlink() or path.parent.resolve() != expected_root or path.name != value['sha256'] + '.jsonl':
        raise AnalysisError('GRID_FILE_ACCESS', '网格文件不属于当前会话。')
    _verify_file(path, value)
    value['captureRef'] = capture['captureRef']
    value['numberFormat'] = capture.get('numberFormat')
    return value


def captured_cells(capture, form):
    if not form.get('file'):
        return (form.get('page') or {}).get('cells', form.get('cells', []))
    oid = form.get('runtime', {}).get('observation', {}).get('observationId')
    values = [value for value in capture.get('gridFiles', []) if value['observationId'] == oid
              and value['sha256'] == form['file'].get('sha256')]
    if not values:
        raise AnalysisError('GRID_FILE_NOT_FOUND', '完整网格文件尚未落盘，不能将空 cells 解释为无数据。')
    _verify_file(Path(values[0]['_path']), values[0])
    return records(values[0])


def collect_expired_files(store, db, cutoff):
    """沿用输入保留策略，只回收专用目录中无人引用的旧文件；运行记录钉住所用回执。"""
    root = store.root / 'grid-files'
    if not root.is_dir() or root.is_symlink() or root.resolve() != store.root.resolve() / 'grid-files':
        return 0
    retained = set()
    for row in db.execute('SELECT owner,digest FROM captures'):
        capture = store._read(row['digest'])
        retained.update((digest(row['owner']), value['sha256']) for value in capture.get('gridFiles', []))
    removed = 0
    for owner_root in root.iterdir():
        if not owner_root.is_dir() or owner_root.is_symlink() or owner_root.resolve().parent != root.resolve():
            continue
        for path in owner_root.iterdir():
            if path.is_symlink() or path.resolve().parent != owner_root.resolve() or not path.is_file():
                continue
            if path.stat().st_mtime >= cutoff:
                continue
            hashed = path.suffix == '.jsonl' and len(path.stem) == 64 and all(char in '0123456789abcdef' for char in path.stem)
            temporary = path.name.startswith('grid-') and path.suffix == '.tmp'
            if temporary or hashed and (owner_root.name, path.stem) not in retained:
                path.unlink()
                removed += 1
    return removed
