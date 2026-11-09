import { useQuery } from '@tanstack/react-query';
import { RefreshCw } from 'lucide-react';
import { useState } from 'react';

import { Check, Field } from './fields';
import { managementApi, type ManagedAgent } from '@/api/agent-management';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from '@/components/ui/dialog';
import { Disclosure } from '@/components/ui/disclosure';
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from '@/components/ui/empty';
import { Item, ItemContent, ItemGroup, ItemTitle } from '@/components/ui/item';
import { Textarea } from '@/components/ui/textarea';

const names: Record<string, string> = {
	name: '名称',
	description: '简介',
	system_prompt: '系统提示词',
	chat_config: '运行配置',
	defaults: '默认配置',
	memory_config: '记忆设置',
	resource_config: '资源绑定',
	chat_model_config: '对话模型',
	fallback_chat_model_config: '备用模型',
	knowledge_config: '知识库',
	model: '模型',
	parameters: '参数',
	skills: 'Skill',
	mcps: 'MCP',
	client: '连接配置',
	artifact_id: '安装包版本',
	enabled: '启用状态',
	permission_mode: '操作权限',
};
const describe = (path: string) =>
	path
		.split('/')
		.filter((p) => p && p !== 'agent')
		.map((p) => names[p] || (/^[a-f0-9]{32}$/.test(p) ? p.slice(0, 8) : p))
		.join(' / ');
const display = (value: unknown) =>
	typeof value === 'string' ? value : JSON.stringify(value, null, 2);

export function TemplateUpdateDialog({
	agent,
	onClose,
	onUpdated,
	onReturnFocus,
}: {
	agent: ManagedAgent;
	onClose: () => void;
	onUpdated: (agent: ManagedAgent) => void;
	onReturnFocus: () => void;
}) {
	const preview = useQuery({
		queryKey: ['template-preview', agent.user_id, agent.id, agent.revision],
		queryFn: () => managementApi.templatePreview(agent.user_id, agent.id),
		refetchOnWindowFocus: false,
		retry: false,
	});
	const [selected, setSelected] = useState<string[]>([]);
	const [busy, setBusy] = useState(false);
	const [error, setError] = useState('');
	return (
		<Dialog
			open
			onOpenChange={(open) => {
				if (!open && !busy) onClose();
			}}
		>
			<DialogContent
				className="max-h-[90dvh] overflow-y-auto sm:max-w-3xl"
				showCloseButton={!busy}
				onCloseAutoFocus={(event) => {
					event.preventDefault();
					onReturnFocus();
				}}
			>
				<DialogHeader>
					<DialogTitle>检查智能体更新</DialogTitle>
					<DialogDescription>
						来源：{agent.template?.source_name}。上次同步：
						{agent.template?.synced_at
							? new Date(agent.template.synced_at).toLocaleString()
							: '未知'}
						。
					</DialogDescription>
				</DialogHeader>
				<Alert>
					<AlertDescription>
						更新会用于后续对话，保留历史记录、工作文件和记忆。继承默认配置的已有会话也会采用更新；单独设置的模型保持不变。冲突项默认保留你的修改。
					</AlertDescription>
				</Alert>
				{preview.isPending && <p role="status">正在检查来源配置…</p>}
				{(preview.error || error) && (
					<Alert variant="destructive">
						<AlertDescription role="alert">
							{error || preview.error?.message}
						</AlertDescription>
					</Alert>
				)}
				{preview.data && !preview.data.changes.length && (
					<Empty>
						<EmptyHeader>
							<EmptyTitle>没有需要同步的更新</EmptyTitle>
							<EmptyDescription>你的个人修改会继续保留。</EmptyDescription>
						</EmptyHeader>
					</Empty>
				)}
				<ItemGroup>
					{preview.data?.changes.map((change) => (
						<Item key={change.path} variant="outline">
							<ItemContent>
								<ItemTitle>
									{describe(change.path)}{' '}
									{change.conflict && (
										<Badge variant="outline">存在个人修改</Badge>
									)}
								</ItemTitle>
								<Disclosure summary="查看差异" contentClassName="space-y-3">
									<Field label="当前内容">
										<Textarea
											readOnly
											value={display(change.current)}
											className="max-h-48 font-mono text-xs"
										/>
									</Field>
									<Field label={change.removed ? '来源已移除此项' : '来源更新'}>
										<Textarea
											readOnly
											value={
												change.removed ? '移除' : display(change.incoming)
											}
											className="max-h-48 font-mono text-xs"
										/>
									</Field>
								</Disclosure>
								{change.conflict && (
									<Check
										label="用来源更新替换我的修改"
										checked={selected.includes(change.path)}
										disabled={busy}
										onChange={(checked) =>
											setSelected((current) =>
												checked
													? [...current, change.path]
													: current.filter((p) => p !== change.path),
											)
										}
									/>
								)}
							</ItemContent>
						</Item>
					))}
				</ItemGroup>
				<DialogFooter>
					<Button
						type="button"
						variant="outline"
						disabled={busy || preview.isFetching}
						onClick={async () => {
							setSelected([]);
							setError('');
							await preview.refetch();
						}}
					>
						<RefreshCw className="size-4" />
						重新检查
					</Button>
					<Button type="button" variant="outline" disabled={busy} onClick={onClose}>
						关闭
					</Button>
					<Button
						type="button"
						disabled={busy || preview.isFetching || !preview.data?.changes.length}
						onClick={async () => {
							if (!preview.data) return;
							setBusy(true);
							setError('');
							try {
								onUpdated(
									await managementApi.templateUpdate(
										agent.user_id,
										agent.id,
										preview.data,
										selected,
									),
								);
							} catch (e) {
								setError((e as Error).message);
							} finally {
								setBusy(false);
							}
						}}
					>
						{busy ? '正在更新…' : '确认更新'}
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}
