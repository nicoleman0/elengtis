#!/usr/bin/env python3
"""Preflight and run a frozen live-comparison plan in its recorded order."""
import argparse
import asyncio
import json
import os
from pathlib import Path

from elengtis.budget import BudgetExceeded, PilotBudget
from elengtis.cli import load_resume, run_matrix
from elengtis.config import load_campaign

ENGINES = ('reference', 'graph', 'langchain', 'create_agent')
LEGACY_PLAN_SCHEMA = 2
PLAN_SCHEMA = 3


def preflight(plan_path, max_model_calls):
    plan = json.loads(plan_path.read_text())
    if plan.get('schema_version') not in {LEGACY_PLAN_SCHEMA, PLAN_SCHEMA} or not isinstance(plan.get('runs'), list):
        raise ValueError('invalid live-comparison plan')
    engines = tuple(plan.get('engines', ENGINES))
    if (not engines or len(engines) != len(set(engines)) or
            any(engine not in ENGINES for engine in engines)):
        raise ValueError('plan must declare a unique set of supported engines')
    if plan.get('max_model_calls', 0) > max_model_calls:
        raise ValueError(f"plan allows {plan['max_model_calls']} model calls, above the {max_model_calls} ceiling")
    if not os.environ.get('OPENROUTER_API_KEY'):
        raise ValueError('OPENROUTER_API_KEY is required for a live pilot')
    if plan.get('budget_usd', 0) <= 0:
        raise ValueError('plan must declare a positive budget_usd')
    models = {profile['id']: profile for profile in plan.get('models', [])}
    if not models:
        raise ValueError('plan must declare model pricing profiles')
    for profile in models.values():
        pricing = profile.get('pricing', {})
        if (set(pricing) != {'input_per_million', 'output_per_million'} or
                any(not isinstance(pricing[key], (int, float)) or pricing[key] < 0 for key in pricing)):
            raise ValueError(f'model profile {profile["id"]} lacks valid pricing')
        generation = profile.get('generation', {})
        if generation.get('max_retries', 0) != 0:
            raise ValueError(f'model profile {profile["id"]} must set max_retries to 0')
        output_limit = generation.get('max_tokens') or generation.get('max_completion_tokens')
        if (not isinstance(generation.get('timeout'), (int, float)) or generation['timeout'] <= 0 or
                not isinstance(output_limit, (int, float)) or output_limit <= 0):
            raise ValueError(f'model profile {profile["id"]} needs positive timeout and output limit')
    bundles = []
    expected = [(run['block'], run['order']) for run in plan['runs']]
    if len(expected) != len(set(expected)):
        raise ValueError('plan has duplicate block/order entries')
    for run in plan['runs']:
        if run['model_id'] not in models:
            raise ValueError(f"run references unknown model profile {run['model_id']}")
        if run['engine'] not in engines:
            raise ValueError(f"run references engine {run['engine']} outside the plan engine set")
        bundle = load_campaign(plan_path.parent / run['config'])
        campaign = bundle.campaign
        generation = campaign.generation
        output_limit = generation.get('max_tokens') or generation.get('max_completion_tokens')
        if generation.get('max_retries', 0) != 0 or not generation.get('timeout') or not output_limit:
            raise ValueError(f"campaign generation is not bounded for {run['result']}")
        if (campaign.model_id, campaign.engine, bundle.scenarios[0].id, campaign.block, campaign.order) != (
                run['model_id'], run['engine'], run['scenario'], run['block'], run['order']):
            raise ValueError(f"campaign metadata does not match plan for {run['result']}")
        if plan.get('experiment_id') is not None and campaign.experiment_id != plan['experiment_id']:
            raise ValueError(f"campaign experiment does not match plan for {run['result']}")
        bundles.append(bundle)
    return plan, bundles, models


def prior_spend(results):
    total = 0.0
    if not results.exists():
        return total
    for path in results.glob('*/runs.jsonl'):
        for line in path.read_text().splitlines():
            if line.strip():
                total += json.loads(line).get('usage', {}).get('budget_charge_usd') or 0.0
    return total


def check_smoke(results, runs):
    missing = []
    for run in runs:
        path = results / run['result'] / 'runs.jsonl'
        if not path.exists():
            missing.append(run['result'])
            continue
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        row = next((item for item in reversed(rows) if item['evidence_status'] == 'complete'), None)
        if not row:
            missing.append(f"{run['result']} (no completed result)")
            continue
        evidence = json.loads((path.parent / row['evidence']).read_text())
        if not evidence.get('trajectory') or not evidence['trajectory'][0].get('calls'):
            diagnostic = (evidence.get('response_diagnostics') or [{}])[0]
            missing.append(
                f"{run['result']} (no valid first tool call; "
                f"finish_reason={diagnostic.get('finish_reason')!r}; "
                f"native_finish_reason={diagnostic.get('native_finish_reason')!r}; "
                f"invalid_tool_calls={len(diagnostic.get('invalid_tool_calls', []))})")
    if missing:
        raise ValueError('smoke check found no first tool call in: ' + ', '.join(missing))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--max-model-calls', type=int, required=True)
    parser.add_argument('--budget-usd', type=float,
                        help='override the cap recorded in the plan')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    plan, bundles, models = preflight(args.plan, args.max_model_calls)
    budget_usd = plan['budget_usd'] if args.budget_usd is None else args.budget_usd
    if budget_usd <= 0 or budget_usd > plan['budget_usd']:
        raise ValueError(f'--budget-usd must be positive and no greater than the plan cap ${plan["budget_usd"]}')
    print(f"Preflight passed: {len(plan['runs'])} cells, at most {plan['max_model_calls']} model calls.")
    if args.dry_run:
        return
    pricing = {profile['model']: profile['pricing'] for profile in models.values()}
    budget = PilotBudget(budget_usd, pricing, prior_spend(args.results))
    if budget.committed >= budget.limit:
        raise ValueError(f'prior results already consume the ${budget.limit:.6f} budget')
    blocks = sorted({run['block'] for run in plan['runs']})
    for block in blocks:
        for run, bundle in ((run, bundle) for run, bundle in zip(plan['runs'], bundles)
                            if run['block'] == block):
            out = args.results / run['result']
            if out.exists():
                if not args.resume:
                    raise ValueError(f'{out} exists; use --resume to continue the recorded cell')
                bundle, run_id, prior = load_resume(out)
            else:
                run_id, prior = None, ()
            try:
                asyncio.run(run_matrix(bundle, out, run_id, prior, budget))
            except BudgetExceeded as exc:
                print(f'Budget stopped the pilot: {exc}')
                return
        if args.smoke:
            check_smoke(args.results, [run for run in plan['runs'] if run['block'] == block])
            print(f'Smoke check passed for block {block}: every cell made a first tool call.')
            return


if __name__ == '__main__':
    main()
