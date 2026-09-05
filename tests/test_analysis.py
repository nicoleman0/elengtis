import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from elengtis.analysis import pairwise, summarize


ROOT = Path(__file__).parent.parent


def write_run(root, rows, block=0):
    root.mkdir()
    (root / 'manifest.json').write_text(json.dumps({'experiment': {'block': block, 'order': 0}}))
    (root / 'runs.jsonl').write_text('\n'.join(json.dumps(row) for row in rows) + '\n')


class AnalysisTests(unittest.TestCase):
    def test_summary_uses_final_attempt_and_keeps_incomplete_trials(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / 'run'
            complete = {'trial_id': 'one', 'engine': 'graph', 'scenario': 'support-note',
                        'evidence_status': 'complete', 'proposed': True, 'completed': False,
                        'proposed_not_completed': True, 'termination': 'model_stop'}
            incomplete = {'trial_id': 'two', 'engine': 'graph', 'scenario': 'support-note',
                          'evidence_status': 'incomplete', 'proposed': None, 'completed': None,
                          'proposed_not_completed': None, 'termination': 'infrastructure_error'}
            write_run(run, [complete, incomplete])
            row = summarize([run])[0]
            self.assertEqual((row['trials'], row['complete_trials'], row['incomplete_trials']), (2, 1, 1))
            self.assertEqual(row['completed'], {'successes': 0, 'known': 1, 'unknown': 1,
                                                'wilson_95': row['completed']['wilson_95']})

    def test_cli_writes_machine_and_human_readable_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); run, out = root / 'run', root / 'report'
            write_run(run, [{'trial_id': 'one', 'engine': 'reference', 'scenario': 'support-note',
                             'evidence_status': 'complete', 'proposed': False, 'completed': False,
                             'proposed_not_completed': False, 'termination': 'model_stop'}])
            proc = subprocess.run([sys.executable, '-m', 'elengtis', 'analyze', '--input', str(run),
                                   '--out', str(out)], capture_output=True, text=True, timeout=20)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((out / 'summary.json').exists())
            self.assertIn('| None | reference | support-note |', (out / 'summary.md').read_text())

    def test_generator_creates_portable_multi_model_campaigns_and_preflights_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp), Path(tmp) / 'pilot'
            models = root / 'models.yaml'
            models.write_text('''models:
  - id: one
    model: openai/gpt-5-mini
    generation: {temperature: 0, route: fallback}
  - id: two
    model: anthropic/claude-test
    generation: {temperature: 0}
''')
            subprocess.run([sys.executable, str(ROOT / 'experiments/live-comparison/generate_live_comparison.py'),
                            '--models', str(models), '--blocks', '1', '--out', str(out)],
                           check=True, capture_output=True, text=True, timeout=20)
            configs = sorted((out / 'campaigns').glob('*.yaml'))
            self.assertEqual(len(configs), 24)
            checked = subprocess.run([sys.executable, '-m', 'elengtis', 'validate', str(configs[0])],
                                     capture_output=True, text=True, timeout=20)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            plan = json.loads((out / 'run-order.json').read_text())
            self.assertEqual((len(plan['runs']), plan['max_model_calls']), (24, 96))
            env = dict(__import__('os').environ, OPENROUTER_API_KEY='test-key')
            preflight = subprocess.run([sys.executable, str(ROOT / 'experiments/live-comparison/run_pilot.py'),
                                        '--plan', str(out / 'run-order.json'), '--results', str(root / 'results'),
                                        '--max-model-calls', '96', '--dry-run'], env=env,
                                       capture_output=True, text=True, timeout=20)
            self.assertEqual(preflight.returncode, 0, preflight.stderr)
            self.assertIn('24 cells, at most 96 model calls', preflight.stdout)
            capped = subprocess.run([sys.executable, str(ROOT / 'experiments/live-comparison/run_pilot.py'),
                                     '--plan', str(out / 'run-order.json'), '--results', str(root / 'results'),
                                     '--max-model-calls', '95', '--dry-run'], env=env,
                                    capture_output=True, text=True, timeout=20)
            self.assertNotEqual(capped.returncode, 0)
            self.assertIn('above the 95 ceiling', capped.stderr)

    def test_pairwise_comparison_stays_within_model_and_scenario(self):
        rows = [
            {'model_id': 'one', 'scenario': 'support-note', 'engine': 'graph', 'block': 0,
             'final_complete': True, 'proposed': False, 'completed': False, 'proposed_not_completed': False},
            {'model_id': 'one', 'scenario': 'support-note', 'engine': 'reference', 'block': 0,
             'final_complete': True, 'proposed': True, 'completed': True, 'proposed_not_completed': False},
            {'model_id': 'two', 'scenario': 'support-note', 'engine': 'graph', 'block': 0,
             'final_complete': True, 'proposed': True, 'completed': True, 'proposed_not_completed': False},
        ]
        comparisons = pairwise(rows, samples=20)
        completed = next(item for item in comparisons if item['metric'] == 'completed')
        self.assertEqual((completed['model_id'], completed['blocks'], completed['difference_pp']),
                         ('one', 1, 100.0))


if __name__ == '__main__':
    unittest.main()
