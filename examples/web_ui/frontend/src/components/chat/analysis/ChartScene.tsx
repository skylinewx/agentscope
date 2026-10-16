import type { ChartScene as Scene, Tone } from '@/lib/workspace-analysis';
import './analysis-scenes.css';
import { Markdown } from '@/components/markdown';

const colors: Record<Tone, string> = { positive: '#167d8d', negative: '#b45339', axis: '#9aa9b4', label: '#243b4b', value: '#142d3e', surface: '#f1f5f9' };

export function ChartScene({ scene, showTitle = true }: { scene: Scene; showTitle?: boolean }) {
	const title = scene.title || scene.description || '数值图表';
	return <figure aria-label={showTitle ? undefined : title} className="m-0 min-w-0 overflow-hidden rounded-xl border border-slate-200/80 bg-white p-4 text-slate-800 shadow-sm">
		{showTitle && scene.title && <figcaption className="mb-3 break-words text-sm font-semibold leading-6">{scene.title}</figcaption>}
		{scene.relationship ? <RelationshipDiagram graph={scene.relationship} hierarchy={scene.type === 'HIERARCHY'} /> : <div className="overflow-x-auto" tabIndex={0} role="region" aria-label={title}>
			<svg viewBox={`0 0 ${scene.width} ${scene.height}`} role="img" aria-label={title} className="mx-auto block w-full" style={{ minWidth: scene.width }}>
				<title>{title}</title>
				{scene.primitives.map((p, index) => p.kind === 'text' ? <text key={index} x={p.x} y={p.y} fill={colors[p.tone]} fontSize="13" fontWeight={p.tone === 'value' ? 600 : 400}>{p.text}</text> : p.kind === 'line' ? <line key={index} x1={p.x1} x2={p.x2} y1={p.y1} y2={p.y2} stroke={colors[p.tone]} /> : <rect key={index} x={p.x} y={p.y} width={p.width} height={p.height} rx={p.tone === 'surface' ? 8 : 0} fill={colors[p.tone]} />)}
			</svg>
		</div>}
		{scene.description && <div className="mt-3 border-t border-slate-100 pt-2 text-xs leading-5 text-slate-500"><Markdown>{scene.description}</Markdown></div>}
	</figure>;
}

function RelationshipDiagram({ graph, hierarchy }: { graph: NonNullable<Scene['relationship']>; hierarchy: boolean }) {
    const node = (item: typeof graph.result, result = false) => <div key={item.key} data-tone={item.tone} data-emphasis={item.emphasis || undefined} className={`relation-node ${result ? 'relation-result' : ''}`}>
        <p className="relation-label">{item.operator && <span className="relation-operator">{item.operator}{item.coefficient && ` ${item.coefficient} ×`}</span>}{item.label}</p>
        <p className="relation-value">{item.display}</p>
        {item.weight !== undefined && <div className="relation-magnitude" role="img" aria-label={`本图相对贡献大小，影响值 ${item.contribution}`} title={`影响值 ${item.contribution}`}><span style={{ width: `${item.weight}%` }} /></div>}
        {item.note && <p className="relation-caption">{item.note}</p>}
    </div>;
    const inputs = <div className="relation-inputs">{graph.inputs.map(item => node(item))}</div>;
    const output = <div className="relation-output">{node(graph.result, true)}{graph.checks.length > 0 && <div className="relation-checks">{graph.checks.map((c,i) => <div key={i}><p className="relation-caption">{c.label}</p><p className="relation-value">{c.display}</p></div>)}</div>}</div>;
    return <div className="relation-container">
        {graph.equation && <p className="relation-equation">{graph.equation}</p>}
        <div className={`relation-layout relation-${graph.layout ?? 'compact'}`}>
            {hierarchy ? output : inputs}<div className="relation-arrow" aria-hidden="true">→</div>{hierarchy ? inputs : output}
        </div>{graph.note && <p className="relation-caption">{graph.note}</p>}
    </div>;
}
