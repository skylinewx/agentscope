"""Adapters consume captured source receipts, never caller-supplied amounts."""
"""Example service's source composition; shared code depends on SourceRegistry."""


def default_sources(bindings=None):
    from ..sources import SourceRegistry
    from .budget_v2 import BudgetV2Adapter
    from .json_records import JsonRecordsAdapter
    return SourceRegistry([BudgetV2Adapter, JsonRecordsAdapter], bindings)
