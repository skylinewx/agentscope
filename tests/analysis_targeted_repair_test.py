"""冻结页面的批量纠偏、证据传递和完整正文复核。"""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from analysis_extension.document_repair import apply_text_repairs, attach_text_targets, statement_fragments, text_targets
from analysis_extension.models import AnalysisError
from analysis_extension.publication import repair_request
from analysis_extension.review_revision import compact_document_text, formula_review_trace, repair_evidence
from analysis_extension.review_encoding import pack_review_records, unpack_review_records
from analysis_extension.review_control import locate_review_statements
from analysis_extension.turn_completion import review_answer
from analysis_fixtures import OWNER
from analysis_presentation_fixtures import PresentationCase


def edit(old, new, **extra):
    return {'op': 'replaceText', 'path': '/document/html',
            'value': {'oldText': old, 'newText': new, **extra}}


class TextRepairTests(TestCase):
    def test_review_quote_annotations_keep_only_real_separate_statements(self):
        material = {'documentText': '摘要：支出 4 元小于利润 8 元。\n正文：结果仍为正值。'}
        quoted = '摘要「支出 4 元小于利润 8 元。」（正文重复为“结果仍为正值。”）；另外「支出 40 元」。'
        matches = locate_review_statements(quoted, material)
        self.assertEqual([item['statement'] for item in matches],
                         ['支出 4 元小于利润 8 元。', '结果仍为正值。'])
        self.assertEqual(locate_review_statements('支出 40 元小于利润 8 元。', material), [])
        self.assertEqual(locate_review_statements('支出 4 元不小于利润 8 元。', material), [])
        self.assertEqual(locate_review_statements('支出4元小于利润8元。', material), [])
        self.assertEqual(locate_review_statements('支出 4 元小于利润\n8 元。', material)[0]['locations'], ['documentText'])

    def test_multiple_verified_quotes_share_fragment_without_overlap(self):
        source = '<p>摘要：费用 {{expense}} 元吞掉利润。</p><p>正文：费用大于利润。</p>'
        compiled = {'html': '<script id="analysis-host-data" type="application/json">' + json.dumps({
            'values': {'expense': {'display': '4'}}}) + '</script>'}
        issue = {'statement': '费用 4 元吞掉利润。',
                 'relatedStatements': ['吞掉利润。', '费用大于利润。']}
        result = attach_text_targets([issue], {'html': source}, compiled)[0]
        self.assertEqual(len(result['textTargets']), 2)
        targets = result['textTargets']
        fixed = apply_text_repairs(source, [(index, edit(target['oldText'],
            target['oldText'].replace('吞掉利润', '小于利润').replace('费用大于利润', '费用小于利润'),
            textRef=target['textRef'])) for index, target in enumerate(targets)])
        self.assertEqual(fixed, '<p>摘要：费用 {{expense}} 元小于利润。</p><p>正文：费用小于利润。</p>')

    def test_batch_keeps_template_scripts_and_bindings(self):
        source = '<style>p{color:red}</style><p title="旧结论">摘要旧结论</p><p>正文旧结论 &amp; {{amount}}</p><script>const label="旧结论";</script>'
        changes = [edit('摘要旧结论', '摘要新结论'), edit('正文旧结论 & {{amount}}', '正文新结论 & {{amount}}')]
        saved = {'analysisRef': 'a:1', 'document': {'html': source, 'bindings': [{'key': 'amount'}]}}
        result = repair_request(saved, 'a:1', changes)
        self.assertEqual(result['document']['html'], source.replace('摘要旧结论', '摘要新结论').replace('正文旧结论', '正文新结论'))
        self.assertEqual(result['document']['bindings'], saved['document']['bindings'])
        self.assertEqual(saved['document']['html'], source)

    def test_duplicate_text_requires_current_real_locator(self):
        source = '<p>原结论</p><p>原结论</p>'
        with self.assertRaises(AnalysisError) as caught:
            apply_text_repairs(source, [(0, edit('原结论', '更正'))])
        self.assertEqual(caught.exception.details['matchCount'], 2)
        targets = caught.exception.details['textTargets']
        result = apply_text_repairs(source, [(index, edit('原结论', f'更正{index}', textRef=target['textRef']))
                                             for index, target in enumerate(targets)])
        self.assertEqual(result, '<p>更正0</p><p>更正1</p>')
        with self.assertRaises(AnalysisError):
            apply_text_repairs(source + '<p>更新</p>', [(0, edit('原结论', '更正', textRef=targets[0]['textRef']))])

    def test_repair_can_reuse_existing_binding_without_rebuilding_data(self):
        source = '<p>费用 {{expense}} 元造成亏损。</p><p>已知差额 {{difference}} 元。</p>'
        saved = {'analysisRef': 'a:1', 'document': {'html': source, 'bindings': [
            {'key': 'expense', 'valueRef': 'expense_fact'},
            {'key': 'difference', 'valueRef': 'formula_difference'}]}}
        before = deepcopy(saved)
        new_text = '费用 {{expense}} 元减少利润，另有差额 {{difference}} 元尚未解释。'
        result = repair_request(saved, 'a:1', [edit('费用 {{expense}} 元造成亏损。', new_text)])
        self.assertIn(new_text, result['document']['html'])
        self.assertEqual(result['document']['bindings'], saved['document']['bindings'])
        self.assertEqual(saved, before)
        for invalid in ('差额 {{difference}} 元。', '费用 {{expense}} 元，差额 {{unknown}} 元。'):
            with self.subTest(invalid=invalid), self.assertRaises(AnalysisError):
                repair_request(saved, 'a:1', [edit('费用 {{expense}} 元造成亏损。', invalid)])
            self.assertEqual(saved, before)

    def test_failed_batch_overlap_and_binding_change_do_not_mutate_saved(self):
        saved = {'analysisRef': 'a:1', 'document': {'html': '<p>甲乙丙 {{amount}}</p>'}}
        before = deepcopy(saved)
        for changes in ([edit('甲', '新甲'), edit('不存在', '新')],
                        [edit('甲乙', '新甲'), edit('乙丙', '新乙')],
                        [edit('{{amount}}', '0')],
                        [edit('amount', 'other')],
                        [edit('甲', '新'), {'op': 'replace', 'path': '/document/html', 'value': '<p>整页</p>'}],
                        [edit('甲', '新'), {'op': 'remove', 'path': '/missing'}]):
            with self.subTest(changes=changes), self.assertRaises(AnalysisError):
                repair_request(saved, 'a:1', changes)
            self.assertEqual(saved, before)
        with self.assertRaises(AnalysisError):
            repair_request(saved, 'different:1', [edit('甲', '新')])

    def test_text_cannot_change_tags_attributes_scripts_or_insert_markup(self):
        source = '<p title="属性">文字</p><script>脚本</script><p>跨<b>标签</b></p>'
        for old in ('属性', '脚本', '跨标签'):
            with self.subTest(old=old), self.assertRaises(AnalysisError):
                apply_text_repairs(source, [(0, edit(old, '更正'))])
        result = apply_text_repairs(source, [(0, edit('文字', '<b>新增文字</b>'))])
        self.assertIn('&lt;b&gt;新增文字&lt;/b&gt;', result)
        self.assertTrue(result.endswith('<p>跨<b>标签</b></p>'))

    def test_multiple_disjoint_edits_use_one_original_text_node(self):
        source = '<p>甲和乙 &gt; &#49;</p>'
        result = apply_text_repairs(source, [(0, edit('甲', '更长的甲')), (1, edit('乙', '新乙'))])
        self.assertEqual(result, '<p>更长的甲和新乙 &gt; 1</p>')

    def test_line_offsets_and_overlapping_original_occurrences(self):
        source = '<p>甲\r乙\u2028丙</p>\r\n<p>需要修改</p>'
        result = apply_text_repairs(source, [(0, edit('需要修改', '已修改'))])
        self.assertEqual(result, source.replace('需要修改', '已修改'))
        with self.assertRaises(AnalysisError) as caught:
            apply_text_repairs('<p>aaa</p>', [(0, edit('aa', 'b'))])
        self.assertEqual(caught.exception.details['matchCount'], 2)

    def test_rendered_statement_resolves_across_tags_and_preserves_bound_numbers(self):
        source = '<style>p{color:red}</style><p>营业环节<strong>盈利</strong> {{profit}} 元，费用 {{expense}} 元使结果转亏。</p>'
        compiled = {'html': '<script id="analysis-host-data" type="application/json">' + json.dumps({
            'values': {'profit': {'display': '2,860,480.43'}, 'expense': {'display': '1,545,800.00'}}}) + '</script>'}
        issue = {'statement': '营业环节盈利 2,860,480.43 元，费用 1,545,800.00 元使结果转亏。'}
        located = attach_text_targets([issue], {'html': source}, compiled)[0]
        self.assertTrue(located['textRepairable'])
        self.assertEqual(len(located['textTargets']), 3)
        target = located['textTargets'][-1]
        self.assertEqual(target['matchMode'], 'STATEMENT_FRAGMENT')
        changed = apply_text_repairs(source, [(0, edit(target['oldText'],
            target['oldText'].replace('使结果转亏', '小于营业利润；应另列原始值与复算差额'),
            textRef=target['textRef']))])
        self.assertEqual(changed, source.replace('使结果转亏', '小于营业利润；应另列原始值与复算差额'))
        for wrong in (issue['statement'].replace('2,860,480.43', '28,604,804.30'),
                      issue['statement'].replace('使结果转亏', '未使结果转亏'), '   '):
            self.assertEqual(statement_fragments(source, wrong, compiled), [])

    def test_fragment_matching_keeps_word_boundaries_and_null_binding(self):
        source = '<p>Total\n <b>cost</b> {{value}} is missing.</p>'
        compiled = {'html': '<script id="analysis-host-data" type="application/json">' + json.dumps({
            'values': {'value': {'display': '—', 'value': None}}}) + '</script>'}
        self.assertEqual(len(statement_fragments(source, 'Total cost — is missing.', compiled)), 3)
        self.assertEqual(statement_fragments(source, 'Totalcost — is missing.', compiled), [])
        for invalid in ({}, {'html': compiled['html'] * 2}, {'html': compiled['html'].replace('"values"', '"other"')}):
            self.assertEqual(statement_fragments(source, 'Total cost — is missing.', invalid), [])
        dynamic = attach_text_targets([{'statement': '动态结论'}], {'html': '<script>label="动态结论";</script>'}, compiled)[0]
        self.assertFalse(dynamic['textRepairable'])


