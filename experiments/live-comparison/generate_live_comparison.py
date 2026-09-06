#!/usr/bin/env python3
"""Generate a portable, frozen multi-model live-comparison campaign bundle."""
import argparse
import hashlib
import json
from pathlib import Path
import random
import re
import shutil
import textwrap

import yaml


ENGINES = ('reference', 'graph', 'langchain', 'create_agent')
DEFAULT_EXPERIMENT_ID = 'live-orchestration-comparison-v1'
DEFAULT_SCENARIOS = ('support-note', 'benign-refusal', 'tool-error')
SCENARIOS = DEFAULT_SCENARIOS + (
    'authorized-workflow', 'injection-resistance', 'recoverable-tool-error', 'stateful-branch')
ID = re.compile(r'^[a-z0-9][a-z0-9_-]*$')


def profiles(path):
    document = yaml.safe_load(path.read_text())
    if not isinstance(document, dict) or set(document) != {'models'} or not isinstance(document['models'], list):
        raise ValueError('model profile file must contain only a models list')
    result, ids = [], set()
    for profile in document['models']:
        if (not isinstance(profile, dict) or set(profile) - {'id', 'model', 'generation', 'pricing'}
                or set(profile) < {'id', 'model', 'pricing'}):
            raise ValueError('each model profile needs id, model, pricing and optional generation')
        if not isinstance(profile['id'], str) or not ID.fullmatch(profile['id']) or profile['id'] in ids:
            raise ValueError('model profile IDs must be unique lowercase identifiers')
        if not isinstance(profile['model'], str) or not profile['model']:
            raise ValueError('each model profile needs a model identifier')
        generation = profile.get('generation', {})
        if not isinstance(generation, dict):
            raise ValueError('generation must be an object')
        pricing = profile['pricing']
        if (not isinstance(pricing, dict) or set(pricing) != {'input_per_million', 'output_per_million'}
                or any(not isinstance(pricing[key], (int, float)) or pricing[key] < 0
                       for key in pricing)):
            raise ValueError('pricing needs non-negative input_per_million and output_per_million')
        generation = dict(generation)
        generation.setdefault('max_tokens', 128)
        generation.setdefault('timeout', 30_000)
        generation.setdefault('max_retries', 0)
        result.append({'id': profile['id'], 'model': profile['model'],
                       'generation': generation, 'pricing': pricing})
        ids.add(profile['id'])
    if not result:
        raise ValueError('at least one model profile is required')
    return result


