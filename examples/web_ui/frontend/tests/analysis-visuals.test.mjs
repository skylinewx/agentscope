import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { parseSnapshot, validateScene } from '../src/lib/workspace-analysis.ts';

const diagrams = v => v.scenes.filter(s => !['BAR_VALUES','BAR_RANK'].includes(s.type));
const comparison = v => v.scenes.find(s => ['BAR_VALUES','BAR_RANK'].includes(s.type));
const fixture=JSON.parse(readFileSync(new URL('./analysis-visuals.fixture.json',import.meta.url),'utf8'));
test('the current snapshot accepts formula and hierarchy alongside a comparison chart',()=>{
  const view=parseSnapshot(fixture);
  assert.deepEqual(diagrams(view).map(s=>s.type),['FORMULA','HIERARCHY']);
  assert.equal(comparison(view).type,'BAR_VALUES');
  assert.equal(new Set(view.metrics.map(m=>m.label)).size,3);
});
test('relationship scenes reject nonfinite coordinates, markup primitives and invalid metadata',()=>{
  for (const mutate of [s=>{s.width=Infinity},s=>{s.primary='yes'},s=>{s.collapsed='false'},s=>{s.title={text:'fake'}},s=>{s.primitives[0]={kind:'html',text:'<script>',tone:'value'}}]) {
    const scene=structuredClone(diagrams(fixture)[0]); mutate(scene);
    assert.throws(()=>validateScene(scene));
  }
});

test('the published scene preserves model-selected grouping and collapse metadata',()=>{
  const snapshot=structuredClone(fixture);
  snapshot.scenes=[comparison(fixture), ...Array.from({length:6},(_,i)=>({...diagrams(fixture)[0],sceneId:`formula-${i}`,collapsed:i===5}))];
  snapshot.sections=snapshot.sections.filter(s=>s.blocks.some(b=>b.metricIndexes.length || b.tableIndexes.length));
  snapshot.sections.push({groupId:'scenes',title:'',description:'',width:'full',claimsPosition:'after',claimIndexes:[],blocks:[{viewId:comparison(snapshot).viewId,title:'',description:'',metricIndexes:[],tableIndexes:[],sceneIndexes:[0]}, {viewId:diagrams(snapshot)[0].viewId,title:'',description:'',metricIndexes:[],tableIndexes:[],sceneIndexes:[1,2,3,4,5,6]}]});
  const parsed=parseSnapshot(snapshot);
  assert.equal(diagrams(parsed).length,6);
  assert.equal(diagrams(parsed).filter(s=>!s.collapsed).length,5);
  assert.equal(diagrams(parsed)[5].collapsed,true);
});
test('all relationship labels are text and exact amounts stay in the snapshot',()=>{
  for(const scene of diagrams(fixture)){
    assert.ok(scene.primitives.every(p=>['text','rect','line'].includes(p.kind)));
    assert.ok([scene.relationship.result,...scene.relationship.inputs].some(n=>n.display==='-5'));
    assert.equal(scene.primitives.length,0);
  }
});

test('semantic text and section references are bounded and cannot point to other content',()=>{
  for(const mutate of [s=>{s.relationship.equation={html:'x'}},s=>{s.relationship.inputs[0].label=null},s=>{s.relationship.checks[0].display=12}]) {
    const scene=structuredClone(diagrams(fixture)[0]); mutate(scene); assert.throws(()=>validateScene(scene));
  }
  for(const mutate of [s=>{s.sections[0].blocks[0].metricIndexes=[999]},s=>{s.sections[0].blocks[0].viewId='other'},s=>{s.claimsCollapsed='true'},s=>{s.sections=[]}]) {
    const snapshot=structuredClone(fixture); mutate(snapshot); assert.throws(()=>parseSnapshot(snapshot));
  }
});
