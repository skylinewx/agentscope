import { toast } from 'sonner';

import { getAuth, getAuthSignal, getCsrfToken, handleUnauthorized } from './auth-state';

export const getBaseUrl = () => new URL('/api/', window.location.origin).toString();
export const getUserId = () => getAuth()?.user.id ?? '';
export const apiUrl = (path: string) => new URL(path.replace(/^\/+/, ''), getBaseUrl());

/**
 * Structured error thrown for non-2xx HTTP responses.
 * `message` contains the human-readable detail extracted from the backend.
 */
export class ApiError extends Error {
	readonly status: number;
	readonly detail: string;

	constructor(status: number, detail: string) {
		super(detail);
		this.name = 'ApiError';
		this.status = status;
		this.detail = detail;
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

function buildHeaders(hasBody: boolean): Record<string, string> {
	const headers: Record<string, string> = { 'X-CSRF-Token': getCsrfToken() };
	if (hasBody) headers['Content-Type'] = 'application/json';
	return headers;
}

/** Parse the response body and extract the `detail` field if the backend returned JSON. */
async function extractErrorDetail(res: Response): Promise<string> {
	const text = await res.text();
	try {
		const json = JSON.parse(text) as { detail?: unknown };
		if (typeof json.detail === 'string') return json.detail;
		if (json.detail && typeof json.detail === 'object' && 'message' in json.detail
			&& typeof json.detail.message === 'string') return json.detail.message;
		if (json.detail !== undefined) return JSON.stringify(json.detail);
	} catch {
		// not JSON – fall through
	}
	return text || res.statusText;
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
		const detail = await extractErrorDetail(res);
		// A response from a previous login must not invalidate the current one.
		if (path !== '/auth/login' && !authSignal.aborted) handleUnauthorized(res.status);
		const error = new ApiError(res.status, detail);
		if (!silent) toast.error(detail);
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
	delete: <T = void>(path: string, params?: Record<string, string>) =>
		request<T>(path, { method: 'DELETE', params }),
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
    delete: (path, values) => client.delete(path, params(values)),
    upload: (path, body, values) => client.upload(path, body, params(values)),
    stream: (path, options) => client.stream(path, { ...options, params: params(options?.params) }),
  };
}
