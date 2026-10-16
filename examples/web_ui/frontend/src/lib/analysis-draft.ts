/** In-memory drafts belong to a server/user/agent/session, never to a card. */
const drafts = new Map<string, string>();
export function draftScope(server: string, user: string, agent: string | null, session: string | null) {
	return JSON.stringify([server, user, agent, session]);
}
export function readDraft(scope: string) { return drafts.get(scope) ?? ''; }
export function writeDraft(scope: string, text: string) { if (text) drafts.set(scope, text); else drafts.delete(scope); }
export function insertDraft(scope: string, expectedScope: string, text: string, append = false): { kind: 'stale' | 'confirm' | 'inserted'; value: string } {
	const value = readDraft(scope);
	if (scope !== expectedScope || !text || text.length > 16000) return { kind: 'stale', value };
	if (value && !append) return { kind: 'confirm', value };
	const next = value ? `${value}\n\n${text}` : text;
	writeDraft(scope, next);
	return { kind: 'inserted', value: next };
}
