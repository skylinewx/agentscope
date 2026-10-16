import { freeReportContent } from '@/lib/analysis-document';
import { useQuery } from '@tanstack/react-query';
import { Download, FileText, Loader2, RotateCcw } from 'lucide-react';
import { useContext, useEffect, useRef, useState } from 'react';

import { AnalysisVisuals } from './analysis/AnalysisVisuals';
import { AnalysisDraftContext } from './AnalysisDraftContext';
import { analysisApi, type AnalysisWorkspace } from '@/api/analysis';
import { ApiError, getBaseUrl, getUserId } from '@/api/client';
import { Markdown } from '@/components/markdown';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog';
import { useTranslation } from '@/i18n/useI18n';
import { DEFAULT_VIEW, type ExportDelivery } from '@/lib/workspace-analysis';
import { prepareReportPreview } from '@/lib/workspace-reports';

function failure(error: unknown): { key: string; restore: boolean } {
	if (error instanceof Error && ['UNSUPPORTED_VERSION', 'INVALID_VIEW'].includes(error.message)) return { key: 'analysis.unsupported', restore: false };
	if (error instanceof ApiError) {
		let code = '';
		try { code = (JSON.parse(error.detail) as { code?: string }).code ?? ''; } catch { /* ordinary HTTP error */ }
		if (['PROJECTION_CHANGED', 'MISSING_PROJECTION'].includes(code)) return { key: 'analysis.changed', restore: true };
		if (['UNSUPPORTED_VERSION', 'INVALID_ARTIFACT', 'INVALID_PRESENTATION'].includes(code)) return { key: 'analysis.unsupported', restore: false };
		if (['ANALYSIS_DISABLED', 'SKILL_VERSION_MISMATCH'].includes(code)) return { key: 'analysis.activationRequired', restore: false };
		if ([401, 403, 404].includes(error.status)) return { key: 'analysis.noAccess', restore: false };
		if (error.status === 413) return { key: 'analysis.tooLarge', restore: false };
		if (code === 'REPORTS_DISABLED') return { key: 'analysis.reportsDisabled', restore: false };
	}
	return { key: 'analysis.unavailable', restore: false };
}

export function WorkspaceAnalysis({ path, workspace }: { path: string; workspace: AnalysisWorkspace }) {
	const identity = JSON.stringify([getBaseUrl(), getUserId(), workspace.agentId, workspace.sessionId, path]);
	return <AnalysisCard key={identity} path={path} workspace={workspace} />;
}

