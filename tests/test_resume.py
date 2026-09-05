"""Resume continues a matrix between trials without merging or double-counting."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from elengtis.cli import METRICS_VERSION, run_matrix

CONFIG = {'policies': ['comply', 'refuse', 'tool_error', 'budget'], 'trials': 1,
          'step_budget': 4, 'engine': 'reference', 'model': None}


def rows_of(root):
    return [json.loads(line) for line in (root / 'runs.jsonl').read_text().splitlines()]


def failing_on(trial_id, message='synthetic infrastructure failure'):
    """An engine that fails one named trial and defers the rest to the real one."""
    from elengtis.reference import run_episode  # pylint: disable=import-outside-toplevel

    async def engine(provider, model, client, collector, step_budget):
        if model.endswith(trial_id.rsplit('-', 1)[0]):
            raise RuntimeError(message)
        return await run_episode(provider, model, client, collector, step_budget)
    return engine


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    async def interrupted(self, root):
        """Run a matrix that fails on tool_error-0, leaving partial output."""
        with patch('elengtis.cli.resolve_engine', return_value=failing_on('tool_error-0')):
            with self.assertRaises(RuntimeError):
                await run_matrix(CONFIG, root)
        return rows_of(root)

    def resume(self, root):
        return subprocess.run([sys.executable, '-m', 'elengtis', '--resume', '--out', str(root)],
                              capture_output=True, text=True, timeout=90)

    async def test_interrupted_matrix_leaves_partial_output_and_no_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = await self.interrupted(root)
            self.assertEqual([row['trial_id'] for row in rows],
                             ['comply-0', 'refuse-0', 'tool_error-0'])
            self.assertEqual([row['evidence_status'] for row in rows],
                             ['complete', 'complete', 'incomplete'])
            self.assertFalse((root / 'summary.txt').exists())

    async def test_resume_skips_completed_trials_and_retries_the_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = await self.interrupted(root)
            kept = {row['trial_id']: (row['attempt_id'], (root / row['evidence']).read_bytes())
                    for row in before if row['evidence_status'] == 'complete'}
            failed_evidence = (root / 'tool_error-0.json').read_bytes()

            proc = self.resume(root)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            after = rows_of(root)

            # Completed trials were not re-run: same attempt, byte-identical evidence.
            for row in after[:len(kept)]:
                attempt_id, evidence = kept[row['trial_id']]
                self.assertEqual(row['attempt_id'], attempt_id)
                self.assertEqual((root / row['evidence']).read_bytes(), evidence)

            retried = [row for row in after if row['trial_id'] == 'tool_error-0']
            self.assertEqual(len(retried), 2)
            self.assertNotEqual(retried[0]['attempt_id'], retried[1]['attempt_id'])
            self.assertEqual(retried[1]['evidence'], 'tool_error-0.retry-1.json')
            self.assertTrue((root / 'tool_error-0.retry-1.json').exists())
            # The failed attempt's evidence survives its retry.
            self.assertEqual((root / 'tool_error-0.json').read_bytes(), failed_evidence)
            self.assertEqual([row['run_id'] for row in after].count(after[0]['run_id']), len(after))

    async def test_denominators_count_trials_not_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            await self.interrupted(root)
            proc = self.resume(root)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            summary = (root / 'summary.txt').read_text()
            self.assertIn('trials: 4', summary)
            self.assertIn('completed: 1/4', summary)
            self.assertIn('attempts: 5, of which 1 were retried', summary)
            self.assertEqual(len(rows_of(root)), 5)

    async def test_metrics_version_change_blocks_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            await self.interrupted(root)
            manifest = json.loads((root / 'manifest.json').read_text())
            manifest['metrics_version'] = METRICS_VERSION + 1
            (root / 'manifest.json').write_text(json.dumps(manifest))
            before = rows_of(root)

            proc = self.resume(root)
            self.assertEqual(proc.returncode, 2)
            self.assertIn('metrics_version', proc.stderr)
            self.assertEqual(rows_of(root), before)
            self.assertFalse((root / 'summary.txt').exists())

    async def test_resume_rejects_configuration_flags_and_missing_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            await self.interrupted(root)
            proc = subprocess.run(
                [sys.executable, '-m', 'elengtis', '--resume', '--out', str(root),
                 '--trials', '2'], capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 2)
            self.assertIn('--trials', proc.stderr)

            empty = root / 'nothing-here'
            empty.mkdir()
            proc = subprocess.run([sys.executable, '-m', 'elengtis', '--resume', '--out', str(empty)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 2)
            self.assertIn('resume', proc.stderr)

    async def test_resuming_a_finished_matrix_runs_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            await self.interrupted(root)
            self.assertEqual(self.resume(root).returncode, 0)
            rows = rows_of(root)
            self.assertEqual(self.resume(root).returncode, 0)
            self.assertEqual(rows_of(root), rows)
            self.assertIn('trials: 4', (root / 'summary.txt').read_text())


if __name__ == '__main__':
    unittest.main()
