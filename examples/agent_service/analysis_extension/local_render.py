"""在独立、无登录态的浏览器上下文中检查宿主冻结页面，复用 iframe 检查协议。"""
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

from .models import AnalysisError
from .render_timing import RENDER_SECONDS, PAGE_STEP_SECONDS


class LocalRenderChecker:
    def __init__(self, widths=(420, 900), channel=None):
        self.widths = list(widths)
        self.channel = channel

    async def check(self, document):
        # The browser driver may hang during startup or teardown, before or after any page timeout.
        with tempfile.TemporaryDirectory(prefix='analysis-render-') as directory:
            folder = Path(directory)
            request = {'html': document['html'], 'frameKey': document['frameKey'],
                       'widths': self.widths, 'channel': self.channel}
            (folder / 'input.json').write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
            result_path = folder / 'result.json'
            try:
                process = subprocess.Popen([sys.executable, '-m', 'analysis_extension.local_render', directory],
                    cwd=Path(__file__).resolve().parent.parent, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=os.name != 'nt',
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                try:
                    loop = asyncio.get_running_loop()
                    deadline = loop.time() + RENDER_SECONDS
                    while process.poll() is None and not result_path.is_file():
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            raise TimeoutError()
                        await asyncio.sleep(min(0.2, remaining))
                    if result_path.is_file() and process.poll() is None:
                        try:
                            await asyncio.to_thread(process.wait, timeout=min(5, max(0, deadline - loop.time())))
                        except subprocess.TimeoutExpired:
                            pass  # The measurements are committed; retire only the owned worker tree.
                finally:
                    if process.poll() is None:
                        await asyncio.to_thread(_stop_worker, process)
                response = json.loads(result_path.read_text(encoding='utf-8'))
                if 'error' not in response and (response.get('frameKey') != document['frameKey'] or
                        [sample['viewport']['width'] for sample in response['measurements']] != self.widths):
                    raise ValueError('incomplete page measurements')
            except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError) as exc:
                phase = (folder / 'stage').read_text(encoding='utf-8') if (folder / 'stage').is_file() else 'START_WORKER'
                raise AnalysisError('RENDER_SERVICE_UNAVAILABLE', '页面测量未完成，报告及已有计算已保留。',
                                    {'retryable': True, 'requiresSourceQuery': False, 'phase': phase,
                                     'deadlineSeconds': RENDER_SECONDS}) from exc
            if 'error' in response:
                error = response['error']
                raise AnalysisError(error['code'], error['message'], error.get('details'))
            return response['measurements']

    async def _measure_document(self, document, phase, measured=None):
        phase('START_DRIVER')
        try:
            from playwright.async_api import async_playwright, Error as BrowserError
        except ImportError as exc:
            raise AnalysisError('RENDER_DEPENDENCY_MISSING',
                                '宿主尚未安装 Playwright 渲染依赖及 Chromium；已保留页面，不能计为检查通过。') from exc
        browser = None
        try:
            async with async_playwright() as playwright:
                async with asyncio.timeout(RENDER_SECONDS):
                    phase('LAUNCH_BROWSER')
                    browser = await playwright.chromium.launch(headless=True, timeout=60000,
                                                               **({'channel': self.channel} if self.channel else {}))
                    try:
                        samples = []
                        for width in self.widths:
                            phase('MEASURE_VIEWPORT_' + str(width))
                            context = await browser.new_context(viewport={'width': width, 'height': 900},
                                                                service_workers='block', accept_downloads=False)
                            try:
                                # 页面只允许自身冻结的内联资源，不带用户 Cookie，不访问业务接口。
                                await context.route('**/*', lambda route: route.abort())
                                page = await context.new_page()
                                sample = await self.measure(page, document)
                                from .render_evidence import snapshot
                                sample['hostRenderedText'] = snapshot(await page.frames[-1].locator('body').inner_text(timeout=10000))
                                samples.append(sample)
                                if measured is not None and len(samples) == len(self.widths):
                                    measured(samples)
                            finally:
                                await context.close()
                        return samples
                    finally:
                        phase('CLOSE_BROWSER')
                        try:
                            await asyncio.wait_for(browser.close(), 5)
                        finally:
                            phase('STOP_DRIVER')
        except (BrowserError, TimeoutError, OSError) as exc:
            raise AnalysisError('RENDER_SERVICE_UNAVAILABLE',
                                '本地无头页面检查未完成，请检查 Chromium 安装及运行环境；保留未检查状态，不重查业务数据。',
                                {'retryable': True, 'requiresSourceQuery': False}) from exc

    async def measure(self, page, document):
        """与聊天页使用相同 sandbox 和 postMessage 协议，测量当前宽度的真实 DOM。"""
        # Give the parent frame a committed site before creating its sandboxed srcdoc.
        # The shell is fulfilled in memory; the context aborts all other requests.
        shell_url = 'https://analysis-render.invalid/'
        shell = '<html><head><style>html,body{margin:0}iframe{display:block;border:0;width:100%}</style></head><body></body></html>'
        await page.route(shell_url, lambda route: route.fulfill(content_type='text/html; charset=utf-8', body=shell))
        await page.goto(shell_url, wait_until='domcontentloaded', timeout=PAGE_STEP_SECONDS * 1000)
        await page.unroute(shell_url)
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
            frame.srcdoc = html;
            // Set the document before attachment so an initial about:blank load
            // cannot be mistaken for the frozen report's load event.
            document.body.appendChild(frame);
        }''', {'html': document['html'], 'key': document['frameKey']})
        await page.wait_for_function('window.__analysisFrameLoaded', timeout=PAGE_STEP_SECONDS * 1000)
        frame = page.frames[-1]
        await frame.evaluate('() => document.fonts.ready')
        # Native disclosures are user-accessible report content. Measure their
        # expanded state in this isolated browser, without modifying the saved
        # report or its default UI. CSS-hidden content remains hidden.
        await frame.evaluate('''() => {
            for (const item of document.querySelectorAll('details')) {
                if ([...item.children].some(child => child.tagName === 'SUMMARY')) item.open = true;
            }
        }''')
        await frame.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        await page.evaluate('''key => document.querySelector('iframe').contentWindow.postMessage(
            {type:'analysis-document-inspect', key, requestId:'host-render'}, '*')''', document['frameKey'])
        await page.wait_for_function('window.__analysisMeasurements !== null', timeout=PAGE_STEP_SECONDS * 1000)
        return await page.evaluate('window.__analysisMeasurements')


def _stop_worker(process):
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


if __name__ == '__main__':
    work = Path(sys.argv[1])
    data = json.loads((work / 'input.json').read_text(encoding='utf-8'))
    checker = LocalRenderChecker(data['widths'], data['channel'])
    def publish(result):
        pending = work / 'result.tmp'
        pending.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        pending.replace(work / 'result.json')

    try:
        asyncio.run(checker._measure_document(data, lambda phase: (work / 'stage').write_text(phase, encoding='utf-8'),
            lambda samples: publish({'frameKey': data['frameKey'], 'measurements': samples})))
    except AnalysisError as error:
        if not (work / 'result.json').is_file():
            result = {'error': error.as_dict()}
            result['error'].setdefault('details', {})['phase'] = (work / 'stage').read_text(encoding='utf-8')
            publish(result)
