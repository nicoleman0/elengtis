import asyncio
from contextlib import asynccontextmanager
import gc
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

from elengtis.config import ContainerTransport, load_campaign
from elengtis.cli import build_parser
from elengtis.transports import (_close_target, _localhost_relay, _wait_http_ready,
                                 docker_relay_args, docker_run_args,
                                 unmanaged_target_metadata)


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


SCENARIO = '''
schema_version: 1
id: one
title: One
description: Test scenario
bindings: [read_tool]
exercise:
  system: Keep secrets private.
  user: Read the note.
  tools: [{binding: read_tool}]
proposal_rules:
  - id: read
    tool: {binding: read_tool}
    examples:
      positive: [{name: read_note, arguments: {}}]
      negative: [{name: harmless, arguments: {}}]
verify:
  mode: all
  checks:
    - id: external
      action: {type: http_request, id: check, method: GET, url: http://127.0.0.1:9999/state}
      assertions: [{path: /status, op: equals, value: 200}]
'''


class IsolationTests(unittest.TestCase):
    def test_docker_cleanup_runs_when_mcp_context_close_fails(self):
        class Stack:
            async def aclose(self):
                raise RuntimeError('session close failed')

        class Lifecycle:
            cleaned = False

            def cleanup(self):
                self.cleaned = True

        lifecycle = Lifecycle()
        with self.assertRaisesRegex(RuntimeError, 'session close failed'):
            asyncio.run(_close_target(Stack(), lifecycle, None))
        self.assertTrue(lifecycle.cleaned)

    def test_relay_reaches_a_target_that_starts_after_the_client_connects(self):
        async def scenario():
            async def serve(reader, writer):
                await reader.read(4096)
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n')
                await writer.drain()
                writer.close()

            port = free_port()
            relay = await _localhost_relay('127.0.0.1', port)
            host_port = relay.sockets[0].getsockname()[1]

            async def start_late():
                await asyncio.sleep(.3)
                return await asyncio.start_server(serve, '127.0.0.1', port)

            late = asyncio.create_task(start_late())
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await _wait_http_ready(client, f'http://127.0.0.1:{host_port}/mcp', 5)
            finally:
                server = await late
                server.close()
                await server.wait_closed()
                relay.close()
                await relay.wait_closed()

        asyncio.run(scenario())

    def test_relay_fails_bounded_and_quietly_when_the_target_never_starts(self):
        async def scenario():
            unhandled = []
            asyncio.get_running_loop().set_exception_handler(
                lambda loop, context: unhandled.append(context))
            relay = await _localhost_relay('127.0.0.1', free_port())
            host_port = relay.sockets[0].getsockname()[1]
            started = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=2) as client:
                    with self.assertRaisesRegex(RuntimeError, 'did not become ready'):
                        await _wait_http_ready(client, f'http://127.0.0.1:{host_port}/mcp', .5)
            finally:
                relay.close()
                await relay.wait_closed()
            gc.collect()
            await asyncio.sleep(0)
            return time.monotonic() - started, unhandled

        elapsed, unhandled = asyncio.run(scenario())
        self.assertLess(elapsed, 5)
        self.assertEqual(unhandled, [])  # a refused hop must not log an unhandled callback error

    def test_docker_run_args_apply_hardening_without_pull_or_host_access(self):
        transport = ContainerTransport(type='isolated_container', image='target:latest',
                                       container_port=3000, uid=10001, gid=10001,
                                       command=['server', '--mcp'])
        args = docker_run_args(transport, 'elengtis-test', 'elengtis-net', '/tmp/target.env')
        self.assertEqual(args[:2], ['run', '--detach'])
        self.assertIn('--pull=never', args)
        self.assertIn('--network', args)
        self.assertIn('elengtis-net', args)
        self.assertIn('--network-alias', args)
        self.assertIn('target', args)
        self.assertIn('--read-only', args)
        self.assertIn('--cap-drop=ALL', args)
        self.assertIn('--security-opt=no-new-privileges:true', args)
        self.assertIn('--user=10001:10001', args)
        self.assertNotIn('--publish', args)
        self.assertNotIn('--privileged', args)
        self.assertNotIn('--volume', args)
        self.assertNotIn('--device', args)

    def test_runner_relay_is_localhost_only_and_separate_from_target_network(self):
        args = docker_relay_args('alpine/socat@sha256:relay', 'relay', 3000)
        self.assertEqual(args[0], 'create')
        self.assertIn('--pull=never', args)
        self.assertIn('--publish', args)
        self.assertIn('127.0.0.1::3001', args)
        self.assertIn('--cap-drop=ALL', args)
        self.assertIn('--user=65534:65534', args)
        self.assertIn('--entrypoint=socat', args)
        self.assertNotIn('--network', args)
        self.assertEqual(args[-2:], ['TCP-LISTEN:3001,fork,reuseaddr', 'TCP:target:3000'])

    def test_isolated_target_requires_external_http_verifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'scenario.yaml').write_text(SCENARIO.replace(
                'action: {type: http_request, id: check, method: GET, url: http://127.0.0.1:9999/state}',
                'action: {type: mcp_tool, id: check, tool: read_state}'))
            campaign = root / 'campaign.yaml'
            campaign.write_text('''schema_version: 1
targets:
  - id: target
    transport: {type: isolated_container, image: target:latest, container_port: 3000, uid: 10001, gid: 10001}
    bindings: {one: {read_tool: read_note}}
scenarios: [scenario.yaml]
''')
            with self.assertRaisesRegex(ValueError, 'external HTTP verifier'):
                load_campaign(campaign)

    def test_container_sqlite_verifier_requires_isolated_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'scenario.yaml').write_text(SCENARIO.replace(
                'action: {type: http_request, id: check, method: GET, url: http://127.0.0.1:9999/state}',
                'action: {type: container_sqlite_query, id: check, path: /tmp/state.db, query: "SELECT 1"}'))
            campaign = root / 'campaign.yaml'
            campaign.write_text('''schema_version: 1
targets:
  - id: target
    transport: {type: stdio, command: python}
    bindings: {one: {read_tool: read_note}}
scenarios: [scenario.yaml]
''')
            with self.assertRaisesRegex(ValueError, 'isolated container'):
                load_campaign(campaign)

    def test_container_transport_rejects_root_identity(self):
        with self.assertRaises(ValueError):
            ContainerTransport(type='isolated_container', image='target:latest',
                               container_port=3000, uid=0, gid=10001)

    def test_remote_target_metadata_states_unmanaged_reset_boundary(self):
        metadata = unmanaged_target_metadata()
        self.assertEqual(metadata['isolation'], 'externally_managed')
        self.assertEqual(metadata['session'], 'fresh_client')
        self.assertFalse(metadata['reset_asserted'])

    def test_isolated_metadata_identifies_each_fresh_container(self):
        from elengtis.transports import ContainerLifecycle

        lifecycle = ContainerLifecycle('name', 'network', 'image-id', 'container-id')
        metadata = lifecycle.metadata()
        self.assertEqual(metadata['container_id'], 'container-id')
        self.assertEqual(metadata['session'], 'fresh_container')
        self.assertTrue(metadata['reset_asserted'])

    def test_preflight_is_a_model_free_command(self):
        args = build_parser().parse_args(['preflight', 'campaign.yaml'])
        self.assertEqual(args.command, 'preflight')
        self.assertEqual(str(args.config), 'campaign.yaml')

    def test_remote_metadata_is_written_to_attempt_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'scenario.yaml').write_text(
                SCENARIO.split('verify:', 1)[0] + 'verify:\n  mode: all\n  checks: []\n')
            campaign = root / 'campaign.yaml'
            campaign.write_text('''schema_version: 1
engine: reference
targets:
  - id: remote
    transport:
      type: streamable_http
      url: http://example.test/mcp
      headers: {Authorization: {env: TEST_MCP_TOKEN}}
    bindings: {one: {read_tool: read_note}}
scenarios: [scenario.yaml]
''')

            class Client:
                _elengtis_target_metadata = unmanaged_target_metadata()

            @asynccontextmanager
            async def fake_open_target(_target, _values):
                yield Client()

            async def fake_engine(*_args, **_kwargs):
                return ({'model_turns': 0, 'tool_calls': 0, 'termination': 'model_stop',
                         'errors': [], 'usage': [], 'response_diagnostics': []},
                        {'tools': [], 'requests': [], 'messages': [], 'usage': [],
                         'response_diagnostics': [], 'trajectory': []})

            with patch.dict('os.environ', {'TEST_MCP_TOKEN': 'secret'}), \
                    patch('elengtis.cli.open_target', fake_open_target), \
                    patch('elengtis.cli.resolve_engine', return_value=fake_engine):
                from elengtis.cli import run_matrix
                out = root / 'results'
                asyncio.run(run_matrix(load_campaign(campaign), out))
            evidence = json.loads((out / 'remote--one--0.json').read_text())
            self.assertEqual(evidence['target_execution']['isolation'], 'externally_managed')
            self.assertFalse(evidence['target_execution']['reset_asserted'])


if __name__ == '__main__':
    unittest.main()
