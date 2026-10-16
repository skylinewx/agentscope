import type { AnalysisTable } from './workspace-analysis.ts';

export type TableOrder = { columnKey: string | null; direction: 'published' | 'asc' | 'desc' };

// Compare decimal strings without a floating-point conversion or exponent-sized allocation.
function decimal(value: string) {
    const match = /^(-?)(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/.exec(value);
    if (!match) throw new Error('INVALID_DECIMAL');
    const digits = (match[2] + (match[3] ?? '')).replace(/^0+/, '');
    return { sign: digits ? (match[1] ? -1 : 1) : 0, digits: digits.replace(/0+$/, ''),
        magnitude: BigInt(match[4] ?? '0') - BigInt((match[3] ?? '').length) + BigInt(digits.length) };
}

export function compareExact(left: string, right: string) {
    const a = decimal(left), b = decimal(right);
    if (a.sign !== b.sign) return a.sign - b.sign;
    if (!a.sign) return 0;
    if (a.magnitude !== b.magnitude) return (a.magnitude < b.magnitude ? -1 : 1) * a.sign;
    const length = Math.max(a.digits.length, b.digits.length);
    const x = a.digits.padEnd(length, '0'), y = b.digits.padEnd(length, '0');
    return (x === y ? 0 : x < y ? -1 : 1) * a.sign;
}

export function tablePage(table: AnalysisTable, order: TableOrder, selectedPage: number, size: number) {
    const rows = [...table.rows];
    if (order.columnKey !== null && order.direction !== 'published') {
        const key = order.columnKey;
        rows.sort((a, b) => {
            const x = a.cells[key]?.exact, y = b.cells[key]?.exact;
            if (x == null) return y == null ? 0 : 1;
            if (y == null) return -1;
            return compareExact(x, y) * (order.direction === 'desc' ? -1 : 1);
        });
    }
    const pages = Math.max(1, Math.ceil(rows.length / size));
    const page = Math.max(0, Math.min(selectedPage, pages - 1));
    return { rows: rows.slice(page * size, (page + 1) * size), page, pages };
}
