import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from elengtis.config import ContainerTransport, load_campaign
from elengtis.cli import build_parser
from elengtis.transports import docker_run_args, unmanaged_target_metadata


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

    def test_container_transport_rejects_root_identity(self):
        with self.assertRaises(ValueError):
            ContainerTransport(type='isolated_container', image='target:latest',
                               container_port=3000, uid=0, gid=10001)

    def test_remote_target_metadata_states_unmanaged_reset_boundary(self):
        metadata = unmanaged_target_metadata()
        self.assertEqual(metadata['isolation'], 'externally_managed')
        self.assertEqual(metadata['session'], 'fresh_client')
        self.assertFalse(metadata['reset_asserted'])

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
