import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { parseSnapshot } from '../src/lib/workspace-analysis.ts';

const source = JSON.parse(readFileSync(new URL('./analysis-visuals.fixture.json', import.meta.url), 'utf8'));
function fixture() {
  const v=structuredClone(source);
  v.claims=['总体结论','主题解读']; v.summaryClaimIndexes=[0];
  v.summaryPosition='start'; v.summaryTitle='关键判断'; v.reportTitle='业务报告';
  v.sections[0].claimIndexes=[1]; v.sections[0].width='half'; v.sections[0].claimsPosition='before';
  return v;
}
test('current composition preserves the selected title, position and claim membership',()=>{
  const view=parseSnapshot(fixture());
  assert.equal(view.summaryPosition,'start'); assert.equal(view.summaryTitle,'关键判断');
  assert.deepEqual(view.sections[0].claimIndexes,[1]); assert.equal(view.sections[0].width,'half');
});
test('all conclusions must appear exactly once and only through valid indexes',()=>{
  for(const mutate of [v=>{v.summaryClaimIndexes=[]},v=>{v.summaryClaimIndexes=[0,1]},v=>{v.sections[0].claimIndexes=[2]},
    v=>{v.sections[0].claimIndexes=[true]},v=>{v.summaryClaimIndexes=[0,0]}]) {
    const view=fixture();mutate(view);assert.throws(()=>parseSnapshot(view),/INVALID_VIEW/);
  }
});
test('composition values cannot inject CSS or unrecognized layouts',()=>{
  for(const mutate of [v=>{v.sections[0].width='style="color:red"'},v=>{v.summaryPosition='hidden'},
    v=>{v.sections[0].claimsPosition='floating'},v=>{v.summaryTitle={html:'bad'}}]) {
    const view=fixture();mutate(view);assert.throws(()=>parseSnapshot(view),/INVALID_VIEW/);
  }
});
test('two object comparison scenes keep independent identities and section indexes',()=>{
  const view=fixture(), comparison=view.scenes.find(s=>s.type==='BAR_VALUES');
  const i=view.scenes.length;view.scenes.push({...comparison,viewId:'second',title:'另一个指标'});
  view.sections.push({groupId:'another',title:'另一个主题',description:'',width:'half',claimsPosition:'after',claimIndexes:[],
    blocks:[{viewId:'second',title:'另一个指标',description:'',metricIndexes:[],tableIndexes:[],sceneIndexes:[i]}]});
  const parsed=parseSnapshot(view);
  assert.equal(parsed.scenes.filter(s=>s.type==='BAR_VALUES').length,2);
  const invalid=structuredClone(view);invalid.sections.at(-1).blocks[0].sceneIndexes=[0];
  assert.throws(()=>parseSnapshot(invalid),/INVALID_VIEW/);
});
