import { parseFreeDocument, type FreeAnalysisDocument } from './analysis-document.ts';
/** Registered analysis links use one canonical, case-sensitive ASCII spelling. */
export const ANALYSIS_VIEW_MAX_BYTES = 1024 * 1024;
export const ANALYSIS_PAGE_SIZE = 12;

export function analysisPath(href: string): string | null {
	return /^\.\/analysis\/ana_[0-9a-f]{32}\/revisions\/[1-9][0-9]{0,8}\/analysis\.json$/.test(href) ? href : null;
}

type Node = { type: string; url?: string; identifier?: string; children?: Node[]; data?: Record<string, unknown> };
const internalAnalysisLink = (href: string) => /^(?:\.\/|\/)analysis\//.test(href);

/** Complete standalone links become cards; inline links open the registered view on demand. */
export function remarkWorkspaceAnalyses(options: { source?: string } = {}) {
	return (tree: Node, file?: { value?: unknown }) => {
		const source = options.source ?? String(file?.value ?? '');
		const complete = new Set<string>();
		const inlineComplete = new Set<string>();
		const definitions = new Map<string, string>();
		for (const line of source.split('\n')) {
			const definition = /^ {0,3}\[([^\]]+)\]:\s*(\S+)\s*$/.exec(line);
			if (definition && internalAnalysisLink(definition[2])) definitions.set(definition[1].toLowerCase(), definition[2]);
			const direct = /^ {0,3}\[[^\]\n]+\]\(([^\s)]+)\)\s*$/.exec(line);
			if (direct && internalAnalysisLink(direct[1])) complete.add(direct[1]);
		}
		for (const line of source.split('\n')) {
			const ref = /^ {0,3}\[([^\]]+)\]\[([^\]]*)\]\s*$/.exec(line);
			if (ref) { const path = definitions.get((ref[2] || ref[1]).toLowerCase()); if (path) complete.add(path); }
		}
		for (const match of source.matchAll(/\[[^\]\n]+\]\(([^\s)]+)\)/g)) {
			if (internalAnalysisLink(match[1])) inlineComplete.add(match[1]);
		}
		for (const match of source.matchAll(/\[([^\]\n]+)\]\[([^\]\n]*)\]/g)) {
			const path = definitions.get((match[2] || match[1]).toLowerCase());
			if (path) inlineComplete.add(path);
		}
		const seen = new Set<string>();
		const walk = (node: Node) => {
			if (node.type === 'paragraph' && node.children?.length === 1) {
				const link = node.children[0];
				const path = link.type === 'link' ? link.url : link.type === 'linkReference' ? definitions.get(link.identifier?.toLowerCase() ?? '') : undefined;
				if (path && complete.has(path) && !analysisPath(path)) {
					node.data = { ...node.data, hName: 'workspace-analysis-unavailable', hProperties: { standalone: 'true' } };
					node.children = link.children;
					return;
				}
				if (path && complete.has(path) && !seen.has(path)) {
					seen.add(path);
					node.data = { ...node.data, hName: 'workspace-analysis', hProperties: { path } };
					node.children = link.children;
					return;
				}
			}
			const path = node.type === 'link' ? node.url : node.type === 'linkReference' ? definitions.get(node.identifier?.toLowerCase() ?? '') : undefined;
			if (path && inlineComplete.has(path)) {
				if (!analysisPath(path)) {
					node.data = { ...node.data, hName: 'workspace-analysis-unavailable' };
					return;
				}
				node.data = { ...node.data, hName: 'workspace-analysis-link', hProperties: { path } };
				return;
			}
			if (node.type !== 'code' && node.type !== 'image' && node.type !== 'imageReference') node.children?.forEach(walk);
		};
		walk(tree);
	};
}

