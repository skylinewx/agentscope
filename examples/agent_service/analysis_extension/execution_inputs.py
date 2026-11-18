"""Host-owned grants, grid streaming and durable delivery journal. Never imported by the worker."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import threading
from uuid import uuid4

from .execution_protocol import encode, MAX_DATA_BYTES
from .grid_contract import grid_matches, grid_value, validate_where
from .grid_files import records, _verify_file

PROVENANCE_MODE = 'HOST_RECORDED_DELIVERY'


class InputRequestError(ValueError):
    pass


def delivery_summary(manifest):
    fields = ('ref', 'sha256', 'where', 'scannedCount', 'sentThrough', 'matchedCount', 'sentCount',
              'scanComplete', 'sendComplete', 'status')
    return {'provenanceMode': PROVENANCE_MODE, 'inlineCount': len(manifest.get('inlineRefs', [])),
            'inlineSelection': 'ALL_FROZEN_NON_GRID_INPUTS',
            'inlineComplete': manifest.get('inlineComplete', False),
            'sources': [{key: read[key] for key in fields if key in read} for read in manifest.get('reads', [])],
            'algorithmUsage': 'NOT_PROVEN_BY_DELIVERY'}


class InputBroker:
    def __init__(self, inputs, journal_path, *, block_bytes=256 * 1024, block_rows=512,
                 cache_bytes=2 * MAX_DATA_BYTES):
        self.inputs = deepcopy(inputs)
        self.files = {item['ref']: item for item in self.inputs if item.get('kind') == 'GRID_FILE'}
        self.public_inputs = [{k: v for k, v in item.items() if k != '_path'} for item in self.inputs]
        self.manifest = {'provenanceMode': PROVENANCE_MODE,
            'inlineRefs': [item.get('ref', 'inline:' + str(index)) for index, item in enumerate(inputs) if item.get('kind') != 'GRID_FILE'],
            'inlineComplete': False, 'reads': [], 'violations': []}
        self.cursors, self.verified = {}, set()
        self.stopped = threading.Event()
        self.block_bytes, self.block_rows = block_bytes, block_rows
        self.cache_bytes = cache_bytes
        self.journal_path = Path(journal_path)
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self.journal = self.journal_path.open('xb')
        self._event('AUTHORIZED', {'refs': [item.get('ref', 'inline:' + str(index)) for index, item in enumerate(inputs)]})

    def _event(self, kind, data):
        self.journal.write(encode({'type': kind, **data}) + b'\n')
        self.journal.flush()
        os.fsync(self.journal.fileno())

    def inline_pending(self):
        self._event('INLINE_PENDING', {'refs': self.manifest['inlineRefs']})

    def inline_sent(self):
        self._event('INLINE_SENT', {})
        self.manifest['inlineComplete'] = True

    def open(self, ref, where):
        if not isinstance(ref, str) or ref not in self.files:
            self.violation('UNAUTHORIZED_SOURCE')
            raise ValueError('grid_rows 需要 inputs 中已授权的网格 ref')
        if len(self.cursors) >= 100:
            raise ValueError('单次执行最多读取 100 个网格范围')
        try:
            where = validate_where(where)
        except ValueError as error:
            raise InputRequestError(str(error)) from error
        source = self.files[ref]
        if ref not in self.verified:
            _verify_file(Path(source['_path']), source)
            self.verified.add(ref)
        cursor = uuid4().hex
        record = {'ref': ref, 'sha256': source['sha256'], 'where': where,
            'scannedCount': 0, 'matchedCount': 0, 'sentCount': 0, 'sentThrough': 0,
            'scanComplete': False, 'sendComplete': False, 'status': 'OPEN', 'blocks': []}
        self.manifest['reads'].append(record)
        state = {'source': source, 'read': record, 'iterator': iter(records(source)), 'position': 0,
                 'sequence': 0, 'buffered': None, 'closed': False, 'last': None, 'lastBytes': 0}
        self.cursors[cursor] = state
        self._event('READ_OPEN', {'cursor': cursor, 'ref': ref, 'where': where, 'sha256': source['sha256']})
        return {'type': 'READ_OPENED', 'cursor': cursor, 'sourceRef': ref, 'sha256': source['sha256']}

    def _state(self, cursor):
        if not isinstance(cursor, str) or cursor not in self.cursors:
            self.violation('UNAUTHORIZED_CURSOR')
            raise ValueError('输入游标不属于本次执行')
        return self.cursors[cursor]

    def next(self, cursor, sequence):
        state = self._state(cursor)
        read, source = state['read'], state['source']
        if type(sequence) is not int or sequence < 0:
            raise ValueError('输入块序号不合法')
        if sequence == state['sequence'] - 1 and state['last'] is not None:
            return state['last'], None
        if sequence != state['sequence'] or state['closed'] or read['sendComplete']:
            self.violation('INVALID_SEQUENCE')
            raise ValueError('输入块必须按顺序读取')
        # Only the immediately preceding block can be replayed. Do not retain
        # an old large block while producing the next one on the same cursor.
        state['last'], state['lastBytes'] = None, 0
        rows, size, through = [], 0, read['sentThrough']
        while len(rows) < self.block_rows:
            if self.stopped.is_set():
                raise ValueError('输入读取已取消')
            if state['buffered'] is not None:
                position, cell, cell_size = state.pop('buffered')
                state['buffered'] = None
            else:
                try:
                    cell = next(state['iterator'])
                except StopIteration:
                    read['scanComplete'] = state['position'] == source['cellCount']
                    if not read['scanComplete']:
                        raise ValueError('网格文件记录数量不完整')
                    through = state['position']
                    break
                state['position'] += 1
                position = state['position']
                read['scannedCount'] = position
                dimensions = {**source.get('context', {}).get('effectiveDimensions', {}), **cell.get('dimensions', {})}
                if not grid_matches(cell, dimensions, read['where']):
                    through = position
                    continue
                cell = {**cell, 'dimensions': dimensions, 'value': grid_value(cell, source.get('numberFormat')),
                        'sourceRef': source['ref']}
                cell_size = len(encode(cell))
                if cell_size > MAX_DATA_BYTES - 4096:
                    raise ValueError('单条网格传输记录超过容量')
                read['matchedCount'] += 1
            retained = sum(item['lastBytes'] + (item['buffered'][2] if item['buffered'] else 0)
                           for item in self.cursors.values())
            if retained + size + cell_size > self.cache_bytes:
                self.violation('INPUT_BUFFER_LIMIT')
                raise ValueError('同时打开的网格读取缓存超过容量；按顺序读取并关闭已完成的游标。')
            if rows and size + cell_size > self.block_bytes:
                state['buffered'] = (position, cell, cell_size)
                break
            rows.append(cell)
            size += cell_size
            through = position
        message = {'type': 'READ_CHUNK', 'sequence': sequence, 'rows': rows, 'scanComplete': read['scanComplete']}
        block = {'sequence': sequence, 'fromPosition': read['sentThrough'] + 1, 'throughPosition': through,
                 'count': len(rows), 'sha256': hashlib.sha256(encode(rows)).hexdigest(), 'status': 'PENDING'}
        self._event('BLOCK_PENDING', {'cursor': cursor, **block})
        read['blocks'].append(block)
        read['status'] = 'PENDING'
        state['last'] = message
        state['lastBytes'] = size
        return message, (cursor, block)

    def sent(self, pending):
        if pending is None:
            return
        cursor, block = pending
        state = self._state(cursor)
        self._event('BLOCK_SENT', {'cursor': cursor, 'sequence': block['sequence']})
        block['status'] = 'SENT'
        read = state['read']
        read['sentThrough'] = block['throughPosition']
        read['sentCount'] += block['count']
        read['sendComplete'] = read['scanComplete'] and state['buffered'] is None
        read['status'] = 'COMPLETE' if read['sendComplete'] else 'PARTIAL'
        state['sequence'] += 1

    def close_cursor(self, cursor):
        state = self._state(cursor)
        state['closed'] = True
        state['iterator'].close()
        state['last'], state['lastBytes'], state['buffered'] = None, 0, None
        return {'type': 'READ_CLOSED', 'cursor': cursor}

    def violation(self, code):
        self.manifest['violations'].append(code)
        self._event('VIOLATION', {'code': code})

    def finish(self):
        for state in self.cursors.values():
            state['iterator'].close()
        try:
            self._event('FINAL', {'complete': self.complete})
        finally:
            self.journal.close()
        return deepcopy(self.manifest)

    @property
    def complete(self):
        return self.manifest['inlineComplete'] and not self.manifest['violations'] and all(
            block['status'] == 'SENT' for read in self.manifest['reads'] for block in read['blocks'])
