"""Small repair handoff over the saved, complete publication diagnostics."""
from copy import deepcopy

from .build_projection import compact_scope
from .business_tables import encode


def project(value):
    result = deepcopy(value)
    if not result.get('repairRef') or not isinstance(result.get('diagnostics'), dict):
        return result
    analysis_ref = result['analysisRef']
    summary = result.get('summary', {})
    if isinstance(summary.get('scope'), dict):
        summary['scope'] = compact_scope(summary['scope'], analysis_ref)
        # These are the immutable provenance already used to build the analysis,
        # not new repair choices. Their business restrictions remain in scope.
        for key in ('populationEvidence', 'sourceScopes'):
            if key in summary['scope']:
                summary['scope'][key + 'Count'] = len(summary['scope'].pop(key))
    diagnostics = result['diagnostics']
    checks = diagnostics.get('renderChecks')
    if isinstance(checks, dict):
        for viewport in checks.get('viewports', []):
            coverage = viewport.get('valueCoverage')
            if coverage:
                viewport['valueCoverage'] = {key: item for key, item in coverage.items()
                    if key not in ('sourceRefs', 'charted', 'presented')}
                viewport['valueCoverage'].update({key + 'Count': len(coverage[key])
                    for key in ('sourceRefs', 'charted', 'presented') if key in coverage})
            # Repeated markers can yield identical diagnostics. Preserve each
            # distinct issue and its actual occurrence count, not 11 copies.
            for key in ('errors', 'orders'):
                if isinstance(viewport.get(key), list):
                    items = {}
                    for item in viewport[key]:
                        entry = items.setdefault(encode(item), {'item': item, 'count': 0})
                        entry['count'] += 1
                    viewport[key] = [entry['item'] for entry in items.values()]
                    if any(entry['count'] > 1 for entry in items.values()):
                        viewport[key + 'Occurrences'] = [entry['count'] for entry in items.values()]
        details = (diagnostics.get('publicationCheck') or {}).get('details') or {}
        if isinstance(details.get('renderChecks'), dict):
            # Keep any additional check state, while reusing identical measured
            # viewport records from the top-level diagnostics above.
            nested = details['renderChecks']
            if nested.get('viewports') == value['diagnostics']['renderChecks'].get('viewports'):
                nested.pop('viewports', None)
                nested['viewportsAt'] = 'diagnostics.renderChecks.viewports'
    result['savedDiagnosticsRead'] = {'analysisRef': analysis_ref, 'section': 'preview',
                                    'previewRef': result['repairRef']}
    return result