export interface ViewParameters { filter: 'all' | 'positive' | 'negative' | 'zero' | 'unavailable'; sort: 'published' | 'asc' | 'desc'; topN: number | null; columnKey: string | null }
export const DEFAULT_VIEW: ViewParameters = { filter: 'all', sort: 'published', topN: null, columnKey: null };
export type Tone = 'positive' | 'negative' | 'axis' | 'label' | 'value' | 'surface';
export type Primitive =
	| { kind: 'text'; x: number; y: number; text: string; tone: Tone }
	| { kind: 'line'; x1: number; x2: number; y1: number; y2: number; tone: Tone }
	| { kind: 'rect'; x: number; y: number; width: number; height: number; tone: Tone; rowKey: string };
export interface RelationNode { key: string; label: string; display: string; note?: string; tone?: Tone; operator?: string; coefficient?: string; contribution?: string; weight?: number; emphasis?: boolean }
export interface Relationship { layout?: 'compact' | 'flow'; equation: string; inputs: RelationNode[]; result: RelationNode; note: string; checks: { label: string; display: string }[] }
export interface PresentationSection { groupId: string; title: string; description: string; width: 'full' | 'half'; claimsPosition: 'before' | 'after'; claimIndexes: number[]; blocks: { viewId: string; title: string; description: string; metricIndexes: number[]; sceneIndexes: number[]; tableIndexes: number[] }[] }
export interface ChartScene { viewId: string; relationship?: Relationship; type: 'BAR_VALUES' | 'BAR_RANK' | 'BRIDGE' | 'FORMULA' | 'HIERARCHY' | 'WATERFALL' | 'RECONCILIATION'; title?: string; primary?: boolean; collapsed?: boolean; width: number; height: number; zeroBaseline: number; description: string; rowKeys: string[]; primitives: Primitive[] }
export interface AnalysisRow { rowKey: string; memberKey: string; valueRef: string; label: string; measure: string; exact: string | null; display: string; unit: string; state: string }
export interface AnalysisColumn { key: string; label: string; unit: string }
export interface AnalysisTable { viewId: string; collapsed: boolean; pageSize: number; tableKey: string; title: string; kind: 'values' | 'formula' | 'calculation'; note?: string; columns: AnalysisColumn[]; rows: { memberKey: string; label: string; note?: string; cells: Record<string, AnalysisRow> }[] }
export type FindingSupport = 'SUPPORTED' | 'NEEDS_EVIDENCE' | 'CONTRADICTED';
export interface FindingView { findingId: string; statement: string; support: FindingSupport; type: string; basis: string; ruleVersion: string; applicability: string; sourceType: string; previousAnalysisRef: string | null; explanations: { kind: 'observation' | 'arithmetic_composition' | 'business_cause_hypothesis'; statement: string; support: FindingSupport; missingEvidence: string[] }[] }
export interface ActionView { actionRef: string; findingRefs: string[]; goal: string; scopeLabel: string; requiredEvidence: string[]; expectedDecision: string; availability: 'AVAILABLE' | 'NEEDS_INPUT' | 'UNAVAILABLE'; availabilityReason: string; priorityBasis: string }
export interface ActionRunView { actionRunId: string; actionRef: string; status: 'PROPOSED' | 'RUNNING' | 'AWAITING_INPUT' | 'PARTIAL' | 'COMPLETED' | 'FAILED' | 'CANCELLED'; resultAnalysisRef: string | null; updatedAt: string; resultNote: string }
export interface ViewSnapshot {
    freeDocument?: FreeAnalysisDocument;
	schemaVersion: 1; rendererVersion: string; engineVersion: string; analysisRef: string; dataDigest: string; viewId: string; scopeId: string;
	viewParameters: ViewParameters; scopeLabel: string; isFiltered: boolean; publishedAt: string | null; verificationRecordedAt: string | null;
	technicalDetails: boolean; statuses: Record<string, string>; rows: AnalysisRow[]; sections: PresentationSection[]; claimsCollapsed: boolean; metrics: { viewId: string; valueRef: string; label: string; display: string; unit: string; scopeId: string }[];
	scenes: ChartScene[]; summaryPosition: 'start' | 'end'; summaryTitle: string; reportTitle: string; summaryClaimIndexes: number[]; chartUnavailable: string | null; chartsRequested: boolean; claims: string[]; claimsScopeLabel: string; sources: string[]; limitations: string[];
	total: number; originalTotal: number; page: { offset: number; limit: number; total: number; nextOffset: number | null };
	objectCount: number; originalObjectCount: number; memberKeys: string[]; columns: AnalysisColumn[]; activeColumnKey: string | null; tables: AnalysisTable[];
	capabilities?: { cards: boolean; generateReports: boolean; followUp: boolean };
	reports?: ExportDelivery[];
	findings: FindingView[]; actions: ActionView[]; findingsScopeLabel: string; actionRuns?: ActionRunView[];
}
export interface ExportDelivery { analysisRef: string; exportRef: string; path: string; contentDigest: string; scopeLabel: string; isFiltered: boolean; sizeBytes: number; registered: boolean; viewParameters: ViewParameters; layout: 'standard' | 'table'; rendererVersion: string }

