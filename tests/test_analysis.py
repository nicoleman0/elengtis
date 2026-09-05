import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from elengtis.analysis import summarize


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
            self.assertIn('| reference | support-note |', (out / 'summary.md').read_text())

    def test_generator_creates_valid_randomized_cell_campaigns(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'generated'
            subprocess.run([sys.executable, str(ROOT / 'experiments/live-comparison/generate_live_comparison.py'),
                            '--model', 'openai/gpt-5-mini', '--blocks', '1', '--out', str(out)],
                           check=True, capture_output=True, text=True, timeout=20)
            configs = sorted(out.glob('*.yaml'))
            self.assertEqual(len(configs), 12)
            checked = subprocess.run([sys.executable, '-m', 'elengtis', 'validate', str(configs[0])],
                                     capture_output=True, text=True, timeout=20)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertEqual(len(json.loads((out / 'run-order.json').read_text())['runs']), 12)


if __name__ == '__main__':
    unittest.main()
