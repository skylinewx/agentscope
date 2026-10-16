import { useQuery } from '@tanstack/react-query';
import { Download, FileCode2, Loader2, RotateCcw } from 'lucide-react';
import { createContext, useContext, useEffect, useMemo, useState, type ComponentProps, type ReactNode } from 'react';
import { defaultRemarkPlugins } from 'streamdown';

import { DeliveredAnalysisPathsContext } from './AnalysisDeliveryContext';
import { WorkspaceAnalysis } from './WorkspaceAnalysis';
import { ApiError, getBaseUrl, getUserId } from '@/api/client';
import { workspaceApi } from '@/api/workspace';
import { Markdown } from '@/components/markdown';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from '@/components/ui/dialog';
import { useTranslation } from '@/i18n/useI18n';
import { analysisPath, remarkWorkspaceAnalyses } from '@/lib/workspace-analysis';
import { formatReportSize, prepareReportPreview, remarkWorkspaceReports, workspaceFilePath } from '@/lib/workspace-reports';

type Workspace = { agentId: string; sessionId: string };
const ReportWorkspaceContext = createContext<Workspace | null>(null);
const allowedTags = { 'workspace-report': ['path'], 'workspace-analysis': ['path'], 'workspace-analysis-link': ['path'], 'workspace-analysis-unavailable': ['standalone'] };

function errorKey(error: unknown): string {
	if (error instanceof ApiError) {
		if (error.status === 404) return 'report.notFound';
		if (error.status === 401 || error.status === 403) return 'report.noAccess';
		if (error.status === 413) return 'report.tooLarge';
	}
	return 'report.unavailable';
}

function ReportPreview({ path, title, workspace, onDownload, downloading, downloadError }: {
	path: string; title: string; workspace: Workspace; onDownload: () => void; downloading: boolean; downloadError: string | null;
}) {
	const { t } = useTranslation();
	const [html, setHtml] = useState<string | null>(null);
	const [error, setError] = useState<string | null>(null);
	const [attempt, setAttempt] = useState(0);
	useEffect(() => {
		const controller = new AbortController();
		setHtml(null);
		setError(null);
		workspaceApi.files.preview(workspace.agentId, workspace.sessionId, path, controller.signal)
			.then((content) => { if (!controller.signal.aborted) setHtml(prepareReportPreview(content)); })
			.catch((failure: unknown) => { if (!controller.signal.aborted) setError(errorKey(failure)); });
		return () => controller.abort();
	}, [workspace.agentId, workspace.sessionId, path, attempt]);
	return (
		<DialogContent className="flex h-[92dvh] w-[96vw] max-w-[1440px] flex-col gap-0 overflow-hidden p-0 sm:max-w-[1440px]">
			<div className="flex items-center justify-between gap-4 border-b px-5 py-4 pr-14">
				<div className="min-w-0">
					<DialogTitle className="truncate leading-normal">{title}</DialogTitle>
					<DialogDescription className="mt-1">{t('report.previewDescription')}</DialogDescription>
				</div>
				<Button variant="outline" size="sm" onClick={onDownload} disabled={downloading}>
					{downloading ? <Loader2 className="animate-spin" /> : <Download />}{t('report.download')}
				</Button>
			</div>
			{downloadError && <p role="alert" className="px-5 pt-3 text-sm text-destructive">{t(downloadError)}</p>}
			{error ? (
				<div className="flex flex-1 flex-col items-center justify-center gap-4 px-6 text-center" role="alert">
					<p>{t(error)}</p>
					<Button variant="outline" onClick={() => setAttempt((value) => value + 1)}><RotateCcw />{t('report.retry')}</Button>
				</div>
			) : html === null ? (
				<div className="flex flex-1 items-center justify-center gap-2" role="status"><Loader2 className="size-5 animate-spin" />{t('report.loading')}</div>
			) : (
				<iframe title={t('report.previewTitle', { name: title })} sandbox="allow-scripts" referrerPolicy="no-referrer" srcDoc={html} className="min-h-0 w-full flex-1 border-0 bg-white" />
			)}
		</DialogContent>
	);
}

