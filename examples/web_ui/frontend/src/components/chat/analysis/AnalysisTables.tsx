import { useId, useState } from 'react';

import { Markdown } from '@/components/markdown';
import { Button } from '@/components/ui/button';
import { Disclosure } from '@/components/ui/disclosure';
import { Label } from '@/components/ui/label';
import { SelectField, SelectOption } from '@/components/ui/select-field';
import {
	Table,
	TableHeader,
	TableBody,
	TableRow,
	TableHead,
	TableCell,
} from '@/components/ui/table';
import { useTranslation } from '@/i18n/useI18n';
import { tablePage, type TableOrder } from '@/lib/analysis-table-state';
import { financialColumnAlignment } from '@/lib/financial-table-alignment';
import type { AnalysisTable } from '@/lib/workspace-analysis';

export function AnalysisTables({ tables }: { tables: AnalysisTable[] }) {
	return (
		<div className="space-y-5">
			{tables
				.filter((table) => table.rows.length)
				.map((table) => (
					<AnalysisTableCard key={table.tableKey} table={table} />
				))}
		</div>
	);
}

function AnalysisTableCard({ table }: { table: AnalysisTable }) {
	const { t } = useTranslation();
	const pageSizeId = useId();
	const [selectedPage, setPage] = useState(0);
	const [size, setSize] = useState(table.pageSize);
	const [order, setOrder] = useState<TableOrder>({ columnKey: null, direction: 'published' });
	const { rows: visibleRows, page, pages } = tablePage(table, order, selectedPage, size);
	// 本组件的数据列来自金额/计算事实；列名称仍可明确表达排名或标识。
	const alignments = Object.fromEntries(table.columns.map((column) => [
		column.key, financialColumnAlignment({ label: column.label, dataType: 'DECIMAL_STRING' }),
	]));
	const hasNotes = table.rows.some(
		(row) =>
			row.note ||
			Object.values(row.cells).some((cell) => !['', '已取得'].includes(cell.state)) ||
			Object.keys(row.cells).length < table.columns.length,
	);
	const content = (
		<>
			{table.note && (
				<Markdown className="text-xs leading-5 text-muted-foreground">
					{table.note}
				</Markdown>
			)}
			<div className="rounded border border-border">
				<Table
					className="w-full min-w-[500px] border-collapse text-sm"
					containerProps={{ role: 'region', 'aria-label': table.title, tabIndex: 0 }}
				>
					<TableHeader className="bg-muted text-foreground">
						<TableRow>
							<TableHead
								scope="col"
								className="whitespace-normal min-w-36 p-2 text-left"
							>
								{t(
									table.kind !== 'values'
										? 'analysis.itemName'
										: 'analysis.memberName',
								)}
							</TableHead>
							{table.columns.map((column) => (
								<TableHead
									key={column.key}
									scope="col"
									className="whitespace-normal min-w-36 p-2"
									style={{ textAlign: alignments[column.key] }}
									aria-sort={
										table.kind !== 'values'
											? undefined
											: order.columnKey === column.key &&
												  order.direction !== 'published'
												? order.direction === 'desc'
													? 'descending'
													: 'ascending'
												: 'none'
									}
								>
									{table.kind === 'values' ? (
										<Button
											variant="ghost"
											type="button"
											className="h-auto w-full whitespace-normal px-1 py-2 font-medium"
											style={{ textAlign: alignments[column.key], justifyContent: {
												left: 'flex-start', right: 'flex-end', center: 'center',
											}[alignments[column.key]] }}
											aria-label={`${column.label} ${t('analysis.sort')}`}
											onClick={() => {
												setPage(0);
												setOrder({
													columnKey: column.key,
													direction:
														order.columnKey !== column.key ||
														order.direction === 'published'
															? 'desc'
															: order.direction === 'desc'
																? 'asc'
																: 'published',
												});
											}}
										>
											{column.label}
											{column.unit && (
												<span className="ml-1 text-xs">
													（{column.unit}）
												</span>
											)}
											<span aria-hidden="true" className="ml-2 text-primary">
												{order.columnKey !== column.key ||
												order.direction === 'published'
													? '↕'
													: order.direction === 'desc'
														? '↓'
														: '↑'}
											</span>
										</Button>
									) : (
										<div className="break-words font-medium">
											{column.label}
											{column.unit && (
												<span className="ml-1 text-xs">
													（{column.unit}）
												</span>
											)}
										</div>
									)}
								</TableHead>
							))}
							{hasNotes && (
								<TableHead
									scope="col"
									className="whitespace-normal min-w-40 p-2 text-left"
								>
									{t('analysis.necessaryNotes')}
								</TableHead>
							)}
						</TableRow>
					</TableHeader>
					<TableBody>
						{visibleRows.map((row, index) => (
							<TableRow
								key={`${row.memberKey}:${index}`}
								className="border-t border-border"
							>
								<TableHead
									scope="row"
									className="whitespace-normal break-words p-2 text-left align-top font-normal"
								>
									{row.label}
								</TableHead>
								{table.columns.map((column) => {
									const cell = row.cells[column.key];
									return (
										<TableCell
											key={column.key}
											className="whitespace-normal break-words p-2 align-top tabular-nums"
											style={{ textAlign: alignments[column.key] }}
										>
											{cell?.display ?? '—'}
											{cell?.unit && cell.unit !== column.unit
												? ` ${cell.unit}`
												: ''}
										</TableCell>
									);
								})}
								{hasNotes && (
									<TableCell className="whitespace-normal break-words p-2 text-left align-top text-xs leading-5">
										{[
											row.note,
											...table.columns.map((column) => {
												const cell = row.cells[column.key];
												return !cell
													? `${column.label}：${t('analysis.notSelected')}`
													: !['', '已取得'].includes(cell.state)
														? `${column.label}：${cell.state}`
														: null;
											}),
										]
											.filter(Boolean)
											.join('；')}
									</TableCell>
								)}
							</TableRow>
						))}
					</TableBody>
				</Table>
			</div>
			{table.rows.length > table.pageSize && (
				<div
					className="flex flex-wrap items-center gap-2 text-xs"
					aria-label={t('analysis.tablePagination')}
				>
					<span>
						{page * size + 1}–{Math.min((page + 1) * size, table.rows.length)} /{' '}
						{table.rows.length}
					</span>
					<Label htmlFor={pageSizeId} className="text-xs">
						{t('analysis.tablePageSize')}
					</Label>
					<SelectField
						id={pageSizeId}
						className="h-8 w-20"
						value={String(size)}
						onValueChange={(nextValue) => {
							setSize(Number(nextValue));
							setPage(0);
						}}
					>
						{[...new Set([table.pageSize, 12, 24, 48, 100])]
							.sort((a, b) => a - b)
							.map((n) => (
								<SelectOption key={n} value={n}>
									{n}
								</SelectOption>
							))}
					</SelectField>
					<Button
						variant="outline"
						size="sm"
						type="button"
						disabled={page === 0}
						onClick={() => setPage(page - 1)}
					>
						{t('analysis.previous')}
					</Button>
					<Button
						variant="outline"
						size="sm"
						type="button"
						disabled={page + 1 >= pages}
						onClick={() => setPage(page + 1)}
					>
						{t('analysis.next')}
					</Button>
				</div>
			)}
		</>
	);
	return table.collapsed ? (
		<Disclosure
			className="space-y-2"
			aria-label={table.title}
			summary={<>{table.title}</>}
			summaryClassName="cursor-pointer text-sm font-semibold"
		>
			{content}
		</Disclosure>
	) : (
		<section className="space-y-2" aria-label={table.title}>
			<h3 className="text-sm font-semibold">{table.title}</h3>
			{content}
		</section>
	);
}
