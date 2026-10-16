import { useState } from 'react';

import { Button } from '@/components/ui/button';
import { Switch } from '@/components/ui/switch';
import { useTranslation } from '@/i18n/useI18n';

export function ResourceSwitch({
	enabled,
	name,
	onChange,
	disabled = false,
}: {
	enabled: boolean;
	disabled?: boolean;
	name: string;
	onChange: (enabled: boolean) => Promise<unknown>;
}) {
	const { t } = useTranslation();
	const [busy, setBusy] = useState(false);
	const [error, setError] = useState<string | null>(null);
	return (
		<div className="flex flex-col items-end gap-1" onClick={(event) => event.stopPropagation()}>
			<div className="flex items-center gap-2" title={t('resource.switchHelp')}>
				<span className="text-xs text-muted-foreground">
					{t(enabled ? 'resource.enabled' : 'resource.disabled')}
				</span>
				<Switch
					size="sm"
					checked={enabled}
					disabled={busy || disabled}
					aria-label={t('resource.toggle', { name })}
					onCheckedChange={async (value: boolean) => {
						setBusy(true);
						setError(null);
						try {
							await onChange(value);
						} catch (reason) {
							setError(reason instanceof Error ? reason.message : String(reason));
						} finally {
							setBusy(false);
						}
					}}
				/>
			</div>
			{error && (
				<span role="alert" className="max-w-64 text-xs text-destructive">
					{error}
				</span>
			)}
		</div>
	);
}

export function ResourceUpgrade({
	onUpgrade,
	apply = false,
}: {
	onUpgrade: () => Promise<unknown>;
	apply?: boolean;
}) {
	const { t } = useTranslation();
	const [busy, setBusy] = useState(false);
	const [error, setError] = useState<string | null>(null);
	return (
		<div className="flex flex-col items-end gap-1" onClick={(event) => event.stopPropagation()}>
			<Button
				size="sm"
				variant="outline"
				disabled={busy}
				onClick={async () => {
					setBusy(true);
					setError(null);
					try {
						await onUpgrade();
					} catch (reason) {
						setError(reason instanceof Error ? reason.message : String(reason));
					} finally {
						setBusy(false);
					}
				}}
			>
				{t(apply ? 'resource.applyUpgrade' : 'resource.upgrade')}
			</Button>
			{error && (
				<span role="alert" className="max-w-64 text-xs text-destructive">
					{error}
				</span>
			)}
		</div>
	);
}