function record(value: unknown): Record<string, unknown> {
	if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('INVALID_VIEW');
	return value as Record<string, unknown>;
}
function str(value: unknown): asserts value is string { if (typeof value !== 'string' || value.length > 16000) throw new Error('INVALID_VIEW'); }
function count(value: unknown): asserts value is number { if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) throw new Error('INVALID_VIEW'); }
function strings(value: unknown) { if (!Array.isArray(value) || value.length > 50000) throw new Error('INVALID_VIEW'); value.forEach(str); }
function coordinate(value: unknown) { if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 10000) throw new Error('INVALID_VIEW'); }
function viewParameters(value: unknown) {
	const params = record(value);
	if (!['all', 'positive', 'negative', 'zero', 'unavailable'].includes(String(params.filter)) || !['published', 'asc', 'desc'].includes(String(params.sort))) throw new Error('INVALID_VIEW');
	if (params.topN !== null) { count(params.topN); if (params.topN < 1 || params.topN > 1000) throw new Error('INVALID_VIEW'); }
	if (params.columnKey !== null && (typeof params.columnKey !== 'string' || !/^column_[0-9a-f]{24}$/.test(params.columnKey))) throw new Error('INVALID_VIEW');
}

export function parseDelivery(value: unknown, analysisRef: string): ExportDelivery {
	const d = record(value);
	for (const key of ['analysisRef', 'exportRef', 'path', 'contentDigest', 'scopeLabel', 'rendererVersion']) str(d[key]);
	if (d.analysisRef !== analysisRef || !/^export_[0-9a-f]{64}$/.test(d.exportRef as string) || !/^[0-9a-f]{64}$/.test(d.contentDigest as string) || d.registered !== true || typeof d.isFiltered !== 'boolean') throw new Error('INVALID_VIEW');
	const [id, revision] = analysisRef.split(':');
	if (d.path !== `./analysis/${id}/revisions/${revision}/reports/${d.exportRef}.html`) throw new Error('INVALID_VIEW');
	count(d.sizeBytes); if (d.sizeBytes > 10 * 1024 * 1024) throw new Error('INVALID_VIEW');
	if (!['standard', 'table'].includes(String(d.layout))) throw new Error('INVALID_VIEW');
	// Self-contained registered HTML keeps its original renderer identity.
	if (!d.rendererVersion) throw new Error('INVALID_VIEW');
	viewParameters(d.viewParameters);
	return d as unknown as ExportDelivery;
}

