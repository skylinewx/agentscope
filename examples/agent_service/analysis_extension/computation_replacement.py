"""Objective execution changes; the host never infers arbitrary code equivalence."""
from .models import digest


def replacement_changes(previous, current):
    def records(run):
        return {item['ref']: item for item in run.get('inputRecords', []) + run.get('fileInputs', [])}
    old, new = records(previous), records(current)
    return {'previousRunRef': previous['runRef'],
            'addedInputRefs': sorted(new.keys() - old.keys()),
            'removedInputRefs': sorted(old.keys() - new.keys()),
            'changedInputRefs': sorted(ref for ref in old.keys() & new.keys() if digest(old[ref]) != digest(new[ref])),
            'inputOrderChanged': previous.get('inputRefs') != current.get('inputRefs'),
            'codeChanged': previous.get('code') != current.get('code'),
            'purposeChanged': previous.get('purpose') != current.get('purpose'),
            'businessValidation': 'REQUIRES_REVIEW',
            'meaning': '仅记录实际变更，不认定新旧算法语义等价。新执行成功且被交付采用后才替代；仍被使用的旧输出及其依赖继续核对。'}
