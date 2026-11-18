import { queryClient } from '@/lib/query-client';

export interface AuthUser {
	id: string;
	username: string;
	display_name: string;
	role: 'admin' | 'user';
	status: 'active' | 'disabled' | 'deleted';
	created_at: string;
	updated_at: string;
	last_login_at: string | null;
}
export interface AuthSession {
	user: AuthUser;
	csrf_token: string;
}

let session: AuthSession | null = null;
let notice: 'sessionInvalid' | null = null;
let controller = new AbortController();
const listeners = new Set<() => void>();
export const getAuth = () => session;
export const getAuthNotice = () => notice;
export const getAuthSignal = () => controller.signal;
export const getCsrfToken = () => session?.csrf_token ?? '';
export const subscribeAuth = (listener: () => void) => {
	listeners.add(listener);
	return () => {
		listeners.delete(listener);
	};
};

export function setAuth(value: AuthSession | null, broadcast = false, nextNotice: typeof notice = null) {
	if (session?.user.id !== value?.user.id || session?.csrf_token !== value?.csrf_token) {
		controller.abort();
		controller = new AbortController();
		queryClient.clear();
		localStorage.removeItem('chat_last_agent');
		localStorage.removeItem('chat_last_session');
	}
	session = value;
	notice = value ? null : nextNotice;
	localStorage.removeItem('server_url');
	localStorage.removeItem('username');
	listeners.forEach((listener) => listener());
	// Only an event marker is persisted; credentials and user identity stay in memory.
	if (broadcast) localStorage.setItem('auth_changed', `${Date.now()}:${Math.random()}`);
}

export function handleUnauthorized(status: number) {
	if (status === 401 && session) setAuth(null, true, 'sessionInvalid');
}