export function validateScene(value: unknown): asserts value is ChartScene {
	const scene = record(value);
	if (!['BAR_VALUES', 'BAR_RANK', 'BRIDGE', 'FORMULA', 'HIERARCHY', 'WATERFALL', 'RECONCILIATION'].includes(String(scene.type))) throw new Error('UNSUPPORTED_VERSION');
    str(scene.viewId);
    if (scene.relationship !== undefined) {
        if (!['FORMULA','HIERARCHY'].includes(String(scene.type))) throw new Error('INVALID_VIEW');
        const r = record(scene.relationship); str(r.equation); str(r.note);
        const node = (value: unknown) => { const n = record(value); for (const k of ['key','label','display']) str(n[k]); if (n.note !== undefined) str(n.note);
            for (const k of ['operator','coefficient','contribution']) if (n[k] !== undefined) str(n[k]);
            if (n.tone !== undefined && !['positive','negative'].includes(String(n.tone))) throw new Error('INVALID_VIEW');
            if (n.emphasis !== undefined && typeof n.emphasis !== 'boolean') throw new Error('INVALID_VIEW');
            if (n.weight !== undefined) { coordinate(n.weight); if ((n.weight as number) > 100 || n.contribution === undefined) throw new Error('INVALID_VIEW'); }
        };
        if (r.layout !== undefined && !['compact','flow'].includes(String(r.layout))) throw new Error('INVALID_VIEW');
        node(r.result);
        if (!Array.isArray(r.inputs) || r.inputs.length > 100 || !Array.isArray(r.checks) || r.checks.length > 10) throw new Error('INVALID_VIEW');
        r.inputs.forEach(node);
        r.checks.forEach((value) => { const c=record(value); str(c.label); str(c.display); });
    }
	if (scene.title !== undefined) str(scene.title);
	if (scene.primary !== undefined && typeof scene.primary !== 'boolean') throw new Error('INVALID_VIEW');
	if (scene.collapsed !== undefined && typeof scene.collapsed !== 'boolean') throw new Error('INVALID_VIEW');
	coordinate(scene.width); coordinate(scene.height); coordinate(scene.zeroBaseline); str(scene.description); strings(scene.rowKeys);
	if (!Array.isArray(scene.primitives) || scene.primitives.length > 1000) throw new Error('INVALID_VIEW');
	for (const value of scene.primitives) {
		const p = record(value);
		if (!['positive', 'negative', 'axis', 'label', 'value', 'surface'].includes(String(p.tone))) throw new Error('INVALID_VIEW');
		const fields = p.kind === 'text' ? ['x', 'y'] : p.kind === 'line' ? ['x1', 'x2', 'y1', 'y2'] : p.kind === 'rect' ? ['x', 'y', 'width', 'height'] : null;
		if (!fields) throw new Error('UNSUPPORTED_VERSION');
		fields.forEach((key) => coordinate(p[key]));
		if (p.kind === 'text') str(p.text);
		if (p.kind === 'rect') str(p.rowKey);
	}
}

