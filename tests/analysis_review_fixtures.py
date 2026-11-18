"""A valid review response for host-control tests; semantics use separate cases."""
from copy import deepcopy


def with_coverage(verdict):
    result = deepcopy(verdict)
    result.setdefault('coverage', [{'requirement': '核对当前用户要求', 'status': 'COVERED',
                                   'location': '本阶段完整材料', 'evidenceRefs': []}])
    return result