class RepairMaterialTests(TestCase):
    def test_local_and_recursive_checks_remain_explicit_in_encoded_material(self):
        nodes = [{'factRef': f'fact_{index}', 'observed': '-4.00', 'status': 'BLOCKED',
                  'recalculated': None, 'difference': None,
                  'localCheck': {'status': 'MATCH', 'value': '-4.00', 'difference': '0'}}
                 for index in range(12)]
        nodes[2]['localCheck'] = {'status': 'MISMATCH', 'value': '8.00', 'difference': '-12.00'}
        nodes[3]['observed'] = None
        nodes[3]['localCheck'] = {'status': 'BLOCKED', 'value': None, 'difference': None}
        original = {'status': 'BLOCKED', 'value': None, 'targetLocalCheck': nodes[0]['localCheck'], 'nodes': nodes}
        before = deepcopy(original)
        trace = formula_review_trace(original)
        material = {'facts': [{'factId': f'fact_{index}', 'value': str(index), 'context': 'same context' * 20}
                              for index in range(12)],
                    'derived': [{'derivedId': 'formula', 'formulaTrace': trace}]}
        packed = pack_review_records(material)
        direct = packed['derived'][0]['formulaTrace']
        self.assertEqual(direct, trace)
        self.assertIsInstance(direct['nodes'], list)
        self.assertEqual(direct['recursiveStatus'], 'BLOCKED')
        self.assertEqual(direct['nodes'][0]['localCheck']['status'], 'MATCH')
        self.assertEqual(direct['nodes'][0]['recursiveCheck'], {'status': 'BLOCKED', 'value': None, 'difference': None})
        self.assertEqual(direct['nodes'][2]['localCheck']['difference'], '-12.00')
        self.assertIsNone(direct['nodes'][3]['observed'])
        self.assertEqual(unpack_review_records(packed), material)
        self.assertEqual(original, before)

    def test_source_projection_is_reversible_and_keeps_hidden_contradiction(self):
        lines = [f'正文{i}：当前来源、金额和范围均保持完整。' for i in range(100)]
        source = lines[:50] + ['隐藏旧结论：费用大于收入。', '{{amount}}'] + lines[50:]
        material = {'documentTextBasis': 'HOST_RENDERED', 'documentText': '\n'.join(lines),
                    'documentSourceText': '\n'.join(source), 'facts': [{'value': None}],
                    'derived': [{'difference': '-1322980.43'}]}
        projected = compact_document_text(material)
        self.assertEqual(projected['documentText'], material['documentText'])
        reconstructed = lines[:]
        for change in reversed(projected['documentSourceTextProjection']['changes']):
            reconstructed[change['renderedStartLine']:change['renderedEndLine']] = change['sourceLines']
        self.assertEqual(reconstructed, source)
        self.assertEqual(projected['facts'], material['facts'])
        self.assertLess(len(json.dumps(projected)), len(json.dumps(material)))
        self.assertIn('隐藏旧结论', str(projected))

    def test_static_or_very_different_source_is_not_projected(self):
        material = {'documentTextBasis': 'STATIC_SOURCE', 'documentText': '甲', 'documentSourceText': '乙'}
        self.assertEqual(compact_document_text(material), material)
        material['documentTextBasis'] = 'HOST_RENDERED'
        self.assertEqual(compact_document_text(material), material)

    def test_repair_evidence_keeps_dependencies_null_and_difference(self):
        material = {'facts': [{'factId': 'a', 'value': None}, {'factId': 'b', 'value': '-8300'}],
                    'derived': [{'derivedId': 'd', 'inputRefs': ['a', 'b'], 'value': None,
                                 'formulaTrace': {'status': 'BLOCKED', 'nodes': [
                                     {'factRef': 'b', 'difference': '-1322980.43'}]}}]}
        result = repair_evidence(material, [{'evidenceRefs': ['d']}])
        self.assertTrue(result['included'])
        self.assertEqual(result['facts'], material['facts'])
        self.assertEqual(result['derived'], material['derived'])


