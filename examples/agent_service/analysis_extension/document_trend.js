// Mounted inside the isolated report. The model supplies only the chart's placement and reference.
const periodPlots = new Map();
const trendColors = ['#276298', '#ad4e31', '#16806c', '#755091', '#8a671a', '#516572'];
const svgNS = 'http://www.w3.org/2000/svg';
const svgNode = (parent, name, attrs) => {
    const node = document.createElementNS(svgNS, name);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    parent.append(node);
    return node;
};
const trendText = (parent, tag, value) => {
    const node = document.createElement(tag);
    node.textContent = value;
    parent.append(node);
    return node;
};
function renderPeriodPlot(box, chart) {
    const periods = chart.periods, first = periods[0].order, last = periods.at(-1).order;
    const x = period => 12 + 976 * (period.order - first) / (last - first);
    const y = point => 168 - 156 * point.height;
    const figure = document.createElement('figure');
    figure.style.cssText = 'margin:0;min-width:0;max-width:100%;font:inherit';
    const metric = chart.metric.trim();
    const unit = chart.unit ? '（' + chart.unit + '）' : '';
    const caption = metric.normalize('NFKC').endsWith(unit.normalize('NFKC')) ? metric : metric + unit;
    const title = trendText(figure, 'figcaption', caption + ' · 时期趋势');
    title.style.fontWeight = '600';
    if (chart.context) trendText(figure, 'small', chart.context);
    const legend = document.createElement('div');
    legend.style.cssText = 'display:flex;flex-wrap:wrap;gap:.25em 1em;margin:.3em 0';
    figure.append(legend);
    const high = trendText(figure, 'div', (chart.range[1] === chart.range[0] ? '各期有值点均为 ' : '') + groupedNumber(chart.range[1]));
    high.style.fontVariantNumeric = 'tabular-nums';
    const svg = svgNode(figure, 'svg', {viewBox:'0 0 1000 180', preserveAspectRatio:'none', role:'img',
        'aria-label':caption + '，' + periods[0].label + '至' + periods.at(-1).label});
    svg.style.cssText = 'display:block;width:100%;min-width:0;height:152px;overflow:visible';
    for (const py of [12,90,168]) svgNode(svg,'line',{x1:12,x2:988,y1:py,y2:py,stroke:'#dce3e9','stroke-width':1,'vector-effect':'non-scaling-stroke'});
    if (chart.range[0] !== chart.range[1]) trendText(figure,'div',groupedNumber(chart.range[0])).style.fontVariantNumeric='tabular-nums';
    const axis = document.createElement('div');
    axis.style.cssText = 'position:relative;min-height:1.8em;font-size:.9em';
    figure.append(axis);
    const records = [], paths = [], texts = [];
    const remember = (node, value) => texts.push({node, value});
    let previousTick = -Infinity;
    for (let i=0;i<periods.length;i++) {
        const offset = x(periods[i])/10, end = i === periods.length-1;
        if (i && !end && (offset-previousTick < 29 || 98.8 - offset < 29)) continue;
        const tick = trendText(axis,'span',periods[i].label);
        tick.style.cssText = 'position:absolute;white-space:nowrap;top:0;left:'+offset+'%;transform:translateX('+
            (end ? '-100%' : i ? '-50%' : '0')+')';
        remember(tick,periods[i].label);
        previousTick = offset;
    }
    let gaps = false;
    chart.series.forEach((line, index) => {
        const color = trendColors[index % trendColors.length];
        const entry = trendText(legend,'span',line.label);
        entry.style.cssText = 'border-left:4px solid '+color+';padding-left:.45em';
        remember(entry,line.label);
        let previous, segment = [];
        function finish() {
            if (segment.length >= 2) {
                const points = segment.join(' ');
                const path = svgNode(svg,'polyline',{points,fill:'none',stroke:color,'stroke-width':2.5,
                    'vector-effect':'non-scaling-stroke','data-trend-series':String(index)});
                if (index >= trendColors.length) path.setAttribute('stroke-dasharray','6 3');
                paths.push({path,points});
            }
            segment = [];
        }
        if (line.points.length !== periods.length) gaps = true;
        line.points.forEach(point => {
            const period = periods[point.periodIndex];
            if (previous != null && period.order !== previous + 1) { gaps = true; finish(); }
            previous = period.order;
            if (point.value == null) { gaps = true; finish(); return; }
            const cx = x(period), cy = y(point);
            segment.push(cx+','+cy);
            const circle = svgNode(svg,'circle',{cx,cy,r:4,fill:color,stroke:color,'stroke-width':1.5,
                'vector-effect':'non-scaling-stroke','data-trend-period':period.code,'data-trend-series':index});
            const tooltip = svgNode(circle,'title',{});
            tooltip.textContent = line.label+' · '+period.label+' · '+groupedNumber(point.value)+unit;
            records.push({circle,cx,cy,period:period.code,tooltip,text:tooltip.textContent});
        });
        finish();
    });
    if (gaps) trendText(figure,'small','缺少数值或中间时期的位置已断开连线。');
    box.replaceChildren(figure);
    return () => visible(svg) && svg.querySelectorAll('circle').length === records.length
        && svg.querySelectorAll('polyline').length === paths.length
        && texts.every(({node,value}) => node.isConnected && rendered(node) && node.textContent === value)
        && records.every(({circle,cx,cy,period,tooltip,text}) => svg.contains(circle) && visible(circle)
            && circle.cx.baseVal.value.toFixed(3) === Math.fround(cx).toFixed(3)
            && circle.cy.baseVal.value.toFixed(3) === Math.fround(cy).toFixed(3)
            && circle.dataset.trendPeriod === period && tooltip.textContent === text)
        && paths.every(({path,points}) => svg.contains(path) && rendered(path) && path.getAttribute('points') === points
            && getComputedStyle(path).stroke !== 'none' && parseFloat(getComputedStyle(path).strokeWidth)>0);
}
addEventListener('DOMContentLoaded',() => {
    for (const box of document.querySelectorAll('div[data-analysis-trend]')) {
        const ref = box.dataset.analysisTrend;
        if (!Object.hasOwn(data.trends,ref)) continue;
        const verify = renderPeriodPlot(box,data.trends[ref]);
        if (!periodPlots.has(ref)) periodPlots.set(ref,[]);
        periodPlots.get(ref).push(verify);
    }
});
measurePeriodTrends = () => [...periodPlots].filter(([,plots]) => plots.some(verify => verify())).map(([ref]) => ref);