export function parseSnapshot(value: unknown): ViewSnapshot {
	const v = record(value);
    if (v.freeDocument !== undefined) parseFreeDocument(v.freeDocument);
	if (v.schemaVersion !== 1 || v.rendererVersion !== '1.11.1' || v.engineVersion !== '1.5.0') throw new Error('UNSUPPORTED_VERSION');
	if (typeof v.technicalDetails !== 'boolean' || typeof v.chartsRequested !== 'boolean') throw new Error('INVALID_VIEW');
	for (const key of ['analysisRef', 'dataDigest', 'viewId', 'scopeId', 'scopeLabel', 'claimsScopeLabel', 'engineVersion']) str(v[key]);
	if (!/^ana_[0-9a-f]{32}:[1-9][0-9]{0,8}$/.test(v.analysisRef as string) || typeof v.isFiltered !== 'boolean') throw new Error('INVALID_VIEW');
	viewParameters(v.viewParameters);
	if (!Array.isArray(v.rows) || v.rows.length > 50000 || !Array.isArray(v.metrics) || v.metrics.length > 200) throw new Error('INVALID_VIEW');
	for (const row of v.rows) validateRow(row);
	for (const metric of v.metrics) { const m = record(metric); for (const k of ['viewId', 'valueRef', 'label', 'display', 'unit', 'scopeId']) str(m[k]); if (m.scopeId !== v.scopeId) throw new Error('INVALID_VIEW'); }
    if (!Array.isArray(v.scenes) || v.scenes.length > 100) throw new Error('INVALID_VIEW');
    v.scenes.forEach(validateScene);
    if (typeof v.claimsCollapsed !== 'boolean' || !Array.isArray(v.sections) || v.sections.length > 20) throw new Error('INVALID_VIEW');
    if (!['start', 'end'].includes(String(v.summaryPosition))) throw new Error('INVALID_VIEW');
    str(v.summaryTitle); str(v.reportTitle); strings(v.claims);
    const usedClaims = new Set<number>();
    const claimIndexes = (indexes: unknown) => {
        if (!Array.isArray(indexes) || indexes.length > 50) throw new Error('INVALID_VIEW');
        for (const i of indexes) { count(i); if (i >= (v.claims as string[]).length || usedClaims.has(i)) throw new Error('INVALID_VIEW'); usedClaims.add(i); }
    };
    claimIndexes(v.summaryClaimIndexes);
    const scenes = v.scenes;
    if (!Array.isArray(v.tables)) throw new Error('INVALID_VIEW');
    const usedMetrics = new Set<number>(), usedScenes = new Set<number>(), usedTables = new Set<number>(), groups = new Set<string>();
    for (const value of v.sections) {
        const section = record(value); for (const k of ['groupId','title','description']) str(section[k]);
        if (groups.has(String(section.groupId)) || !Array.isArray(section.blocks) || section.blocks.length > 20) throw new Error('INVALID_VIEW');
        groups.add(String(section.groupId));
        if (!['full', 'half'].includes(String(section.width)) || !['before', 'after'].includes(String(section.claimsPosition))) throw new Error('INVALID_VIEW');
        claimIndexes(section.claimIndexes);
        for (const value of section.blocks) {
            const block = record(value); for (const k of ['viewId','title','description']) str(block[k]);
            for (const [key, values, used] of [['metricIndexes', v.metrics, usedMetrics], ['sceneIndexes', scenes, usedScenes], ['tableIndexes', v.tables, usedTables]] as const) {
                const indexes=block[key];
                if (!Array.isArray(indexes) || indexes.length > 200) throw new Error('INVALID_VIEW');
                for (const i of indexes) { count(i); if (i >= values.length || used.has(i) || record(values[i]).viewId !== block.viewId) throw new Error('INVALID_VIEW'); used.add(i); }
            }
        }
    }
    if (usedClaims.size !== (v.claims as string[]).length) throw new Error('INVALID_VIEW');
    if (usedMetrics.size !== v.metrics.length || usedScenes.size !== scenes.length || usedTables.size !== v.tables.length) throw new Error('INVALID_VIEW');
	for (const key of ['claims', 'sources', 'limitations']) strings(v[key]);
	const states = record(v.statuses);
	for (const key of ['publicationState', 'receiptVerification', 'coverage', 'comparability', 'calculationValidation']) str(states[key]);
	if (states.publicationState !== 'PUBLISHED') throw new Error('INVALID_VIEW');
	count(v.total); count(v.originalTotal); count(v.objectCount); count(v.originalObjectCount); strings(v.memberKeys);
	const members = v.memberKeys as string[];
	if (new Set(members).size !== members.length || members.some((key) => !/^member_[0-9a-f]{24}$/.test(key)) || v.rows.some((row) => !members.includes(String(record(row).memberKey)))) throw new Error('INVALID_VIEW');
	validateTables(v);
	const page = record(v.page); for (const key of ['offset', 'limit', 'total']) count(page[key]); if (page.nextOffset !== null) count(page.nextOffset);
	if (page.total !== v.objectCount || page.offset as number > v.objectCount || (page.limit as number) < 1 || (page.limit as number) > 100 || members.length !== Math.min(page.limit as number, v.objectCount - (page.offset as number)) || v.rows.length > v.total) throw new Error('INVALID_VIEW');
	const next = (page.offset as number) + members.length;
	if (page.nextOffset !== (next < v.objectCount ? next : null)) throw new Error('INVALID_VIEW');
	if (v.chartUnavailable !== null) str(v.chartUnavailable);
	if (v.capabilities !== undefined) { const c = record(v.capabilities); if (typeof c.cards !== 'boolean' || typeof c.generateReports !== 'boolean') throw new Error('INVALID_VIEW'); }
	if (v.reports !== undefined) {
		if (!Array.isArray(v.reports) || v.reports.length > 100) throw new Error('INVALID_VIEW');
		v.reports.forEach((r) => parseDelivery(r, v.analysisRef as string));
	}
	validateActions(v);
	return v as unknown as ViewSnapshot;
}

