import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';

const runtime=readFileSync(new URL('../../../agent_service/analysis_extension/document_runtime.js',import.meta.url),'utf8');
function fixture() {
    class Element {
        constructor(left=0,width=0,parent=null){Object.assign(this,{left,width,parentElement:parent,offsetParent:parent,
            clientLeft:0,clientWidth:width,offsetWidth:width,style:{},dataset:{}});}
        getBoundingClientRect(){return {left:this.left,width:this.width};}
    }
    const callbacks=[];
    const steps=[{value:'10',geometry:{left:0,width:1}},
        {value:'-3',geometry:{left:.7,width:.3}},{value:'7',geometry:{left:0,width:.7}}];
    const data={values:{},sets:{formula:{nodes:[{factRef:'profit',waterfall:{steps,zero:0}}]}}};
    const window={};
    runInNewContext(runtime,{window,HTMLElement:Element,Element,document:{getElementById:()=>({textContent:JSON.stringify(data)})},
        addEventListener(){},getComputedStyle:()=>({position:'static'}),requestAnimationFrame:f=>{f();return 1;},
        cancelAnimationFrame(){},ResizeObserver:class {constructor(fn){callbacks.push(fn);} observe(){} disconnect(){} }});
    const tracks=[new Element(10,100),new Element(16,88),new Element(10,100)];
    const items=tracks.map((track,stepIndex)=>({track,stepIndex,element:new Element(0,0,track),zero:new Element(0,0,track)}));
    window.analysisCharts.bindHorizontalWaterfall(items,{setKey:'formula',nodeRef:'profit'});
    const pixels=item=>({left:item.track.left+parseFloat(item.element.style.left),width:parseFloat(item.element.style.width)});
    return {items,callbacks,pixels};
}
test('decorated rows use one shared waterfall scale and zero, without changing their layout',()=>{
    const {items,pixels}=fixture();
    assert.deepEqual(pixels(items[0]),{left:16,width:88});
    assert.ok(Math.abs(pixels(items[1]).width-26.4)<1e-9);
    assert.ok(Math.abs(pixels(items[2]).width-61.6)<1e-9);
    assert.equal(pixels(items[0]).left,pixels(items[2]).left);
    assert.equal(items[0].track.width,100);
    assert.equal(items[1].track.width,88);
    for(const item of items)assert.equal(item.track.left+parseFloat(item.zero.style.left),16);
});
test('responsive resize recalculates the shared geometry',()=>{
    const {items,callbacks,pixels}=fixture();
    for(const [i,item] of items.entries())Object.assign(item.track,{width:i===1?288:300,clientWidth:i===1?288:300,offsetWidth:i===1?288:300});
    callbacks[0]();
    assert.equal(pixels(items[0]).width,288);
    assert.ok(Math.abs(pixels(items[1]).width-86.4)<1e-9);
});
