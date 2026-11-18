"""Single projection for new host delivery evidence and read-only legacy execution records."""
from copy import deepcopy

from .execution_inputs import PROVENANCE_MODE, delivery_summary
from .models import AnalysisError


def delivery_reads(run):
    if run.get('provenanceMode') != PROVENANCE_MODE:
        # This is historical evidence, never a new execution fallback.
        return [{**read, 'positionLimit': read['scannedCount'], 'expectedCount': read['matchedCount']}
                for read in run.get('fileReads', [])]
    manifest = run.get('inputDeliveryManifest')
    if (not isinstance(manifest, dict) or manifest.get('provenanceMode') != PROVENANCE_MODE
            or not manifest.get('inlineComplete') or manifest.get('violations')):
        raise AnalysisError('PYTHON_INPUT_INCOMPLETE', '宿主输入提供记录不完整，不能恢复为已核对来源。')
    result = []
    for read in manifest.get('reads', []):
        blocks, position, count = read.get('blocks', []), 0, 0
        for sequence, block in enumerate(blocks):
            if (block.get('status') != 'SENT' or block.get('sequence') != sequence
                    or block.get('fromPosition') != position + 1
                    or type(block.get('throughPosition')) is not int or block['throughPosition'] < position):
                raise AnalysisError('GRID_READ_INTEGRITY', '宿主发送记录存在缺口或未决块。')
            position, count = block['throughPosition'], count + block['count']
        if position != read.get('sentThrough') or count != read.get('sentCount'):
            raise AnalysisError('GRID_READ_INTEGRITY', '宿主发送数量与块记录不一致。')
        result.append({**read, 'positionLimit': position, 'expectedCount': count})
    return result


def delivery_projection(run):
    if run.get('provenanceMode') != PROVENANCE_MODE:
        from .grid_files import read_summary
        return {'provenanceMode': 'LEGACY_WORKER_REPORTED', 'fileReads': read_summary(run.get('fileReads', []))}
    summary = (delivery_summary(run['inputDeliveryManifest']) if 'inputDeliveryManifest' in run
               else deepcopy(run.get('inputDeliverySummary', {})))
    return {**{key: run[key] for key in ('provenanceMode', 'runtimeVersion', 'numericPolicyVersion',
                'imageId', 'inputDeliveryManifestRef') if key in run},
            'inputDeliverySummary': summary}


def project_execution_material(material):
    if not material.get('pythonRuns'):
        return material
    result = dict(material)
    result['pythonRuns'] = []
    for run in material['pythonRuns']:
        if run.get('provenanceMode') != PROVENANCE_MODE:
            result['pythonRuns'].append(run)
            continue
        # Full frozen input records remain; transport journals and operational
        # timings are not duplicated into semantic review or its identity.
        omitted = {'inputDeliveryManifest', 'resourcePolicy', 'effectiveCapacity', 'prepareMs', 'queueMs', 'startupMs',
                   'executeMs', 'cleanupMs', 'cleanupStatus', 'logBytes', 'logTruncated', 'inputRecordExtras',
                   'elapsedMs', 'startedAt', 'executionDeadline', 'interruptedInputJournal'}
        result['pythonRuns'].append({**{key: value for key, value in run.items() if key not in omitted},
                                     **delivery_projection(run)})
    return result
