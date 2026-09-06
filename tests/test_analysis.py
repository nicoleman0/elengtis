import json
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from elengtis.analysis import pairwise, recommend_engine, summarize


ROOT = Path(__file__).parent.parent
PILOT_SPEC = importlib.util.spec_from_file_location(
    'run_pilot', ROOT / 'experiments/live-comparison/run_pilot.py')
PILOT = importlib.util.module_from_spec(PILOT_SPEC)
PILOT_SPEC.loader.exec_module(PILOT)


def write_run(root, rows, block=0):
    root.mkdir()
    (root / 'manifest.json').write_text(json.dumps({'experiment': {'block': block, 'order': 0}}))
    (root / 'runs.jsonl').write_text('\n'.join(json.dumps(row) for row in rows) + '\n')


class AnalysisTests(unittest.TestCase):
    def test_smoke_reports_invalid_tool_call_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, cell = Path(tmp), Path(tmp) / 'cell'
            cell.mkdir()
            (cell / 'runs.jsonl').write_text(json.dumps({
                'evidence_status': 'complete', 'evidence': 'evidence.json'}) + '\n')
            (cell / 'evidence.json').write_text(json.dumps({
                'trajectory': [{'calls': []}],
                'response_diagnostics': [{'finish_reason': 'stop',
                                          'native_finish_reason': 'length',
                                          'invalid_tool_calls': [{'error': 'invalid JSON'}]}]}))
            with self.assertRaisesRegex(ValueError, "finish_reason='stop'.*invalid_tool_calls=1"):
                PILOT.check_smoke(root, [{'result': 'cell'}])

    def test_summary_uses_final_attempt_and_keeps_incomplete_trials(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / 'run'
            complete = {'trial_id': 'one', 'engine': 'graph', 'scenario': 'support-note',
                        'evidence_status': 'complete', 'proposed': True, 'completed': False,
                        'proposed_not_completed': True, 'termination': 'model_stop',
                        'diagnostics': {'first_finish_reason': 'stop', 'invalid_tool_calls': 1}}
            incomplete = {'trial_id': 'two', 'engine': 'graph', 'scenario': 'support-note',
                          'evidence_status': 'incomplete', 'proposed': None, 'completed': None,
                          'proposed_not_completed': None, 'termination': 'infrastructure_error'}
            write_run(run, [complete, incomplete])
            row = summarize([run])[0]
            self.assertEqual((row['trials'], row['complete_trials'], row['incomplete_trials']), (2, 1, 1))
            self.assertEqual(row['completed'], {'successes': 0, 'known': 1, 'unknown': 1,
                                                'wilson_95': row['completed']['wilson_95']})
            self.assertEqual(row['tool_call_diagnostics'], {
                'invalid_tool_calls': 1, 'first_finish_reasons': {'stop': 1}})

    def test_summary_reports_safety_and_safe_completion_without_implying_legacy_safety(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'run'
            write_run(run, [
                {'trial_id': 'safe', 'engine': 'graph', 'scenario': 'injection-resistance',
                 'evidence_status': 'complete', 'proposed': False, 'completed': True,
                 'safety_pass': True, 'safe_completed': True,
                 'proposed_not_completed': False, 'termination': 'model_stop'},
                {'trial_id': 'legacy', 'engine': 'graph', 'scenario': 'support-note',
                 'evidence_status': 'complete', 'proposed': False, 'completed': False,
                 'proposed_not_completed': False, 'termination': 'model_stop'},
            ])
            rows = {item['scenario']: item for item in summarize([run])}
            self.assertEqual(rows['injection-resistance']['safe_completed']['successes'], 1)
            self.assertEqual(rows['injection-resistance']['safety_pass']['known'], 1)
            self.assertEqual(rows['support-note']['safe_completed']['known'], 0)

    def test_pairwise_comparison_includes_safe_completion(self):
        rows = [
            {'model_id': 'one', 'scenario': 'authorized-workflow', 'engine': 'graph', 'block': 0,
             'final_complete': True, 'proposed': False, 'completed': True,
             'safety_pass': True, 'safe_completed': True, 'proposed_not_completed': False},
            {'model_id': 'one', 'scenario': 'authorized-workflow', 'engine': 'create_agent', 'block': 0,
             'final_complete': True, 'proposed': False, 'completed': False,
             'safety_pass': True, 'safe_completed': False, 'proposed_not_completed': False},
        ]
        comparisons = pairwise(rows, samples=20)
        safe = next(item for item in comparisons if item['metric'] == 'safe_completed')
        self.assertEqual(safe['difference_pp'], 100.0)

    def test_summary_reports_recovery_and_tool_ordering(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / 'run'
            write_run(run, [{
                'trial_id': 'recovered', 'model_id': 'one', 'engine': 'graph',
                'scenario': 'recoverable-tool-error', 'evidence_status': 'complete',
                'proposed': True, 'completed': True, 'safety_pass': None,
                'safe_completed': None, 'proposed_not_completed': False,
                'tool_sequence': ['read_note', 'read_primary_diagnostic',
                                  'read_fallback_diagnostic', 'record_resolution'],
                'tool_error_count': 1, 'recovery_succeeded': True,
                'termination': 'model_stop'}])
            row = summarize([run])[0]
            self.assertEqual(row['recovery_succeeded']['successes'], 1)
            self.assertEqual(row['tool_sequences'], {
                'read_note -> read_primary_diagnostic -> read_fallback_diagnostic -> record_resolution': 1})

    def test_recommendation_requires_conformance_and_multiple_families(self):
        rows = []
        for scenario in ('authorized-workflow', 'injection-resistance',
                          'recoverable-tool-error', 'stateful-branch'):
            for engine, safe in (('graph', True), ('create_agent', False if scenario != 'stateful-branch' else True)):
                rows.append({'model_id': 'one', 'scenario': scenario, 'engine': engine,
                             'final_complete': True, 'safe_completed': safe,
                             'safety_pass': True})
        self.assertEqual(recommend_engine(rows, conformance_passed=False)['status'],
                         'conformance_required')
        decision = recommend_engine(rows, conformance_passed=True)
        self.assertEqual(decision['status'], 'graph')
        self.assertGreaterEqual(decision['winning_families'], 2)

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
    pricing: {input_per_million: 0.02, output_per_million: 0.10}
    generation: {temperature: 0, route: fallback}
  - id: two
    model: anthropic/claude-test
    pricing: {input_per_million: 0.05, output_per_million: 0.16}
    generation: {temperature: 0}
''')
            subprocess.run([sys.executable, str(ROOT / 'experiments/live-comparison/generate_live_comparison.py'),
                            '--models', str(models), '--blocks', '1', '--budget-usd', '0.15', '--out', str(out)],
                           check=True, capture_output=True, text=True, timeout=20)
            configs = sorted((out / 'campaigns').glob('*.yaml'))
            self.assertEqual(len(configs), 24)
            tool_error = next(path for path in configs if 'tool-error' in path.name)
            self.assertIn('--scenario, tool-error', tool_error.read_text())
            self.assertIn('Call read_note first', (out / 'scenarios' / 'tool-error.yaml').read_text())
            checked = subprocess.run([sys.executable, '-m', 'elengtis', 'validate', str(configs[0])],
                                     capture_output=True, text=True, timeout=20)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            plan = json.loads((out / 'run-order.json').read_text())
            self.assertEqual((len(plan['runs']), plan['max_model_calls']), (24, 96))
            self.assertEqual(plan['schema_version'], 3)
            self.assertEqual(plan['engines'], ['reference', 'graph', 'langchain', 'create_agent'])
            env = dict(__import__('os').environ, OPENROUTER_API_KEY='test-key')
            preflight = subprocess.run([sys.executable, str(ROOT / 'experiments/live-comparison/run_pilot.py'),
                                        '--plan', str(out / 'run-order.json'), '--results', str(root / 'results'),
                                        '--max-model-calls', '96', '--budget-usd', '0.15', '--dry-run'], env=env,
                                       capture_output=True, text=True, timeout=20)
            self.assertEqual(preflight.returncode, 0, preflight.stderr)
            self.assertIn('24 cells, at most 96 model calls', preflight.stdout)
            capped = subprocess.run([sys.executable, str(ROOT / 'experiments/live-comparison/run_pilot.py'),
                                     '--plan', str(out / 'run-order.json'), '--results', str(root / 'results'),
                                     '--max-model-calls', '95', '--budget-usd', '0.15', '--dry-run'], env=env,
                                    capture_output=True, text=True, timeout=20)
            self.assertNotEqual(capped.returncode, 0)
            self.assertIn('above the 95 ceiling', capped.stderr)
            frozen = json.loads((out / 'run-order.json').read_text())
            frozen['models'][0]['generation']['max_retries'] = 1
            (out / 'run-order.json').write_text(json.dumps(frozen))
            retries = subprocess.run([sys.executable, str(ROOT / 'experiments/live-comparison/run_pilot.py'),
                                      '--plan', str(out / 'run-order.json'), '--results', str(root / 'retry-results'),
                                      '--max-model-calls', '96', '--budget-usd', '0.15', '--dry-run'], env=env,
                                     capture_output=True, text=True, timeout=20)
            self.assertNotEqual(retries.returncode, 0)
            self.assertIn('max_retries', retries.stderr)

            focused = root / 'focused'
            subprocess.run([sys.executable, str(ROOT / 'experiments/live-comparison/generate_live_comparison.py'),
                            '--models', str(models), '--engines', 'graph', 'create_agent',
                            '--experiment-id', 'live-framework-comparison-v1', '--blocks', '1',
                            '--budget-usd', '0.15', '--out', str(focused)],
                           check=True, capture_output=True, text=True, timeout=20)
            focused_plan = json.loads((focused / 'run-order.json').read_text())
            self.assertEqual(focused_plan['engines'], ['graph', 'create_agent'])
            self.assertEqual((len(focused_plan['runs']), focused_plan['max_model_calls']), (12, 48))
            focused_configs = sorted((focused / 'campaigns').glob('*.yaml'))
            self.assertEqual({path.read_text().split('engine: ', 1)[1].splitlines()[0]
                              for path in focused_configs}, {'graph', 'create_agent'})
            focused_preflight = subprocess.run(
                [sys.executable, str(ROOT / 'experiments/live-comparison/run_pilot.py'),
                 '--plan', str(focused / 'run-order.json'), '--results', str(root / 'focused-results'),
                 '--max-model-calls', '48', '--budget-usd', '0.15', '--dry-run'], env=env,
                capture_output=True, text=True, timeout=20)
            self.assertEqual(focused_preflight.returncode, 0, focused_preflight.stderr)
            self.assertIn('12 cells, at most 48 model calls', focused_preflight.stdout)

            focused_v2 = root / 'focused-v2'
            subprocess.run([sys.executable, str(ROOT / 'experiments/live-comparison/generate_live_comparison.py'),
                            '--models', str(models), '--engines', 'graph', 'create_agent',
                            '--scenarios', 'authorized-workflow', 'injection-resistance',
                            'recoverable-tool-error', 'stateful-branch', '--blocks', '1',
                            '--step-budget', '6', '--budget-usd', '0.15', '--out', str(focused_v2)],
                           check=True, capture_output=True, text=True, timeout=20)
            focused_v2_plan = json.loads((focused_v2 / 'run-order.json').read_text())
            self.assertEqual((len(focused_v2_plan['runs']), focused_v2_plan['max_model_calls']), (16, 96))
            self.assertEqual(focused_v2_plan['engines'], ['graph', 'create_agent'])
            self.assertEqual({run['scenario'] for run in focused_v2_plan['runs']}, {
                'authorized-workflow', 'injection-resistance', 'recoverable-tool-error', 'stateful-branch'})

            duplicate_engines = subprocess.run(
                [sys.executable, str(ROOT / 'experiments/live-comparison/generate_live_comparison.py'),
                 '--models', str(models), '--engines', 'graph', 'graph', '--blocks', '1',
                 '--budget-usd', '0.15', '--out', str(root / 'duplicate')],
                capture_output=True, text=True, timeout=20)
            self.assertNotEqual(duplicate_engines.returncode, 0)
            self.assertIn('engines must be unique', duplicate_engines.stderr)

            legacy = dict(plan)
            legacy['schema_version'] = 2
            legacy.pop('engines')
            legacy.pop('experiment_id')
            (out / 'legacy-run-order.json').write_text(json.dumps(legacy))
            legacy_preflight = subprocess.run(
                [sys.executable, str(ROOT / 'experiments/live-comparison/run_pilot.py'),
                 '--plan', str(out / 'legacy-run-order.json'), '--results', str(root / 'legacy-results'),
                 '--max-model-calls', '96', '--budget-usd', '0.15', '--dry-run'], env=env,
                capture_output=True, text=True, timeout=20)
            self.assertEqual(legacy_preflight.returncode, 0, legacy_preflight.stderr)

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
