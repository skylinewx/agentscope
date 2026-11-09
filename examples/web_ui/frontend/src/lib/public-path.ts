/** Apply the deployment prefix to application routes and API endpoints. */
export function withPublicBase(path: string, base: string): string {
	return `${base.replace(/\/+$/, '')}/${path.replace(/^\/+/, '')}`;
}
