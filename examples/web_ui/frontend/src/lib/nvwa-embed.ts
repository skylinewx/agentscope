import { getBaseUrl } from '@/api/client';

const BOOTSTRAP_KEY = 'nvwa_sso_bootstrap';

function pageParams(): URLSearchParams {
	const merged = new URLSearchParams(window.location.search);
	const hash = window.location.hash || '';
	const queryAt = hash.indexOf('?');
	if (queryAt >= 0) {
		new URLSearchParams(hash.slice(queryAt + 1)).forEach((value, key) => {
			if (value.trim() && !merged.has(key)) merged.set(key, value.trim());
		});
	}
	return merged;
}

function credential(params: URLSearchParams): { key: string; value: string } | undefined {
	for (const key of ['ticket', 'JTOKENID', 'nv_t', 'token']) {
		const value = params.get(key)?.trim();
		if (value) return { key, value };
	}
	return undefined;
}

/**
 * Send the ticket already on this page to `/api/sso/nvwa`.
 * Empty when the page has no ticket, or this load already tried.
 */
export function bootstrapNvwaSsoUrl(): string {
	const params = pageParams();
	const found = credential(params);
	if (!found) return '';
	try {
		if (sessionStorage.getItem(BOOTSTRAP_KEY) === found.value) return '';
	} catch {
		return '';
	}
	const url = new URL('sso/nvwa', getBaseUrl());
	url.searchParams.set(found.key, found.value);
	const next = params.get('next')?.trim();
	if (next) url.searchParams.set('next', next);
	try {
		sessionStorage.setItem(BOOTSTRAP_KEY, found.value);
	} catch {
		return '';
	}
	return url.toString();
}

export function clearNvwaSsoBootstrap(): void {
	try {
		sessionStorage.removeItem(BOOTSTRAP_KEY);
	} catch {
		/* ignore */
	}
}
