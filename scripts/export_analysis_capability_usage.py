"""按需导出自定义代码的能力缺口，读取现有运行索引，不影响每轮问答。"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3


def export_usage(data_dir, agent_id=None, session_id=None, include_code=False):
    """只读运行记录；失败、取消和未声明原因均保留，不把尝试算成成功。"""
    data_dir = Path(data_dir).resolve()
    results = []
    with closing(sqlite3.connect((data_dir / 'analysis.sqlite3').as_uri() + '?mode=ro', uri=True)) as connection:
        for reference, blob in connection.execute('SELECT ref,digest FROM python_runs'):
            record = json.loads((data_dir / 'blobs' / (blob + '.json')).read_text('utf-8'))
            if agent_id and record.get('agentId') != agent_id:
                continue
            if session_id and record.get('sessionId') != session_id:
                continue
            item = {key: record.get(key) for key in ('agentId', 'sessionId', 'startedAt', 'status', 'elapsedMs',
                                                    'prepareMs', 'executeMs', 'error', 'registrationError')}
            item.update(pythonRunRef=reference,
                        capabilityDecision=record.get('capabilityDecision', {
                            'declaration': 'UNDECLARED', 'processing': record.get('purpose'), 'reason': None}),
                        outputLabels=[value.get('label') for value in record.get('outputs', [])])
            if include_code:
                item['code'] = record.get('code')
            results.append(item)
    return sorted(results, key=lambda item: (item.get('startedAt') or '', item['pythonRunRef']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--agent-id')
    parser.add_argument('--session-id')
    parser.add_argument('--include-code', action='store_true')
    arguments = parser.parse_args()
    print(json.dumps(export_usage(arguments.data_dir, arguments.agent_id, arguments.session_id,
                                  arguments.include_code), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