function validateRow(value: unknown) {
	const r = record(value);
	for (const key of ['rowKey', 'memberKey', 'valueRef', 'label', 'measure', 'display', 'unit', 'state']) str(r[key]);
	if (r.exact !== null) str(r.exact);
}

function validateTables(v: Record<string, unknown>) {
	if (!Array.isArray(v.columns) || v.columns.length > 50000 || !Array.isArray(v.tables) || v.tables.length > 120) throw new Error('INVALID_VIEW');
	const columns = new Set<string>();
	for (const item of v.columns) { const c = record(item); for (const key of ['key','label','unit']) str(c[key]); if (!/^column_[0-9a-f]{24}$/.test(String(c.key)) || columns.has(String(c.key))) throw new Error('INVALID_VIEW'); columns.add(String(c.key)); }
	if (v.activeColumnKey !== null && !v.columns.some((c) => record(c).key === v.activeColumnKey)) throw new Error('INVALID_VIEW');
	if (record(v.viewParameters).columnKey !== null && !v.columns.some((c) => record(c).key === record(v.viewParameters).columnKey)) throw new Error('INVALID_VIEW');
	for (const item of v.tables) {
		const t = record(item);
		if (typeof t.collapsed !== 'boolean') throw new Error('INVALID_VIEW');
		count(t.pageSize); if (t.pageSize < 1 || t.pageSize > 100) throw new Error('INVALID_VIEW');
		str(t.viewId); str(t.tableKey); str(t.title); if (t.note !== undefined) str(t.note);
		if (!['values','formula','calculation'].includes(String(t.kind)) || !Array.isArray(t.columns) || t.columns.length > 1000 || !Array.isArray(t.rows) || t.rows.length > 50000) throw new Error('INVALID_VIEW');
		const keys = new Set<string>();
		for (const item of t.columns) { const c = record(item); for (const key of ['key','label','unit']) str(c[key]); if (keys.has(String(c.key))) throw new Error('INVALID_VIEW'); keys.add(String(c.key)); }
		for (const item of t.rows) {
			const row = record(item); str(row.memberKey); str(row.label); if (row.note !== undefined) str(row.note);
			if (!/^member_[0-9a-f]{24}$/.test(String(row.memberKey))) throw new Error('INVALID_VIEW');
			for (const [key, value] of Object.entries(record(row.cells))) { if (!keys.has(key)) throw new Error('INVALID_VIEW'); validateRow(value); if (record(value).memberKey !== row.memberKey) throw new Error('INVALID_VIEW'); }
		}
	}
}

