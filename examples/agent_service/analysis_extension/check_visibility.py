"""Numeric comparisons are business data, independent of technical provenance."""
from .models import decimal_value


def has_difference(comparison):
    difference = comparison.get('difference')
    return difference is not None and decimal_value(difference) != 0


def show_checks(artifact, view, comparison):
    # An explicit model choice takes precedence; never choose business content.
    return view.get('showChecks', has_difference(comparison) or artifact.get('technicalDetails', False))
