import { createContext, useContext } from 'react';

import type { ReplyAddress, SessionAddress } from '@/api/session';
import type { SessionStatus } from '@/api/types';

interface SessionActivityContextValue {
	connected: boolean;
	statuses: ReadonlyMap<string, SessionStatus>;
	unread: ReadonlyMap<string, readonly ReplyAddress[]>;
	registerView: (session: SessionAddress) => () => void;
	markRead: (reply: ReplyAddress) => void;
}

export const SessionActivityContext = createContext<SessionActivityContextValue | null>(null);

export function useSessionActivity() {
	return useContext(SessionActivityContext);
}
