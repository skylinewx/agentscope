"""Real browser checks for version-bound status outside the frozen report body."""
import os
from unittest import skipUnless

from analysis_presentation_fixtures import PresentationCase
from analysis_fixtures import OWNER
import analysis_report_review_delivery_test as review_fixtures
from analysis_extension.report_review import save_review


@skipUnless(os.environ.get('RUN_ANALYSIS_BROWSER_TESTS') == '1', 'explicit real browser regression')
class ReportReviewBrowserTests(PresentationCase):
    async def test_current_report_export_keeps_current_text_and_visible_review_status(self):
        from playwright.async_api import async_playwright
        request = await review_fixtures.ReportReviewDeliveryTests.prepare(self)
        async def approved(artifact): return {'issues': []}
        self.service.preview_reviewer = approved
        await self.service.publish(**request, previewOnly=True)
        self.service.preview_reviewer = lambda artifact: review_fixtures.ReportReviewDeliveryTests.partial_review(self, artifact)
        request['document']['html'] = request['document']['html'].replace('完整内容', '后来修改内容')
        request['requestKey'] = 'next-preview'
        await self.service.publish(**request, previewOnly=True)
        await self.service.publish_available_report(self.ref, 'REVIEW_LIMIT')
        export = self.delivery.render_report(self.ref, 'fallback-export')
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, **({'channel': os.environ['ANALYSIS_TEST_BROWSER_CHANNEL']} if os.environ.get('ANALYSIS_TEST_BROWSER_CHANNEL') else {}))
            try:
                page = await browser.new_page(viewport={'width': 420, 'height': 900})
                await page.route('**/*', lambda route: route.abort())
                await page.set_content(self.delivery.report(self.ref, export['exportRef'])['html'])
                for summary in await page.locator('[role=status] summary').all(): await summary.click()
                text = await page.locator('[role=status]').inner_text()
                self.assertIn('当前成果的审核详情', text)
                self.assertNotIn('较新候选的审查详情', text)
                self.assertIn('调用未完成', text)
                self.assertIn('后来修改内容', await page.frames[-1].locator('body').inner_text())
                self.assertNotIn('完整内容', await page.frames[-1].locator('body').inner_text())
            finally:
                await browser.close()

    async def test_pending_export_and_later_verdict_keep_complete_iframe_and_visible_status(self):
        from playwright.async_api import async_playwright
        request = await review_fixtures.ReportReviewDeliveryTests.prepare(self)
        self.service.preview_reviewer = lambda artifact: review_fixtures.ReportReviewDeliveryTests.partial_review(self, artifact)
        await self.service.publish(**request, previewOnly=True)
        delivered = await self.service.publish_available_report(self.ref, 'REVIEW_LIMIT')
        export = self.delivery.render_report(self.ref, 'browser-export')
        original_html = self.store.get_analysis(OWNER, self.ref)['freeDocument']['html']
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, **({'channel': os.environ['ANALYSIS_TEST_BROWSER_CHANNEL']} if os.environ.get('ANALYSIS_TEST_BROWSER_CHANNEL') else {}))
            try:
                for width in (420, 900):
                    page = await browser.new_page(viewport={'width': width, 'height': 900})
                    await page.route('**/*', lambda route: route.abort())
                    await page.set_content(self.delivery.report(self.ref, export['exportRef'])['html'])
                    self.assertIn('AI 审核尚未全部完成', await page.locator('[role=status]').inner_text())
                    await page.locator('summary').click()
                    self.assertIn('调用未完成', await page.locator('[role=status]').inner_text())
                    await page.wait_for_function("document.querySelector('iframe') !== null")
                    self.assertIn('完整内容', await page.frames[-1].locator('body').inner_text())
                    await page.close()
                record = self.store.get_report_review(OWNER, delivered['reportDelivery']['reviewRef'])
                record['parts'][1].update(status='REVISE', issues=[{'severity': 'BLOCKING', 'message': '当前版本需要修正数值'}])
                save_review(self.turn, record)
                page = await browser.new_page()
                await page.set_content(self.delivery.report(self.ref, export['exportRef'])['html'])
                await page.locator('summary').click()
                text = await page.locator('[role=status]').inner_text()
                self.assertIn('需要修正数值', text)
                self.assertNotIn('AI 审核尚未全部完成', text)
                self.assertEqual(self.store.get_analysis(OWNER, self.ref)['freeDocument']['html'], original_html)
            finally:
                await browser.close()
