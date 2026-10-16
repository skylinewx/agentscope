import test from 'node:test';
import assert from 'node:assert/strict';
import { documentHeight, documentInspection, parseFreeDocument } from '../src/lib/analysis-document.ts';

test('resize accepts current frame key and bounded integer height', () => {
    assert.equal(documentHeight({type:'analysis-document-size',key:'current',height:812},'current'),812);
    for (const data of [null,[],{}, {type:'analysis-document-size',key:'old',height:812},
        ...[NaN,Infinity,-1,24001,1.5,'812'].map(height=>({type:'analysis-document-size',key:'current',height}))]) {
        assert.equal(documentHeight(data,'current'),null);
    }
});
test('unbounded or incomplete HTML cannot enter the document frame', () => {
    const valid={version:'free-html-1',title:'标题',html:'<html></html>',contentDigest:'a'.repeat(64),frameKey:'b'.repeat(64),bindingCount:3};
    assert.deepEqual(parseFreeDocument(valid),valid);
    assert.equal(parseFreeDocument({...valid,bindingCount:0,datasetCount:1}).datasetCount,1);
    for(const change of [{version:'unknown'},{frameKey:'not-a-key'},{bindingCount:0},{html:'字'.repeat(300000)},{title:''}]) {
        assert.throws(()=>parseFreeDocument({...valid,...change}),/INVALID_VIEW/);
    }
});
test('inspection is bounded and belongs to the current document and request', () => {
    const valid={type:'analysis-document-inspection',key:'current',requestId:'run',measurements:{version:1}};
    assert.deepEqual(documentInspection(valid,'current','run'),{version:1});
    for(const data of [null,[],{}, {...valid,key:'old'},{...valid,requestId:'other'},
        {...valid,measurements:[]},{...valid,measurements:{data:'x'.repeat(60000)}}]) {
        assert.equal(documentInspection(data,'current','run'),null);
    }
    assert.equal(documentInspection(valid,'current'),null);
});
