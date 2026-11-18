"""在独立、无登录态的浏览器上下文中检查宿主冻结页面，复用 iframe 检查协议。"""
import asyncio

from .models import AnalysisError


class LocalRenderChecker:
    def __init__(self, widths=(420, 900)):
        self.widths = list(widths)

    async def check(self, document):
        try:
            from playwright.async_api import async_playwright, Error as BrowserError
        except ImportError as exc:
            raise AnalysisError('RENDER_DEPENDENCY_MISSING',
                                '宿主尚未安装 Playwright 渲染依赖及 Chromium；已保留页面，不能计为检查通过。') from exc
        browser = None
        try:
            async with async_playwright() as playwright:
                async with asyncio.timeout(35):
                    browser = await playwright.chromium.launch(headless=True)
                    try:
                        samples = []
                        for width in self.widths:
                            context = await browser.new_context(viewport={'width': width, 'height': 900},
                                                                service_workers='block', accept_downloads=False)
                            try:
                                # 页面只允许自身冻结的内联资源，不带用户 Cookie，不访问业务接口。
                                await context.route('**/*', lambda route: route.abort())
                                page = await context.new_page()
                                sample = await self.measure(page, document)
                                from .render_evidence import snapshot
                                sample['hostRenderedText'] = snapshot(await page.frames[-1].locator('body').inner_text(timeout=2000))
                                samples.append(sample)
                            finally:
                                await context.close()
                        return samples
                    finally:
                        await asyncio.wait_for(browser.close(), 5)
        except (BrowserError, TimeoutError, OSError) as exc:
            raise AnalysisError('RENDER_SERVICE_UNAVAILABLE',
                                '本地无头页面检查未完成，请检查 Chromium 安装及运行环境；保留未检查状态，不重查业务数据。',
                                {'retryable': True, 'requiresSourceQuery': False}) from exc

    async def measure(self, page, document):
        """与聊天页使用相同 sandbox 和 postMessage 协议，测量当前宽度的真实 DOM。"""
        await page.set_content('<html><head><style>html,body{margin:0}iframe{display:block;border:0;width:100%}</style></head><body></body></html>')
        await page.evaluate('''({html,key}) => {
            window.__analysisMeasurements = null;
            window.__analysisFrameLoaded = false;
            const frame = document.createElement('iframe');
            frame.setAttribute('sandbox','allow-scripts');
            frame.style.height = '900px';
            frame.addEventListener('load', () => { window.__analysisFrameLoaded = true; });
            window.addEventListener('message', event => {
                if(event.source !== frame.contentWindow || event.data?.key !== key) return;
                if(event.data.type === 'analysis-document-size') frame.style.height = event.data.height + 'px';
                if(event.data.type === 'analysis-document-inspection' && event.data.requestId === 'host-render')
                    window.__analysisMeasurements = event.data.measurements;
            });
            document.body.appendChild(frame);
            frame.srcdoc = html;
        }''', {'html': document['html'], 'key': document['frameKey']})
        await page.wait_for_function('window.__analysisFrameLoaded', timeout=10000)
        frame = page.frames[-1]
        await frame.evaluate('() => document.fonts.ready')
        await page.evaluate('''key => document.querySelector('iframe').contentWindow.postMessage(
            {type:'analysis-document-inspect', key, requestId:'host-render'}, '*')''', document['frameKey'])
        await page.wait_for_function('window.__analysisMeasurements !== null', timeout=10000)
        return await page.evaluate('window.__analysisMeasurements')
