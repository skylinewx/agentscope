import { Check, Copy, ExternalLink } from 'lucide-react';
import { useEffect, useState } from 'react';
import type { LinkSafetyModalProps } from 'streamdown';

import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { useTranslation } from '@/i18n/useI18n';
import { copyToClipboard } from '@/utils/common';

/** Portal outside message containment so long, expanded replies cannot move the modal. */
export function LinkSafetyDialog({ isOpen, onClose, onConfirm, url }: LinkSafetyModalProps) {
	const { t } = useTranslation();
	const [copied, setCopied] = useState(false);
	const [copyFailed, setCopyFailed] = useState(false);
	useEffect(() => { setCopied(false); setCopyFailed(false); }, [isOpen, url]);
	return <Dialog open={isOpen} onOpenChange={(open) => { if (!open) onClose(); }}>
		<DialogContent style={{ width: 'min(28rem, calc(100vw - 2rem))' }} data-testid="markdown-link-dialog">
			<DialogHeader>
				<DialogTitle className="flex items-center gap-2"><ExternalLink className="size-5" />{t('markdownLinks.title')}</DialogTitle>
				<DialogDescription>{t('markdownLinks.description')}</DialogDescription>
			</DialogHeader>
			<p className="max-h-32 overflow-y-auto wrap-anywhere rounded-md bg-muted p-3 font-mono text-sm">{url}</p>
			{copyFailed && <p role="alert" className="text-destructive">{t('markdownLinks.copyFailed')}</p>}
			<DialogFooter>
				<Button variant="outline" onClick={async () => { const ok = await copyToClipboard(url); setCopied(ok); setCopyFailed(!ok); }}>
					{copied ? <Check /> : <Copy />}{t(copied ? 'markdownLinks.copied' : 'markdownLinks.copy')}
				</Button>
				<Button onClick={() => { onConfirm(); onClose(); }}><ExternalLink />{t('markdownLinks.open')}</Button>
			</DialogFooter>
		</DialogContent>
	</Dialog>;
}
