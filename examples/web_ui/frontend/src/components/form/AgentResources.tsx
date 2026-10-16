import { useTranslation } from 'react-i18next';

import type { AgentResourceConfig, SkillResourceBinding } from '@/api';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Switch } from '@/components/ui/switch';
import { useMCPs } from '@/hooks/useMCPs';
import { useSkills } from '@/hooks/useSkills';

export function AgentResources({
	value,
	onChange,
	disabled = false,
}: {
	value: AgentResourceConfig;
	onChange: (value: AgentResourceConfig) => void;
	disabled?: boolean;
}) {
	const { t } = useTranslation();
	const mcps = useMCPs();
	const skills = useSkills();
	return (
		<section className="mt-6 space-y-4" aria-label={t('resource.agentTitle')}>
			<h3 className="font-medium">{t('resource.agentTitle')}</h3>
			<p className="text-sm text-muted-foreground">{t('resource.agentHelp')}</p>
			{(['mcps', 'skills'] as const).map((kind) => {
				const library = kind === 'mcps' ? mcps.mcps : skills.skills;
				const source = kind === 'mcps' ? mcps : skills;
				const ids = [
					...new Set([...library.map((r) => r.id), ...Object.keys(value[kind])]),
				];
				return (
					<fieldset key={kind} disabled={disabled} className="space-y-2">
						<legend className="mb-2 text-sm font-medium">
							{kind === 'mcps' ? 'MCP' : 'Skill'}
						</legend>
						{source.loading && <p className="text-sm">{t('common.loading')}</p>}
						{source.error && (
							<p role="alert" className="text-sm text-destructive">
								{source.error.message}
							</p>
						)}
						{!source.loading && !ids.length && (
							<p className="text-sm text-muted-foreground">
								{t('resource.libraryEmpty')}
							</p>
						)}
						{ids.map((id) => {
							const record = library.find((r) => r.id === id);
							const binding = value[kind][id];
							const name = record?.name ?? id;
							return (
								<div
									key={id}
									className="flex items-center gap-3 rounded-md border p-3"
								>
									<Checkbox
										checked={!!binding}
										disabled={disabled || (!record && !binding)}
										aria-label={t('resource.bind', { name })}
										onCheckedChange={(checked) => {
											const next = { ...value[kind] };
											if (checked) next[id] = { enabled: true };
											else delete next[id];
											onChange({ ...value, [kind]: next });
										}}
									/>
									<div className="min-w-0 flex-1 text-sm">
										<div className="truncate">{name}</div>
										{!record && (
											<span className="text-muted-foreground">
												{t('resource.unavailable')}
											</span>
										)}
										{kind === 'skills' && binding && (
											<span className="text-xs text-muted-foreground">
												{(binding as SkillResourceBinding).version ??
													record?.version ??
													''}
											</span>
										)}
									</div>
									{binding && (
										<>
											<span className="text-xs text-muted-foreground">
												{t('resource.agentDefault')}
											</span>
											<Switch
												checked={binding.enabled}
												disabled={disabled || !record}
												aria-label={t('resource.toggle', { name })}
												onCheckedChange={(enabled) =>
													onChange({
														...value,
														[kind]: {
															...value[kind],
															[id]: { ...binding, enabled },
														},
													})
												}
											/>
											{kind === 'skills' &&
												record &&
												(binding as SkillResourceBinding).version != null &&
												record.version !==
													(binding as SkillResourceBinding).version && (
													<Button
														type="button"
														size="sm"
														variant="outline"
														onClick={() =>
															onChange({
																...value,
																skills: {
																	...value.skills,
																	[id]: {
																		...binding,
																		artifact_id: null,
																		version: record.version,
																	},
																},
															})
														}
													>
														{t('resource.applyUpgrade')}
													</Button>
												)}
										</>
									)}
								</div>
							);
						})}
					</fieldset>
				);
			})}
		</section>
	);
}