function AnalysisCard({ path, workspace }: { path: string; workspace: AnalysisWorkspace }) {
	const { t } = useTranslation();
	const draft = useContext(AnalysisDraftContext);
	const [relatedPath, setRelatedPath] = useState<string | null>(null);
	const [mode, setMode] = useState<'table' | 'chart'>('chart');
	const [open, setOpen] = useState(false);
	const [html, setHtml] = useState<string | null>(null);
	const [exported, setExported] = useState<ExportDelivery | null>(null);
	const [busy, setBusy] = useState(false);
	const [error, setError] = useState<unknown>(null);
	const operation = useRef<AbortController | null>(null);
	const identity = JSON.stringify([getBaseUrl(), getUserId(), workspace.agentId, workspace.sessionId, path]);
	useEffect(() => () => operation.current?.abort(), [identity]);
	const key = ['workspace-analysis', getBaseUrl(), getUserId(), workspace.agentId, workspace.sessionId, path];
	const original = useQuery({ queryKey: key, queryFn: ({ signal }) => analysisApi.view(workspace, path, signal), retry: false, staleTime: 30_000 });
	const view = original.data;
	const run = async (action: (signal: AbortSignal) => Promise<void>) => {
		if (operation.current) return;
		const controller = new AbortController(); operation.current = controller; setBusy(true); setError(null);
		try { await action(controller.signal); }
		catch (err) { if (!controller.signal.aborted) setError(err); }
		finally { if (!controller.signal.aborted) setBusy(false); operation.current = null; }
	};
	const deliver = (download: boolean) => run(async (signal) => {
		if (!original.data) return;
		const chosen = DEFAULT_VIEW;
		const saved = original.data.reports?.slice().reverse().find((r) => r.layout === 'standard' && r.viewParameters.filter === chosen.filter && r.viewParameters.sort === chosen.sort && r.viewParameters.topN === chosen.topN && r.viewParameters.columnKey === chosen.columnKey);
		const report = saved ?? await analysisApi.render(workspace, original.data.analysisRef, chosen, signal);
		if (download) {
			const url = await analysisApi.downloadUrl(workspace, report, signal);
			if (signal.aborted) return;
			const link = document.createElement('a'); link.href = url; link.download = `${report.exportRef}.html`; link.rel = 'noopener';
			document.body.appendChild(link); link.click(); link.remove();
		} else {
			const content = await analysisApi.preview(workspace, report, signal);
			if (signal.aborted) return;
			setExported(report); setHtml(prepareReportPreview(original.data.freeDocument ? freeReportContent(content) : content)); setOpen(true);
		}
	});
	const issue = error ?? original.error;
	const followUp = (actionRef: string) => run(async (signal) => {
		const resolved = await analysisApi.resolveAction(workspace, actionRef, signal);
		if (!signal.aborted) draft?.insert(resolved.text, workspace);
	});
	const problem = failure(issue);
	const reviewNotes = [...new Set([...(view?.limitations ?? []), ...(view?.findings.flatMap((f) => f.explanations.flatMap((e) => e.missingEvidence)) ?? [])])];
	return <Dialog open={open} onOpenChange={setOpen}>
		<section data-slot="workspace-analysis" className="not-prose my-4 w-full min-w-0 overflow-hidden rounded-2xl border border-slate-200 bg-slate-50/80 shadow-sm text-slate-900 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100" aria-label={t('analysis.title')}>
			<header className="border-b border-slate-200 bg-white p-5 dark:border-slate-700 dark:bg-slate-900">
				<div className="mb-1 flex items-center gap-2 font-semibold"><FileText className="size-4 text-teal-700" />{t('analysis.title')}</div>
				{(view || !issue) && <p className="m-0 break-words text-sm leading-6">{view?.scopeLabel ?? t('analysis.loading')}</p>}

			</header>
			{issue && <div role="alert" className="m-4 rounded-lg bg-amber-50 p-3 text-sm text-amber-950">
				<p>{t(problem.key)}</p>{problem.key !== 'analysis.unsupported' && <Button variant="outline" size="sm" disabled={busy} onClick={() => void run(async (signal) => {
					if (problem.restore) await analysisApi.restore(workspace, path, signal);
					await original.refetch();
				})}><RotateCcw />{t(problem.restore ? 'analysis.restore' : 'report.retry')}</Button>}
			</div>}
			{!issue && (original.isPending || !view) && <div role="status" className="flex items-center gap-2 p-5 text-sm"><Loader2 className="size-4 animate-spin" />{t('analysis.loading')}</div>}
			{!issue && view && <div className="space-y-5 p-5">
				{view.technicalDetails && view.statuses.receiptVerification !== 'VALID' && <p role="status" className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-xs leading-5 text-amber-950">{t(view.statuses.receiptVerification === 'INVALID' ? 'analysis.sourceConflict' : 'analysis.sourcePending')}</p>}


				{original.data?.capabilities?.cards !== false && !view.freeDocument && view.chartsRequested && <div className="flex gap-2" role="group" aria-label={t('analysis.viewType')}>
					<Button size="sm" variant={mode === 'chart' ? 'secondary' : 'outline'} aria-pressed={mode === 'chart'} onClick={() => setMode('chart')}>{t('analysis.chart')}</Button>
					{view.tables.length > 0 && <Button size="sm" variant={mode === 'table' ? 'secondary' : 'outline'} aria-pressed={mode === 'table'} onClick={() => setMode('table')}>{t('analysis.table')}</Button>}
				</div>}
                <AnalysisVisuals key={view.viewId} view={view} showCharts={mode === 'chart' && original.data?.capabilities?.cards !== false} />

				{!view.technicalDetails && view.actions.length > 0 && <section className="space-y-3" aria-label={t('analysis.findings')}>
                    {view.actions.map((action) => <article key={action.actionRef} className="space-y-2 rounded-xl border bg-white p-4 text-sm leading-6">
                        <p className="font-medium">{action.goal}</p><Markdown>{action.expectedDecision}</Markdown>
                        {action.availability === 'AVAILABLE' && original.data?.capabilities?.followUp && draft && <Button size="sm" variant="outline" disabled={busy} onClick={() => void followUp(action.actionRef)}>{t('analysis.continueCheck')}</Button>}
                        {original.data?.actionRuns?.filter((r) => r.actionRef === action.actionRef).map((run) => <div key={run.actionRunId} className="flex flex-wrap items-center gap-2 text-xs">
                            <span>{t(`analysis.run_${run.status}`)}</span>{run.resultAnalysisRef && <Button size="sm" variant="ghost" onClick={() => { const [id, revision] = run.resultAnalysisRef!.split(':'); setRelatedPath(`./analysis/${id}/revisions/${revision}/analysis.json`); }}>{t('analysis.followupResult')}</Button>}
                        </div>)}
                    </article>)}
                </section>}
				{view.technicalDetails && <details className="rounded-xl border border-slate-200 bg-white p-4"><summary className="cursor-pointer text-sm font-medium">{t('analysis.reviewDetails')}{reviewNotes.length > 0 && <span className="ml-2 text-xs text-amber-800">{t('analysis.reviewNoteCount', { count: reviewNotes.length })}</span>}</summary><div className="mt-3 space-y-3">
				{view && <p className="mt-2 text-xs leading-5 text-slate-600 dark:text-slate-300">{t('analysis.status', {
					receipt: t(view.statuses.receiptVerification === 'VALID' ? 'analysis.valid' : 'analysis.unverified'),
					coverage: t(view.statuses.coverage === 'COMPLETE' ? 'analysis.complete' : 'analysis.partial'),
					comparison: t(view.statuses.comparability === 'CONFIRMED' ? 'analysis.confirmed' : 'analysis.unconfirmed'),
					calculation: t(view.statuses.calculationValidation === 'PASS' ? 'analysis.passed' : 'analysis.limited'),
				})}</p>}
				{reviewNotes.length > 0 && <ul className="list-disc space-y-1 rounded-lg bg-amber-50 py-3 pr-3 pl-7 text-xs leading-5 text-amber-950">{reviewNotes.map((item, i) => <li key={i}>{item}</li>)}</ul>}
				{view.findings.length > 0 && <section aria-label={t('analysis.findings')} className="space-y-3">

					{view.findings.map((finding) => <article key={finding.findingId} className="space-y-2 rounded-lg border p-3 text-sm leading-6">
						<p>{finding.statement}</p>
						<p className="text-xs">{t('analysis.basis')}{finding.basis} · {finding.ruleVersion} · {finding.applicability}</p>
						{finding.explanations.map((explanation, i) => <div key={i}>
							{explanation.kind === 'business_cause_hypothesis' && <p>{t('analysis.hypothesis')}{explanation.statement}</p>}

						</div>)}
						{view.actions.filter((action) => action.findingRefs.includes(finding.findingId)).map((action) => <div key={action.actionRef} className="space-y-2 border-t pt-2">
							<p className="font-medium">{action.goal}</p><p>{t('analysis.decision')}{action.expectedDecision}</p>
							<p>{t('analysis.requiredEvidence')}{action.requiredEvidence.join('；')}</p>
							<p className="text-xs">{t(`analysis.availability_${action.availability}`)} · {action.availabilityReason}</p><p className="text-xs">{action.priorityBasis}</p>
							{action.availability === 'AVAILABLE' && original.data?.capabilities?.followUp && draft && <Button size="sm" variant="outline" disabled={busy} onClick={() => void followUp(action.actionRef)}>{t('analysis.continueCheck')}</Button>}
							{original.data?.actionRuns?.filter((r) => r.actionRef === action.actionRef).map((run) => <div key={run.actionRunId} className="flex flex-wrap items-center gap-2 text-xs">
								<span>{t(`analysis.run_${run.status}`)}</span>{run.resultNote && <span>{run.resultNote}</span>}{run.resultAnalysisRef && <Button size="sm" variant="ghost" onClick={() => { const [id, revision] = run.resultAnalysisRef!.split(':'); setRelatedPath(`./analysis/${id}/revisions/${revision}/analysis.json`); }}>{t('analysis.followupResult')}</Button>}
							</div>)}
						</div>)}
					</article>)}
					<p className="text-xs">{t('analysis.actionNote')}</p><Button size="sm" variant="ghost" onClick={() => void original.refetch()}>{t('analysis.refreshActions')}</Button>
				</section>}
				</div></details>}
				{view.technicalDetails && view.sources.length > 0 && <p className="break-words text-xs leading-5 text-slate-600 dark:text-slate-300">{t('analysis.sources')}{view.sources.join('；')}</p>}
			</div>}
			{original.data && <footer className="space-y-2 border-t border-slate-200 p-4 dark:border-slate-700">
				<div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" disabled={busy} onClick={() => void deliver(false)}>{busy ? <Loader2 className="animate-spin" /> : <FileText />}{t('analysis.viewReport')}</Button><Button size="sm" variant="outline" disabled={busy} onClick={() => void deliver(true)}><Download />{t('analysis.downloadReport')}</Button></div>
				<p className="text-xs leading-5 text-slate-600 dark:text-slate-300">{t('analysis.exportScope')}</p>
			</footer>}
		</section>
		{relatedPath && <section className="border-l-2 border-teal-600 pl-3"><Button size="sm" variant="ghost" onClick={() => setRelatedPath(null)}>{t('analysis.closeFollowup')}</Button><WorkspaceAnalysis key={relatedPath} path={relatedPath} workspace={workspace} /></section>}
		{open && html && <DialogContent className="flex h-[92dvh] w-[96vw] max-w-[1440px] flex-col gap-2 overflow-hidden p-4 sm:max-w-[1440px]">
			<DialogTitle>{t(exported?.isFiltered ? 'analysis.filteredReport' : 'analysis.fullReport')}</DialogTitle><DialogDescription>{exported?.scopeLabel}</DialogDescription>
			<iframe title={t('analysis.fullReport')} sandbox="allow-scripts" referrerPolicy="no-referrer" srcDoc={html} className="min-h-0 w-full flex-1 border-0 bg-white" />
		</DialogContent>}
	</Dialog>;
}
