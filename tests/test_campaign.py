from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).parent.parent


class CampaignCliTests(unittest.TestCase):
    def test_validate_prints_planned_trial_without_creating_results(self):
        proc = subprocess.run([sys.executable, '-m', 'elengtis', 'validate',
                               str(ROOT / 'examples/offline.yaml')],
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('local-demo--support-note--0', proc.stdout)

    def test_offline_yaml_campaign_records_declarative_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'results'
            proc = subprocess.run([sys.executable, '-m', 'elengtis', 'run', '--config',
                                   str(ROOT / 'examples/offline.yaml'), '--out', str(out)],
                                  capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn('completed: 1/1', (out / 'summary.txt').read_text())
            self.assertTrue((out / 'local-demo--support-note--0.json').exists())


if __name__ == '__main__':
    unittest.main()
