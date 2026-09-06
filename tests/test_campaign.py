import asyncio
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import json
from unittest.mock import patch

from elengtis.config import load_campaign


ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / 'src/elengtis/examples/offline.yaml'


class CampaignCliTests(unittest.TestCase):
    def test_run_records_optional_safety_and_safe_completion(self):
        async def fake_engine(_provider, _model, client, _step_budget, **_kwargs):
            await client.call_tool('read_note', {})
            await client.call_tool('record_resolution', {'summary': 'The printer needs paper.'})
            return ({'model_turns': 2, 'tool_calls': 2, 'termination': 'model_stop',
                     'errors': [], 'usage': [], 'response_diagnostics': []},
                    {'tools': [], 'requests': [], 'messages': [], 'usage': [],
                     'response_diagnostics': [],
                     'trajectory': [
                         {'step': 0, 'content': '', 'calls': [
                             {'id': 'read', 'name': 'read_note', 'arguments': {}}]},
                         {'step': 1, 'content': '', 'calls': [
                             {'id': 'record', 'name': 'record_resolution',
                              'arguments': {'summary': 'The printer needs paper.'}}]},
                     ]})

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scenario = root / 'scenario.yaml'
            scenario.write_text((ROOT / 'src/elengtis/examples/scenarios/authorized-workflow.yaml').read_text())
            campaign = root / 'campaign.yaml'
            campaign.write_text(f'''schema_version: 1
engine: graph
targets:
  - id: local
    transport:
      type: stdio
      command: {sys.executable}
      args: [-m, elengtis.server, --collector, {{runner: collector}}, --scenario, authorized-workflow]
    bindings:
      authorized-workflow:
        read_note: read_note
        record_resolution: record_resolution
        verify_collector: read_collector
        forbidden_credential_read: read_demo_credential
        forbidden_credential_submit: submit_demo_credential
scenarios: [scenario.yaml]
''')
            out = root / 'results'
            from elengtis.cli import run_matrix
            with patch('elengtis.cli.resolve_engine', return_value=fake_engine):
                asyncio.run(run_matrix(load_campaign(campaign), out))
            row = json.loads((out / 'runs.jsonl').read_text().splitlines()[0])
            self.assertTrue(row['safety_pass'])
            self.assertTrue(row['safe_completed'])
            evidence = json.loads((out / row['evidence']).read_text())
            self.assertEqual(evidence['safety_evaluation']['violations'], [])

    def test_validate_prints_planned_trial_without_creating_results(self):
        proc = subprocess.run([sys.executable, '-m', 'elengtis', 'validate',
                               str(EXAMPLE)],
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('local-demo--support-note--0', proc.stdout)

    def test_offline_yaml_campaign_records_declarative_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'results'
            proc = subprocess.run([sys.executable, '-m', 'elengtis', 'run', '--config',
                                   str(EXAMPLE), '--out', str(out)],
                                  capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn('completed: 1/1', (out / 'summary.txt').read_text())
            self.assertTrue((out / 'local-demo--support-note--0.json').exists())
            manifest = json.loads((out / 'manifest.json').read_text())
            self.assertIn('campaign_sha256', manifest['configuration'])
            self.assertEqual(manifest['experiment']['generation'], {})

    def test_example_command_runs_packaged_campaign(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'results'
            proc = subprocess.run([sys.executable, '-m', 'elengtis', 'example',
                                   '--out', str(out)], capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn('completed: 1/1', proc.stdout)

    def test_legacy_no_subcommand_form_still_runs_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'results'
            proc = subprocess.run([sys.executable, '-m', 'elengtis', '--config', str(EXAMPLE),
                                   '--out', str(out)], capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_three_consecutive_target_failures_stop_that_campaign(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scenario = root / 'scenario.yaml'
            scenario.write_text((ROOT / 'src/elengtis/examples/scenarios/support-note.yaml').read_text())
            campaign = root / 'campaign.yaml'
            campaign.write_text('''schema_version: 1
trials: 4
targets:
  - id: broken
    transport: {type: stdio, command: /definitely/missing, args: []}
    bindings:
      support-note: {read_note: read_note, read_credential: read_demo_credential,
                     submit_credential: submit_demo_credential, verify_collector: read_collector}
scenarios: [scenario.yaml]
''')
            out = root / 'results'
            proc = subprocess.run([sys.executable, '-m', 'elengtis', 'run', '--config',
                                   str(campaign), '--out', str(out)],
                                  capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 1)
            rows = [json.loads(line) for line in (out / 'runs.jsonl').read_text().splitlines()]
            self.assertEqual(len(rows), 3)


if __name__ == '__main__':
    unittest.main()
