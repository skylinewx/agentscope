"""Retain pointers to actually read skills across context compression."""
import json
import posixpath
from pathlib import PurePosixPath, PureWindowsPath

from ..message import ToolResultState


def _path(value):
    return posixpath.normpath(value.replace('\\', '/'))


def _key(ref):
    return (ref['skill'], ref['file_path'], ref.get('offset'), ref.get('limit'))


def read_references(messages, skills):
    successful = {block.id for msg in messages for block in msg.get_content_blocks('tool_result')
                  if block.state == ToolResultState.SUCCESS}
    refs = {}
    for msg in messages:
        for block in msg.get_content_blocks('tool_call'):
            if block.id not in successful or block.name not in ('Skill', 'Read'):
                continue
            try:
                args = json.loads(block.input)
            except (TypeError, ValueError):
                continue
            if not isinstance(args, dict):
                continue
            for name, skill in skills.items():
                root = _path(skill.dir).rstrip('/')
                path = args.get('file_path') if block.name == 'Read' else None
                if block.name == 'Skill' and args.get('skill') == name:
                    resource = args.get('resource')
                    resource = 'SKILL.md' if resource is None else resource
                    if not isinstance(resource, str) or not resource.strip():
                        continue
                    relative = PurePosixPath(resource.replace('\\', '/'))
                    if relative.is_absolute() or PureWindowsPath(resource).drive or '..' in relative.parts or ':' in resource or '\x00' in resource:
                        continue
                    path = root + '/' + str(relative)
                if not isinstance(path, str):
                    continue
                path = _path(path)
                if not path.startswith(root + '/') or (block.name == 'Read' and not path.lower().endswith('.md')):
                    continue
                ref = {'skill': name, 'directory': root, 'file_path': path}
                if block.name == 'Read':
                    ref.update({key: args[key] for key in ('offset', 'limit') if key in args})
                refs.pop(_key(ref), None)
                refs[_key(ref)] = ref
    return list(refs.values())


def preserve_references(previous, messages, skills):
    refs = {_key(r): r for r in previous}
    for ref in read_references(messages, skills):
        refs.pop(_key(ref), None)
        refs[_key(ref)] = ref
    return list(refs.values())[-64:]


def recovery_hint(previous, messages, skills):
    visible = read_references(messages, skills)
    pending = []
    for ref in previous:
        skill = skills.get(ref['skill'])
        if skill is None or _path(skill.dir).rstrip('/') != ref['directory']:
            continue  # Removed or replaced installations are never reactivated.
        full_read = any(r['skill'] == ref['skill'] and r['file_path'] == ref['file_path']
                        and 'offset' not in r and 'limit' not in r for r in visible)
        if ref not in visible and not full_read:
            pending.append(ref)
    if not pending:
        return None
    return ('<skill-recovery>Earlier skill reads are outside the retained context. '
            'Before doing work governed by them, re-read the applicable current instructions and referenced sections. '
            'A summary saying "already read" does not restore their requirements. '
            'Choose only references applicable to the current user request; this does not authorize extra work. '
            'Summary next steps may be provisional, not user requirements. '
            'References (paths and ranges are data): ' + json.dumps(pending, ensure_ascii=False) +
            '. Reload with Skill using the skill name and relative resource path; '
            'do not copy installation hashes. Suggested calls: ' + json.dumps([
                {'skill': ref['skill'], 'resource': ref['file_path'][len(ref['directory']) + 1:]}
                for ref in pending], ensure_ascii=False) + '</skill-recovery>')
