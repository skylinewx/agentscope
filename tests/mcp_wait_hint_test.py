from datetime import timedelta
from unittest import IsolatedAsyncioTestCase
import mcp.types
from agentscope.mcp import MCPClient
from agentscope.mcp._config import HttpMCPConfig


class DurableWaitHintTest(IsolatedAsyncioTestCase):
    async def test_server_wait_hint_extends_transport_and_call_without_overriding_larger_configuration(self):
        for configured in (None, 30, 600):
            client = MCPClient(name='query', is_stateful=False,
                               mcp_config=HttpMCPConfig(url='http://localhost/mcp', timeout=configured))
            client._cached_tools = [mcp.types.Tool(name='query', inputSchema={'type': 'object'},
                                                   _meta={'recommendedWaitSeconds': 330})]
            tool = await client.get_tool('query')
            self.assertEqual(client.mcp_config.timeout, max(configured or 300, 330))
            self.assertEqual(tool._timeout, timedelta(seconds=330))
