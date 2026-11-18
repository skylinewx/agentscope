"""Container entrypoint. The host alone owns execution authority and delivery evidence."""
import os
from pathlib import Path
import platform
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent))
from execution_protocol import PROTOCOL_VERSION, MAX_BYTES, encode, read_message, write_message
from python_program import evaluate
from python_sdk import InputChannel


def main():
    # Ordinary print and child-process stdout go to the bounded log stream.
    # Raw writes to the protocol remain untrusted and are validated by the host.
    target = os.fdopen(os.dup(sys.stdout.fileno()), 'wb', buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    source = sys.stdin.buffer
    channel = InputChannel(source, target)
    try:
        request = read_message(source)
        if request.get('type') != 'INIT' or request.get('protocolVersion') != PROTOCOL_VERSION:
            raise ValueError('执行协议版本不一致')
        result = evaluate(request['code'], request['inputs'], channel.grid_rows)
        channel.close()
        if len(encode({'result': result})) > MAX_BYTES:
            raise ValueError('Python 输出超过容量上限')
        write_message(target, {'type': 'RESULT', 'result': result,
            'interpreter': {'python': platform.python_version(), 'system': platform.system(), 'architecture': platform.machine()}})
    except BaseException as error:
        frames = traceback.extract_tb(error.__traceback__)
        location = next((frame.lineno for frame in reversed(frames) if frame.filename == '<analysis-python>'), None)
        write_message(target, {'type': 'ERROR', 'error': {'type': type(error).__name__,
            'message': str(error)[:1600], 'line': location}})
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
