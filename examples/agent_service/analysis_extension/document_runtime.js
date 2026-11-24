/* Generic measurements of the rendered document; never chooses its layout. */
(()=>{
const data=JSON.parse(document.getElementById('analysis-host-data').textContent);
// A report template may pass either the wrapped row or its business fields to a
// chart. Both views read the same frozen cell; never copy values or shadow row metadata.
for(const set of Object.values(data.sets)){
    if(set.rowContract!=='BUSINESS_DATASET_V1'||!Array.isArray(set.rows))continue;
    const fields=Object.create(Object.prototype);
    const reserved=new Set(['rowRef','values','valueStates','metadata','then','toJSON']);
    for(const {name} of set.columns){
        if(typeof name!=='string'||reserved.has(name)||name in fields)continue;
        Object.defineProperty(fields,name,{get(){return Object.hasOwn(this.values,name)?this.values[name]:undefined;}});
    }
    Object.freeze(fields);
    for(const row of set.rows)Object.setPrototypeOf(row,fields);
}
const freeze=x=>{if(x&&typeof x==='object'){Object.values(x).forEach(freeze);Object.freeze(x);}};freeze(data);
Object.defineProperty(window,'analysisData',{value:data.values,writable:false,configurable:false});
Object.defineProperty(window,'analysisSets',{value:data.sets,writable:false,configurable:false});
const errors=[];
const error=text=>{if(errors.length<20)errors.push(String(text).slice(0,300));};
let measurePeriodTrends=()=>[];
// The numeric binding already establishes that this text is a value, not an ID.
const groupedNumber = value => {
    const raw = String(value);
    const match = /^([+−-]?)(\d+)(?:\.(\d+))?(?:e([+-]?\d+))?$/i.exec(raw);
    if (!match) return raw;
    let [, sign, whole, fraction = '', exponent] = match;
    if (exponent) {
        const shift = Number(exponent);
        if (Math.abs(shift) > 128) return raw;
        const digits = whole + fraction, decimalAt = whole.length + shift;
        whole = decimalAt <= 0 ? '0' : digits.padEnd(decimalAt, '0').slice(0, decimalAt).replace(/^0+(?=\d)/, '');
        fraction = '0'.repeat(Math.max(0, -decimalAt)) + digits.slice(Math.max(0, decimalAt));
    }
    return sign + whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (fraction ? '.' + fraction : '');
};
/* TREND_COMPONENT */
/* TREND_COMPONENT_END */
addEventListener('DOMContentLoaded',()=>{
    for(const label of document.querySelectorAll('[data-analysis-order-label]')){
        const key=label.getAttribute('data-analysis-order-label'), direction=data.sets[key]?.directionLabel;
        if(direction)label.textContent=direction;
        else error('排序说明需要已绑定的原生排序或统计集合，请检查集合名：'+key);
    }
    for(const label of document.querySelectorAll('[data-analysis-formula-address]')){
        const set=data.sets[label.dataset.analysisFormulaAddress];
        const node=set?.nodes?.find(item=>item.factRef===label.dataset.analysisNodeRef);
        label.textContent=node?.cellAddress||'';
        if(!node)error('公式地址须绑定当前公式集合及实际节点引用。');
    }
});
addEventListener('error',e=>error(e.message||'resource error'));
addEventListener('unhandledrejection',e=>error(e.reason?.message||'unhandled rejection'));
addEventListener('securitypolicyviolation',e=>error('resource policy: '+e.violatedDirective));
// 只绑定现有图形的数值和线性长度，不创建布局、颜色或章节。
// 标记与数值绑定由同一函数设置在实际条形上，避免父容器误标。
let linearScaleSequence=0;
const waterfallBindings=new WeakMap();
const linearBindings=new WeakMap();
const registeredCharts=new Set(), valueLabels=new Map();
// 排序、原生统计和脚本表格都按实际字段名读取，不依赖列下标。
// 仅接受明确的 factRef/value 列，不猜金额列，也不把空值当成零。
const setRows=new WeakMap();
const setDatum=(setKey,factRef)=>{
    const set=data.sets[setKey];
    if(!set||!Array.isArray(set.rows)||typeof factRef!=='string'||!factRef)return undefined;
    if(!setRows.has(set)){
        const names=(set.columns||[]).map(column=>column.name);
        const facts=new Map(),rows=new Map();
        const add=(map,id,value)=>{
            if(typeof id==='string'&&id)map.set(id,map.has(id)?null:value);
        };
        if(names.includes('factRef')&&names.includes('value')){
            for(const row of set.rows){
                add(facts,row.values?.factRef,row.values);
                add(rows,row.rowRef,row.values);
            }
        }
        setRows.set(set,{facts,rows});
    }
    const {facts,rows}=setRows.get(set);
    if(facts.has(factRef))return facts.get(factRef)??undefined;
    // Both the supplied row and its target fact must be unique in this set.
    const row=rows.get(factRef);
    return row&&facts.get(row.factRef)===row?row:undefined;
};
const bindingDatum=binding=>{
    if(binding.valueKey)return data.values[binding.valueKey];
    if(binding.nodeRef!=null)return data.sets[binding.setKey]?.nodes?.find(node=>node.factRef===binding.nodeRef)?.waterfall?.steps?.[binding.stepIndex];
    return setDatum(binding.setKey,binding.factRef);
};
const bindValueLabel=(element,binding)=>{
    const datum=bindingDatum(binding);
    if(!(element instanceof Element)||datum?.value==null)throw new Error('数值标签需要实际元素和当前文档的非空数值绑定');
    element.textContent=datum.display??groupedNumber(datum.value);
    valueLabels.set(element,{...binding});
};
const allowZeroLength=(element,axis)=>{
    // Decorative minimums would enlarge zero and subpixel amounts on the value axis.
    if(element instanceof HTMLElement)element.style[axis==='x'?'minWidth':'minHeight']='0px';
};
const bindLinearBars=(items,options={})=>{
    // 每次调用默认独立刻度，避免局部放大图误用全量图的分组名称。
    // 明确共享名称时，检查器仍会验证跨图比例，不静默拆组放过失真。
    const axis=options.axis||'x',scale=options.scale||('auto-linear-'+(++linearScaleSequence));
    if(!Array.isArray(items)||!items.length||!scale||!['x','y'].includes(axis))throw new Error('线性条形需要元素、比例尺及有效轴向');
    const rows=items.map(item=>{
        const element=item.element;
        const datum=item.valueKey?data.values[item.valueKey]:setDatum(item.setKey,item.factRef);
        if(!(element instanceof Element)||datum?.value==null||!Number.isFinite(Number(datum.value)))throw new Error(
            '线性条形必须绑定实际非空数值。请使用已挂入页面的条形元素，并从同一行的 row.values 读取 factRef 和 value；空值只显示为空。');
        allowZeroLength(element,axis);
        waterfallBindings.delete(element);
        delete element.dataset.analysisWaterfallSet;delete element.dataset.analysisWaterfallNode;delete element.dataset.analysisWaterfallStep;
        element.dataset.analysisScale=scale;element.dataset.analysisBarAxis=axis;
        if(item.valueKey){element.dataset.analysisBarValue=item.valueKey;delete element.dataset.analysisBarSet;delete element.dataset.analysisBarRef;}
        else{delete element.dataset.analysisBarValue;element.dataset.analysisBarSet=item.setKey;element.dataset.analysisBarRef=datum.factRef;}
        registeredCharts.add(element);
        const binding=item.valueKey?{valueKey:item.valueKey}:{setKey:item.setKey,factRef:datum.factRef};
        const registration={axis,length:0,binding};
        linearBindings.set(element,registration);
        return {element,value:Math.abs(Number(datum.value)),registration};
    });
    const maximum=options.maximum??Math.max(...rows.map(row=>row.value));
    if(!Number.isFinite(maximum)||maximum<Math.max(...rows.map(row=>row.value)))throw new Error('线性条形的刻度上限不得小于实际最大值');
    const draw=()=>{
        const pixels=options.extent?.getBoundingClientRect()[axis==='x'?'width':'height']??options.pixels;
        if(!Number.isFinite(pixels)||pixels<0)throw new Error('线性条形需要真实容器或非负像素长度');
        for(const row of rows){
            if(linearBindings.get(row.element)!==row.registration)continue;
            const length=maximum?row.value/maximum*pixels:0;
            row.registration.length=length;
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
        if(item.zero!=null&&(!(item.zero instanceof HTMLElement)||item.zero.parentElement!==track))
            throw new Error('瀑布零线需要属于对应轨道的实际元素');
        if(item.labelElement!=null&&!(item.labelElement instanceof Element))
            throw new Error('瀑布图的 labelElement 须引用该步骤显示业务名称的元素。');
        return {element,track,step,index:item.stepIndex,zero:item.zero,labelElement:item.labelElement};
    });
    if(new Set(bindings.map(item=>item.element)).size!==bindings.length)
        throw new Error('横向瀑布图每一步需要独立条形，不能用同一元素覆盖多个贡献');
    const rowContainers=new Set(bindings.map(item=>item.track.parentElement));
    for(const item of bindings){
        const {element,track,step,index}=item;
        // 专用轨道只负责定位；厚度、标签、颜色和报告布局仍由页面决定。
        if(getComputedStyle(track).position==='static')track.style.position='relative';
        element.style.position='absolute';element.style.display='block';element.style.boxSizing='border-box';
        allowZeroLength(element,'x');
        element.dataset.analysisScale=scale;element.dataset.analysisBarAxis='x';
        element.dataset.analysisWaterfallSet=options.setKey;element.dataset.analysisWaterfallNode=options.nodeRef;
        element.dataset.analysisWaterfallStep=String(index);
        delete element.dataset.analysisBarValue;delete element.dataset.analysisBarSet;delete element.dataset.analysisBarRef;
        linearBindings.delete(element);
        item.registration={track,step,labelElement:item.labelElement,unit:node.unit,
            separateRows:rowContainers.size===bindings.length,binding:{setKey:options.setKey,nodeRef:options.nodeRef,stepIndex:index}};
        waterfallBindings.set(element,item.registration);
        registeredCharts.add(element);
    }
    // All rows share a real drawing interval, even when a decorated total row
    // has padding. Keep the report layout; never apply a different scale per row.
    const draw=()=>{
        const active=bindings.filter(item=>waterfallBindings.get(item.element)===item.registration);
        if(!active.length)return;
        const tracks=active.map(({track})=>{
            const box=track.getBoundingClientRect(),zoom=track.offsetWidth?box.width/track.offsetWidth:0;
            return {left:box.left+track.clientLeft*zoom,width:track.clientWidth*zoom,zoom};
        });
        if(tracks.some(t=>!t.zoom||!t.width))return; // Collapsed content is drawn on resize/show.
        const left=Math.max(...tracks.map(t=>t.left));
        const width=Math.min(...tracks.map(t=>t.left+t.width))-left;
        if(width<=0)throw new Error('同一瀑布图的轨道没有共同绘图区；请保持各步骤纵向对齐，保留全部公式项');
        active.forEach(({element,step,zero,registration},i)=>{
            const track=tracks[i];
            const cssLeft=(left-track.left+step.geometry.left*width)/track.zoom;
            const cssWidth=step.geometry.width*width/track.zoom;
            element.style.left=cssLeft+'px';element.style.width=cssWidth+'px';
            Object.assign(registration,{cssLeft,cssWidth});
            if(zero)zero.style.left=((left-track.left+waterfall.zero*width)/track.zoom)+'px';
        });
    };
    draw();
    let pendingFrame=null;
    const observer=typeof ResizeObserver==='function'?new ResizeObserver(()=>{
        if(pendingFrame!==null)cancelAnimationFrame(pendingFrame);
        pendingFrame=requestAnimationFrame(()=>{pendingFrame=null;draw();});
    }):null;
    for(const {track} of bindings)observer?.observe(track);
    return ()=>{observer?.disconnect();if(pendingFrame!==null)cancelAnimationFrame(pendingFrame);};
};
// Author scripts sometimes catch binding errors. Keep failed, still-mounted
// bindings visible to publication checks, clearing them if the page repairs them.
const failedCharts=new Map(),failedLabels=new Map();
const checked=(bind,failures)=>(...args)=>{
    const first=args[0];
    const elements=Array.isArray(first)?first.map(item=>item?.element).filter(el=>el instanceof Element)
        :first instanceof Element?[first]:[];
    try{
        const result=bind(...args);
        for(const el of elements)failures.delete(el);
        return result;
    }catch(cause){
        const message=String(cause.message||cause);
        if(elements.length){for(const el of elements)failures.set(el,message);}
        else error(message);
        throw cause;
    }
};
Object.defineProperty(window,'analysisCharts',{value:Object.freeze({
    bindLinearBars:checked(bindLinearBars,failedCharts),bindHorizontalWaterfall:checked(bindHorizontalWaterfall,failedCharts),
    bindValueLabel:checked(bindValueLabel,failedLabels)
}),writable:false,configurable:false});
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
const waterfallHasName=({track,step,unit,labelElement,separateRows})=>{
    if(!step.label)return true; // The source supplied no business name to display.
    const readable=el=>{
        if(!visible(el))return false;
        const text=(el.innerText??el.textContent??'').trim();
        return text===step.label||/\p{L}/u.test(unit?text.split(unit).join(''):text);
    };
    if(labelElement)return readable(labelElement);
    // Custom templates can bind an external label explicitly. For a traditional
    // label/track/amount row, verify its own text, never a nearby table or heading.
    if(!separateRows||!track.parentElement)return true;
    const siblings=[...track.parentElement.children].filter(el=>el!==track);
    if(siblings.length<2)return true;
    return siblings.some(readable);
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
    const failedMessages=new Set();
    for(const failures of [failedCharts,failedLabels])for(const [el,message] of failures)if(el.isConnected)failedMessages.add(message);
    for(const message of failedMessages)mismatch(message);
    // Only a single, explicitly declared source address on the caption of a
    // bound formula can be checked. Other references may be operands or prose.
    for(const figure of document.querySelectorAll('[data-analysis-set][data-analysis-ref]')){
        const set=data.sets[figure.dataset.analysisSet];
        if(set?.kind!=='FORMULA')continue;
        const address=set.nodes.find(node=>node.factRef===figure.dataset.analysisRef)?.cellAddress;
        if(!address||!/^\$?[A-Z]{1,3}\$?[1-9]\d*$/i.test(address))continue;
        const caption=figure.querySelector(':scope > figcaption')||figure.previousElementSibling;
        if(!caption?.matches('h1,h2,h3,h4,h5,h6,figcaption')||!visible(caption))continue;
        const claim=/[（(]\s*(?:表内公式|原表单元格|来源单元格)\s*[：:]?\s*(\$?[A-Z]{1,3}\$?[1-9]\d*)\s*[）)]\s*$/i.exec(caption.innerText);
        const normalized=ref=>ref.replaceAll('$','').toUpperCase();
        if(claim&&normalized(claim[1])!==normalized(address))
            mismatch('公式图标题标注 '+claim[1]+'，该节点的来源单元格是 '+address+'。请用 node.cellAddress 填写原表坐标，或省略该标注。');
    }
    const counters=[...document.querySelectorAll('[data-analysis-count-for]')];
    const declaredBars=[...new Set([...document.querySelectorAll('[data-analysis-scale]'),...registeredCharts].filter(el=>el.isConnected))];
    const charted=new Map(), presented=new Map();
    const keep=(map,binding)=>{
        if(!binding)return;
        if(binding.factRef){
            const datum=setDatum(binding.setKey,binding.factRef);
            if(datum)binding={...binding,factRef:datum.factRef};
        }
        map.set(JSON.stringify(binding),binding);
    };
    const chartBinding=bar=>linearBindings.get(bar)?.binding||waterfallBindings.get(bar)?.binding||
        (bar.dataset.analysisWaterfallSet?{setKey:bar.dataset.analysisWaterfallSet,nodeRef:bar.dataset.analysisWaterfallNode,stepIndex:Number(bar.dataset.analysisWaterfallStep)}:
        bar.dataset.analysisBarValue?{valueKey:bar.dataset.analysisBarValue}:
        bar.dataset.analysisBarSet&&bar.dataset.analysisBarRef?{setKey:bar.dataset.analysisBarSet,factRef:bar.dataset.analysisBarRef}:null);
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
    const geometryTolerancePx=1;
    const scales=new Map();
    for(const bar of declaredBars.slice(0,1000)){
        const axis=bar.dataset.analysisBarAxis||'x';
        if(!rendered(bar)){
            // Collapsible formula layers remain part of the document. Their
            // geometry is measured once opened, not fabricated while hidden.
            if(waterfallBindings.has(bar)&&bar.closest('details:not([open])')){
                const binding=chartBinding(bar);keep(charted,binding);keep(presented,binding);
            }
            continue;
        }
        numericChecks.linearBars.measured++;
        const numericBinding=chartBinding(bar);
        keep(charted,numericBinding);keep(presented,numericBinding);
        if(!bar.dataset.analysisScale){numericChecks.linearBars.invalid++;mismatch('实际条形移除了比例尺标记：'+elementPath(bar));continue;}
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
            if(!waterfallHasName(waterfall)){
                numericChecks.linearBars.invalid++;
                mismatch('瀑布图第'+(waterfall.binding.stepIndex+1)+'步缺少可见名称“'+step.label+'”。用 step.label 填入名称元素；独立图例用 labelElement 绑定。保留现有数据与图形。');
                continue;
            }
            const zoom=track.offsetWidth?trackBox.width/track.offsetWidth:0;
            if(!zoom||!Number.isFinite(waterfall.cssLeft)||!Number.isFinite(waterfall.cssWidth)){
                numericChecks.linearBars.invalid++;
                mismatch('横向瀑布图 '+waterfall.binding.nodeRef+' 第'+waterfall.binding.stepIndex+'步：'+elementPath(track)+
                    '；当前视口 '+document.documentElement.clientWidth+'px，本轨道实际宽 '+trackBox.width.toFixed(2)+
                    'px。同一图的轨道尚未全部取得可绘宽度。请检查窄屏下标签和金额列是否挤占绘图区，允许换行或调整列宽；保留全部公式项。');
                continue;
            }
            const expectedLeft=trackBox.left+(track.clientLeft+waterfall.cssLeft)*zoom;
            const expectedWidth=waterfall.cssWidth*zoom;
            // 极小贡献可能小于浏览器可绘像素；保留精确标签，不用最小宽度放大。
            subpixel=expectedWidth>0&&expectedWidth<0.5;
            if(bar.parentElement!==track||bar.offsetParent!==track
               ||Math.abs(box.left-expectedLeft)>geometryTolerancePx||Math.abs(box.width-expectedWidth)>geometryTolerancePx){
                numericChecks.linearBars.invalid++;
                mismatch('横向瀑布图 '+waterfall.binding.nodeRef+' 第'+waterfall.binding.stepIndex+'步：'+elementPath(bar)+
                    '；累计位置或长度不符。实际 left/width='+box.left.toFixed(2)+'/'+box.width.toFixed(2)+
                    '，期望='+expectedLeft.toFixed(2)+'/'+expectedWidth.toFixed(2)+
                    '。保留瀑布及全部公式项，仅修正轨道或覆盖绑定的 margin、transform、最小宽度；不要删图改表。');
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
        const different=bars.find(bar=>bar.value===0 ? bar.length!==0 :
            (bar.length===0&&!bar.subpixel)||Math.abs(bar.length-bar.value/pivot.value*pivot.length)>geometryTolerancePx);
        if(different||bars.length>1||pivot.value===0)numericChecks.linearBars.checkedGroups++;
        if(different){
            numericChecks.linearBars.failedGroups++;
            mismatch('线性条形比例不一致：'+key+'；'+different.path+'。同名 scale 共享刻度；全量图与局部放大图须使用不同组名，或分别调用 analysisCharts.bindLinearBars 并省略 scale；同图检查最小长度与绑定值。');
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
    // A table/label can replace a tiny bar. Compare exact decimal text, without
    // float rounding or guessing units, and retain the immutable value identity.
    const decimal=text=>{
        const raw=String(text??'').replaceAll(',','').replaceAll('−','-');
        if(!/^[+-]?\d+(\.\d+)?$/.test(raw))return null;
        const negative=raw.startsWith('-'),parts=raw.replace(/^[+-]/,'').split('.');
        const whole=parts[0].replace(/^0+(?=\d)/,''),fraction=(parts[1]||'').replace(/0+$/,'');
        return (negative&&(whole!=='0'||fraction)?'-':'')+whole+(fraction?'.'+fraction:'');
    };
    const containsValue=(element,datum)=>{
        if(!datum||datum.value==null)return false;
        const wanted=decimal(datum.value),display=decimal(datum.display);
        return [...(element.innerText??element.textContent??'').matchAll(/[+\-−]?\d[\d,]*(?:\.\d+)?/g)]
            .some(match=>{const actual=decimal(match[0]);return actual!==null&&(actual===wanted||display!==null&&actual===display);});
    };
    for(const [element,binding] of valueLabels){
        if(!element.isConnected||!visible(element))continue;
        if(containsValue(element,bindingDatum(binding)))keep(presented,binding);
        else mismatch('数值标签与冻结绑定不一致：'+elementPath(element));
    }
    const numericRows=[...document.querySelectorAll('[data-analysis-ref]')];
    for(const row of numericRows.slice(0,1200)){
        if(!visible(row))continue;
        const setKey=row.closest('[data-analysis-set]')?.dataset.analysisSet;
        const binding={setKey,factRef:row.dataset.analysisRef};
        if(setKey&&containsValue(row,bindingDatum(binding)))keep(presented,binding);
    }
    const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_COMMENT);
    const markerPrefix='analysis-static:'+FRAME_KEY+':', staticKeys=new Set(data.staticKeys||[]);
    while(walker.nextNode()){
        const start=walker.currentNode,tag=start.data;
        if(!tag.startsWith(markerPrefix)||!tag.endsWith(':start'))continue;
        const key=tag.slice(markerPrefix.length,-6),datum=data.values[key];
        if(!staticKeys.has(key)||!datum||datum.value==null)continue;
        const text=start.nextSibling,end=text?.nextSibling;
        if(text?.nodeType!==Node.TEXT_NODE||end?.nodeType!==Node.COMMENT_NODE||
           end.data!==markerPrefix+key+':end'||!rendered(start.parentElement))continue;
        const range=document.createRange();range.selectNodeContents(text);
        const rect=range.getBoundingClientRect();
        if(rect.width<=0||rect.height<=0||rect.right<=0||rect.left>=width)continue;
        if(range.toString()===String(datum.display))keep(presented,{valueKey:key});
        else mismatch('静态数值与冻结绑定不一致：'+key);
    }
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
    const containers=[],rowSections=new Map(),rowOwners=new WeakMap();
    for(const marked of document.querySelectorAll('[data-analysis-set]')){
        const section=marked.parentElement;
        // A table row can carry both identifiers. Read sibling rows of the same
        // dataset in this section, keeping independent tables and datasets apart.
        if(marked.matches('tr[data-analysis-ref]')&&section?.matches('tbody,thead,tfoot')
            &&!marked.querySelector('[data-analysis-ref],[data-analysis-set]')&&!marked.hasAttribute('data-analysis-partial')&&!marked.hasAttribute('data-analysis-order-axis')){
            const outer=section.closest('[data-analysis-set]'),key=marked.dataset.analysisSet;
            if(outer&&outer.dataset.analysisSet===key){rowOwners.set(marked,outer);continue;}
            if(!rowSections.has(section))rowSections.set(section,new Map());
            const byKey=rowSections.get(section);
            if(!byKey.has(key)){
                const view={container:section,key,rows:[]};byKey.set(key,view);containers.push(view);
            }
            byKey.get(key).rows.push(marked);
        }else containers.push({container:marked,key:marked.dataset.analysisSet});
    }
    const completeTables=new Map(),unmarkedOrders=[];
    let refBytes=0;
    for(const {container,key,rows:markedRows} of containers.slice(0,50)){
        if(!visible(container))continue;
        const axis=container.dataset.analysisOrderAxis==='x'?'left':'top';
        const items=markedRows?markedRows.filter(visible):[...container.querySelectorAll('[data-analysis-ref]')]
            .filter(el=>(rowOwners.get(el)??el.closest('[data-analysis-set]'))===container&&visible(el));
        items.sort((a,b)=>a.getBoundingClientRect()[axis]-b.getBoundingClientRect()[axis]);
        const refs=[];
        for(const el of items){
            const supplied=el.getAttribute('data-analysis-ref');
            const ref=setDatum(key,supplied)?.factRef??supplied;
            refBytes+=encoder.encode(JSON.stringify(ref)).length;
            if(refBytes>18000||refs.length>=2000)break;
            refs.push(ref);
        }
        const order={key,partial:container.dataset.analysisPartial==='true',refs,truncated:refs.length<items.length};
        orders.push(order);
        if(order.truncated||!refs.length)continue;
        if(!container.hasAttribute('data-analysis-partial'))unmarkedOrders.push(order);
        const table=items[0].closest('table');
        if(!table||order.partial||!items.every(el=>el.closest('table')===table))continue;
        const rows=data.sets[order.key]?.rows;
        const tableRows=new Set(items.map(el=>el.closest('tr')));
        if(!Array.isArray(rows)||rows.length!==refs.length||tableRows.has(null)||tableRows.size!==refs.length||new Set(refs).size!==refs.length)continue;
        if(rows.every((row,i)=>typeof row.values?.factRef==='string'&&row.values.factRef===refs[i]))completeTables.set(order.key,rows.length);
    }
    // Only a complete, visible table of the same source proves that another view can be partial.
    // The host still verifies each companion's identities and relative order.
    for(const order of unmarkedOrders){
        const fullSize=completeTables.get(order.key);
        if(fullSize!==undefined&&order.refs.length<fullSize)order.partial=true;
    }
    for(const trendRef of measurePeriodTrends()){
        keep(charted,{trendRef});keep(presented,{trendRef});
    }
    const result={version:4,viewport:{width,height:Math.round(innerHeight)},numericChecks,
        valueCoverage:{charted:[...charted.values()].slice(0,1200),presented:[...presented.values()].slice(0,1200),
            complete:charted.size<=1200&&presented.size<=1200&&numericRows.length<=1200&&declaredBars.length<=1000},
        pageOverflow:document.documentElement.scrollWidth>width+2,overflow,overlaps,smallText,clippedText,orders,errors:[...errors,...consistencyErrors].slice(0,20),
        truncated:diagnosticsTruncated||elements.length>limit||containers.length>50||counters.length>50||declaredBars.length>1000||labelChecks>=20000};
    // Fit the host's 60 kB transport limit without losing script errors or order evidence.
    const diagnostics=[overflow,overlaps,smallText,clippedText];
    while(encoder.encode(JSON.stringify(result)).length>58000){
        const largest=diagnostics.filter(items=>items.length).sort((a,b)=>JSON.stringify(b).length-JSON.stringify(a).length)[0];
        if(!largest){
            const coverage=[result.valueCoverage.charted,result.valueCoverage.presented].filter(items=>items.length).sort((a,b)=>b.length-a.length)[0];
            if(!coverage)break;
            coverage.length=Math.floor(coverage.length/2);result.valueCoverage.complete=false;result.truncated=true;continue;
        }
        largest.pop();result.truncated=true;
    }
    const observedText=document.body.innerText;
    result.hostRenderedText={text:observedText.length<=262144?observedText:'',complete:observedText.length<=262144};
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
// A printed copy includes adopted source data. Restore the reader's previous disclosure state afterwards.
{
    let printOpened = [];
    addEventListener('beforeprint', () => {
        for (const el of document.querySelectorAll('details.source-data:not([open])')) {
            printOpened.push(el);
            el.open = true;
        }
    });
    addEventListener('afterprint', () => {
        for (const el of printOpened) el.open = false;
        printOpened = [];
    });
}
