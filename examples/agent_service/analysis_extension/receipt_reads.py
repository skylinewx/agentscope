"""Reuse pure reads only while assembling one immutable set of source captures."""
from contextlib import contextmanager
from contextvars import ContextVar


_READS = ContextVar('analysis_receipt_reads', default=None)


@contextmanager
def receipt_read_scope():
    """Synchronous read-only operation; no state survives into another build or owner."""
    token = _READS.set({})
    try:
        yield
    finally:
        _READS.reset(token)


def read_once(kind, sources, read):
    cache = _READS.get()
    if cache is None:
        return read()
    key = (kind, *(id(source) for source in sources))
    if key not in cache:
        # Keep the actual objects alive: equal refs or recycled object ids cannot
        # borrow validation from a different request, response or connection.
        value = read()
        cache[key] = (sources, value)
    return cache[key][1]
