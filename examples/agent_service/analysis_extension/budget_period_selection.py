"""Validate calendar containment without equating task-opening time with cell time."""
import re


def bounds(code):
    if not isinstance(code, str) or not re.fullmatch(r'\d{4}[NJY]\d{4}', code):
        return None
    year, kind, number = int(code[:4]), code[4], int(code[5:])
    if year < 1000 or not 1 <= number <= {'N': 1, 'J': 4, 'Y': 12}[kind]:
        return None
    width = {'N': 12, 'J': 3, 'Y': 1}[kind]
    start = year * 12 + (number - 1) * width
    return start, start + width


def contains(parent, child):
    if parent == child:
        return parent is not None
    outer, inner = bounds(parent), bounds(child)
    return bool(outer and inner and outer[0] <= inner[0] and inner[1] <= outer[1])


def opening_matches(requested, opening):
    return contains(requested, opening) or contains(opening, requested)


def selected_period(context, row, scope, exact=False):
    if exact:
        return True  # The committed fact's full actual dimensions are checked by sourceSelection validation.
    evidence = context.get('dataPeriodSelection') or {}
    if not evidence:
        return row.get('dataPeriod') == context.get('period')
    if evidence.get('mode') != 'CONTAINED_PREFERRED_GRAIN' or evidence.get('preference') != ['N', 'J', 'Y']:
        return False
    requested = evidence.get('requestedPeriods', [])
    if scope.get('periodCode') and requested != [scope['periodCode']]:
        return False
    request = scope.get('periodRequest') or {}
    point = request.get('start') or {}
    # Absolute PERIOD points need no offset calculation. Preserve their exact requested
    # grain even when the task is opened at its containing annual period.
    if request.get('kind') == 'PERIOD' and point.get('year') is not None:
        period = point.get('period', 1 if point.get('type') == 'N' else None)
        if period is None or requested != [f"{point['year']:04d}{point['type']}{period:04d}"]:
            return False
    actual = row.get('dataPeriod')
    return (actual == row.get('dimensions', {}).get('DATATIME')
            and any(contains(period, actual) for period in requested)
            and any(row.get('factId') in group.get('selectedFactIds', [])
                    and group.get('requestedPeriod') in requested
                    and actual in group.get('selectedPeriods', [])
                    and contains(group['requestedPeriod'], actual) for group in evidence.get('groups', [])))
