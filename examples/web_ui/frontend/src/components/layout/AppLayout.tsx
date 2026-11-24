import { Outlet } from 'react-router-dom';

import { AppSidebar } from '@/components/layout/AppSidebar';
import { SidebarInset, SidebarProvider } from '@/components/ui/sidebar';
import { SessionActivityProvider } from '@/context/SessionActivityProvider';
import { isEmbedded } from '@/lib/embed';

export function AppLayout() {
	const embedded = isEmbedded();
	return (
		<SessionActivityProvider>
			<div className="h-screen flex">
				<SidebarProvider>
					{!embedded && <AppSidebar />}
					<SidebarInset className="flex-1 overflow-hidden bg-canvas">
						<Outlet />
					</SidebarInset>
				</SidebarProvider>
			</div>
		</SessionActivityProvider>
	);
}
