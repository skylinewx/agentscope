/** Workspace links are file references, never browser-relative navigation. */
export function workspaceFilePath(href: string): string | null {
	let path = href.trim();
	// The delivery contract is explicit: ./ is relative to the workspace root.
	// Existing file:// links and ordinary web links are deliberately not converted.
	if (!path.startsWith('./') || Array.from(path).some((char) => char.charCodeAt(0) < 32)) return null;
	try { path = decodeURIComponent(path.split(/[?#]/, 1)[0]); }
	catch { return null; }
	path = path.replace(/\\/g, '/');
	if ((path.includes(':') || Array.from(path).some((char) => char.charCodeAt(0) < 32)) || path.slice(2).startsWith('/') || path.split('/').includes('..')) return null;
	// Only HTML reports are promoted; ordinary Markdown/document links are unchanged.
	return /\.html?$/i.test(path) ? path : null;
}

type MarkdownNode = {
	type: string;
	url?: string;
	identifier?: string;
	children?: MarkdownNode[];
	data?: Record<string, unknown>;
};

/** Convert links at the AST level so code samples and images stay untouched. */
export function remarkWorkspaceReports() {
	return (tree: MarkdownNode) => {
		const definitions = new Map<string, string>();
		const walk = (node: MarkdownNode, visit: (node: MarkdownNode) => void) => {
			visit(node);
			for (const child of node.children ?? []) walk(child, visit);
		};
		walk(tree, (node) => {
			if (node.type === 'definition' && node.identifier && node.url) definitions.set(node.identifier.toLowerCase(), node.url);
		});
		walk(tree, (node) => {
			const url = node.type === 'link' ? node.url : node.type === 'linkReference' ? definitions.get(node.identifier?.toLowerCase() ?? '') : undefined;
			const path = url ? workspaceFilePath(url) : null;
			if (path) node.data = { ...node.data, hName: 'workspace-report', hProperties: { path: url } };
		});
	};
}

export const REPORT_PREVIEW_MAX_BYTES = 10 * 1024 * 1024;

/** The frame runs report interactions, with no access to the app's origin or network. */
export const REPORT_PREVIEW_CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'";

export function prepareReportPreview(html: string): string {
	const doc = new DOMParser().parseFromString(html, 'text/html');
	// A report cannot replace the preview policy, refresh itself, or change the base URL.
	doc.querySelectorAll('base, meta[http-equiv]').forEach((element) => element.remove());
	const policy = doc.createElement('meta');
	policy.httpEquiv = 'Content-Security-Policy';
	policy.content = REPORT_PREVIEW_CSP;
	doc.head.prepend(policy);
	return '<!doctype html>\n' + doc.documentElement.outerHTML;
}

export function formatReportSize(bytes: number | null | undefined): string | null {
	if (bytes == null || !Number.isFinite(bytes) || bytes < 0) return null;
	if (bytes < 1024) return `${bytes} B`;
	return bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
