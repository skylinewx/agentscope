import { Loader2, Palette, TriangleAlert } from 'lucide-react';
import { useState } from 'react';

import { authApi } from '@/api/auth';
import { setAuth } from '@/api/auth-state';
import { ApiError } from '@/api/client';
import { AppearanceDialog } from '@/components/appearance/AppearanceSettings';
import { AgentScopeLogo } from '@/components/brand/AgentScopeLogo';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useBranding } from '@/context/branding';
import { useTranslation } from '@/i18n/useI18n';
import { DEFAULT_BRANDING } from '@/lib/branding';

export function SetupPage({ sessionInvalid = false }: { sessionInvalid?: boolean }) {
	const { t } = useTranslation();
	const { data: branding = DEFAULT_BRANDING } = useBranding();
	const [username, setUsername] = useState('');
	const [password, setPassword] = useState('');
	const [busy, setBusy] = useState(false);
	const [error, setError] = useState('');

	async function submit(event: React.FormEvent) {
		event.preventDefault();
		setBusy(true);
		setError('');
		try {
			const session = await authApi.login(username.trim(), password);
			setPassword('');
			setAuth(session, true);
		} catch (e) {
			setError(
				e instanceof ApiError && e.status === 0
					? t('auth.unreachable')
					: e instanceof Error
						? e.message
						: t('auth.failed'),
			);
		} finally {
			setBusy(false);
		}
	}

	return (
		<main className="relative flex min-h-screen items-center justify-center bg-canvas px-6 py-24">
			<div className="absolute right-5 top-5">
				<AppearanceDialog>
					<Button variant="outline" size="sm">
						<Palette className="size-4" />
						{t('appearance.title')}
					</Button>
				</AppearanceDialog>
			</div>
			<Card className="w-full max-w-sm">
				<CardHeader className="gap-2">
					<AgentScopeLogo className="mb-2 size-12" />
					<CardTitle>{t('auth.loginTitle', { brand: branding.browser_title })}</CardTitle>
					<CardDescription>{t('auth.loginDescription')}</CardDescription>
				</CardHeader>
				<CardContent>
					{sessionInvalid && (
						<Alert className="mb-5">
							<TriangleAlert />
							<AlertDescription>{t('auth.sessionInvalid')}</AlertDescription>
						</Alert>
					)}
					<form onSubmit={submit} className="space-y-5">
						<div className="space-y-2">
							<Label htmlFor="login-username">{t('auth.username')}</Label>
							<Input
								id="login-username"
								autoComplete="username"
								autoFocus
								required
								maxLength={64}
								value={username}
								onChange={(e) => setUsername(e.target.value)}
								disabled={busy}
							/>
						</div>
						<div className="space-y-2">
							<Label htmlFor="login-password">{t('auth.password')}</Label>
							<Input
								id="login-password"
								type="password"
								autoComplete="current-password"
								required
								maxLength={128}
								value={password}
								onChange={(e) => setPassword(e.target.value)}
								disabled={busy}
							/>
						</div>
						{error && (
							<p role="alert" className="text-sm text-destructive">
								{error}
							</p>
						)}
						<Button type="submit" className="w-full" disabled={busy}>
							{busy && <Loader2 className="size-4 animate-spin" />}
							{t(busy ? 'auth.loggingIn' : 'auth.login')}
						</Button>
						<p className="text-center text-xs text-muted-foreground">
							{t('auth.contactAdmin')}
						</p>
					</form>
				</CardContent>
			</Card>
		</main>
	);
}
