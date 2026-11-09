/** True when running inside a host frame (e.g. Nuwa FUNCTAB url-app iframe). */
export function isEmbedded(): boolean {
	try {
		if (window.parent !== window) return true;
	} catch {
		return true;
	}
	try {
		return sessionStorage.getItem('agentscope_embed') === '1';
	} catch {
		return false;
	}
}
