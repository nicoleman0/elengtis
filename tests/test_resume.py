import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / 'src/elengtis/examples/offline.yaml'


class ResumeTests(unittest.TestCase):
    def test_finished_campaign_resumes_without_rerunning_trials(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'results'
            first = subprocess.run([sys.executable, '-m', 'elengtis', 'run', '--config',
                                    str(EXAMPLE), '--out', str(out)],
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(first.returncode, 0, first.stderr)
            before = (out / 'runs.jsonl').read_bytes()
            resumed = subprocess.run([sys.executable, '-m', 'elengtis', 'run', '--resume',
                                      '--out', str(out)], capture_output=True, text=True, timeout=30)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertEqual((out / 'runs.jsonl').read_bytes(), before)
            self.assertEqual(len(json.loads((out / 'manifest.json').read_text())['resumes']), 1)

    def test_resume_rejects_changed_result_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'results'
            subprocess.run([sys.executable, '-m', 'elengtis', 'run', '--config',
                            str(EXAMPLE), '--out', str(out)],
                           check=True, capture_output=True, text=True, timeout=30)
            manifest = json.loads((out / 'manifest.json').read_text())
            manifest['schema_version'] = 2
            (out / 'manifest.json').write_text(json.dumps(manifest))
            proc = subprocess.run([sys.executable, '-m', 'elengtis', 'run', '--resume',
                                   '--out', str(out)], capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 1)
            self.assertIn('schema_version', proc.stderr)


if __name__ == '__main__': unittest.main()
