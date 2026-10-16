/** Keep library switches and equipped resource panels in sync. */
export const RESOURCE_CHANGED = 'agentscope:resources-changed';

export function notifyResourceChanged() {
	window.dispatchEvent(new Event(RESOURCE_CHANGED));
}
