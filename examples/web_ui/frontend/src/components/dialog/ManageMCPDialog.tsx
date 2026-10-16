import { CircleAlert } from 'lucide-react';
import { useEffect, useState } from 'react';

import { mcpApi } from '@/api/mcp';
import type { MCPClient, MCPView } from '@/api/types';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Spinner } from '@/components/ui/spinner';
import { Textarea } from '@/components/ui/textarea';
import { useTranslation } from '@/i18n/useI18n';
import { notifyResourceChanged } from '@/lib/resource-events';

/** Accept both the service client shape and a single standard mcpServers entry. */
function parseClient(raw: string): MCPClient {
	const parsed = JSON.parse(raw);
	if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('Invalid MCP configuration.');
	if (parsed.mcpServers) {
		const entries = Object.entries(parsed.mcpServers as Record<string, Record<string, unknown>>);
		if (entries.length !== 1) throw new Error('Configure one MCP at a time.');
		const [name, config] = entries[0];
		return {
			name,
			is_stateful: typeof config.is_stateful === 'boolean' ? config.is_stateful : !('url' in config),
			mcp_config: { ...config, type: 'url' in config ? 'http_mcp' : 'stdio_mcp' } as MCPClient['mcp_config'],
		};
	}
	if (!parsed.name || !parsed.mcp_config || typeof parsed.is_stateful !== 'boolean') throw new Error('Provide name, is_stateful and mcp_config.');
	return parsed as MCPClient;
}

interface Props {
	record: MCPView | null;
	onClose: () => void;
}

export function ManageMCPDialog({ record, onClose }: Props) {
	const { t } = useTranslation();
	const [raw, setRaw] = useState('');
	const [description, setDescription] = useState(record?.description ?? '');
	const [version, setVersion] = useState('');
	const [loading, setLoading] = useState(Boolean(record));
	const [loadError, setLoadError] = useState<string | null>(null);
	const [error, setError] = useState<string | null>(null);
	const [saving, setSaving] = useState(false);
	const [attempt, setAttempt] = useState(0);

	useEffect(() => {
		if (!record) return;
		let current = true;
		mcpApi.get(record.id).then((detail) => {
			if (current) {
				setRaw(JSON.stringify(detail.client, null, 2));
				setDescription(detail.description);
				setLoadError(null);
			}
		}).catch((reason) => {
			if (current) setLoadError(reason instanceof Error ? reason.message : String(reason));
		}).finally(() => { if (current) setLoading(false); });
		return () => { current = false; };
	}, [record, attempt]);

	const save = async () => {
		if (saving || loading || loadError) return;
		setError(null);
		setSaving(true);
		try {
			const body = { client: parseClient(raw), description, ...(version.trim() ? { version: version.trim() } : {}) };
			if (record) await mcpApi.update(record.id, body, { silent: true });
			else await mcpApi.create(body);
			notifyResourceChanged();
			onClose();
		} catch (reason) {
			setError(reason instanceof Error ? reason.message : String(reason));
		} finally { setSaving(false); }
	};

	return (
		<Dialog open onOpenChange={(open) => { if (!open && !saving) onClose(); }}>
			<DialogContent className="sm:max-w-2xl max-h-[90vh] overflow-y-auto" showCloseButton={!saving}>
				<DialogHeader>
					<DialogTitle>{t(record ? 'resource.manage.editMcp' : 'resource.manage.createMcp')}</DialogTitle>
					<DialogDescription>{t('resource.manage.mcpHelp')}</DialogDescription>
				</DialogHeader>
				{loading ? <Spinner /> : loadError ? (
					<Alert variant="destructive"><CircleAlert /><AlertDescription>{loadError}
						<Button size="sm" variant="ghost" onClick={() => { setLoading(true); setAttempt((value) => value + 1); }}>{t('error.retry')}</Button>
					</AlertDescription></Alert>
				) : (
					<div className="flex flex-col gap-3">
						<Label htmlFor="manage-mcp-config">{t('resource.manage.config')}</Label>
						<Textarea id="manage-mcp-config" className="min-h-72 font-mono text-xs" value={raw} onChange={(event) => setRaw(event.target.value)} disabled={saving}
							placeholder={'{\n  "mcpServers": {\n    "my-mcp": {\n      "command": "your-command",\n      "args": []\n    }\n  }\n}'} />
						{record && <p className="text-xs text-muted-foreground">{t('resource.manage.secretHelp')}</p>}
						<Label htmlFor="manage-mcp-description">{t('resource.manage.description')}</Label>
						<Input id="manage-mcp-description" value={description} onChange={(event) => setDescription(event.target.value)} disabled={saving} />
						<Label htmlFor="manage-mcp-version">{t('resource.manage.version')}</Label>
						<Input id="manage-mcp-version" value={version} onChange={(event) => setVersion(event.target.value)} placeholder={t('resource.manage.autoVersion')} disabled={saving} />
					</div>
				)}
				{error && <Alert variant="destructive"><CircleAlert /><AlertDescription>{error}</AlertDescription></Alert>}
				<DialogFooter>
					<Button variant="outline" disabled={saving} onClick={onClose}>{t('common.cancel')}</Button>
					<Button onClick={save} disabled={saving || loading || Boolean(loadError) || !raw.trim()}>{saving && <Spinner />}{t('common.save')}</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}
