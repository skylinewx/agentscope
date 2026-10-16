"""Host-configured headless checks; no model-selected URLs or local Chrome paths."""
import asyncio
import json
import os
from urllib.parse import urlsplit

import httpx

from .models import AnalysisError


class RenderChecker:
    def __init__(self, url, token, widths=(420, 900)):
        try:
            target = urlsplit(url)
            target.port
        except (TypeError, ValueError) as exc:
            raise AnalysisError('INVALID_RENDER_CONFIG', '无头检查服务地址无效。') from exc
        if (target.scheme not in ('http', 'https') or not target.hostname or target.username
                or target.password or target.query or target.fragment or not isinstance(token, str) or not token.strip()
                or not isinstance(widths, (tuple, list)) or not 2 <= len(widths) <= 4
                or any(type(w) is not int or not 320 <= w <= 2400 for w in widths)
                or len(set(widths)) != len(widths)):
            raise AnalysisError('INVALID_RENDER_CONFIG', '无头检查服务地址、凭据或视口配置无效。')
        self.url, self.token, self.widths = url.rstrip('/'), token, list(widths)

    @classmethod
    def from_environment(cls):
        url = os.environ.get('ANALYSIS_RENDER_URL')
        if not url: return None
        try: widths = json.loads(os.environ.get('ANALYSIS_RENDER_WIDTHS', '[420,900]'))
        except ValueError as exc:
            raise AnalysisError('INVALID_RENDER_CONFIG', 'ANALYSIS_RENDER_WIDTHS 须为宽度数组。') from exc
        if not isinstance(widths, list):
            raise AnalysisError('INVALID_RENDER_CONFIG', 'ANALYSIS_RENDER_WIDTHS 须为宽度数组。')
        return cls(url, os.environ.get('ANALYSIS_RENDER_TOKEN', ''), widths)

    async def check(self, document):
        payload = {key: document[key] for key in ('html', 'contentDigest', 'frameKey')}
        payload.update(version=1, widths=self.widths)
        try:
            async with asyncio.timeout(40):
                async with httpx.AsyncClient(timeout=httpx.Timeout(35, connect=5), trust_env=False,
                                             follow_redirects=False) as client:
                    async with client.stream('POST', self.url + '/check', json=payload,
                                             headers={'Authorization': 'Bearer ' + self.token}) as response:
                        response.raise_for_status()
                        chunks = bytearray()
                        async for chunk in response.aiter_bytes():
                            chunks.extend(chunk)
                            if len(chunks) > 300000: raise ValueError('oversized result')
                        result = json.loads(chunks)
            if (type(result.get('version')) is not int or result.get('version') != 1 or result.get('contentDigest') != document['contentDigest']
                    or result.get('frameKey') != document['frameKey']
                    or not isinstance(result.get('measurements'), list)
                    or len(result['measurements']) != len(self.widths)
                    or [m.get('viewport', {}).get('width') for m in result['measurements']] != self.widths):
                raise ValueError('mismatched result')
            return result['measurements']
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, AttributeError) as exc:
            raise AnalysisError('RENDER_SERVICE_UNAVAILABLE',
                                '无头页面检查未完成；检查渲染服务的健康状态、连接和版本，或使用可用浏览器检查。不会计为通过。') from exc
