import type { ContentBlock, Msg } from '@agentscope-ai/agentscope/message';
import { ChevronDown, ChevronUp, ListOrdered, Paperclip, Pause, Pencil, Play, Trash2, X } from 'lucide-react';
import { useRef, useState } from 'react';
import { toast } from 'sonner';

import type { QueuedMessage } from '@/api/message-queue';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { Item, ItemActions, ItemContent, ItemDescription, ItemGroup, ItemMedia } from '@/components/ui/item';
import { Textarea } from '@/components/ui/textarea';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import type { MessageQueueController } from '@/hooks/useMessageQueue';
import { useTranslation } from '@/i18n/useI18n';

const messageText = (message: Msg) => message.content.filter((x) => x.type === 'text').map((x) => x.text).join('\n');
const iconClass = 'size-8 text-muted-foreground [@media(pointer:coarse)]:size-11';

export function MessageQueue({ controller, disabled = false }: { controller: MessageQueueController; disabled?: boolean }) {
	const { t, i18n } = useTranslation();
	const [open, setOpen] = useState(true);
	const [all, setAll] = useState(false);
	const [busy, setBusy] = useState(false);
	const [editing, setEditing] = useState<{ item: QueuedMessage; text: string; attachments: ContentBlock[] } | null>(null);
	const header = useRef<HTMLButtonElement>(null);
	const queue = controller.state;
	const locked = disabled || busy;

	const perform = async (work: () => Promise<void>) => {
		if (locked) return;
		setBusy(true);
		try { await work(); } catch { /* The API client displays the error; preserve the editor draft. */ }
		finally { setBusy(false); }
	};
	const beginEdit = (item: QueuedMessage) => perform(async () => {
		const updated = await controller.action('begin_edit', item);
		const current = updated.items.find((x) => x.message.id === item.message.id);
		if (current) setEditing({ item: current, text: messageText(current.message), attachments: current.message.content.filter((x) => x.type !== 'text') });
	});
	const finishEdit = (save: boolean) => perform(async () => {
		if (!editing) return;
		const latest = queue?.items.find((item) => item.message.id === editing.item.message.id);
		if (!save && latest?.version !== editing.item.version) {
			setEditing(null);
			header.current?.focus();
			return;
		}
		const content: ContentBlock[] = [...editing.attachments];
		if (editing.text.trim()) content.unshift({ type: 'text', id: crypto.randomUUID(), text: editing.text.trim(), created_at: new Date().toISOString(), finished_at: new Date().toISOString() });
		await controller.action(save ? 'save' : 'cancel_edit', editing.item, save ? { ...editing.item.message, content } : undefined);
		setEditing(null);
		header.current?.focus();
	});
	const remove = (item: QueuedMessage) => perform(async () => {
		await controller.action('delete', item);
		if (editing?.item.message.id === item.message.id) setEditing(null);
		toast(t('queue.deleted'), { duration: 8000, action: { label: t('queue.undo'), onClick: () => { void controller.action('restore', item).catch(() => {}); } } });
	});

	if (!queue?.items.length && !editing) return null;
	const items = queue?.items ?? [];
	const staleEdit = !!editing && items.find((item) => item.message.id === editing.item.message.id)?.version !== editing.item.version;
	const visible = all || editing ? items : items.slice(0, 3);
	const notice = queue?.editing_id ? t('queue.editing') : queue?.paused ? t(`queue.paused_${queue.pause_reason ?? 'manual'}`) : queue?.activity === 'waiting' ? t(`queue.waiting_${queue.waiting_reason ?? 'background'}`) : null;

	return <Card className="@container/queue mb-1 gap-0 rounded-xl py-0 shadow-none" aria-label={t('queue.title')}>
		<Collapsible open={open} onOpenChange={setOpen}>
			<Item className="min-h-9 flex-nowrap gap-2 rounded-none border-0 px-2.5 py-0.5">
				<ItemMedia className="text-muted-foreground"><ListOrdered className="size-3.5" /></ItemMedia>
				<ItemContent className="min-w-0 flex-row items-center gap-1.5 text-xs font-medium">{t('queue.title')} <Badge variant="secondary" className="h-4 min-w-4 px-1 text-[10px] tabular-nums">{items.length}</Badge></ItemContent>
				<ItemActions className="gap-0.5">
					<Button variant="ghost" size="sm" disabled={locked} onClick={() => void perform(async () => { await controller.action(queue?.paused ? 'resume' : 'pause'); })}>
						{queue?.paused ? <Play /> : <Pause />}{t(queue?.paused ? 'queue.resume' : 'queue.pause')}
					</Button>
					<CollapsibleTrigger asChild><Button ref={header} variant="ghost" size="icon-sm" tooltip={t(open ? 'queue.collapse' : 'queue.expand')} className="[@media(pointer:coarse)]:size-11">{open ? <ChevronUp /> : <ChevronDown />}</Button></CollapsibleTrigger>
				</ItemActions>
			</Item>
			{notice && <ItemDescription role="status" className="px-3 pb-1.5 text-xs">{notice}</ItemDescription>}
			{controller.error && <ItemDescription role="status" className="px-3 pb-1.5 text-xs text-destructive">{t('queue.syncError')}</ItemDescription>}
			<CollapsibleContent className="max-h-[40vh] overflow-y-auto overscroll-contain">
				<ItemGroup className="gap-0">
					{visible.map((item, index) => {
						const text = messageText(item.message);
						const files = item.message.content.filter((x) => x.type !== 'text').length;
						const stamp = new Date(item.enqueued_at);
						return <Item key={item.message.id} role="listitem" className="flex-nowrap gap-2 rounded-none border-x-0 border-t border-b-0 px-2.5 py-0.5" data-queue-id={item.message.id}>
							<ItemMedia className="w-4 text-center text-xs tabular-nums text-muted-foreground">{index + 1}</ItemMedia>
							<ItemContent className="min-w-0 gap-0 @min-[28rem]/queue:flex-row @min-[28rem]/queue:items-center @min-[28rem]/queue:gap-2">
								<Tooltip><TooltipTrigger asChild><p tabIndex={0} className="min-w-0 flex-1 truncate text-xs leading-5 outline-offset-1">{text || t('queue.attachments', { count: files })}</p></TooltipTrigger><TooltipContent className="max-w-sm whitespace-pre-wrap break-words">{text || t('queue.attachments', { count: files })}</TooltipContent></Tooltip>
								{files > 0 && <Badge variant="outline" className="h-4 shrink-0 gap-0.5 px-1 text-[10px]" aria-label={t('queue.attachments', { count: files })}><Paperclip className="size-2.5" />{files}</Badge>}
								<time dateTime={item.enqueued_at} title={stamp.toLocaleString(i18n.language)} className="shrink-0 text-[11px] tabular-nums text-muted-foreground">{stamp.toLocaleTimeString(i18n.language, { hour: '2-digit', minute: '2-digit', hour12: false })}</time>
							</ItemContent>
							<ItemActions className="gap-0">
								<Button variant="ghost" size="icon" className={iconClass} tooltip={t('queue.edit')} disabled={locked || !!editing || (!!queue?.editing_id && queue.editing_id !== item.message.id)} onClick={() => void beginEdit(item)}><Pencil className="size-3.5" /></Button>
								<Button variant="ghost" size="icon" className={iconClass} tooltip={t('queue.delete')} disabled={locked} onClick={() => void remove(item)}><Trash2 className="size-3.5" /></Button>
							</ItemActions>
						</Item>;
					})}
				</ItemGroup>
				{items.length > 3 && !editing && <Button variant="ghost" size="sm" className="w-full rounded-none border-t text-xs text-muted-foreground" onClick={() => setAll(!all)}>{t(all ? 'queue.showLess' : 'queue.showMore', { count: items.length - 3 })}</Button>}
				{editing && <CardContent className="space-y-2 border-t px-2.5 py-2">
					{staleEdit && <ItemDescription role="status" className="text-xs text-destructive">{t('queue.editConflict')}</ItemDescription>}
					<Textarea autoFocus aria-label={t('queue.edit')} value={editing.text} disabled={busy} onChange={(e) => setEditing({ ...editing, text: e.target.value })} onKeyDown={(e) => {
						if (e.nativeEvent.isComposing) return;
						if (e.key === 'Escape') { e.preventDefault(); void finishEdit(false); }
						if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); void finishEdit(true); }
					}} className="max-h-40 min-h-16 text-sm" />
					{editing.attachments.map((block, index) => <Badge variant="secondary" key={block.id} className="mr-1 gap-1"><Paperclip className="size-3" />{t('queue.attachment', { index: index + 1 })}<Button size="icon-xs" variant="ghost" tooltip={t('queue.removeAttachment')} disabled={busy} onClick={() => setEditing({ ...editing, attachments: editing.attachments.filter((x) => x !== block) })}><X /></Button></Badge>)}
					<ItemActions className="justify-end gap-1"><Button size="sm" variant="ghost" disabled={locked} onClick={() => void finishEdit(false)}>{t('queue.cancel')}</Button><Button size="sm" disabled={locked || staleEdit || (!editing.text.trim() && !editing.attachments.length)} onClick={() => void finishEdit(true)}>{t('queue.save')}</Button></ItemActions>
				</CardContent>}
			</CollapsibleContent>
		</Collapsible>
	</Card>;
}
