from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import json


ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / 'src/elengtis/examples/offline.yaml'


class CampaignCliTests(unittest.TestCase):
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
