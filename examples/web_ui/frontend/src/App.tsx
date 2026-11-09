import { QueryClientProvider } from '@tanstack/react-query';
import { Onborda, OnbordaProvider } from 'onborda';
import { useMemo, type ReactNode } from 'react';
import { createBrowserRouter, Navigate, RouterProvider } from 'react-router-dom';

import { MCPHubPage } from './pages/mcp';
import { SkillHubPage } from './pages/skill';
import { BrowserBranding } from '@/components/brand/BrowserBranding';
import { RouteError } from '@/components/error/RouteError';
import { AppLayout } from '@/components/layout/AppLayout';
import { buildChatTour } from '@/components/tour/chatTourSteps';
import { TourCard } from '@/components/tour/TourCard';
import { Toaster } from '@/components/ui/sonner';
import { AuthGate } from '@/context/AuthContext';
import { UploadProvider } from '@/context/UploadContext';
import { useTranslation } from '@/i18n/useI18n';
import { isEmbedded } from '@/lib/embed';
import { queryClient } from '@/lib/query-client';
import { AccountPage } from '@/pages/account';
import { AgentsPage, AgentEditorPage } from '@/pages/agents';
import { AnalysisPreviewPage } from '@/pages/analysis-preview';
import { ChannelPage } from '@/pages/channel';
import { ChatPage } from '@/pages/chat';
import { CredentialPage } from '@/pages/credential';
import { KnowledgePage } from '@/pages/knowledge';
import { SchedulePage } from '@/pages/schedule';
import { UsersPage } from '@/pages/users';

const routerBasename = (import.meta.env.BASE_URL || '/').replace(/\/$/, '') || '/';

const router = createBrowserRouter(
	[
	{
		element: <AppLayout />,
		errorElement: <RouteError />,
		children: [
			{
				// Content-level boundary: a crash in a page replaces only
				// the Outlet area, so AppLayout (the icon rail / nav) stays
				// usable. The parent route keeps its own errorElement as a
				// last-resort catch-all for AppLayout/AppSidebar crashes.
				errorElement: <RouteError />,
				children: [
					{ path: '/', element: <Navigate to="/chat" replace /> },
					{
						path: '/chat/:agentId?/:sessionId?/:memberId?',
						element: <ChatPage />,
					},
					{ path: '/schedule', element: <SchedulePage /> },
					{ path: '/agents', element: <AgentsPage /> },
					{ path: '/agents/:agentId', element: <AgentEditorPage /> },
					{ path: '/account', element: <AccountPage /> },
					{ path: '/admin/users', element: <UsersPage /> },
					{
						path: '/analysis-preview/:agentId/:sessionId/:previewRef',
						element: <AnalysisPreviewPage />,
					},
					{ path: '/channel', element: <ChannelPage /> },
					{ path: '/credential', element: <CredentialPage /> },
					{ path: '/mcp', element: <MCPHubPage /> },
					{ path: '/mcp/:hubId', element: <MCPHubPage /> },
					{ path: '/skill', element: <SkillHubPage /> },
					{ path: '/skill/:hubId', element: <SkillHubPage /> },
					{ path: '/knowledge', element: <KnowledgePage /> },
					{ path: '/knowledge/:kbId', element: <KnowledgePage /> },
				],
			},
		],
	},
	{ path: '/setup', element: <Navigate to="/account" replace /> },
	{ path: '/login', element: <Navigate to="/chat" replace /> },
	],
	{ basename: routerBasename },
);

function TourStack({ children }: { children: ReactNode }) {
	const { t } = useTranslation();
	const tours = useMemo(() => [buildChatTour(t)], [t]);
	const embedded = isEmbedded();

	// Keep OnbordaProvider so useOnborda() callers don't crash; skip the
	// Onborda overlay itself in Nuwa FUNCTAB (its z-[900] shield blocks clicks
	// when the tour card ends up off-screen).
	return (
		<OnbordaProvider>
			{embedded ? (
				children
			) : (
				<Onborda
					steps={tours}
					cardComponent={TourCard}
					shadowOpacity="0.6"
					cardTransition={{ type: 'spring', duration: 0.4 }}
				>
					{children}
				</Onborda>
			)}
		</OnbordaProvider>
	);
}

function App() {
	return (
		<QueryClientProvider client={queryClient}>
			<BrowserBranding />
			<AuthGate>
				<TourStack>
					<UploadProvider>
						<RouterProvider router={router} />
					</UploadProvider>
					<Toaster richColors position="top-right" />
				</TourStack>
			</AuthGate>
		</QueryClientProvider>
	);
}

export default App;
