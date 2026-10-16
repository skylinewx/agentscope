import { useState } from 'react';
import { tablePage, type TableOrder } from '@/lib/analysis-table-state';

import { Markdown } from '@/components/markdown';
import { useTranslation } from '@/i18n/useI18n';
import type { AnalysisTable } from '@/lib/workspace-analysis';

export function AnalysisTables({ tables }: { tables: AnalysisTable[] }) {
    return <div className="space-y-5">{tables.filter(table => table.rows.length).map(table => <AnalysisTableCard key={table.tableKey} table={table} />)}</div>;
}

function AnalysisTableCard({ table }: { table: AnalysisTable }) {
    const { t } = useTranslation();
    const [selectedPage, setPage] = useState(0);
    const [size, setSize] = useState(table.pageSize);
    const [order, setOrder] = useState<TableOrder>({ columnKey: null, direction: 'published' });
    const { rows: visibleRows, page, pages } = tablePage(table, order, selectedPage, size);
    const hasNotes = table.rows.some((row) => row.note || Object.values(row.cells).some((cell) => !['', '已取得'].includes(cell.state)) || Object.keys(row.cells).length < table.columns.length);
    const content = <>
			{table.note && <Markdown className="text-xs leading-5 text-slate-600">{table.note}</Markdown>}
			<div className="overflow-x-auto rounded border border-slate-200" role="region" aria-label={table.title} tabIndex={0}>
				<table className="w-full min-w-[500px] border-collapse text-sm">
					<thead className="bg-slate-100 text-slate-800"><tr>
						<th scope="col" className="min-w-36 p-2 text-left">{t(table.kind !== 'values' ? 'analysis.itemName' : 'analysis.memberName')}</th>
						{table.columns.map((column) => <th key={column.key} scope="col" className="min-w-36 p-2 text-right" aria-sort={table.kind !== 'values' ? undefined : order.columnKey === column.key && order.direction !== 'published' ? (order.direction === 'desc' ? 'descending' : 'ascending') : 'none'}>
							{table.kind === 'values' ? <button className="w-full cursor-pointer rounded px-1 py-2 text-right font-medium hover:bg-slate-200 focus-visible:outline-2 focus-visible:outline-teal-700" aria-label={`${column.label} ${t('analysis.sort')}`}
								onClick={() => { setPage(0); setOrder({ columnKey: column.key, direction: order.columnKey !== column.key || order.direction === 'published' ? 'desc' : order.direction === 'desc' ? 'asc' : 'published' }); }}>
								{column.label}{column.unit && <span className="ml-1 text-xs">（{column.unit}）</span>}<span aria-hidden="true" className="ml-2 text-teal-700">{order.columnKey !== column.key || order.direction === 'published' ? '↕' : order.direction === 'desc' ? '↓' : '↑'}</span>
							</button> : <div className="break-words font-medium">{column.label}{column.unit && <span className="ml-1 text-xs">（{column.unit}）</span>}</div>}
						</th>)}
						{hasNotes && <th scope="col" className="min-w-40 p-2 text-left">{t('analysis.necessaryNotes')}</th>}
					</tr></thead>
					<tbody>{visibleRows.map((row, index) => <tr key={`${row.memberKey}:${index}`} className="border-t border-slate-200">
						<th scope="row" className="break-words p-2 text-left align-top font-normal">{row.label}</th>
						{table.columns.map((column) => {
                            const cell = row.cells[column.key];
                            return <td key={column.key} className="break-words p-2 text-right align-top tabular-nums">{cell?.display ?? '—'}{cell?.unit && cell.unit !== column.unit ? ` ${cell.unit}` : ''}</td>;
                        })}
						{hasNotes && <td className="break-words p-2 align-top text-xs leading-5">{[
							row.note,
							...table.columns.map((column) => {
								const cell = row.cells[column.key];
								return !cell ? `${column.label}：${t('analysis.notSelected')}` : !['', '已取得'].includes(cell.state) ? `${column.label}：${cell.state}` : null;
							}),
						].filter(Boolean).join('；')}</td>}
					</tr>)}</tbody>
				</table>
			</div>
        {table.rows.length > table.pageSize && <div className="flex flex-wrap items-center gap-2 text-xs" aria-label={t('analysis.tablePagination')}>
            <span>{page * size + 1}–{Math.min((page + 1) * size, table.rows.length)} / {table.rows.length}</span>
            <label>{t('analysis.tablePageSize')} <select className="rounded border p-1" value={size} onChange={(event) => { setSize(Number(event.target.value)); setPage(0); }}>{[...new Set([table.pageSize, 12, 24, 48, 100])].sort((a, b) => a - b).map((n) => <option key={n} value={n}>{n}</option>)}</select></label>
            <button type="button" className="rounded border px-2 py-1 disabled:opacity-40" disabled={page === 0} onClick={() => setPage(page - 1)}>{t('analysis.previous')}</button>
            <button type="button" className="rounded border px-2 py-1 disabled:opacity-40" disabled={page + 1 >= pages} onClick={() => setPage(page + 1)}>{t('analysis.next')}</button>
        </div>}
    </>;
    return table.collapsed
        ? <details className="space-y-2" aria-label={table.title}><summary className="cursor-pointer text-sm font-semibold">{table.title}</summary>{content}</details>
        : <section className="space-y-2" aria-label={table.title}><h3 className="text-sm font-semibold">{table.title}</h3>{content}</section>;
}