const ANALYSIS_REF = /^ana_[0-9a-f]{32}:[1-9][0-9]{0,8}$/;
const ACTION_REF = /^(ana_[0-9a-f]{32}:[1-9][0-9]{0,8})#action_[0-9a-f]{24}$/;
function support(value: unknown) { if (!['SUPPORTED', 'NEEDS_EVIDENCE', 'CONTRADICTED'].includes(String(value))) throw new Error('INVALID_VIEW'); }
function validateActions(v: Record<string, unknown>) {
	const findings = v.findings, actions = v.actions, runs = v.actionRuns ?? [];
	if (!Array.isArray(findings) || findings.length > 300 || !Array.isArray(actions) || actions.length > 300 || !Array.isArray(runs) || runs.length > 100) throw new Error('INVALID_VIEW');
	const ids = new Set<string>();
	for (const item of findings) {
		const f = record(item); for (const k of ['findingId', 'statement', 'type', 'basis', 'ruleVersion', 'applicability', 'sourceType']) str(f[k]);
		if (ids.has(f.findingId as string)) throw new Error('INVALID_VIEW'); ids.add(f.findingId as string); support(f.support);
		if (f.previousAnalysisRef !== null && !ANALYSIS_REF.test(String(f.previousAnalysisRef))) throw new Error('INVALID_VIEW');
		if (!Array.isArray(f.explanations) || f.explanations.length > 31) throw new Error('INVALID_VIEW');
		for (const item of f.explanations) { const e = record(item); str(e.statement); strings(e.missingEvidence); support(e.support); if (!['observation', 'arithmetic_composition', 'business_cause_hypothesis'].includes(String(e.kind)) || (e.kind === 'business_cause_hypothesis' && e.support !== 'NEEDS_EVIDENCE')) throw new Error('INVALID_VIEW'); }
	}
	const refs = new Set<string>();
	for (const item of actions) {
		const a = record(item); for (const k of ['actionRef', 'goal', 'scopeLabel', 'expectedDecision', 'availabilityReason', 'priorityBasis']) str(a[k]);
		const match = ACTION_REF.exec(a.actionRef as string);
		strings(a.findingRefs); strings(a.requiredEvidence);
		if (!match || match[1] !== v.analysisRef || refs.has(a.actionRef as string) || !(a.findingRefs as string[]).length || (a.findingRefs as string[]).some((id) => !ids.has(id)) || !['AVAILABLE', 'NEEDS_INPUT', 'UNAVAILABLE'].includes(String(a.availability))) throw new Error('INVALID_VIEW');
		refs.add(a.actionRef as string);
	}
	for (const item of runs) {
		const r = record(item); for (const k of ['actionRunId', 'actionRef', 'updatedAt']) str(r[k]);
		if (!refs.has(r.actionRef as string) || !['PROPOSED', 'RUNNING', 'AWAITING_INPUT', 'PARTIAL', 'COMPLETED', 'FAILED', 'CANCELLED'].includes(String(r.status))) throw new Error('INVALID_VIEW');
		if (r.resultAnalysisRef !== null && (!ANALYSIS_REF.test(String(r.resultAnalysisRef)) || String(r.resultAnalysisRef).split(':')[0] !== String(v.analysisRef).split(':')[0] || Number(String(r.resultAnalysisRef).split(':')[1]) <= Number(String(v.analysisRef).split(':')[1]))) throw new Error('INVALID_VIEW');
		if (r.status === 'COMPLETED' && r.resultAnalysisRef === null) throw new Error('INVALID_VIEW');
		str(r.resultNote);
	}
	str(v.findingsScopeLabel);
	if (v.capabilities !== undefined) { const c = record(v.capabilities); if (typeof c.followUp !== 'boolean') throw new Error('INVALID_VIEW'); }
}

export function parseActionResolution(value: unknown, actionRef: string): { actionRef: string; text: string; availability: string } {
	const r = record(value); str(r.text);
	if (r.actionRef !== actionRef || !ACTION_REF.test(actionRef) || !['AVAILABLE', 'NEEDS_INPUT', 'UNAVAILABLE'].includes(String(r.availability))) throw new Error('INVALID_VIEW');
	return r as { actionRef: string; text: string; availability: string };
}
