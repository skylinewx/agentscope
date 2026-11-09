import { useEffect, useState, useSyncExternalStore, type ReactNode } from 'react';

import { authApi } from '@/api/auth';
import { getAuth, getAuthNotice, setAuth, subscribeAuth } from '@/api/auth-state';
import { ApiError, appPath, clearSsoSessionToken } from '@/api/client';
import { Button } from '@/components/ui/button';
import { useTranslation } from '@/i18n/useI18n';
import { SetupPage } from '@/pages/setup';

// Shared external-store hook; no mutable React context is required.
// eslint-disable-next-line react-refresh/only-export-components
export const useAuth = () => useSyncExternalStore(subscribeAuth, getAuth);

export function AuthGate({ children }: { children: ReactNode }) {
	const session = useAuth();
	const userId = session?.user.id;
	const { t } = useTranslation();
	const [ready, setReady] = useState(false);
	const [error, setError] = useState(false);
	const [attempt, setAttempt] = useState(0);
	useEffect(() => {
		let cancelled = false;
		authApi
			.me()
			.then((value) => {
				if (!cancelled) {
					setAuth(value);
					setReady(true);
				}
			})
			.catch((e) => {
				if (!cancelled) {
					setError(!(e instanceof ApiError && e.status === 401));
					setReady(true);
				}
			});
		return () => {
			cancelled = true;
		};
	}, [attempt]);
	useEffect(() => {
		const changed = (event: StorageEvent) => {
			if (event.key === 'auth_changed') {
				setAuth(null);
				clearSsoSessionToken();
				window.location.replace(appPath('/login'));
			}
		};
		window.addEventListener('storage', changed);
		return () => window.removeEventListener('storage', changed);
	}, []);
	useEffect(() => {
		if (!userId) return;
		const verify = () => {
			authApi
				.me()
				.then((value) => setAuth(value))
				.catch(() => {
					/* 401 is handled by the client; transient outages keep the current view. */
				});
		};
		const timer = window.setInterval(verify, 60_000);
		window.addEventListener('focus', verify);
		return () => {
			window.clearInterval(timer);
			window.removeEventListener('focus', verify);
		};
	}, [userId]);
	if (!ready)
		return (
			<main className="flex min-h-screen items-center justify-center" role="status">
				{t('auth.checking')}
			</main>
		);
	if (error)
		return (
			<main className="flex min-h-screen flex-col items-center justify-center gap-4">
				<p role="alert">{t('auth.unreachable')}</p>
				<Button
					onClick={() => {
						setError(false);
						setReady(false);
						setAttempt((v) => v + 1);
					}}
				>
					{t('auth.retry')}
				</Button>
			</main>
		);
	if (!session) return <SetupPage sessionInvalid={getAuthNotice() === 'sessionInvalid'} />;
	return <>{children}</>;
}
