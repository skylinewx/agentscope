/* Generic measurements of the rendered document; never chooses its layout. */
(()=>{
const data=JSON.parse(document.getElementById('analysis-host-data').textContent);
const freeze=x=>{if(x&&typeof x==='object'){Object.values(x).forEach(freeze);Object.freeze(x);}};freeze(data);
Object.defineProperty(window,'analysisData',{value:data.values,writable:false,configurable:false});
Object.defineProperty(window,'analysisSets',{value:data.sets,writable:false,configurable:false});
const errors=[];
const error=text=>{if(errors.length<20)errors.push(String(text).slice(0,300));};
addEventListener('error',e=>error(e.message||'resource error'));
addEventListener('unhandledrejection',e=>error(e.reason?.message||'unhandled rejection'));
addEventListener('securitypolicyviolation',e=>error('resource policy: '+e.violatedDirective));
// 只绑定现有图形的数值和线性长度，不创建布局、颜色或章节。
// 标记与数值绑定由同一函数设置在实际条形上，避免父容器误标。
let linearScaleSequence=0;
const waterfallBindings=new WeakMap();
const linearBindings=new WeakMap();
// 原生统计和脚本表格使用 columns/rows；排序集使用对象行。两者共享同一读取与测量逻辑。
// 仅接受明确的 factRef/value 列，不猜金额列，也不把空值当成零。
const setDatum=(setKey,factRef)=>{
    const set=data.sets[setKey];
    if(!set||!Array.isArray(set.rows))return undefined;
    if(set.kind!=='TABULAR')return set.rows.find(row=>row.factRef===factRef);
    const names=(set.columns||[]).map(column=>column.name);
    const refIndex=names.indexOf('factRef'),valueIndex=names.indexOf('value');
    if(refIndex<0||valueIndex<0||names.lastIndexOf('factRef')!==refIndex||names.lastIndexOf('value')!==valueIndex)return undefined;
    const rows=set.rows.filter(row=>row[refIndex]===factRef);
    return rows.length===1?{factRef,value:rows[0][valueIndex]}:undefined;
};
const bindLinearBars=(items,options={})=>{
    // 每次调用默认独立刻度，避免局部放大图误用全量图的分组名称。
    // 明确共享名称时，检查器仍会验证跨图比例，不静默拆组放过失真。
    const axis=options.axis||'x',scale=options.scale||('auto-linear-'+(++linearScaleSequence));
    if(!Array.isArray(items)||!items.length||!scale||!['x','y'].includes(axis))throw new Error('线性条形需要元素、比例尺及有效轴向');
    const rows=items.map(item=>{
        const element=item.element;
        const datum=item.valueKey?data.values[item.valueKey]:setDatum(item.setKey,item.factRef);
        if(!(element instanceof Element)||datum?.value==null||!Number.isFinite(Number(datum.value)))throw new Error('线性条形必须绑定实际非空数值');
        waterfallBindings.delete(element);
        delete element.dataset.analysisWaterfallSet;delete element.dataset.analysisWaterfallNode;delete element.dataset.analysisWaterfallStep;
        element.dataset.analysisScale=scale;element.dataset.analysisBarAxis=axis;
        if(item.valueKey){element.dataset.analysisBarValue=item.valueKey;delete element.dataset.analysisBarSet;delete element.dataset.analysisBarRef;}
        else{delete element.dataset.analysisBarValue;element.dataset.analysisBarSet=item.setKey;element.dataset.analysisBarRef=item.factRef;}
        return {element,value:Math.abs(Number(datum.value))};
    });
    const maximum=options.maximum??Math.max(...rows.map(row=>row.value));
    if(!Number.isFinite(maximum)||maximum<Math.max(...rows.map(row=>row.value)))throw new Error('线性条形的刻度上限不得小于实际最大值');
    const draw=()=>{
        const pixels=options.extent?.getBoundingClientRect()[axis==='x'?'width':'height']??options.pixels;
        if(!Number.isFinite(pixels)||pixels<0)throw new Error('线性条形需要真实容器或非负像素长度');
        for(const row of rows){
            const length=maximum?row.value/maximum*pixels:0;
            linearBindings.set(row.element,{axis,length});
            if(row.element instanceof SVGElement)row.element.setAttribute(axis==='x'?'width':'height',String(length));
            else row.element.style[axis==='x'?'width':'height']=length+'px';
        }
    };
    draw();
    // 观察回调内直接改子条形会再次触发布局，浏览器将未送达通知报为循环错误。
    // 同帧合并到下一次绘制；仍由实际容器尺寸计算，不改布局或放宽几何检查。
    let pendingFrame=null;
    const scheduleDraw=()=>{
        if(pendingFrame!==null)return;
        pendingFrame=requestAnimationFrame(()=>{pendingFrame=null;draw();});
    };
    const observer=options.extent?new ResizeObserver(scheduleDraw):null;
    observer?.observe(options.extent);
    return ()=>{observer?.disconnect();if(pendingFrame!==null)cancelAnimationFrame(pendingFrame);};
};
// 瀑布图复用同一数值检查链路。金额和累计区间由宿主精确计算，浏览器只定位图形。
const bindHorizontalWaterfall=(items,options={})=>{
    const node=data.sets[options.setKey]?.nodes?.find(item=>item.factRef===options.nodeRef);
    const waterfall=node?.waterfall;
    if(!waterfall||!Array.isArray(items)||items.length!==waterfall.steps.length
       ||new Set(items.map(item=>item.stepIndex)).size!==items.length)
        throw new Error('横向瀑布图需要完整的本层公式 waterfall.steps，包含全部贡献和计算结果');
    const scale='auto-waterfall-'+(++linearScaleSequence);
    const bindings=items.map(item=>{
        const step=waterfall.steps[item.stepIndex],element=item.element,track=item.track;
        if(!Number.isInteger(item.stepIndex)||!step||!(element instanceof HTMLElement)
           ||!(track instanceof HTMLElement)||element.parentElement!==track)
            throw new Error('横向瀑布图每一步需要实际条形、其直接父轨道及有效 stepIndex');
        return {element,track,step,index:item.stepIndex};
    });
    if(new Set(bindings.map(item=>item.element)).size!==bindings.length)
        throw new Error('横向瀑布图每一步需要独立条形，不能用同一元素覆盖多个贡献');
    for(const {element,track,step,index} of bindings){
        // 专用轨道只负责定位；厚度、标签、颜色和报告布局仍由页面决定。
        if(getComputedStyle(track).position==='static')track.style.position='relative';
        element.style.position='absolute';element.style.display='block';element.style.boxSizing='border-box';
        element.style.left=(step.geometry.left*100)+'%';element.style.width=(step.geometry.width*100)+'%';
        element.dataset.analysisScale=scale;element.dataset.analysisBarAxis='x';
        element.dataset.analysisWaterfallSet=options.setKey;element.dataset.analysisWaterfallNode=options.nodeRef;
        element.dataset.analysisWaterfallStep=String(index);
        delete element.dataset.analysisBarValue;delete element.dataset.analysisBarSet;delete element.dataset.analysisBarRef;
        waterfallBindings.set(element,{track,step});
    }
};
Object.defineProperty(window,'analysisCharts',{value:Object.freeze({bindLinearBars,bindHorizontalWaterfall}),writable:false,configurable:false});
const send=()=>{
    if(!document.body)return;
    const rect=document.body.getBoundingClientRect(),style=getComputedStyle(document.body);
    const height=Math.ceil(rect.height+(parseFloat(style.marginTop)||0)+(parseFloat(style.marginBottom)||0));
    parent.postMessage({type:'analysis-document-size',key:FRAME_KEY,height:Math.max(160,Math.min(24000,height))},'*');
};
// Intentional hiding and broken geometry are different: a declared, unhidden
// bar must be checked even when the browser gives it no drawable box.
const rendered=el=>{
    const s=getComputedStyle(el);
    if(s.visibility==='hidden'||s.visibility==='collapse')return false;
    for(let p=el;p;p=p.parentElement){
        const style=getComputedStyle(p);
        if(style.display==='none'||style.contentVisibility==='hidden'||Number(style.opacity)===0)return false;
        if(p!==el&&p.tagName==='DETAILS'&&!p.open){
            const summary=[...p.children].find(child=>child.tagName==='SUMMARY');
            if(!summary?.contains(el))return false;
        }
    }
    return true;
};
const visible=el=>{
    const r=el.getBoundingClientRect();
    return r.width>0&&r.height>0&&rendered(el);
};
const label=el=>(el.getAttribute('aria-label')||el.textContent||el.id||el.tagName).trim().slice(0,100);
const elementPath=el=>{
    const parts=[];
    for(let node=el;node&&parts.length<4;node=node.parentElement){
        parts.unshift(node.tagName.toLowerCase()+(node.id?'#'+node.id:'.'+[...node.classList].slice(0,2).join('.')));
        if(node.id)break;
    }
    return parts.join(' > ').slice(0,120);
};
// These measurements warn about readability; they never choose a layout or resize text.
const minimumFontPx=12;
const minimumScale=m=>{
    const sum=m.a*m.a+m.b*m.b+m.c*m.c+m.d*m.d,det=m.a*m.d-m.b*m.c;
    return Math.sqrt(Math.max(0,(sum-Math.sqrt(Math.max(0,sum*sum-4*det*det)))/2));
};
const fontPixels=el=>{
    const font=parseFloat(getComputedStyle(el).fontSize);
    if(el instanceof SVGGraphicsElement){
        const matrix=el.getScreenCTM();
        return matrix?font*minimumScale(matrix):font;
    }
    let matrix=new DOMMatrix(),zoom=1;
    for(let p=el;p;p=p.parentElement){
        const style=getComputedStyle(p);
        if(style.transform!=='none')matrix=new DOMMatrix(style.transform).multiply(matrix);
        zoom*=parseFloat(style.zoom)||1;
    }
    return font*minimumScale(matrix)*zoom;
};
const textBounds=el=>{
    if(el instanceof SVGGraphicsElement)return el.getBoundingClientRect();
    const range=document.createRange();range.selectNodeContents(el);
    return range.getBoundingClientRect();
};
const measure=()=>{
    const width=document.documentElement.clientWidth,overflow=[],overlaps=[],orders=[],smallText=[],clippedText=[],encoder=new TextEncoder();
    // Opt-in declarations compare authored labels/geometries with the actual
    // visible rows and frozen values. They never choose or rewrite a chart.
    const consistencyErrors=[];
    const mismatch=text=>{if(consistencyErrors.length<20)consistencyErrors.push(text.slice(0,300));};
    const counters=[...document.querySelectorAll('[data-analysis-count-for]')];
    const declaredBars=[...document.querySelectorAll('[data-analysis-scale]')];
    const numericChecks={counts:{declared:counters.length,measured:0,failed:0},
        linearBars:{declared:declaredBars.length,measured:0,invalid:0,scaleGroups:0,checkedGroups:0,failedGroups:0}};
    for(const counter of counters.slice(0,50)){
        if(!visible(counter))continue;
        numericChecks.counts.measured++;
        const container=document.getElementById(counter.dataset.analysisCountFor);
        const text=counter.textContent.trim();
        if(!container||!/^\d+$/.test(text)){
            numericChecks.counts.failed++;mismatch('集合计数标记无效：'+label(counter));continue;
        }
        const rows=[...container.querySelectorAll('[data-analysis-ref]')].filter(visible);
        const count=rows.filter(row=>!rows.some(parent=>parent!==row&&parent.contains(row))).length;
        if(Number(text)!==count){numericChecks.counts.failed++;mismatch('集合计数不一致：'+text+'，实际可见 '+count+' 项（'+counter.dataset.analysisCountFor+'）');}
    }
    const scales=new Map();
    for(const bar of declaredBars.slice(0,1000)){
        const axis=bar.dataset.analysisBarAxis||'x';
        if(!rendered(bar))continue;
        numericChecks.linearBars.measured++;
        const scalar=bar.dataset.analysisBarValue;
        const waterfall=waterfallBindings.get(bar);
        const datum=waterfall?.step||(scalar?data.values[scalar]:setDatum(bar.dataset.analysisBarSet,bar.dataset.analysisBarRef));
        const value=datum?.value==null?NaN:Number(datum.value);
        if(!Number.isFinite(value)){
            numericChecks.linearBars.invalid++;
            mismatch('线性条形缺少有效绑定：'+elementPath(bar)+'；比例尺标记必须位于实际条形元素，并绑定 data-analysis-bar-value 或 bar-set/bar-ref。容器不要标 scale；优先用 analysisCharts.bindLinearBars 自动绑定。');
            continue;
        }
        if(!['x','y'].includes(axis)){numericChecks.linearBars.invalid++;mismatch('线性条形轴向无效：'+axis);continue;}
        const box=bar.getBoundingClientRect();
        const length=box[axis==='x'?'width':'height'],thickness=box[axis==='x'?'height':'width'];
        const linear=linearBindings.get(bar);
        // 绑定组件的真实计算长度可能低于浏览器布局精度；不放大金额，也不误拦正确比例。
        let subpixel=linear?.axis===axis&&linear.length>0&&linear.length<0.5
            &&Math.abs(length-linear.length)<0.5;
        if(waterfall){
            const {track,step}=waterfall,trackBox=track.getBoundingClientRect();
            const zoom=track.offsetWidth?trackBox.width/track.offsetWidth:0;
            const expectedLeft=trackBox.left+(track.clientLeft+step.geometry.left*track.clientWidth)*zoom;
            const expectedWidth=step.geometry.width*track.clientWidth*zoom;
            // 极小贡献可能小于浏览器可绘像素；保留精确标签，不用最小宽度放大。
            subpixel=expectedWidth>0&&expectedWidth<0.5;
            if(bar.parentElement!==track||bar.offsetParent!==track||!zoom
               ||Math.abs(box.left-expectedLeft)>0.5||Math.abs(box.width-expectedWidth)>0.5){
                numericChecks.linearBars.invalid++;
                mismatch('横向瀑布图累计位置或长度与公式不一致：'+elementPath(bar)+'；保留 bindHorizontalWaterfall 的 left/width，检查轨道宽度及条形 margin、transform、最小宽度。');
                continue;
            }
        }
        if(thickness<=0||(value!==0&&length<=0&&!subpixel)){
            numericChecks.linearBars.invalid++;
            mismatch('线性条形未形成可绘制尺寸：'+(bar.id||label(bar))+'；绑定 '+(scalar||bar.dataset.analysisBarRef)+
                '，display='+getComputedStyle(bar).display+'，实际宽高 '+box.width+' × '+box.height+
                '。检查标记是否在实际图形上及其 display、宽高和父容器；非零值必须可见，零值可为零长度。');
            continue;
        }
        const key=bar.dataset.analysisScale+':'+axis;
        if(!scales.has(key))scales.set(key,[]);
        scales.get(key).push({value:Math.abs(value),length,subpixel,path:elementPath(bar)});
    }
    numericChecks.linearBars.scaleGroups=scales.size;
    for(const [key,bars] of scales){
        const pivot=bars.reduce((a,b)=>b.value>a.value?b:a,{value:0,length:0});
        // A lone nonzero bar does not establish a comparative scale. Zero bars
        // still have a checkable zero length, and positive values cannot vanish.
        const inconsistent=bars.some(bar=>(bar.value>0&&bar.length===0&&!bar.subpixel)||Math.abs(bar.length-(pivot.value?bar.value/pivot.value*pivot.length:0))>0.5);
        if(inconsistent||bars.length>1||pivot.value===0)numericChecks.linearBars.checkedGroups++;
        if(inconsistent){
            numericChecks.linearBars.failedGroups++;
            const different=bars.find(bar=>Math.abs(bar.length-(pivot.value?bar.value/pivot.value*pivot.length:0))>0.5);
            mismatch('线性条形比例不一致：'+key+'；'+(different?.path||'')+'。同名 scale 共享刻度；全量图与局部放大图须使用不同组名，或分别调用 analysisCharts.bindLinearBars 并省略 scale；同图检查最小长度与绑定值。');
        }
    }
    let diagnosticsTruncated=false;
    const observe=(items,item)=>{if(items.length<30)items.push(item);else diagnosticsTruncated=true;};
    // A narrow sample is bounded and explicitly reports truncation, not an all-page pass.
    const elements=[...document.body.querySelectorAll('*')], limit=5000;
    for(const el of elements.slice(0,limit)){
        if(!visible(el)||['SCRIPT','STYLE'].includes(el.tagName))continue;
        const r=el.getBoundingClientRect();
        // Scrollable local tables are deliberate; page-wide overflow is not hidden.
        let left=r.left,right=r.right;
        for(let p=el.parentElement;p&&p!==document.body;p=p.parentElement){
            if(['auto','scroll','hidden','clip'].includes(getComputedStyle(p).overflowX)){
                const clip=p.getBoundingClientRect();left=Math.max(left,clip.left);right=Math.min(right,clip.right);
            }
        }
        if(right>width+2||left< -2)observe(overflow,{text:label(el),left:Math.round(left),right:Math.round(right)});
        if(![...el.childNodes].some(node=>node.nodeType===Node.TEXT_NODE&&node.textContent.trim()))continue;
        const font=fontPixels(el);
        if(Number.isFinite(font)&&font>0&&font<minimumFontPx-0.1)
            observe(smallText,{text:label(el),fontPx:Math.max(0.01,Math.round(font*100)/100)});
        const bounds=textBounds(el);
        if(!bounds.width||!bounds.height)continue;
        // SVG labels can be cut off inside their own viewport without page overflow.
        // Deliberately scrollable regions are excluded; hidden/clip regions are not.
        for(let p=el;p;p=p.parentElement){
            const style=getComputedStyle(p),clip=p.getBoundingClientRect(),edges=[];
            if(['hidden','clip'].includes(style.overflowX)){
                if(bounds.left<clip.left-2)edges.push('left');
                if(bounds.right>clip.right+2)edges.push('right');
            }
            if(['hidden','clip'].includes(style.overflowY)){
                if(bounds.top<clip.top-2)edges.push('top');
                if(bounds.bottom>clip.bottom+2)edges.push('bottom');
            }
            if(edges.length){
                observe(clippedText,{text:label(el),container:label(p),edges});
                break;
            }
        }
    }
    let labelChecks=0;
    for(const group of document.querySelectorAll('svg,[data-analysis-label-group]')){
        const labels=[...group.querySelectorAll(group.tagName.toLowerCase()==='svg'?'text':'[data-analysis-label]')].filter(visible);
        for(let i=0;i<labels.length&&labelChecks<20000;i++)for(let j=i+1;j<labels.length&&labelChecks++<20000;j++){
            const a=labels[i],b=labels[j];
            if(a.contains(b)||b.contains(a))continue;
            const x=a.getBoundingClientRect(),y=b.getBoundingClientRect();
            if(Math.min(x.right,y.right)-Math.max(x.left,y.left)>2&&Math.min(x.bottom,y.bottom)-Math.max(x.top,y.top)>2)
                observe(overlaps,{first:label(a),second:label(b)});
        }
    }
    const containers=[...document.querySelectorAll('[data-analysis-set]')];
    let refBytes=0;
    for(const container of containers.slice(0,50)){
        if(!visible(container))continue;
        const axis=container.dataset.analysisOrderAxis==='x'?'left':'top';
        const items=[...container.querySelectorAll('[data-analysis-ref]')].filter(el=>el.closest('[data-analysis-set]')===container&&visible(el));
        items.sort((a,b)=>a.getBoundingClientRect()[axis]-b.getBoundingClientRect()[axis]);
        const refs=[];
        for(const el of items){
            const ref=el.getAttribute('data-analysis-ref');
            refBytes+=encoder.encode(JSON.stringify(ref)).length;
            if(refBytes>18000||refs.length>=2000)break;
            refs.push(ref);
        }
        orders.push({key:container.dataset.analysisSet,partial:container.dataset.analysisPartial==='true',refs,truncated:refs.length<items.length});
    }
    const result={version:3,viewport:{width,height:Math.round(innerHeight)},numericChecks,
        pageOverflow:document.documentElement.scrollWidth>width+2,overflow,overlaps,smallText,clippedText,orders,errors:[...errors,...consistencyErrors].slice(0,20),
        truncated:diagnosticsTruncated||elements.length>limit||containers.length>50||counters.length>50||declaredBars.length>1000||labelChecks>=20000};
    // Fit the host's 60 kB transport limit without losing script errors or order evidence.
    const diagnostics=[overflow,overlaps,smallText,clippedText];
    while(encoder.encode(JSON.stringify(result)).length>58000){
        const largest=diagnostics.filter(items=>items.length).sort((a,b)=>JSON.stringify(b).length-JSON.stringify(a).length)[0];
        if(!largest)break;
        largest.pop();result.truncated=true;
    }
    return result;
};
addEventListener('DOMContentLoaded',()=>{new ResizeObserver(send).observe(document.body);send();});
addEventListener('load',send);
addEventListener('message',e=>{
    if(e.source!==parent||e.data?.key!==FRAME_KEY)return;
    if(e.data.type==='analysis-document-measure')send();
    if(e.data.type==='analysis-document-inspect'&&typeof e.data.requestId==='string'&&e.data.requestId.length<=80){
        const requestId=e.data.requestId;
        requestAnimationFrame(()=>requestAnimationFrame(()=>parent.postMessage({type:'analysis-document-inspection',key:FRAME_KEY,requestId,measurements:measure()},'*')));
    }
});
})();
