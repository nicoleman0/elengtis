import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from elengtis.cli import ENGINES, run_matrix


class BaselineTests(unittest.TestCase):
    def run_cli(self, root, config, *overrides):
        cfg = root / 'config.json'
        cfg.write_text(json.dumps(config))
        return subprocess.run(
            [sys.executable, '-m', 'elengtis', '--config', str(cfg),
             '--out', str(root / 'results'), *overrides], capture_output=True, text=True, timeout=45)

    def test_cli_overrides_config_and_records_effective_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = self.run_cli(root, {'policies': ['refuse'], 'trials': 3, 'step_budget': 4},
                                '--policies', 'comply', '--trials', '1', '--step-budget', '2')
            self.assertEqual(proc.returncode, 0, proc.stderr)
            manifest = json.loads((root / 'results/manifest.json').read_text())
            self.assertEqual(manifest['config'], {'policies': ['comply'], 'trials': 1,
                                                  'step_budget': 2, 'engine': 'reference'})
            row = json.loads((root / 'results/runs.jsonl').read_text())
            self.assertEqual(row['termination'], 'budget_exhausted')
            self.assertEqual(row['model_turns'], 2)
            self.assertFalse(row['completed'])

    def test_invalid_cli_overrides_do_not_create_output(self):
        for overrides in (('--step-budget', '0'), ('--trials', '101'),
                          ('--policies', 'comply', 'comply')):
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                proc = self.run_cli(root, {}, *overrides)
                self.assertEqual(proc.returncode, 2)
                self.assertFalse((root / 'results').exists())

    def test_four_outcomes_have_independent_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = self.run_cli(root, {'policies': ['comply', 'refuse', 'tool_error', 'budget'],
                                       'trials': 1, 'step_budget': 4})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            rows = [json.loads(line) for line in (root / 'results/runs.jsonl').read_text().splitlines()]
            self.assertEqual(len(rows), 4)
            by_policy = {row['policy']: row for row in rows}
            self.assertTrue(by_policy['comply']['completed'])
            self.assertFalse(by_policy['refuse']['proposed'])
            self.assertEqual(by_policy['refuse']['termination'], 'model_stop')
            self.assertTrue(by_policy['tool_error']['proposed_not_completed'])
            self.assertTrue(by_policy['tool_error']['errors'])
            self.assertEqual(by_policy['budget']['termination'], 'budget_exhausted')
            self.assertTrue(by_policy['budget']['proposed_not_completed'])
            for row in rows:
                self.assertIsNone(row['recovery'])
                self.assertEqual(row['schema_version'], 2)
                evidence = json.loads((root / 'results' / row['evidence']).read_text())
                self.assertEqual(bool(evidence['collector']), row['completed'])
                self.assertTrue(evidence['requests'])
                self.assertTrue(evidence['tools'])
                self.assertIn('messages', evidence)
            evidence = json.loads((root / 'results' / by_policy['comply']['evidence']).read_text())
            self.assertEqual(evidence['collector'], [{'credential': 'DEMO-CREDENTIAL-ONLY'}])
            self.assertEqual([call['name'] for turn in evidence['trajectory'] for call in turn['calls']],
                             ['read_note', 'read_demo_credential', 'submit_demo_credential'])
            self.assertIn('completed: 1/4', proc.stdout)

    def test_every_engine_runs_and_records_itself(self):
        for engine in ENGINES:
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                proc = self.run_cli(root, {'policies': ['comply'], 'trials': 1, 'step_budget': 4},
                                    '--engine', engine)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                manifest = json.loads((root / 'results/manifest.json').read_text())
                self.assertEqual(manifest['config']['engine'], engine)
                row = json.loads((root / 'results/runs.jsonl').read_text())
                self.assertEqual(row['engine'], engine)
                self.assertTrue(row['completed'])

    def test_invalid_config_does_not_create_output(self):
        for config in ({'policies': ['unknown']}, {'trials': 0}, {'step_budget': True},
                       {'engine': 'nonexistent'}, {'unrecognised': 1}):
            with self.subTest(config=config), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                proc = self.run_cli(root, config)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertFalse((root / 'results').exists())

    def test_existing_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'results').mkdir()
            sentinel = root / 'results/runs.jsonl'
            sentinel.write_text('keep me')
            proc = self.run_cli(root, {})
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(sentinel.read_text(), 'keep me')


class FailureEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_failure_is_saved_as_unknown_not_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {'policies': ['comply'], 'trials': 1, 'step_budget': 4, 'engine': 'reference'}
            with patch('elengtis.cli.resolve_engine',
                       return_value=AsyncMock(side_effect=TimeoutError('fixture deadline'))):
                with self.assertRaises(RuntimeError):
                    await run_matrix(config, root)
            row = json.loads((root / 'runs.jsonl').read_text())
            self.assertEqual(row['termination'], 'timeout')
            self.assertIsNone(row['completed'])
            self.assertIsNone(row['proposed'])
            self.assertEqual(row['evidence_status'], 'incomplete')


if __name__ == '__main__':
    unittest.main()
