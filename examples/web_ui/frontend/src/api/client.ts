import { toast } from 'sonner';

import { getAuth, getAuthSignal, getCsrfToken, handleUnauthorized } from './auth-state';
import { withPublicBase } from '@/lib/public-path';


export const appPath = (path: string) => withPublicBase(path, import.meta.env.BASE_URL || '/');
export const getBaseUrl = () => new URL(appPath('/api/'), window.location.origin).toString();
export const getUserId = () => getAuth()?.user.id ?? '';
export const apiUrl = (path: string) => new URL(path.replace(/^\/+/, ''), getBaseUrl());

/**
 * Structured error thrown for non-2xx HTTP responses.
 * `message` contains the human-readable detail extracted from the backend.
 */
export class ApiError extends Error {
	readonly status: number;
	readonly detail: string;
	readonly code?: string;

	constructor(status: number, detail: string, code?: string) {
		super(detail);
		this.name = 'ApiError';
		this.status = status;
		this.detail = detail;
		this.code = code;
	}
}

interface RequestOptions {
	method?: string;
	body?: unknown;
	params?: Record<string, string>;
	/** When true, suppresses the automatic error toast. Useful when the caller shows its own inline error UI. */
	silent?: boolean;
	signal?: AbortSignal;
	/** Gives up after this many ms and reports {@link TIMEOUT_STATUS}. Off by default — a streaming chat is meant to stay open. */
	timeoutMs?: number;
}

/** Reported when `timeoutMs` elapses. Real 408s come from a server, so either way the request did not complete in time. */
export const TIMEOUT_STATUS = 408;

const SSO_SESSION_KEY = 'agentscope_session';

/** Session token for cross-site iframe embeds where cookies are blocked. */
export function getSsoSessionToken(): string {
	try {
		return sessionStorage.getItem(SSO_SESSION_KEY) || '';
	} catch {
		return '';
	}
}

export function clearSsoSessionToken(): void {
	try {
		sessionStorage.removeItem(SSO_SESSION_KEY);
	} catch {
		/* ignore */
	}
}

export function buildHeaders(hasBody = false): Record<string, string> {
	const headers: Record<string, string> = { 'X-CSRF-Token': getCsrfToken() };
	if (hasBody) headers['Content-Type'] = 'application/json';
	const sso = getSsoSessionToken();
	if (sso) headers['X-Agentscope-Session'] = sso;
	return headers;
}

/** Preserve the backend's error code as well as its human-readable detail. */
async function responseError(res: Response): Promise<ApiError> {
	const text = await res.text();
	try {
		const json = JSON.parse(text) as { detail?: unknown };
		const detail = json.detail;
		if (typeof detail === 'string') return new ApiError(res.status, detail);
		if (detail !== undefined) {
			const code = detail && typeof detail === 'object' && 'code' in detail && typeof detail.code === 'string'
				? detail.code : undefined;
			const message = detail && typeof detail === 'object' && 'message' in detail && typeof detail.message === 'string'
				? detail.message : JSON.stringify(detail);
			return new ApiError(res.status, message, code);
		}
	} catch {
		// not JSON – fall through
	}
	return new ApiError(res.status, text || res.statusText);
}

async function streamRequest(path: string, options: RequestOptions = {}): Promise<Response> {
	const {
		method = 'GET',
		body,
		params,
		signal,
		silent = false,
		timeoutMs,
	} = options;
	const url = apiUrl(path);
	const multipart = body instanceof FormData;
	if (params) {
		Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
	}

	// AbortSignal.timeout aborts with a TimeoutError, which is what lets the
	// catch below tell "too slow" apart from the caller's own cancellation.
	const deadline = timeoutMs ? AbortSignal.timeout(timeoutMs) : undefined;
	const authSignal = getAuthSignal();
	const combined = AbortSignal.any([authSignal, ...(signal ? [signal] : []), ...(deadline ? [deadline] : [])]);

	let res: Response;
	try {
		res = await fetch(url.toString(), {
			method,
			headers: buildHeaders(body !== undefined && !multipart),
			credentials: 'same-origin',
			body: multipart ? body : body !== undefined ? JSON.stringify(body) : undefined,
			signal: combined,
		});
	} catch (e) {
		// An abort is the caller's own doing — pass it through untouched.
		if (e instanceof DOMException && e.name === 'AbortError') throw e;
		// A server that accepts the connection then stalls would otherwise
		// leave the caller waiting forever.
		const timedOut = e instanceof DOMException && e.name === 'TimeoutError';
		// Otherwise fetch only rejects when the request never reached the
		// server: wrong address, DNS failure, refused connection, blocked
		// preflight. Status 0 distinguishes that from any HTTP-level failure.
		const error = timedOut
			? new ApiError(TIMEOUT_STATUS, 'The server took too long to respond.')
			: new ApiError(
					0,
					'Cannot reach the server. Please check your network or contact the administrator.',
				);
		if (!silent) toast.error(error.detail);
		throw error;
	}

	if (!res.ok) {
		const error = await responseError(res);
		// A response from a previous login must not invalidate the current one.
		if (path !== '/auth/login' && !authSignal.aborted) handleUnauthorized(res.status);
		if (!silent) toast.error(error.detail);
		throw error;
	}

	return res;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
	const res = await streamRequest(path, options);
	if (res.status === 204) return undefined as T;
	return res.json() as Promise<T>;
}

export const client = {
	upload: <T>(path: string, body: FormData, params?: Record<string, string>) =>
		request<T>(path, { method: 'POST', body, params, silent: true }),
	get: <T>(
		path: string,
		params?: Record<string, string>,
		options?: { silent?: boolean; signal?: AbortSignal; timeoutMs?: number },
	) => request<T>(path, { method: 'GET', params, ...options }),
	post: <T>(
		path: string,
		body?: unknown,
		params?: Record<string, string>,
		options?: { silent?: boolean; signal?: AbortSignal; timeoutMs?: number },
	) => request<T>(path, { method: 'POST', body, params, ...options }),
	patch: <T>(
		path: string,
		body?: unknown,
		params?: Record<string, string>,
		options?: { silent?: boolean },
	) => request<T>(path, { method: 'PATCH', body, params, silent: options?.silent }),
	delete: <T = void>(
		path: string,
		params?: Record<string, string>,
		options?: { silent?: boolean; signal?: AbortSignal; timeoutMs?: number },
	) => request<T>(path, { method: 'DELETE', params, ...options }),
	stream: (path: string, options?: RequestOptions) => streamRequest(path, options),
};

/** Explicit owner scope used by administrator resource editors only. */
export function resourceClient(owner?: string): typeof client {
  if (!owner) return client;
  const params = (values?: Record<string, string>) => ({ ...values, resource_owner: owner });
  return {
    get: (path, values, options) => client.get(path, params(values), options),
    post: (path, body, values, options) => client.post(path, body, params(values), options),
    patch: (path, body, values, options) => client.patch(path, body, params(values), options),
    delete: (path, values, options) => client.delete(path, params(values), options),
    upload: (path, body, values) => client.upload(path, body, params(values)),
    stream: (path, options) => client.stream(path, { ...options, params: params(options?.params) }),
  };
}
