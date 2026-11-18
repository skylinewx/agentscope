"""Length-bounded JSON transport. No business authority or executable deserialization."""
import asyncio
import json
import struct

PROTOCOL_VERSION = 'ANALYSIS_STDIO_V2'
MAX_BYTES = 4 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_BYTES + 16384
MAX_DATA_BYTES = 128 * 1024 * 1024  # A historical 32 MiB cell can contain raw + normalized values.
WRITE_CHUNK = 256 * 1024


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


def decode(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('执行协议包含重复字段')
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError('执行协议包含非有限数值')

    value = json.loads(data, object_pairs_hook=unique, parse_constant=invalid_constant)
    if not isinstance(value, dict) or not isinstance(value.get('type'), str):
        raise ValueError('执行协议需要带 type 的对象')
    return value


def read_exact(stream, length):
    chunks = bytearray()
    while len(chunks) < length:
        chunk = stream.read(min(length - len(chunks), WRITE_CHUNK))
        if not chunk:
            raise EOFError('执行协议中断')
        chunks.extend(chunk)
    return bytes(chunks)


def read_message(stream, limit=MAX_DATA_BYTES):
    length = struct.unpack('>Q', read_exact(stream, 8))[0]
    if not 0 < length <= limit:
        raise ValueError('执行协议消息超过容量')
    return decode(read_exact(stream, length))


def write_message(stream, value, limit=MAX_REQUEST_BYTES):
    data = encode(value)
    if len(data) > limit:
        raise ValueError('执行协议消息超过容量')
    stream.write(struct.pack('>Q', len(data)))
    for position in range(0, len(data), WRITE_CHUNK):
        stream.write(data[position:position + WRITE_CHUNK])
    stream.flush()


async def read_async(stream):
    length = struct.unpack('>Q', await stream.readexactly(8))[0]
    if not 0 < length <= MAX_REQUEST_BYTES:
        raise ValueError('执行协议消息超过容量')
    return decode(await stream.readexactly(length))


async def write_async(stream, value):
    data = encode(value)
    if len(data) > MAX_DATA_BYTES:
        raise ValueError('执行协议消息超过容量')
    stream.write(struct.pack('>Q', len(data)))
    for position in range(0, len(data), WRITE_CHUNK):
        stream.write(data[position:position + WRITE_CHUNK])
        await stream.drain()
