/** 财务展示按列决定对齐；不转换原值，也不从数字外观推断金额单位。 */
export type FinancialAlignment = 'left' | 'right' | 'center';

type ColumnInput = {
	label: string;
	dataType?: string;
	align?: string | null;
	values?: unknown[];
};

const numericTypes = new Set(['DECIMAL_STRING', 'DECIMAL', 'NUMBER', 'NUMERIC', 'INTEGER', 'FLOAT', 'DOUBLE']);
const missingDisplay = /^(?:[—–-]|null|n\/a|暂无数据|缺数|未填报)?$/i;

function numericDisplay(value: unknown): boolean {
	if (typeof value === 'number') return Number.isFinite(value);
	if (typeof value !== 'string') return false;
	let text = value.trim().replaceAll('−', '-');
	if (text.startsWith('(') && text.endsWith(')')) text = text.slice(1, -1).trim();
	text = text.replace(/^[¥￥$€£]\s*/, '').replace(/[%％‰]$/, '');
	// 无类型信息的前导零字符串更可能是标识，保留文本对齐。
	if (/^[+-]?0\d/.test(text)) return false;
	return /^[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(text);
}

export function financialColumnAlignment({ label, dataType, align, values = [] }: ColumnInput): FinancialAlignment {
	const title = label.trim().replace(/[（(][^()（）]*[)）]\s*$/u, '').trim();
	if (/(?:序号|行号|排名|名次|位次)$/u.test(title)
		|| /(?:^|\s)(?:rank|ranking)$/i.test(title)
		|| /^(?:row\s*(?:no\.?|number)|serial\s*(?:no\.?|number)|no\.?|#)$/i.test(title)) return 'center';
	if (/(?:编码|代码|编号|名称|说明|备注|描述)$/u.test(title)
		|| /^(?:年度|年份|月份|日期|时期|期间|单位|机构|项目|科目|板块)$/u.test(title)
		|| /(?:^|[\s_])(?:code|id|name|description|remarks?|notes?)$/i.test(title)
		|| /^(?:year|month|date|period)$/i.test(title)) return 'left';
	if (numericTypes.has(dataType?.toUpperCase() ?? '')) return 'right';
	if (dataType === 'STRING' || dataType === 'BOOLEAN') return 'left';
	// Markdown 显式对齐承载模型已识别的业务语义，缺失时再按整列内容兜底。
	if (align === 'left' || align === 'right' || align === 'center') return align;
	const present = values.filter((value) => value != null
		&& !(typeof value === 'string' && missingDisplay.test(value.trim())));
	return present.length > 0 && present.every(numericDisplay) ? 'right' : 'left';
}

type TableNode = { type: string; value?: string; children?: TableNode[]; align?: (string | null)[] };

function cellText(node: TableNode): string {
	return node.value ?? (node.children ?? []).map(cellText).join('');
}

/** 只补充 GFM 表格的列对齐，保留表格内容、顺序及其他 Markdown 插件行为。 */
export function remarkFinancialTables() {
	return (tree: TableNode) => {
		function visit(node: TableNode) {
			if (node.type === 'table') {
				const [header, ...rows] = node.children ?? [];
				node.align = (header?.children ?? []).map((cell, index) => financialColumnAlignment({
					label: cellText(cell),
					align: node.align?.[index],
					values: rows.map((row) => row.children?.[index]).filter((cell) => cell !== undefined).map(cellText),
				}));
			}
			for (const child of node.children ?? []) visit(child);
		}
		visit(tree);
	};
}
