import { Loader2, CheckCircle, CircleAlert } from 'lucide-react';
import { useState } from 'react';
import type { ReactNode } from 'react';

import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import {
	Dialog,
	DialogContent,
	DialogHeader,
	DialogTitle,
	DialogDescription,
	DialogFooter,
} from '@/components/ui/dialog';
import { useTranslation } from '@/i18n/useI18n';

interface Props {
	open: boolean;
	onOpenChange: (open: boolean) => void;
	title: string;
	description?: ReactNode;
	children?: ReactNode;
	confirmLabel?: string;
	pendingLabel?: string;
	confirmDisabled?: boolean;
	onCloseAutoFocus?: (event: Event) => void;
	onConfirm: () => Promise<void>;
}

export function DeleteDialog({
	open,
	onOpenChange,
	title,
	description,
	children,
	confirmLabel,
	pendingLabel,
	confirmDisabled = false,
	onCloseAutoFocus,
	onConfirm,
}: Props) {
	const { t } = useTranslation();
	const [deleting, setDeleting] = useState(false);
	const [error, setError] = useState('');
	const label = confirmLabel ?? t('common.confirm');
	const changeOpen = (next: boolean) => {
		if (deleting) return;
		setError('');
		onOpenChange(next);
	};

	const handleConfirm = async () => {
		setDeleting(true);
		setError('');
		try {
			await onConfirm();
			onOpenChange(false);
		} catch (reason) {
			setError(reason instanceof Error ? reason.message : String(reason));
		} finally {
			setDeleting(false);
		}
	};

	return (
		<Dialog open={open} onOpenChange={changeOpen}>
			<DialogContent
				className="max-w-sm"
				aria-busy={deleting}
				showCloseButton={!deleting}
				onCloseAutoFocus={onCloseAutoFocus}
			>
				<DialogHeader>
					<DialogTitle>{title}</DialogTitle>
					{description && <DialogDescription>{description}</DialogDescription>}
				</DialogHeader>
				{children}
				{error && (
					<Alert variant="destructive">
						<AlertDescription>{error}</AlertDescription>
					</Alert>
				)}
				<DialogFooter>
					<Button
						type="button"
						variant="ghost"
						onClick={() => changeOpen(false)}
						disabled={deleting}
					>
						<CircleAlert className="size-3.5" />
						{t('common.cancel')}
					</Button>
					<Button
						type="button"
						variant="destructive"
						onClick={handleConfirm}
						disabled={deleting || confirmDisabled}
					>
						{deleting ? (
							<Loader2 className="size-3.5 animate-spin" />
						) : (
							<CheckCircle className="size-3.5" />
						)}
						{deleting ? (pendingLabel ?? label) : label}
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}
