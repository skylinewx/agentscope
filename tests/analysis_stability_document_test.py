"""Presentation revisions keep numeric evidence, not an arbitrary number of bars."""
from copy import deepcopy
import os
from unittest import skipUnless

from analysis_document_checks_test import measurements
from analysis_fixtures import OWNER, saved_fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.document_checks import evaluate as evaluate_observation, require_publication_checks
from analysis_extension.free_document import compile_document
from analysis_extension.local_render import LocalRenderChecker
from analysis_extension.models import AnalysisError


def evaluate(document, sample):
    # The renderer's trusted text snapshot is separate from the iframe protocol.
    return evaluate_observation(document, {key: value for key, value in sample.items() if key != 'hostRenderedText'})


class StableDocumentTests(PresentationCase):
    async def document(self):
        capture, selections, scope = saved_fixture(self.store, ('100', '-0.01'))
        built = await self.service.build([capture], selections, scope, [], 'base')
        self.ref = built['analysisRef']
        self.artifact = self.store.get_analysis(OWNER, self.ref)
        self.source = {'title': '数值表达', 'html': '<html><head></head><body><p>{{a}} / {{b}}</p></body></html>',
                       'bindings': [{'key': key, 'valueRef': fact['factId']} for key, fact in zip(('a', 'b'), self.artifact['facts'])]}
        return compile_document(self.artifact, self.source)

    def sample(self, width, charted=('a', 'b'), presented=('a', 'b')):
        result = measurements(width=width)
        result.update(version=4, orders=[], valueCoverage={
            'charted': [{'valueKey': key} for key in charted],
            'presented': [{'valueKey': key} for key in presented], 'complete': True})
        result['numericChecks']['linearBars'].update(declared=len(charted), measured=len(charted),
                                                     scaleGroups=1 if charted else 0, checkedGroups=1 if charted else 0)
        return result

    async def test_bar_to_label_preserves_identity_and_publishes_after_saved_repair(self):
        compiled = await self.document()
        bad = self.sample(900)
        bad['numericChecks']['linearBars']['failedGroups'] = 1
        self.store.save_document_check(OWNER, self.ref, evaluate(compiled, bad))
        revised = {**self.source, 'html': self.source['html'].replace('<p>', '<p class="labels">')}
        current = compile_document(self.artifact, revised)
        preview = await self.service.publish(self.ref, document=revised, requestKey='labels', previewOnly=True)
        for width in (420, 900):
            self.store.save_document_check(OWNER, self.ref, evaluate(current, self.sample(width, charted=('a',))))
        published = await self.service.publish(**preview['confirmPublish'])
        self.assertTrue(published['delivery']['registered'])

    async def test_removing_a_value_still_fails_when_geometry_passes(self):
        document = await self.document()
        self.store.save_document_check(OWNER, self.ref, evaluate(document, self.sample(900)))
        requirements = self.store.document_check_requirements(OWNER, self.ref)
        records = [evaluate(document, self.sample(width, charted=('a',), presented=('a',))) for width in (420, 900)]
        with self.assertRaises(AnalysisError) as caught:
            require_publication_checks(document, records, requirements=requirements)
        self.assertEqual(caught.exception.code, 'DOCUMENT_CHECK_COVERAGE_LOST')

    async def test_foreign_or_incomplete_coverage_is_not_accepted(self):
        document = await self.document()
        foreign = self.sample(900, presented=('foreign',))
        with self.assertRaises(AnalysisError): evaluate(document, foreign)
        sample = self.sample(900)
        sample['valueCoverage']['complete'] = False
        with self.assertRaises(AnalysisError):
            require_publication_checks(document, [evaluate(document, sample)])

    async def test_old_count_only_preview_migrates_to_value_coverage(self):
        original = await self.document()
        legacy = measurements(width=900)
        legacy['orders'] = []
        legacy['numericChecks']['linearBars'].update(declared=2, measured=2, invalid=2)
        self.store.save_document_check(OWNER, self.ref, evaluate(original, legacy))
        current = compile_document(self.artifact, {**self.source, 'html': self.source['html'].replace('<p>', '<p class="new">')})
        records = []
        for width in (420, 900):
            record = evaluate(current, self.sample(width, charted=()))
            records.append(record)
            self.store.save_document_check(OWNER, self.ref, record)
        requirements = self.store.document_check_requirements(OWNER, self.ref)
        self.assertEqual(len([key for key in requirements if key.startswith('bound:')]), 2)
        require_publication_checks(current, records, requirements=requirements)

    async def test_legacy_coverage_survives_presentation_revision(self):
        original = await self.document()
        legacy = measurements(width=900)
        legacy['orders'] = []
        legacy['numericChecks']['linearBars'].update(declared=2, measured=2, invalid=2)
        self.store.save_document_check(OWNER, self.ref, evaluate(original, legacy))
        revised = await self.service.build(baseAnalysisRef=self.ref, operations=[], requestKey='revision')
        artifact = self.store.get_analysis(OWNER, revised['analysisRef'])
        current = compile_document(artifact, self.source)
        records = [evaluate(current, self.sample(width, charted=('a',), presented=('a',))) for width in (420, 900)]
        for record in records:
            self.store.save_document_check(OWNER, revised['analysisRef'], record)
        with self.assertRaises(AnalysisError) as caught:
            require_publication_checks(current, records, requirements=self.store.document_check_requirements(OWNER, revised['analysisRef']))
        self.assertEqual(caught.exception.code, 'DOCUMENT_CHECK_COVERAGE_LOST')

    async def test_legacy_migration_does_not_require_unit_text_as_a_number(self):
        original = await self.document()
        source = deepcopy(self.source)
        source['bindings'].append({'key': 'unit', 'valueRef': self.artifact['facts'][0]['factId'], 'field': 'unit'})
        current = compile_document(self.artifact, source)
        record = evaluate(current, self.sample(900))
        self.assertEqual(len(record['valueCoverage']['sourceRefs']), 2)


