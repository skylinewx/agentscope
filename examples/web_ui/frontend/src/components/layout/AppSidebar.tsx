import {
	BookText,
	Bot,
	BotMessageSquare,
	Cable,
	Calendars,
	Compass,
	KeyRound,
	Languages,
	LibraryBig,
	Palette,
	UserRound,
	Users,
} from 'lucide-react';
import { useOnborda } from 'onborda';
import { useNavigate, useLocation } from 'react-router-dom';

import MCPSvg from '@/assets/images/mcp.svg?react';
import { AppearanceDialog } from '@/components/appearance/AppearanceSettings';
import { AgentScopeLogo } from '@/components/brand/AgentScopeLogo';
import { CHAT_TOUR_NAME } from '@/components/tour/chatTourSteps';
import {
	Sidebar,
	SidebarContent,
	SidebarFooter,
	SidebarGroup,
	SidebarGroupContent,
	SidebarHeader,
	SidebarMenu,
	SidebarMenuButton,
	SidebarMenuItem,
} from '@/components/ui/sidebar';
import { useAuth } from '@/context/AuthContext';
import i18n from '@/i18n';
import { useTranslation } from '@/i18n/useI18n';
import { isEmbedded } from '@/lib/embed';

export function AppSidebar() {
	const auth = useAuth();
	const navigate = useNavigate();
	const location = useLocation();
	const { t } = useTranslation();
	const { startOnborda } = useOnborda();
	const embedded = isEmbedded();

	const handleStartTour = () => {
		if (embedded) return;
		if (!location.pathname.startsWith('/chat')) {
			// Page not mounted yet — leave a flag, navigate, and let the
			// ChatTourController auto-trigger after ChatPage mounts.
			sessionStorage.setItem('force_tour', '1');
			navigate('/chat');
		} else {
			startOnborda(CHAT_TOUR_NAME);
		}
	};

	const handleToggleLanguage = () => {
		const next = i18n.language.startsWith('zh') ? 'en' : 'zh';
		i18n.changeLanguage(next);
	};

	return (
		<Sidebar
			collapsible="none"
			className="w-[calc(var(--sidebar-width-icon)+1px)]! border-r border-sidebar-border bg-sidebar"
		>
			<SidebarHeader>
				<AgentScopeLogo className="mt-2" />
			</SidebarHeader>
			<SidebarContent>
				<SidebarGroup>
					<SidebarGroupContent>
						<SidebarMenu>
							<SidebarMenuItem key={'chat'}>
								<SidebarMenuButton
									tooltip={{ children: t('common.chat'), hidden: false }}
									isActive={
										location.pathname === '/chat' ||
										location.pathname.startsWith('/chat/')
									}
									onClick={() => navigate('/chat')}
									className="justify-center"
								>
									<BotMessageSquare />
								</SidebarMenuButton>
							</SidebarMenuItem>
							<SidebarMenuItem>
								<SidebarMenuButton tooltip={{ children: t('common.agentManagement'), hidden: false }} aria-label={t('common.agentManagement')} isActive={location.pathname.startsWith('/agents')} onClick={() => navigate('/agents')} className="justify-center"><Bot /></SidebarMenuButton>
							</SidebarMenuItem>
							<SidebarMenuItem>
								<SidebarMenuButton
									tooltip={{ children: t('common.schedule'), hidden: false }}
									isActive={location.pathname === '/schedule'}
									onClick={() => navigate('/schedule')}
									className="justify-center"
								>
									<Calendars />
								</SidebarMenuButton>
							</SidebarMenuItem>
							<SidebarMenuItem>
								<SidebarMenuButton
									tooltip={{ children: t('common.channel'), hidden: false }}
									isActive={location.pathname === '/channel'}
									onClick={() => navigate('/channel')}
									className="px-2"
								>
									<Cable />
								</SidebarMenuButton>
							</SidebarMenuItem>
						</SidebarMenu>
					</SidebarGroupContent>
				</SidebarGroup>
				<SidebarGroup>
					<SidebarGroupContent>
						<SidebarMenu>
							<SidebarMenuItem>
								<SidebarMenuButton
									tooltip={{ children: t('common.credential'), hidden: false }}
									isActive={location.pathname === '/credential'}
									onClick={() => navigate('/credential')}
									className="justify-center"
								>
									<KeyRound />
								</SidebarMenuButton>
							</SidebarMenuItem>
							<SidebarMenuItem>
								<SidebarMenuButton
									tooltip={{ children: t('common.mcp-hub'), hidden: false }}
									// Stays lit while browsing a hub under /mcp/:hubId.
									isActive={location.pathname.startsWith('/mcp')}
									onClick={() => navigate('/mcp')}
									className="justify-center"
								>
									<MCPSvg />
								</SidebarMenuButton>
							</SidebarMenuItem>
							<SidebarMenuItem>
								<SidebarMenuButton
									tooltip={{ children: t('common.skill-hub'), hidden: false }}
									isActive={location.pathname.startsWith('/skill')}
									onClick={() => navigate('/skill')}
									className="justify-center"
								>
									<BookText />
								</SidebarMenuButton>
							</SidebarMenuItem>
							<SidebarMenuItem>
								<SidebarMenuButton
									tooltip={{ children: t('common.knowledge'), hidden: false }}
									isActive={location.pathname === '/knowledge'}
									onClick={() => navigate('/knowledge')}
									className="justify-center"
								>
									<LibraryBig />
								</SidebarMenuButton>
							</SidebarMenuItem>
						</SidebarMenu>
					</SidebarGroupContent>
				</SidebarGroup>
			</SidebarContent>
			<SidebarFooter>
				<SidebarMenu>
					<SidebarMenuItem>
						<AppearanceDialog>
							<SidebarMenuButton
								tooltip={{ children: t('appearance.title'), hidden: false }}
								aria-label={t('appearance.title')}
								className="justify-center"
							>
								<Palette />
							</SidebarMenuButton>
						</AppearanceDialog>
					</SidebarMenuItem>
					{auth?.user.role === 'admin' && (
						<SidebarMenuItem>
							<SidebarMenuButton
								tooltip={{ children: t('auth.usersTitle'), hidden: false }}
								isActive={location.pathname === '/admin/users'}
								onClick={() => navigate('/admin/users')}
								className="justify-center"
								aria-label={t('auth.usersTitle')}
							>
								<Users />
							</SidebarMenuButton>
						</SidebarMenuItem>
					)}
					<SidebarMenuItem>
						<SidebarMenuButton
							tooltip={{
								children: i18n.language.startsWith('zh')
									? t('common.switchToEn')
									: t('common.switchToZh'),
								hidden: false,
							}}
							onClick={handleToggleLanguage}
							className="justify-center"
						>
							<Languages />
						</SidebarMenuButton>
					</SidebarMenuItem>
					{!embedded && (
						<SidebarMenuItem>
							<SidebarMenuButton
								tooltip={{ children: t('tour.trigger'), hidden: false }}
								onClick={handleStartTour}
								className="justify-center"
							>
								<Compass />
							</SidebarMenuButton>
						</SidebarMenuItem>
					)}
					<SidebarMenuItem>
						<SidebarMenuButton
							tooltip={{ children: t('auth.account'), hidden: false }}
							aria-label={t('auth.account')}
							isActive={location.pathname === '/account'}
							onClick={() => navigate('/account')}
							className="justify-center"
						>
							<UserRound />
						</SidebarMenuButton>
					</SidebarMenuItem>
				</SidebarMenu>
			</SidebarFooter>
		</Sidebar>
	);
}
