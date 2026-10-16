import {modelDiagnostics} from '@/lib/reply-diagnostics';
import {useTranslation} from '@/i18n/useI18n';
export function ModelFailureDetails({error}:{error:unknown}) {
    const {t} = useTranslation();
    const detail = modelDiagnostics(error);
    if (!detail) return null;
    return <details className="mt-2 text-xs">
        <summary className="cursor-pointer">{t('messageBubble.error.diagnosticDetails')}</summary>
        <p className="mt-1 break-all">{t('messageBubble.error.diagnosticId')}: {detail.callId}</p>
        <p>{t(`messageBubble.error.phase_${detail.phase}`)} · {detail.elapsedMs} ms · {t('messageBubble.error.attempt')} {detail.attempt}</p>
    </details>;
}