@skipUnless(os.environ.get('RUN_ANALYSIS_BROWSER_TESTS') == '1', 'explicit real browser regression')
class StableBrowserTests(PresentationCase):
    async def prepare(self, script):
        capture, selections, scope = saved_fixture(self.store, ('100000000', '-0.01'))
        built = await self.service.build([capture], selections, scope, [], 'browser')
        self.ref = built['analysisRef']
        self.artifact = self.store.get_analysis(OWNER, self.ref)
        self.source = {'title': '实际图形检查', 'html': '''<html><head><style>
body{font:16px sans-serif}.track{width:300px}.bar{height:12px;background:teal}
</style></head><body><div id="track" class="track"><div id="a" class="bar"></div><div id="b" class="bar"></div></div>
<p id="label"></p><script>''' + script + '</script></body></html>',
            'bindings': [{'key': key, 'valueRef': fact['factId']} for key, fact in zip(('a', 'b'), self.artifact['facts'])]}
        self.checker = LocalRenderChecker(channel=os.environ.get('ANALYSIS_TEST_BROWSER_CHANNEL') or None)
        document = compile_document(self.artifact, self.source)
        samples = await self.checker.check(document)
        return document, [evaluate(document, sample) for sample in samples]

    async def test_subpixel_bar_can_be_replaced_by_exact_label_at_both_widths(self):
        document, records = await self.prepare('''analysisCharts.bindLinearBars(['a','b'].map(key=>({element:document.getElementById(key),valueKey:key})),{extent:document.getElementById('track')});''')
        self.assertFalse(any(record['blocking'] for record in records))
        for record in records: self.store.save_document_check(OWNER, self.ref, record)
        self.source['html'] = self.source['html'].replace("['a','b']", "['a']").replace('</script>', "document.getElementById('b').remove();analysisCharts.bindValueLabel(document.getElementById('label'),{valueKey:'b'});</script>")
        revised = compile_document(self.artifact, self.source)
        samples = await self.checker.check(revised)
        current = [evaluate(revised, sample) for sample in samples]
        self.assertTrue(all(record['numericChecks']['linearBars']['measured'] == 1 for record in current))
        require_publication_checks(revised, current, requirements=self.store.document_check_requirements(OWNER, self.ref))

    async def test_deleting_markers_from_existing_component_does_not_hide_bad_geometry(self):
        _, records = await self.prepare('''analysisCharts.bindLinearBars(['a','b'].map(key=>({element:document.getElementById(key),valueKey:key})),{extent:document.getElementById('track')});
document.getElementById('b').style.minWidth='20px';delete document.getElementById('b').dataset.analysisScale;''')
        self.assertTrue(all(record['blocking'] for record in records))

    async def test_replacement_label_changed_to_wrong_value_is_rejected(self):
        _, records = await self.prepare('''document.getElementById('track').remove();analysisCharts.bindValueLabel(document.getElementById('label'),{valueKey:'b'});document.getElementById('label').textContent='0';''')
        self.assertTrue(all(record['blocking'] for record in records))

    async def test_table_row_can_replace_bar_but_hidden_number_cannot(self):
        script = '''document.getElementById('track').remove();const table=document.createElement('div');table.dataset.analysisSet='rows';
for(const row of analysisSets.rows.rows){const el=document.createElement('div');el.dataset.analysisRef=row.factRef;el.textContent=row.value;table.appendChild(el);}document.body.appendChild(table);'''
        # Build a real ORDERED dataset, then run the same renderer with dynamic table rows.
        await self.prepare('')
        built = await self.service.build(baseAnalysisRef=self.ref, operations=[{'id':'order','operator':'SORT'}], requestKey='sort')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        source = deepcopy(self.source)
        source['datasets'] = [{'key':'rows','valueRef':'derived_order'}]
        source['html'] = source['html'].replace('<script></script>', '<script>'+script+'</script>')
        document = compile_document(artifact, source)
        records = [evaluate(document, sample) for sample in await self.checker.check(document)]
        self.assertTrue(all(len(record['valueCoverage']['presented']) == 2 for record in records))
        source['html'] = source['html'].replace('el.textContent=row.value;', "el.innerHTML='<span hidden>'+row.value+'</span>';" )
        document = compile_document(artifact, source)
        records = [evaluate(document, sample) for sample in await self.checker.check(document)]
        self.assertTrue(all(record['valueCoverage']['presented'] == [] for record in records))
