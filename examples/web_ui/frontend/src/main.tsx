import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import './index.css';
import './i18n';
import App from './App.tsx';
import { TooltipProvider } from '@/components/ui/tooltip.tsx';
import { AppearanceProvider } from '@/context/AppearanceProvider';

// Polyfill: crypto.randomUUID is unavailable in non-secure contexts (plain http with IP).
// The message-sending path relies on it for client-side message IDs. UUID v4 via Math.random,
// non-cryptographic, sufficient for frontend IDs only.
if (typeof crypto !== 'undefined' && !('randomUUID' in crypto)) {
	(crypto as { randomUUID: () => string }).randomUUID = (): string =>
		'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
			const r = (Math.random() * 16) | 0;
			const v = c === 'x' ? r : (r & 0x3) | 0x8;
			return v.toString(16);
		});
}

createRoot(document.getElementById('root')!).render(
	<StrictMode>
		<AppearanceProvider>
			<TooltipProvider>
				<App />
			</TooltipProvider>
		</AppearanceProvider>
	</StrictMode>,
);
