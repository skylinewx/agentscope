// Expand the formula component only at the position chosen by the report author.
addEventListener('DOMContentLoaded', () => {
    const containers = document.querySelectorAll('[data-analysis-waterfalls]');
    if (!containers.length) return;
    const sheet = document.createElement('style');
    sheet.textContent = `
      [data-analysis-waterfalls] .wf-steps{display:grid;grid-template-columns:minmax(6em,1fr) minmax(0,2fr) max-content;column-gap:.8em}
      [data-analysis-waterfalls] .wf-step{display:grid;grid-column:1/-1;grid-template-columns:subgrid;align-items:center;margin:.325em 0}
      [data-analysis-waterfalls] .wf-track{position:relative;height:1.3em;border-radius:5px;background:var(--track,#eef1f5)}
      [data-analysis-waterfalls] .wf-bar{top:0;bottom:0;border-radius:5px;background:var(--increase,#185fa5)}
      [data-analysis-waterfalls] .wf-down{background:var(--decrease,#c3502f)}
      [data-analysis-waterfalls] .wf-total{background:var(--total,#31424d)}
      [data-analysis-waterfalls] .wf-zero{position:absolute;top:0;bottom:0;border-left:1px solid #aab4bf}
      [data-analysis-waterfalls] .wf-number{font-variant-numeric:tabular-nums;text-align:right}
      @media(max-width:600px){[data-analysis-waterfalls] .wf-steps{grid-template-columns:minmax(0,1fr) max-content}
        [data-analysis-waterfalls] .wf-track{grid-row:2;grid-column:1/-1}}
    `;
    document.head.append(sheet);
    function append(parent, tag, text, className) {
        const el = document.createElement(tag);
        if (className) el.className = className;
        if (text) el.textContent = text;
        parent.append(el);
        return el;
    }
    const name = item => item?.shortLabel || (item?.headers || []).filter(Boolean).join(' / ') || '名称待确认';
    for (const container of containers) {
        const setKey = container.dataset.analysisWaterfalls;
        const set = data.sets[setKey];
        if (set?.kind !== 'FORMULA') { error('构成图需要绑定真实的公式数据集'); continue; }
        for (const node of set.nodes) {
            const box = append(container, 'div');
            const unit = node.unit ? '（' + node.unit + '）' : '';
            append(box, 'h4', name(node) + unit);
            const inputs = new Map(node.operands.map(item => [item.factRef, item]));
            const graph = node.waterfall;
            const brief = node.expressionTree?.kind === 'ref' && node.localStatus === 'MATCH'
                && /^[-+]?0+(?:\.0+)?$/.test(node.values.localDifference?.value) && node.values.sourceValue?.value != null;
            append(box, 'p', name(node) + ' = ' + node.displayExpression + (brief ? '　' + node.values.sourceValue.display + unit : ''));
            if (graph && graph.steps.length >= 3) {
                const steps = append(box, 'div', '', 'wf-steps');
                const rows = graph.steps.map((step, stepIndex) => {
                    const total = step.kind === 'TOTAL';
                    const input = total ? node : inputs.get(step.factRef);
                    const row = append(steps, 'div', '', 'wf-step');
                    const labelElement = append(row, 'span', step.label || name(input));
                    const track = append(row, 'div', '', 'wf-track');
                    const zero = append(track, 'span', '', 'wf-zero');
                    const element = append(track, 'span', '', 'wf-bar ' + (total ? 'wf-total' : String(step.value).startsWith('-') ? 'wf-down' : ''));
                    const label = append(row, 'span', '', 'wf-number');
                    bindValueLabel(label, {setKey, nodeRef: node.factRef, stepIndex});
                    return {element, track, zero, stepIndex, labelElement};
                });
                bindHorizontalWaterfall(rows, {setKey, nodeRef: node.factRef});
            } else if (!brief) {
                for (const item of node.operands)
                    append(box, 'p', name(item) + '　' + (item.value == null ? '空值' : item.display));
            }
            const nulls = node.operands.filter(item => item.nullAsZero).map(name);
            if (nulls.length) append(box, 'p', nulls.join('、') + '来源为空，按 0 参与本层运算。');
            const describe = (label, field) => label + ' ' + (field?.value == null ? '空值' : field.display);
            if (!brief) append(box, 'p', [describe('来源值', node.values.sourceValue), describe('本层复算', node.values.localValue),
                describe('本层差额', node.values.localDifference)].join('　'));
            if (node.recursiveStatus === 'MISMATCH') append(box, 'p', describe('递归复算差额', node.values.recursiveDifference));
            const detail = append(box, 'details');
            append(detail, 'summary', '查看来源公式与科目全称');
            append(detail, 'p', (node.headers || []).join(' / ') + ' = ' + node.sourceExpression);
            for (const item of node.operands) append(detail, 'p', (item.headers || []).join(' / '));
        }
    }
});
