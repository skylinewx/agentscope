"""Source-independent selector discovery."""
from .adapters import default_sources


def selectors(capture, sources=None):
    return (sources or default_sources()).for_capture(capture).selectors(capture)
