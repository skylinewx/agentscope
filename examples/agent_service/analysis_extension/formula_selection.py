"""Select traced nodes in model-authored display order, without changing calculations."""


def selected_nodes(trace, view):
    nodes = trace['nodes']
    refs = view.get('nodeRefs')
    if not refs:
        return nodes
    by_ref = {node['factRef']: node for node in nodes}
    return [by_ref[ref] for ref in refs if ref in by_ref]
