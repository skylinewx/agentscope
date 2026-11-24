import { CircleAlert, Clock3, Ellipsis, Pencil, Trash2, type LucideIcon } from 'lucide-react';
import { useId } from 'react';

import type { SessionStatus } from '@/api/types';
import { Checkbox } from '@/components/ui/checkbox';
import {
	DropdownMenu,
	DropdownMenuContent,
	DropdownMenuItem,
	DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Label } from '@/components/ui/label';
import {
	SidebarMenuAction,
	SidebarMenuBadge,
	SidebarMenuButton,
	SidebarMenuItem,
} from '@/components/ui/sidebar';
import { Spinner } from '@/components/ui/spinner';
import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';

interface SessionListItemProps {
	name: string;
	timestamp: string;
	status: SessionStatus;
	unread: boolean;
	active: boolean;
	icon?: LucideIcon;
	selection?: { checked: boolean; onChange: (checked: boolean) => void };
	onOpen: () => void;
	onRename: () => void;
	onDelete: () => void;
}

export function SessionListItem({
	name,
	timestamp,
	status,
	unread,
	active,
	icon: Icon,
	selection,
	onOpen,
	onRename,
	onDelete,
}: SessionListItemProps) {
	const { t } = useTranslation();
	const descriptionId = useId();
	const checkboxId = `${descriptionId}-select`;
	const statusText = status === 'idle' ? '' : t(`chat.session.status.${status}`);
	const description = [statusText, unread && t('chat.session.unreadResult')]
		.filter(Boolean)
		.join(', ');
	const indicator = status === 'idle' ? (unread ? 'unread' : undefined) : status;
	const title = (
		<>
			{Icon && <Icon aria-hidden="true" />}
			<span className="truncate">{name}</span>
		</>
	);
	return (
		<SidebarMenuItem>
			{selection ? (
				<SidebarMenuButton
					asChild
					isActive={active}
					className="cursor-pointer pr-8 font-normal text-muted-foreground hover:text-foreground md:pr-[5.5rem]"
				>
					<Label htmlFor={checkboxId}>
						<Checkbox
							id={checkboxId}
							className="after:hidden"
							checked={selection.checked}
							aria-describedby={description ? descriptionId : undefined}
							onCheckedChange={(checked) => selection.onChange(checked === true)}
						/>
						{title}
					</Label>
				</SidebarMenuButton>
			) : (
				<SidebarMenuButton
					className="text-muted-foreground hover:text-foreground group-has-data-[sidebar=menu-action]/menu-item:pr-12 md:group-has-data-[sidebar=menu-action]/menu-item:pr-[5.5rem]"
					isActive={active}
					aria-current={active ? 'page' : undefined}
					aria-describedby={description ? descriptionId : undefined}
					onClick={onOpen}
				>
					{title}
				</SidebarMenuButton>
			)}
			<SidebarMenuBadge
				className={cn(
					'justify-start gap-1 text-text-tertiary! md:right-1 md:w-20',
					selection ? 'right-1 top-1.5' : 'right-7',
				)}
			>
				<span
					data-session-indicator={indicator}
					aria-hidden="true"
					className="grid size-4 shrink-0 place-items-center text-primary"
				>
					{status === 'running' && (
						<Spinner
							aria-hidden="true"
							className="size-3.5 motion-reduce:animate-none"
						/>
					)}
					{status === 'awaiting_permission' && <CircleAlert className="size-3.5" />}
					{status === 'awaiting_external_result' && <Clock3 className="size-3.5" />}
					{indicator === 'unread' && (
						<span className="size-1.5 rounded-full bg-teal-700 dark:bg-teal-400" />
					)}
				</span>
				<span
					className={cn(
						'ml-auto hidden whitespace-nowrap font-mono md:inline',
						!selection &&
							'group-hover/menu-item:invisible group-has-focus-visible/menu-item:invisible group-has-data-[state=open]/menu-item:invisible',
					)}
				>
					{timestamp}
				</span>
				{description && (
					<span className="sr-only" id={descriptionId}>
						{description}
					</span>
				)}
			</SidebarMenuBadge>
			{!selection && (
				<DropdownMenu>
					<DropdownMenuTrigger asChild>
						<SidebarMenuAction
							aria-label={t('chat.session.actions')}
							className="md:opacity-0 group-hover/menu-item:opacity-100 group-has-focus-visible/menu-item:opacity-100 aria-expanded:opacity-100 peer-data-active/menu-button:text-sidebar-accent-foreground"
						>
							<Ellipsis />
						</SidebarMenuAction>
					</DropdownMenuTrigger>
					<DropdownMenuContent className="w-auto" side="right" align="start">
						<DropdownMenuItem onClick={onRename}>
							<Pencil />
							{t('session-menu.rename')}
						</DropdownMenuItem>
						<DropdownMenuItem variant="destructive" onClick={onDelete}>
							<Trash2 />
							{t('session-menu.delete')}
						</DropdownMenuItem>
					</DropdownMenuContent>
				</DropdownMenu>
			)}
		</SidebarMenuItem>
	);
}
