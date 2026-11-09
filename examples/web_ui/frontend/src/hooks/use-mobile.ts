import * as React from 'react';

import { isEmbedded } from '@/lib/embed';

const MOBILE_BREAKPOINT = 768;

export function useIsMobile() {
	const [isMobile, setIsMobile] = React.useState<boolean | undefined>(undefined);

	React.useEffect(() => {
		// FUNCTAB iframes are often <768px wide; treating them as mobile opens
		// the Sheet drawer + overlay and blocks clicks on the chat pane.
		if (isEmbedded()) {
			setIsMobile(false);
			return;
		}
		const mql = window.matchMedia(`(max-width: ${MOBILE_BREAKPOINT - 1}px)`);
		const onChange = () => {
			setIsMobile(window.innerWidth < MOBILE_BREAKPOINT);
		};
		mql.addEventListener('change', onChange);
		setIsMobile(window.innerWidth < MOBILE_BREAKPOINT);
		return () => mql.removeEventListener('change', onChange);
	}, []);

	return !!isMobile;
}
