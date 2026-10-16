import { CircleAlert, FilePlus, Trash2 } from 'lucide-react';
import { useEffect, useState } from 'react';

import { skillApi } from '@/api/skill';
import type { SkillFile, SkillView } from '@/api/types';
import { SkillVersion } from '@/components/hub/SkillVersion';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Spinner } from '@/components/ui/spinner';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Textarea } from '@/components/ui/textarea';
import { useTranslation } from '@/i18n/useI18n';
import { notifyResourceChanged } from '@/lib/resource-events';

const TEMPLATE = '---\nname: my-skill\ndescription: Describe when this skill should be used.\n---\n\n# Instructions\n\nDescribe the steps for the agent.\n';

interface Props {
	record: SkillView | null;
	onClose: () => void;
}

export function ManageSkillDialog({ record, onClose }: Props) {
	const { t } = useTranslation();
	const [mode, setMode] = useState(record && !record.package_saved ? 'folder' : 'edit');
	const [paths, setPaths] = useState<SkillFile[]>(record ? [] : [{ path: 'SKILL.md', size: 0, editable: true }]);
	const [contents, setContents] = useState<Record<string, string>>(record ? {} : { 'SKILL.md': TEMPLATE });
	const [changes, setChanges] = useState<Record<string, string | null>>(record ? {} : { 'SKILL.md': TEMPLATE });
	const [active, setActive] = useState('SKILL.md');
	const [newPath, setNewPath] = useState('');
	const [version, setVersion] = useState('');
	const [folder, setFolder] = useState<File[]>([]);
	const [archive, setArchive] = useState<File | null>(null);
	const [loading, setLoading] = useState(Boolean(record?.package_saved));
	const [reading, setReading] = useState(false);
	const [loadError, setLoadError] = useState<string | null>(null);
	const [error, setError] = useState<string | null>(null);
	const [saving, setSaving] = useState(false);
	const [attempt, setAttempt] = useState(0);

	useEffect(() => {
		if (!record?.package_saved) return;
		let current = true;
		Promise.all([skillApi.files(record.id), skillApi.readFile(record.id, 'SKILL.md')]).then(([files, markdown]) => {
			if (current) {
				setPaths(files);
				setContents({ 'SKILL.md': markdown.content });
				setLoadError(null);
			}
		}).catch((reason) => { if (current) setLoadError(reason instanceof Error ? reason.message : String(reason)); })
			.finally(() => { if (current) setLoading(false); });
		return () => { current = false; };
	}, [record, attempt]);

	const openFile = async (path: string) => {
		if (saving || reading) return;
		setActive(path);
		setError(null);
		if (path in contents || path in changes || !record) return;
		setReading(true);
		try {
			const file = await skillApi.readFile(record.id, path);
			setContents((previous) => ({ ...previous, [path]: file.content }));
		} catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
		finally { setReading(false); }
	};

	const addFile = () => {
		const path = newPath.trim();
		if (!path || /[\\:\0]/.test(path) || path.split('/').some((part) => !part || part === '.' || part === '..') || paths.some((file) => file.path.toLowerCase() === path.toLowerCase())) {
			setError(t('resource.manage.invalidPath'));
			return;
		}
		setPaths((previous) => [...previous, { path, size: 0, editable: true }]);
		setContents((previous) => ({ ...previous, [path]: '' }));
		setChanges((previous) => ({ ...previous, [path]: '' }));
		setActive(path);
		setNewPath('');
		setError(null);
	};

	const save = async () => {
		if (saving) return;
		setSaving(true);
		setError(null);
		try {
			const revision = version.trim() || undefined;
			if (mode === 'folder') await skillApi.uploadFolder(folder, record?.id, revision);
			else if (mode === 'archive' && archive) await skillApi.uploadArchive(archive, record?.id, revision);
			else {
				const body = { files: changes, ...(revision ? { version: revision } : {}) };
				if (record) await skillApi.saveFiles(record.id, body);
				else await skillApi.create(body);
			}
			notifyResourceChanged();
			onClose();
		} catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
		finally { setSaving(false); }
	};

	const current = changes[active] ?? contents[active] ?? '';
	const loaded = active in contents || typeof changes[active] === 'string';
	const valid = mode === 'folder' ? folder.length > 0 : mode === 'archive' ? Boolean(archive) : !loading && !reading && !loadError && loaded && Object.keys(changes).length > 0;

	return (
		<Dialog open onOpenChange={(open) => { if (!open && !saving) onClose(); }}>
			<DialogContent className="sm:max-w-4xl max-h-[90vh] overflow-y-auto" showCloseButton={!saving}>
				<DialogHeader>
					<DialogTitle>{t(record ? 'resource.manage.editSkill' : 'resource.manage.createSkill')}</DialogTitle>
					<DialogDescription>{t(record ? 'resource.manage.skillEditHelp' : 'resource.manage.skillCreateHelp')}</DialogDescription>
				</DialogHeader>
				{record && <SkillVersion version={record.version} updatedAt={record.version_updated_at} />}
				<Tabs value={mode} onValueChange={setMode}>
					<TabsList className="w-full">
						<TabsTrigger disabled={saving || Boolean(record && !record.package_saved)} value="edit" className="flex-1">{t('resource.manage.files')}</TabsTrigger>
						<TabsTrigger disabled={saving} value="folder" className="flex-1">{t('resource.manage.folder')}</TabsTrigger>
						<TabsTrigger disabled={saving} value="archive" className="flex-1">{t('resource.manage.archive')}</TabsTrigger>
					</TabsList>
					<TabsContent value="edit" className="mt-3 space-y-3">
						{loading ? <Spinner /> : loadError ? (
							<Alert variant="destructive"><CircleAlert /><AlertDescription>{loadError}
								<Button size="sm" variant="ghost" onClick={() => { setLoading(true); setAttempt((value) => value + 1); }}>{t('error.retry')}</Button>
							</AlertDescription></Alert>
						) : (
							<>
								<div className="flex gap-3">
									<div className="w-44 shrink-0 max-h-80 overflow-y-auto space-y-1">
										{paths.map((file) => <Button key={file.path} variant={file.path === active ? 'secondary' : 'ghost'} size="sm" className="w-full justify-start" disabled={!file.editable || saving || reading} onClick={() => openFile(file.path)} title={file.editable ? file.path : t('resource.manage.binaryHelp')}>
											<span className="truncate font-mono text-xs">{file.path}</span>
										</Button>)}
									</div>
									<div className="flex-1 min-w-0 space-y-2">
										<div className="flex items-center justify-between gap-2">
											<Label htmlFor="manage-skill-text" className="truncate">{active}</Label>
											<Button variant="ghost" size="icon-sm" title={t('resource.manage.deleteFile')} disabled={active === 'SKILL.md' || saving || reading} onClick={() => {
												setChanges((previous) => ({ ...previous, [active]: null }));
												setPaths((previous) => previous.filter((file) => file.path !== active));
												setActive('SKILL.md');
												setError(null);
											}}><Trash2 /></Button>
										</div>
										{reading ? <Spinner /> : <Textarea id="manage-skill-text" className="min-h-72 max-h-100 font-mono text-xs" value={current} disabled={!loaded || saving} onChange={(event) => setChanges((previous) => ({ ...previous, [active]: event.target.value }))} />}
									</div>
								</div>
								<div className="flex gap-2">
									<Input aria-label={t('resource.manage.filePath')} placeholder="scripts/run.py" value={newPath} onChange={(event) => setNewPath(event.target.value)} disabled={saving || reading} />
									<Button variant="outline" onClick={addFile} disabled={saving || reading}><FilePlus />{t('resource.manage.addFile')}</Button>
								</div>
							</>
						)}
					</TabsContent>
					<TabsContent value="folder" className="mt-3 space-y-3">
						<Label htmlFor="manage-skill-folder">{t('resource.manage.folderHelp')}</Label>
						<Input id="manage-skill-folder" type="file" disabled={saving} {...({ webkitdirectory: '', directory: '' } as Record<string, string>)} onChange={(event) => { setFolder(Array.from(event.target.files ?? [])); setError(null); }} />
						<div className="max-h-64 overflow-y-auto text-xs font-mono">{folder.map((file) => <div key={file.webkitRelativePath}>{file.webkitRelativePath || file.name}</div>)}</div>
					</TabsContent>
					<TabsContent value="archive" className="mt-3 space-y-3">
						<Label htmlFor="manage-skill-archive">{t('resource.manage.archiveHelp')}</Label>
						<Input id="manage-skill-archive" type="file" accept=".zip,.tar,.tar.gz,.tgz" disabled={saving} onChange={(event) => { setArchive(event.target.files?.[0] ?? null); setError(null); }} />
					</TabsContent>
				</Tabs>
				<Label htmlFor="manage-skill-version">{t('resource.manage.version')}</Label>
				<Input id="manage-skill-version" value={version} onChange={(event) => setVersion(event.target.value)} placeholder={t('resource.manage.autoVersion')} disabled={saving} />
				{error && <Alert variant="destructive"><CircleAlert /><AlertDescription>{error}</AlertDescription></Alert>}
				<DialogFooter>
					<Button variant="outline" disabled={saving} onClick={onClose}>{t('common.cancel')}</Button>
					<Button onClick={save} disabled={saving || !valid}>{saving && <Spinner />}{t('common.save')}</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}
