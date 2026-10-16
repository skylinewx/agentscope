import assert from 'node:assert/strict';
import test from 'node:test';
import { compareExact, tablePage } from '../src/lib/analysis-table-state.ts';

test('table ordering compares exact decimals without losing large amounts, signs or exponents', () => {
  for (const [a,b,expected] of [
    ['9007199254740993','9007199254740992',1], ['-8300','-1033.14',-1],
    ['0','-0.00',0],['1e100000','9e99999',1], ['1.2300','123e-2',0],
    ['0.0001','0.0002',-1], ['-2e-50','-1e-50',-1], ['100','99.99',1]
  ]) assert.equal(Math.sign(compareExact(a,b)),expected, `${a} vs ${b}`);
});

test('sort the full table before pagination and retain missing values last in both directions', () => {
  const values=['3',null,'9007199254740993','9007199254740992','-5','0'];
  const table={rows:values.map((exact,i)=>({memberKey:`m${i}`,cells:{v:{exact}}}))};
  const original=structuredClone(table);
  const desc={columnKey:'v',direction:'desc'};
  const page=tablePage(table,desc,0,2);
  assert.deepEqual(page.rows.map(r=>r.cells.v.exact),['9007199254740993','9007199254740992']);
  assert.deepEqual(tablePage(table,desc,2,2).rows.map(r=>r.cells.v.exact),['-5',null]);
  assert.deepEqual(tablePage(table,{...desc,direction:'asc'},2,2).rows.map(r=>r.cells.v.exact),['9007199254740993',null]);
  assert.deepEqual(table,original);
});

test('table pagination and reset to published order stay independent', () => {
  const a={rows:Array.from({length:17},(_,i)=>({memberKey:`a${i}`,cells:{v:{exact:String(i)}}}))};
  const b={rows:Array.from({length:6},(_,i)=>({memberKey:`b${i}`,cells:{v:{exact:String(i)}}}))};
  const original={columnKey:null,direction:'published'};
  assert.equal(tablePage(a,original,1,12).rows.length,5);
  assert.equal(tablePage(b,original,0,12).rows.length,6);
  assert.equal(tablePage(b,original,10,12).page,0);
  tablePage(a,{columnKey:'v',direction:'desc'},0,12);
  assert.equal(tablePage(a,original,0,12).rows[0].memberKey,'a0');
});
