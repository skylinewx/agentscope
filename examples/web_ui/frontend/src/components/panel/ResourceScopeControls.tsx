import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import type { ResourceScope } from '@/api';
import { ResourceSwitch } from '@/components/hub/ResourceControls';
import { Button } from '@/components/ui/button';

export function ResourceScopeControls({
	resource,
	name,
	onChange,
}: {
	resource: ResourceScope & { enabled: boolean };
	name: string;
	onChange: (enabled: boolean | null) => Promise<unknown>;
}) {
	const { t } = useTranslation();
	const [busy, setBusy] = useState(false);
	const [error, setError] = useState('');
	const local = resource.binding_sources.includes('session');
	return (
		<div className="flex flex-col items-end gap-1">
			<span className="text-xs text-muted-foreground">
				{t(
					local
						? 'resource.sessionAdded'
						: resource.session_override !== null
							? 'resource.sessionOverride'
							: 'resource.inherited',
				)}
			</span>
			{resource.agent_enabled !== null && (
				<span className="text-xs text-muted-foreground">
					{t('resource.agentDefault')}:{' '}
					{t(resource.agent_enabled ? 'resource.enabled' : 'resource.disabled')}
				</span>
			)}
			<ResourceSwitch
				enabled={resource.enabled}
				name={name}
				disabled={!resource.available || busy}
				onChange={onChange}
			/>
			{!local && resource.session_override !== null && (
				<Button
					variant="ghost"
					size="sm"
					disabled={busy}
					onClick={async () => {
						setBusy(true);
						setError('');
						try {
							await onChange(null);
						} catch (reason) {
							setError(reason instanceof Error ? reason.message : String(reason));
						} finally {
							setBusy(false);
						}
					}}
				>
					{t('resource.reset')}
				</Button>
			)}
			{error && (
				<span role="alert" className="text-xs text-destructive">
					{error}
				</span>
			)}
		</div>
	);
}
