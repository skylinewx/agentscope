"""Period charts are anchored to observed budget records and existing calculations."""
import json
import re
from unittest import TestCase

from analysis_extension.document_checks import require_publication_checks
from analysis_extension.document_coverage import reference
from analysis_extension.free_document import compile_document
from analysis_extension.models import AnalysisError
from analysis_extension.period_trends import choices, period_code, selected_trends, trend_catalog


def observation(p, value, org='甲', **kwargs):
    row = {'factId': org + p, 'period': '2026N0001', 'executionPeriod': '2026N0001',
           'dimensions': {'DATATIME': p, 'MD_ORG': org, 'MD_SCENE': 'BUDGET'}, 'sourceMeasureCode': 'M',
           'recordTable': 'T', 'headers': ['收入'], 'unit': '元', 'value': value,
           'valueState': 'NO_VALUE' if value is None else 'PRESENT'}
    return {**row, **kwargs}


def artifact(*facts, derived=()):
    return {'facts': list(facts), 'derivedValues': list(derived)}


def document(a, ref=None):
    element = f'<div data-analysis-trend="{ref}"></div>' if ref else ''
    return compile_document(a, {'title': '预算分析', 'html': '<html><head></head><body>'+element+'</body></html>'})


class PeriodTrendTest(TestCase):
    def test_actual_quarters_series_refs_exact_order_zero_and_gap(self):
        a = artifact(observation('2026J0003', None), observation('2026J0001', '0'),
                     observation('2026J0004', '-1.005'), observation('2026J0004', '9.01', '乙'),
                     observation('2026J0001', '9.00', '乙'))
        charts = trend_catalog(a)
        key, = charts
        self.assertEqual(key, next(iter(trend_catalog(artifact(*reversed(a['facts']))))))
        doc = document(a, key)
        from jsonschema import Draft202012Validator
        from analysis_extension.protocol import validator
        Draft202012Validator(validator().schema['properties']['freeDocument']).validate(doc)
        data = json.loads(re.search(r'<script id="analysis-host-data".*?>(.*?)</script>', doc['html'])[1])['trends'][key]
        self.assertEqual([p['label'] for p in data['periods']], ['2026年第1季度', '2026年第3季度', '2026年第4季度'])
        self.assertEqual(data['periods'][1]['order'] - data['periods'][0]['order'], 2)
        lines = {line['label']: line['points'] for line in data['series']}
        self.assertEqual([p['value'] for p in lines['甲']], ['0', None, '-1.005'])
        self.assertEqual([p['periodIndex'] for p in lines['乙']], [0,2])
        self.assertEqual(set(doc['valueRefs']), {f['factId'] for f in a['facts']})
        self.assertEqual(reference(doc, {'trendRef': key}), 'bound:["periodTrend","'+key+'"]')

    def test_single_period_many_units_reference_only_and_all_null(self):
        self.assertFalse(trend_catalog(artifact(observation('2026N0001','1'),observation('2026N0001','2','乙'))))
        self.assertFalse(trend_catalog(artifact(observation('2026J0001',None),observation('2026J0002',None))))
        self.assertFalse(trend_catalog(artifact(observation('2026J0001','0'),observation('2026J0002','5',inputRole='REFERENCE'))))
        self.assertFalse(trend_catalog(artifact(observation('2026N0001','2'),observation('2026J0001','2'))))
        openings=[observation(p,'2',dimensions={'MD_ORG':'甲'},period=p,executionPeriod=p)
                  for p in ('2025N0001','2026N0001')]
        self.assertFalse(trend_catalog(artifact(*openings)))
        for row in openings:
            row['dataPeriod'] = row['executionPeriod']
        self.assertEqual(len(trend_catalog(artifact(*openings))),1)

    def test_distinct_table_scope_grain_unit_and_cumulative_basis(self):
        first, second = observation('2026J0001','1'), observation('2026J0002','3')
        for change in ({'unit':'%'},{'recordTable':'T2'},{'bindingRef':'B'}, {'periodMode':'YEAR_TO_DATE'},
                       {'sourceMeasureCode':'M2'}, {'dimensions': {**second['dimensions'],'MD_SCENE':'ACTUAL'}}):
            with self.subTest(change=change):
                self.assertFalse(trend_catalog(artifact(first, {**second, **change})))
        self.assertFalse(trend_catalog(artifact(first, second, {**first, 'factId':'duplicate'})))
        self.assertFalse(trend_catalog(artifact({**first,'headers':['收入','本期']}, {**second,'headers':['收入','累计']})))

    def test_supplemental_formula_operands_do_not_invent_a_temporal_objective(self):
        facts = [observation('2026J000'+p,'9', inputMemberships=[{'inputRole':'PRIMARY','inputPurpose':'FORMULA'}]) for p in ('1','2')]
        self.assertFalse(trend_catalog(artifact(*facts)))
        checks=[{'formulaTrace':{'targetFactRef':f['factId']}} for f in facts]
        self.assertEqual(len(trend_catalog(artifact(*facts, derived=checks))),1)

    def test_structured_chart_preserves_time_gaps_and_uses_shared_scale(self):
        from analysis_extension.period_trend_scenes import scenes_for_views
        from analysis_extension.report import svg
        a=artifact(*[observation('2026J000'+p, v, org) for org in ('甲','乙')
                     for p,v in [('1','0'),('2',None),('3','-2'),('4','9')]])
        key, = trend_catalog(a)
        scenes=scenes_for_views(a, [{'viewId':'results','trendRef':key}])
        self.assertEqual(len(scenes),2)
        for scene in scenes:
            self.assertEqual(scene['viewId'],'results')
            self.assertEqual(sum(m['kind']=='rect' for m in scene['primitives']),3)
            self.assertEqual(sum(m['kind']=='line' for m in scene['primitives']),1)
            self.assertIn('2026年第4季度',svg(scene))

    def test_calculated_quarters_use_exact_named_rows_and_own_units(self):
        rows = [{'rowRef': str(i), 'values':{'quarter':p,'team':'甲','revenue':v}} for i,(p,v) in
                enumerate([('2026年第二季度','10000000000000000000001'),('2026年第一季度','10000000000000000000000')])]
        derived = {'derivedId': 'calc1','outputKind':'TABLE','calculationValidation':'PASS','rows':rows,
                   'columns':[{'name':'quarter','type':'STRING'},{'name':'team','type':'STRING'},
                              {'name':'revenue','type':'DECIMAL_STRING','label':'收入','unit':'元'}]}
        a = artifact(derived=[derived])
        catalog = trend_catalog(a)
        k, = catalog
        self.assertEqual(choices(catalog)[0]['期数'],2)
        result = selected_trends(catalog,[k])[k]
        self.assertEqual([p['height'] for p in result['series'][0]['points']], [0,1])
        self.assertEqual(document(a,k)['valueRefs'],['calc1'])
        self.assertFalse(trend_catalog(artifact(derived=[{**derived,'calculationValidation':'BLOCKED'}])))

    def test_missing_or_wrong_marker_has_local_repair_and_no_opaque_point_list(self):
        a=artifact(observation('2026J0001','1'),observation('2026J0002','3'))
        for key in (None,'invented'):
            with self.assertRaises(AnalysisError) as caught:
                document(a,key)
            self.assertEqual(caught.exception.code,'DOCUMENT_TREND_REQUIRED')
            self.assertFalse(caught.exception.details['requiresBuild'])
            self.assertFalse(caught.exception.details['requiresSourceQuery'])
            self.assertNotIn('points',str(caught.exception.details['trendChoices']))
        doc=document(a,next(iter(trend_catalog(a))))
        sample={'viewport':{'width':900,'height':700},'status':'MEASURED','blocking':False,'orders':[],
                'valueCoverage':{'complete':True,'charted':[],'presented':[]}}
        with self.assertRaises(AnalysisError) as caught:
            require_publication_checks(doc,[sample], 'offline')
        self.assertEqual(caught.exception.code,'DOCUMENT_TREND_COVERAGE_MISSING')

    def test_supported_calendar_labels(self):
        self.assertIsNone(period_code('2025R0366'))
        self.assertEqual(period_code('2026年下半年'),'2026H0002')
        self.assertEqual(period_code('2026年10月'),'2026Y0010')
        self.assertIsNone(period_code('2026J0005'))

    def test_tool_schema_accepts_a_chart_only_and_report_review_gets_a_small_receipt(self):
        from jsonschema import validate
        from types import SimpleNamespace
        from analysis_extension.tools import DOCUMENT
        from analysis_extension.render_evidence import review_text
        a = artifact(observation('2026J0001','0'),observation('2026J0002','9'))
        key, = trend_catalog(a)
        validate({'title':'趋势','html':f'<div data-analysis-trend="{key}"></div>'},DOCUMENT)
        validate({'title':'趋势','path':'trend.html'},DOCUMENT)
        a['freeDocument'] = document(a,key)
        result = review_text(SimpleNamespace(service=SimpleNamespace(store=None)), a, '报告')
        self.assertEqual(result['documentTrends'],[{'metric':'收入','unit':'元','series':1,'from':'2026年第1季度',
                                                  'to':'2026年第2季度','periods':2,'measured':False}])
