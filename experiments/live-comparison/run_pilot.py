#!/usr/bin/env python3
"""Preflight and run a frozen live-comparison plan in its recorded order."""
import argparse
import asyncio
import json
import os
from pathlib import Path

from elengtis.cli import load_resume, run_matrix
from elengtis.config import load_campaign


def preflight(plan_path, max_model_calls):
    plan = json.loads(plan_path.read_text())
    if plan.get('schema_version') != 1 or not isinstance(plan.get('runs'), list):
        raise ValueError('invalid live-comparison plan')
    if plan.get('max_model_calls', 0) > max_model_calls:
        raise ValueError(f"plan allows {plan['max_model_calls']} model calls, above the {max_model_calls} ceiling")
    if not os.environ.get('OPENROUTER_API_KEY'):
        raise ValueError('OPENROUTER_API_KEY is required for a live pilot')
    bundles = []
    expected = [(run['block'], run['order']) for run in plan['runs']]
    if len(expected) != len(set(expected)):
        raise ValueError('plan has duplicate block/order entries')
    for run in plan['runs']:
        bundle = load_campaign(plan_path.parent / run['config'])
        campaign = bundle.campaign
        if (campaign.model_id, campaign.engine, bundle.scenarios[0].id, campaign.block, campaign.order) != (
                run['model_id'], run['engine'], run['scenario'], run['block'], run['order']):
            raise ValueError(f"campaign metadata does not match plan for {run['result']}")
        bundles.append(bundle)
    return plan, bundles


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--max-model-calls', type=int, required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    plan, bundles = preflight(args.plan, args.max_model_calls)
    print(f"Preflight passed: {len(plan['runs'])} cells, at most {plan['max_model_calls']} model calls.")
    if args.dry_run:
        return
    for run, bundle in zip(plan['runs'], bundles):
        out = args.results / run['result']
        if out.exists():
            if not args.resume:
                raise ValueError(f'{out} exists; use --resume to continue the recorded cell')
            bundle, run_id, prior = load_resume(out)
        else:
            run_id, prior = None, ()
        asyncio.run(run_matrix(bundle, out, run_id, prior))


if __name__ == '__main__':
    main()
