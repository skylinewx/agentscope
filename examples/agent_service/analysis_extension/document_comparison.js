// The frozen named rows drive both the signed chart and its adjacent table.
addEventListener('DOMContentLoaded', () => {
    for (const container of document.querySelectorAll('[data-markdown-comparison]')) {
        const setKey = container.dataset.markdownComparison;
        const dataset = window.analysisSets[setKey];
        const items = [];
        let extent;
        container.style.cssText = 'display:grid;grid-template-columns:minmax(4em,1fr) minmax(0,2fr) auto;align-items:center;gap:.5em';
        for (const row of dataset.rows) {
            const values = row.values;
            if (values.value == null) continue;
            const line = document.createElement('div');
            line.style.display = 'contents';
            const label = document.createElement('span');
            label.textContent = values.objectLabel || values.object || '空值';
            const track = document.createElement('div');
            track.style.cssText = 'display:flex;min-width:0;height:1em;background:var(--soft)';
            const negative = document.createElement('span');
            const positive = document.createElement('span');
            negative.style.cssText = 'width:50%;display:flex;justify-content:flex-end;box-shadow:inset -1px 0 var(--line)';
            positive.style.width = '50%';
            const element = document.createElement('span');
            const down = Number(values.value) < 0;
            element.style.cssText = 'display:block;height:100%;background:var(' + (down ? '--subtract' : '--accent') + ')';
            (down ? negative : positive).append(element);
            track.append(negative, positive);
            const amount = document.createElement('span');
            line.append(label, track, amount);
            container.append(line);
            const binding = {setKey, factRef: values.factRef};
            window.analysisCharts.bindValueLabel(amount, binding);
            items.push({element, ...binding});
            extent = positive;
        }
        if (items.length) window.analysisCharts.bindLinearBars(items, {extent});
    }
});