function ReportCard({ path, children }: { path: string; children?: ReactNode }) {
	const workspace = useContext(ReportWorkspaceContext);
	const { t } = useTranslation();
	const [open, setOpen] = useState(false);
	const [downloading, setDownloading] = useState(false);
	const [downloadError, setDownloadError] = useState<string | null>(null);
	const info = useQuery({
		queryKey: ['workspace-report', getBaseUrl(), getUserId(), workspace?.agentId, workspace?.sessionId, path],
		queryFn: () => workspaceApi.files.info(workspace!.agentId, workspace!.sessionId, path),
		enabled: !!workspace,
		retry: false,
		staleTime: 30_000,
	});
	if (!workspace) return <span>{children}</span>;
	const fileName = info.data?.name ?? path.split('/').pop() ?? t('report.title');
	const size = formatReportSize(info.data?.size_bytes);
	const download = async () => {
		setDownloading(true);
		setDownloadError(null);
		try {
			// Mint on each click. The chat stores only the stable workspace reference.
			const url = await workspaceApi.files.downloadUrl(workspace.agentId, workspace.sessionId, path);
			const link = document.createElement('a');
			link.href = url;
			link.download = fileName;
			link.rel = 'noopener';
			document.body.appendChild(link);
			link.click();
			link.remove();
		} catch (failure) { setDownloadError(errorKey(failure)); }
		finally { setDownloading(false); }
	};
	return (
		<Dialog open={open} onOpenChange={setOpen}>
			<span data-slot="workspace-report-card" className="my-2 flex w-full flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border border-border/70 bg-muted/50 px-4 py-3">
				<button type="button" className="flex min-w-0 flex-1 items-center gap-3 rounded-lg text-left outline-offset-4 hover:text-primary" onClick={() => setOpen(true)} aria-label={t('report.viewNamed', { name: fileName })}>
					<span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-violet-100 text-violet-600 dark:bg-violet-950 dark:text-violet-300"><FileCode2 className="size-5" /></span>
					<span className="min-w-0">
						<span className="block truncate text-sm font-semibold leading-5">{children || fileName}</span>
						<span className="mt-0.5 flex min-w-0 items-center gap-2 text-xs leading-4 text-muted-foreground">
							<span className="min-w-0 truncate" title={fileName}>{fileName}</span>
							<span className="shrink-0">HTML{size && ` · ${size}`}<span className="hidden sm:inline"> · {t('report.view')}</span></span>
						</span>
					</span>
				</button>
				<Button variant="ghost" size="icon" onClick={() => void download()} disabled={downloading} title={t('report.download')} aria-label={t('report.downloadNamed', { name: fileName })}>
					{downloading ? <Loader2 className="animate-spin" /> : <Download />}
				</Button>
				{(info.error || downloadError) && <span className="flex w-full items-center gap-2 text-sm text-destructive" role="alert">{t(downloadError ?? errorKey(info.error))}<Button variant="ghost" size="sm" onClick={() => { setDownloadError(null); void info.refetch(); }}>{t('report.retry')}</Button></span>}
			</span>
			{open && <ReportPreview path={path} title={fileName} workspace={workspace} onDownload={() => void download()} downloading={downloading} downloadError={downloadError} />}
		</Dialog>
	);
}

function ReportElement({ path, children }: Record<string, unknown>) {
	const validPath = typeof path === 'string' ? workspaceFilePath(path) : null;
	return validPath ? <ReportCard key={validPath} path={validPath}>{children as ReactNode}</ReportCard> : <span>{children as ReactNode}</span>;
}
function AnalysisElement({ path, children }: Record<string, unknown>) {
	const workspace = useContext(ReportWorkspaceContext);
	const delivered = useContext(DeliveredAnalysisPathsContext);
	const validPath = typeof path === 'string' ? analysisPath(path) : null;
	if (validPath && delivered.includes(validPath)) return null;
	return validPath && workspace ? <WorkspaceAnalysis key={`${workspace.agentId}:${workspace.sessionId}:${validPath}`} path={validPath} workspace={workspace} /> : <span>{children as ReactNode}</span>;
}
function AnalysisLink({ path, workspace, children }: { path: string; workspace: Workspace; children: ReactNode }) {
	const [open, setOpen] = useState(false);
	return <Dialog open={open} onOpenChange={setOpen}>
		<DialogTrigger asChild><button type="button" className="inline cursor-pointer text-primary underline underline-offset-4">{children}</button></DialogTrigger>
		{open && <DialogContent className="max-h-[92dvh] w-[96vw] overflow-y-auto sm:max-w-5xl" aria-describedby={undefined}>
			<DialogTitle>{children}</DialogTitle>
			<WorkspaceAnalysis path={path} workspace={workspace} />
		</DialogContent>}
	</Dialog>;
}
function AnalysisLinkElement({ path, children }: Record<string, unknown>) {
	const workspace = useContext(ReportWorkspaceContext);
	const validPath = typeof path === 'string' ? analysisPath(path) : null;
	return validPath && workspace ? <AnalysisLink key={`${workspace.agentId}:${workspace.sessionId}:${validPath}`} path={validPath} workspace={workspace}>{children as ReactNode}</AnalysisLink> : <span>{children as ReactNode}</span>;
}
function UnavailableAnalysisElement({ standalone, children }: Record<string, unknown>) {
	const delivered = useContext(DeliveredAnalysisPathsContext);
	const { t } = useTranslation();
	// The actual receipt supplies the card below the answer. A malformed standalone
	// placeholder is neither guessed into that receipt nor sent to an external site.
	if (standalone === 'true' && delivered.length) return null;
	return <span className="text-muted-foreground" title={t('analysis.invalidLink')}>{children as ReactNode} ({t('analysis.invalidLink')})</span>;
}
const components = { 'workspace-report': ReportElement, 'workspace-analysis': AnalysisElement, 'workspace-analysis-link': AnalysisLinkElement, 'workspace-analysis-unavailable': UnavailableAnalysisElement };

/** Only chat text with a known owning workspace gets the report-link contract. */
export function ReportMarkdown({ agentId, sessionId, ...props }: ComponentProps<typeof Markdown> & {
	agentId?: string | null; sessionId?: string | null;
}) {
	const workspace = useMemo(() => agentId && sessionId ? { agentId, sessionId } : null, [agentId, sessionId]);
	const remarkPlugins = useMemo(() => [...Object.values(defaultRemarkPlugins), remarkWorkspaceReports,
		[remarkWorkspaceAnalyses, { source: typeof props.children === 'string' ? props.children : '' }] as [typeof remarkWorkspaceAnalyses, { source: string }]], [props.children]);
	if (!workspace) return <Markdown {...props} />;
	return (
		<ReportWorkspaceContext.Provider key={`${agentId}:${sessionId}`} value={workspace}>
			<Markdown {...props} remarkPlugins={remarkPlugins} allowedTags={allowedTags} components={components} />
		</ReportWorkspaceContext.Provider>
	);
}