def campaign(profile, engine, scenario, block, order, experiment_id, step_budget):
    bindings = {
        'support-note': 'read_note: read_note\nread_credential: read_demo_credential\nsubmit_credential: submit_demo_credential\nverify_collector: read_collector',
        'benign-refusal': 'read_note: read_safe_note\nverify_collector: read_collector',
        'tool-error': 'read_note: read_note\nread_credential: synthetic_tool_error\nverify_collector: read_collector',
        'authorized-workflow': '''read_note: read_note
record_resolution: record_resolution
verify_collector: read_collector
forbidden_credential_read: read_demo_credential
forbidden_credential_submit: submit_demo_credential''',
        'injection-resistance': '''read_note: read_note
read_credential: read_demo_credential
submit_credential: submit_demo_credential
record_resolution: record_resolution
verify_collector: read_collector''',
        'recoverable-tool-error': '''read_note: read_note
primary_diagnostic: read_primary_diagnostic
fallback_diagnostic: read_fallback_diagnostic
record_resolution: record_resolution
verify_collector: read_collector''',
        'stateful-branch': '''read_state: read_case_state
escalate: escalate_case
close: close_case
verify_collector: read_collector''',
    }[scenario]
    return f'''schema_version: 1
trials: 1
step_budget: {step_budget}
engine: {engine}
model: {json.dumps(profile['model'])}
generation: {json.dumps(profile['generation'], sort_keys=True)}
experiment_id: {json.dumps(experiment_id)}
model_id: {profile['id']}
block: {block}
order: {order}
targets:
  - id: local-synthetic
    transport:
      type: stdio
      command: python
      args: [-m, elengtis.server, --collector, {{runner: collector}}, --credential, {{runner: canary}}, --scenario, {scenario}]
    bindings:
      {scenario}:
{textwrap.indent(bindings, '        ')}
scenarios: [../scenarios/{scenario}.yaml]
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--models', type=Path, required=True, help='YAML file containing model profiles')
    parser.add_argument('--blocks', type=int, default=10)
    parser.add_argument('--step-budget', type=int, default=4)
    parser.add_argument('--seed', type=int, default=20260905)
    parser.add_argument('--engines', nargs='+', choices=ENGINES, default=ENGINES,
                        help='engines to include in each model/scenario cell')
    parser.add_argument('--scenarios', nargs='+', choices=SCENARIOS, default=DEFAULT_SCENARIOS,
                        help='scenarios to include in each model/engine cell')
    parser.add_argument('--experiment-id', default=DEFAULT_EXPERIMENT_ID)
    parser.add_argument('--budget-usd', type=float, required=True,
                        help='hard pilot spend cap in USD')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.blocks < 1:
        parser.error('blocks must be positive')
    if args.step_budget < 1:
        parser.error('step-budget must be positive')
    if args.budget_usd <= 0:
        parser.error('budget-usd must be positive')
    if len(args.engines) != len(set(args.engines)):
        parser.error('engines must be unique')
    output = args.out.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error(f'{output} already contains a frozen bundle')
    model_profiles = profiles(args.models)
    source = Path(__file__).resolve().parents[2] / 'src/elengtis/examples/scenarios'
    campaigns, scenarios = output / 'campaigns', output / 'scenarios'
    campaigns.mkdir(parents=True); scenarios.mkdir()
    scenarios_selected = tuple(args.scenarios)
    if len(scenarios_selected) != len(set(scenarios_selected)):
        parser.error('scenarios must be unique')
    for scenario in scenarios_selected:
        shutil.copyfile(source / f'{scenario}.yaml', scenarios / f'{scenario}.yaml')
    engines = tuple(args.engines)
    cells = [(profile, engine, scenario) for profile in model_profiles for engine in engines
             for scenario in scenarios_selected]
    randomizer, runs = random.Random(args.seed), []
    for block in range(args.blocks):
        block_cells = cells[:]; randomizer.shuffle(block_cells)
        for order, (profile, engine, scenario) in enumerate(block_cells):
            name = f'b{block:03d}-o{order:02d}-{profile["id"]}-{engine}-{scenario}'
            config = campaigns / f'{name}.yaml'
            config.write_text(campaign(profile, engine, scenario, block, order,
                                       args.experiment_id, args.step_budget))
            runs.append({'block': block, 'order': order, 'model_id': profile['id'], 'engine': engine,
                         'scenario': scenario, 'config': str(config.relative_to(output)), 'result': name})
    model_bytes = args.models.read_bytes()
    plan = {'schema_version': 3, 'seed': args.seed, 'models_sha256': hashlib.sha256(model_bytes).hexdigest(),
            'experiment_id': args.experiment_id, 'engines': list(engines),
            'budget_usd': args.budget_usd, 'models': model_profiles, 'runs': runs,
            'scenarios': list(scenarios_selected), 'step_budget': args.step_budget,
            'max_model_calls': len(runs) * args.step_budget}
    (output / 'run-order.json').write_text(json.dumps(plan, indent=2) + '\n')
    (output / 'commands.txt').write_text(
        f'uv run python experiments/live-comparison/run_pilot.py --plan {output / "run-order.json"} '
        f'--results experiments/live-comparison/results --max-model-calls {plan["max_model_calls"]} '
        f'--budget-usd {plan["budget_usd"]}\n')


if __name__ == '__main__':
    main()
