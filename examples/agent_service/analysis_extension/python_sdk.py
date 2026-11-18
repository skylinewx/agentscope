"""Container conveniences. Requests data; never issues provenance or review verdicts."""
import threading

if __package__:
    from .execution_protocol import read_message, write_message
else:
    from execution_protocol import read_message, write_message


class InputChannel:
    def __init__(self, source, target):
        self.source, self.target = source, target
        self.lock = threading.RLock()
        self.cursors = set()

    def exchange(self, request):
        with self.lock:
            write_message(self.target, request)
            response = read_message(self.source)
            if response['type'] == 'ERROR':
                raise ValueError(response.get('message', '输入读取失败'))
            return response

    def grid_rows(self, ref, where=None):
        opened = self.exchange({'type': 'READ_OPEN', 'sourceRef': ref, 'where': {} if where is None else where})
        cursor = opened['cursor']
        self.cursors.add(cursor)
        sequence = 0
        try:
            while True:
                block = self.exchange({'type': 'READ_NEXT', 'cursor': cursor, 'sequence': sequence})
                if block.get('type') != 'READ_CHUNK' or block.get('sequence') != sequence:
                    raise ValueError('输入块顺序不一致')
                yield from block['rows']
                if block['scanComplete']:
                    return
                sequence += 1
        finally:
            if cursor in self.cursors:
                self.exchange({'type': 'READ_CLOSE', 'cursor': cursor})
                self.cursors.discard(cursor)

    def close(self):
        for cursor in list(self.cursors):
            self.exchange({'type': 'READ_CLOSE', 'cursor': cursor})
            self.cursors.discard(cursor)
