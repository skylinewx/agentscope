import { useTranslation } from '@/i18n/useI18n';

/** Revision dates are stored in UTC and displayed in the browser's timezone. */
export function SkillVersionDate({ value }: { value?: string | null }) {
	const { t, i18n } = useTranslation();
	const date = value ? new Date(value) : null;
	if (!date || Number.isNaN(date.getTime())) {
		return <span>{t('skill.dateNotRecorded')}</span>;
	}
	const options: Intl.DateTimeFormatOptions = {
		year: 'numeric', month: '2-digit', day: '2-digit',
		hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
	};
	const locale = i18n.resolvedLanguage || i18n.language;
	return (
		<time dateTime={date.toISOString()} title={date.toLocaleString(locale, { ...options, timeZoneName: 'short' })}>
			{date.toLocaleString(locale, options)}
		</time>
	);
}

export function SkillVersion({ version, updatedAt }: { version: string | null; updatedAt: string | null }) {
	const { t } = useTranslation();
	return (
		<div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-xs text-muted-foreground">
			<span>{t('skill.versionUpdatedLabel')}: <SkillVersionDate value={updatedAt} /></span>
			{version && <span className="min-w-0 break-all">{t('skill.versionLabel')}: <span className="font-mono">{version}</span></span>}
		</div>
	);
}
