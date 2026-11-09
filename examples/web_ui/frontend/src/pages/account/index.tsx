import { useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { authApi } from '@/api/auth';
import { setAuth } from '@/api/auth-state';
import { ApiError } from '@/api/client';
import { AppearanceControls } from '@/components/appearance/AppearanceSettings';
import { BrandingSettings } from '@/components/brand/BrandingSettings';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useAuth } from '@/context/AuthContext';
import { useTranslation } from '@/i18n/useI18n';

export function AccountPage() {
	const auth = useAuth();
	const { t } = useTranslation();
	const navigate = useNavigate();
	const [current, setCurrent] = useState('');
	const [password, setPassword] = useState('');
	const [confirm, setConfirm] = useState('');
	const [error, setError] = useState('');
	const [busy, setBusy] = useState(false);
	async function logout() {
		setBusy(true);
		setError('');
		const { clearSsoSessionToken } = await import('@/api/client');
		try {
			await authApi.logout();
			clearSsoSessionToken();
			navigate('/login', { replace: true });
			setAuth(null, true);
		} catch (e) {
			if (e instanceof ApiError && e.status === 401) {
				clearSsoSessionToken();
				setAuth(null, true);
			} else setError(e instanceof Error ? e.message : t('auth.failed'));
		} finally {
			setBusy(false);
		}
	}
	async function changePassword(event: React.FormEvent) {
		event.preventDefault();
		setError('');
		if (password !== confirm) {
			setError(t('auth.passwordMismatch'));
			return;
		}
		setBusy(true);
		try {
			await authApi.password(current, password);
			navigate('/login', { replace: true });
			setAuth(null, true);
		} catch (e) {
			setError(e instanceof Error ? e.message : t('auth.failed'));
		} finally {
			setBusy(false);
		}
	}
	return (
		<main className="h-full overflow-auto p-4 sm:p-6 md:p-10">
			<div className="mx-auto max-w-xl space-y-8">
				<div>
					<h1 className="text-2xl font-semibold">{t('auth.account')}</h1>
					<p className="mt-2 text-muted-foreground">
						{auth?.user.display_name || auth?.user.username} · {auth?.user.username} ·{' '}
						{t(auth?.user.role === 'admin' ? 'auth.admin' : 'auth.user')}
					</p>
				</div>
				<section
					className="rounded-xl border bg-card p-4 shadow-panel sm:p-6"
					aria-labelledby="appearance-heading"
				>
					<h2 id="appearance-heading" className="font-semibold">
						{t('appearance.title')}
					</h2>
					<p className="mb-6 mt-2 text-sm text-muted-foreground">
						{t('appearance.description')}
					</p>
					<AppearanceControls />
				</section>
				<BrandingSettings />
				<section className="rounded-xl border bg-card p-4 shadow-panel sm:p-6">
					<h2 className="mb-4 font-medium">{t('auth.changePassword')}</h2>
					{auth?.user.role === 'admin' ? (
						<p className="text-sm text-muted-foreground">
							{t('auth.adminPasswordHint')}
						</p>
					) : (
						<form className="space-y-4" onSubmit={changePassword}>
							<input
								type="text"
								autoComplete="username"
								value={auth?.user.username ?? ''}
								readOnly
								hidden
							/>
							<div className="space-y-2">
								<Label htmlFor="current-password">
									{t('auth.currentPassword')}
								</Label>
								<Input
									id="current-password"
									type="password"
									required
									autoComplete="current-password"
									maxLength={128}
									value={current}
									onChange={(e) => setCurrent(e.target.value)}
								/>
							</div>
							<div className="space-y-2">
								<Label htmlFor="new-password">{t('auth.newPassword')}</Label>
								<Input
									id="new-password"
									type="password"
									required
									autoComplete="new-password"
									minLength={8}
									maxLength={128}
									value={password}
									onChange={(e) => setPassword(e.target.value)}
								/>
							</div>
							<div className="space-y-2">
								<Label htmlFor="confirm-password">
									{t('auth.confirmPassword')}
								</Label>
								<Input
									id="confirm-password"
									type="password"
									required
									autoComplete="new-password"
									minLength={8}
									maxLength={128}
									value={confirm}
									onChange={(e) => setConfirm(e.target.value)}
								/>
							</div>
							<p className="text-xs text-muted-foreground">
								{t('auth.passwordHint')}
							</p>
							<Button disabled={busy} type="submit">
								{t('auth.changePassword')}
							</Button>
						</form>
					)}
				</section>
				{error && (
					<p role="alert" className="text-sm text-destructive">
						{error}
					</p>
				)}
				<Button variant="outline" disabled={busy} onClick={() => void logout()}>
					{t('auth.logout')}
				</Button>
			</div>
		</main>
	);
}
