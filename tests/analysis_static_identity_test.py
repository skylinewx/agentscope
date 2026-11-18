"""Static coverage must measure each bound occurrence, not equal text elsewhere."""
import os
from unittest import skipUnless
from analysis_fixtures import OWNER, saved_fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.free_document import compile_document
from analysis_extension.local_render import LocalRenderChecker
from analysis_extension.document_checks import evaluate, require_publication_checks
from analysis_extension.models import AnalysisError


@skipUnless(os.environ.get('RUN_ANALYSIS_BROWSER_TESTS') == '1', 'explicit real browser regression')
class StaticIdentityBrowserTests(PresentationCase):
    async def test_closed_native_disclosure_is_measured_without_changing_default_document(self):
        capture, selections, scope = saved_fixture(self.store, ('100', '50'))
        built = await self.service.build([capture], selections, scope, [], 'disclosure')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        document = compile_document(artifact, {'title': '可展开的完整明细',
            'html': '<html><head></head><body><p>{{a}}</p><details><summary>单位明细</summary><p>{{b}}</p></details></body></html>',
            'bindings': [{'key':key,'valueRef':fact['factId']} for key,fact in zip(('a','b'),artifact['facts'])]})
        original = document['html']
        checker = LocalRenderChecker(channel=os.environ.get('ANALYSIS_TEST_BROWSER_CHANNEL') or None)
        samples = await checker.check(document)
        records = [evaluate(document, {key:value for key,value in sample.items() if key!='hostRenderedText'}) for sample in samples]
        self.assertTrue(all(len(record['valueCoverage']['presented'])==2 for record in records))
        require_publication_checks(document, records)
        self.assertEqual(document['html'], original)
        self.assertIn('<details>', document['html'])

    async def test_equal_values_do_not_cover_hidden_fact_and_valid_table_still_passes(self):
        capture, selections, scope = saved_fixture(self.store, ('100', '100'))
        built = await self.service.build([capture], selections, scope, [], 'static-identity')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        source = {'title': '两个对象', 'html': '<html><head><style>td{padding:8px}</style></head>'
                  '<body><table><tr><td>甲</td><td>{{a}}</td></tr>'
                  '<tr class="second"><td>乙</td><td>{{b}}</td></tr></table></body></html>',
                  'bindings': [{'key': key, 'valueRef': fact['factId']}
                               for key, fact in zip(('a', 'b'), artifact['facts'])]}
        checker = LocalRenderChecker(channel=os.environ.get('ANALYSIS_TEST_BROWSER_CHANNEL') or None)
        for hidden in (True, False):
            html = source['html'].replace('class="second"', 'class="second" style="display:none"') if hidden else source['html']
            document = compile_document(artifact, {**source, 'html': html})
            self.assertNotIn('<span', document['html'])
            samples = await checker.check(document)
            records = [evaluate(document, {key: value for key, value in sample.items() if key != 'hostRenderedText'}) for sample in samples]
            for sample in samples:
                presented = {item.get('valueKey') for item in sample['valueCoverage']['presented']}
                self.assertEqual(presented, {'a'} if hidden else {'a', 'b'})
            if hidden:
                with self.assertRaises(AnalysisError):
                    require_publication_checks(document, records)
            else:
                require_publication_checks(document, records)
