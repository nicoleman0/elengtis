import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from elengtis.budget import BudgetExceeded, PilotBudget
from elengtis.cli import load_resume, run_matrix
from elengtis.config import load_campaign


ROOT = Path(__file__).parent.parent


class BudgetTests(unittest.TestCase):
    def test_reservation_is_conservative_and_enforces_cap(self):
        budget = PilotBudget(0.001, {'test/model': {
            'input_per_million': 1.0, 'output_per_million': 1.0}})
        reservation = budget.reserve('test/model', [{'content': 'x' * 500}], [], 100)
        self.assertGreater(reservation, 0)
        with self.assertRaises(BudgetExceeded):
            budget.reserve('test/model', [{'content': 'x' * 500}], [], 100)

    def test_known_usage_reconciles_against_reservation(self):
        budget = PilotBudget(1.0, {'test/model': {
            'input_per_million': 1.0, 'output_per_million': 1.0}})
        reservation = budget.reserve('test/model', [], [], 10)
        self.assertEqual(budget.settle(reservation, {
            'input_tokens': 2, 'output_tokens': 3, 'cost_usd': 0.000005}), 0.000005)
        self.assertEqual(budget.snapshot()['unknown_costs'], 0)

    def test_provider_error_is_incomplete_and_resume_retries_it(self):
        calls = []

        async def fake_engine(*_args, **_kwargs):
            calls.append(True)
            if len(calls) == 1:
                return ({'termination': 'provider_error', 'model_turns': 1, 'tool_calls': 0,
                         'errors': [{'kind': 'provider_error', 'detail': 'synthetic failure'}],
                         'usage': []},
                        {'tools': [], 'requests': [], 'messages': [], 'trajectory': [], 'usage': []})
            return ({'termination': 'model_stop', 'model_turns': 1, 'tool_calls': 0,
                     'errors': [], 'usage': []},
                    {'tools': [], 'requests': [], 'messages': [], 'trajectory': [], 'usage': []})

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'results'
            bundle = load_campaign(ROOT / 'src/elengtis/examples/offline.yaml')
            with patch('elengtis.cli.resolve_engine', return_value=fake_engine):
                with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                    asyncio.run(run_matrix(bundle, out))
                rows = [json.loads(line) for line in (out / 'runs.jsonl').read_text().splitlines()]
                self.assertEqual(rows[0]['evidence_status'], 'incomplete')
                self.assertEqual(rows[0]['failure_class'], 'provider_error')
                self.assertEqual(rows[0]['errors'][0]['detail'], 'synthetic failure')
                evidence = json.loads((out / rows[0]['evidence']).read_text())
                self.assertEqual(evidence['errors'][0]['detail'], 'synthetic failure')
                resumed_bundle, run_id, prior = load_resume(out)
                asyncio.run(run_matrix(resumed_bundle, out, run_id, prior))
                rows = [json.loads(line) for line in (out / 'runs.jsonl').read_text().splitlines()]
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[-1]['evidence_status'], 'complete')


if __name__ == '__main__':
    unittest.main()