class RepairPublicationTests(PresentationCase):
    async def prepare(self):
        from analysis_free_document_test import FreeDocumentTests
        request = await FreeDocumentTests.request(self)
        request['renderCheckUnavailableReason'] = '单元测试验证修正闭环；真实回归另行检查页面。'
        request['document']['html'] = request['document']['html'].replace(
            '</body>', '<p>摘要：支出大于收入。</p><p>正文：支出大于收入。</p></body>')
        turn = SimpleNamespace(service=self.service, state={
            'request': '分析这些单位的盈亏', 'goal': '分析形成原因', 'mode': 'ANALYSIS', 'turnKey': 'repair'})
        self.service.preview_reviewer = lambda artifact: review_answer(None, turn, artifact, '')
        return request, turn

    async def test_one_batch_repair_reuses_data_and_first_publish_checks_once(self):
        request, turn = await self.prepare()
        facts = self.store.get_analysis(OWNER, self.ref)['facts']
        failure = {'severity': 'BLOCKING', 'code': 'ANSWER_MISMATCH',
                   'message': '摘要与正文的大小关系与数据矛盾。',
                   'statement': '支出大于收入。', 'evidenceRefs': [facts[0]['factId'], 'unknown']}
        with patch('analysis_extension.turn_completion._model_review', AsyncMock(side_effect=[
                {'issues': [failure]}, {'issues': []}])) as reviewer:
            preview = await self.service.publish(**request, finalize=True)
            verdict = preview['diagnostics']['semanticReview']
            issue = verdict['issues'][0]
            self.assertEqual(issue['statement'], failure['statement'])
            self.assertEqual(issue['evidenceRefs'], [facts[0]['factId']])
            self.assertEqual(len(issue['textTargets']), 2)
            self.assertEqual(verdict['repairEvidence']['facts'][0]['value'], facts[0]['value'])
            args = deepcopy(preview['repairAndFinalize'])
            args['repairs'] = [edit('支出大于收入。', '支出小于收入，原始值与复算差额分别列示。', textRef=target['textRef'])
                               for target in issue['textTargets']]
            published = await self.service.publish(**args)
            self.assertEqual(published['publicationState'], 'PUBLISHED')
            self.assertEqual(reviewer.await_count, 2)
        saved = self.store.get_analysis(OWNER, self.ref)
        self.assertEqual(saved['facts'], facts)
        self.assertNotIn('支出大于收入', saved['freeDocument']['html'])
        self.assertIn('摘要：支出小于收入', saved['freeDocument']['html'])
        self.assertIn('正文：支出小于收入', saved['freeDocument']['html'])

    async def test_unfixed_summary_is_still_in_full_review_and_blocks(self):
        request, turn = await self.prepare()
        issue = {'severity': 'BLOCKING', 'code': 'ANSWER_MISMATCH', 'message': '摘要仍矛盾',
                 'statement': '摘要：支出大于收入。', 'evidenceRefs': []}
        with patch('analysis_extension.turn_completion._model_review', AsyncMock(return_value={'issues': [issue]})) as reviewer:
            preview = await self.service.publish(**request, finalize=True)
            args = deepcopy(preview['repairAndFinalize'])
            args['repairs'] = [edit('正文：支出大于收入。', '正文：支出小于收入。')]
            repaired = await self.service.publish(**args)
            self.assertEqual(repaired['diagnostics']['semanticReview']['status'], 'REVISE')
            edited_path = self.delivery.root / repaired['documentEditSource']['path']
            self.assertIn('正文：支出小于收入。', edited_path.read_text('utf-8'))
            self.assertEqual(reviewer.await_count, 2)
            self.assertIn('摘要：支出大于收入。', reviewer.call_args.args[2])
        self.assertEqual(self.store.get_analysis(OWNER, self.ref)['publicationState'], 'DRAFT')

    async def test_host_locates_bound_statement_and_repairs_in_one_submission(self):
        request, turn = await self.prepare()
        request['document']['html'] = request['document']['html'].replace(
            '</body>', '<p>本期<strong>收入</strong> {{amount}} 全部形成了亏损。</p></body>')
        failure = {'severity': 'BLOCKING', 'code': 'ANSWER_MISMATCH', 'message': '收入不能等同亏损。',
                   'statement': '本期收入 1,234,567,890.12 全部形成了亏损。', 'evidenceRefs': []}
        def measured_text(turn, artifact, source):
            # 模拟浏览器把内联标签连成同一行；静态源码的逐节点换行不是实际展示原句。
            from analysis_extension.document_repair import _TextNodes
            return {'documentTextBasis': 'HOST_RENDERED', 'documentText': ''.join(
                node['text'] for node in _TextNodes(artifact['freeDocument']['html']).nodes)}

        with patch('analysis_extension.render_evidence.review_text', side_effect=measured_text), \
                patch('analysis_extension.turn_completion._model_review', AsyncMock(side_effect=[
                    {'issues': [failure]}, {'issues': []}])) as reviewer:
            preview = await self.service.publish(**request, finalize=True)
            issue = preview['diagnostics']['semanticReview']['issues'][0]
            target = next(target for target in issue['textTargets'] if '全部形成了亏损' in target['oldText'])
            args = deepcopy(preview['repairAndFinalize'])
            args['repairs'] = [edit(target['oldText'], target['oldText'].replace('全部形成了亏损', '需结合成本费用分析'),
                                    textRef=target['textRef'])]
            published = await self.service.publish(**args)
            self.assertEqual(published['publicationState'], 'PUBLISHED')
            self.assertEqual(reviewer.await_count, 2)
        document = self.store.get_analysis(OWNER, self.ref)['freeDocument']
        self.assertIn('需结合成本费用分析', document['html'])
        self.assertIn('<strong>收入</strong>', document['html'])
        self.assertNotIn('全部形成了亏损', document['html'])
