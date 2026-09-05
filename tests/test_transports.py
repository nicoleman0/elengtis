from pathlib import Path
import asyncio
import socket
import subprocess
import sys
import tempfile
import unittest

from elengtis.config import HttpTransport, StdioTransport, Target
from elengtis.transports import agent_tools, open_target


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_opens_initialized_session_and_filters_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Target(id='local', transport=StdioTransport(
                type='stdio', command=sys.executable,
                args=['-m', 'elengtis.server', '--collector', str(Path(tmp) / 'out.jsonl')]),
                bindings={'demo': {}})
            async with open_target(target, {}) as client:
                tools = await agent_tools(client, ['read_note'])
            self.assertEqual([tool.name for tool in tools], ['read_note'])

    async def test_missing_allowlisted_tool_fails_before_model_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Target(id='local', transport=StdioTransport(
                type='stdio', command=sys.executable,
                args=['-m', 'elengtis.server', '--collector', str(Path(tmp) / 'out.jsonl')]),
                bindings={'demo': {}})
            async with open_target(target, {}) as client:
                with self.assertRaisesRegex(ValueError, 'missing'):
                    await agent_tools(client, ['missing'])

    async def test_streamable_http_opens_initialized_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            process = subprocess.Popen([
                sys.executable, '-m', 'elengtis.server', '--collector',
                str(Path(tmp) / 'out.jsonl'), '--transport', 'streamable-http',
                '--port', str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(50):
                    try:
                        _, writer = await asyncio.open_connection('127.0.0.1', port)
                        writer.close()
                        await writer.wait_closed()
                        break
                    except OSError:
                        await asyncio.sleep(.02)
                target = Target(id='remote', transport=HttpTransport(
                    type='streamable_http', url=f'http://127.0.0.1:{port}/mcp'),
                    bindings={'demo': {}})
                async with open_target(target, {}) as client:
                    self.assertIn('read_note', [tool.name for tool in await agent_tools(client, 'all')])
            finally:
                process.terminate()
                process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
