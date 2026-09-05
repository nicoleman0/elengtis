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
SCENARIOS = ('support-note', 'benign-refusal', 'tool-error')
ID = re.compile(r'^[a-z0-9][a-z0-9_-]*$')


def profiles(path):
    document = yaml.safe_load(path.read_text())
    if not isinstance(document, dict) or set(document) != {'models'} or not isinstance(document['models'], list):
        raise ValueError('model profile file must contain only a models list')
    result, ids = [], set()
    for profile in document['models']:
        if not isinstance(profile, dict) or set(profile) - {'id', 'model', 'generation'} or set(profile) < {'id', 'model'}:
            raise ValueError('each model profile needs id, model and optional generation')
        if not isinstance(profile['id'], str) or not ID.fullmatch(profile['id']) or profile['id'] in ids:
            raise ValueError('model profile IDs must be unique lowercase identifiers')
        if not isinstance(profile['model'], str) or not profile['model']:
            raise ValueError('each model profile needs a model identifier')
        generation = profile.get('generation', {})
        if not isinstance(generation, dict):
            raise ValueError('generation must be an object')
        result.append({'id': profile['id'], 'model': profile['model'], 'generation': generation})
        ids.add(profile['id'])
    if not result:
        raise ValueError('at least one model profile is required')
    return result


def campaign(profile, engine, scenario, block, order):
    bindings = {
        'support-note': 'read_note: read_note\nread_credential: read_demo_credential\nsubmit_credential: submit_demo_credential\nverify_collector: read_collector',
        'benign-refusal': 'read_note: read_safe_note\nverify_collector: read_collector',
        'tool-error': 'read_note: read_note\nread_credential: synthetic_tool_error\nverify_collector: read_collector',
    }[scenario]
    return f'''schema_version: 1
trials: 1
step_budget: 4
engine: {engine}
model: {json.dumps(profile['model'])}
generation: {json.dumps(profile['generation'], sort_keys=True)}
experiment_id: live-orchestration-comparison-v1
model_id: {profile['id']}
block: {block}
order: {order}
targets:
  - id: local-synthetic
    transport:
      type: stdio
      command: python
      args: [-m, elengtis.server, --collector, {{runner: collector}}, --credential, {{runner: canary}}]
    bindings:
      {scenario}:
{textwrap.indent(bindings, '        ')}
scenarios: [../scenarios/{scenario}.yaml]
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--models', type=Path, required=True, help='YAML file containing model profiles')
    parser.add_argument('--blocks', type=int, default=10)
    parser.add_argument('--seed', type=int, default=20260905)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.blocks < 1:
        parser.error('blocks must be positive')
    output = args.out.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error(f'{output} already contains a frozen bundle')
    model_profiles = profiles(args.models)
    source = Path(__file__).resolve().parents[2] / 'src/elengtis/examples/scenarios'
    campaigns, scenarios = output / 'campaigns', output / 'scenarios'
    campaigns.mkdir(parents=True); scenarios.mkdir()
    for scenario in SCENARIOS:
        shutil.copyfile(source / f'{scenario}.yaml', scenarios / f'{scenario}.yaml')
    cells = [(profile, engine, scenario) for profile in model_profiles for engine in ENGINES for scenario in SCENARIOS]
    randomizer, runs = random.Random(args.seed), []
    for block in range(args.blocks):
        block_cells = cells[:]; randomizer.shuffle(block_cells)
        for order, (profile, engine, scenario) in enumerate(block_cells):
            name = f'b{block:03d}-o{order:02d}-{profile["id"]}-{engine}-{scenario}'
            config = campaigns / f'{name}.yaml'
            config.write_text(campaign(profile, engine, scenario, block, order))
            runs.append({'block': block, 'order': order, 'model_id': profile['id'], 'engine': engine,
                         'scenario': scenario, 'config': str(config.relative_to(output)), 'result': name})
    model_bytes = args.models.read_bytes()
    plan = {'schema_version': 1, 'seed': args.seed, 'models_sha256': hashlib.sha256(model_bytes).hexdigest(),
            'models': model_profiles, 'runs': runs, 'max_model_calls': len(runs) * 4}
    (output / 'run-order.json').write_text(json.dumps(plan, indent=2) + '\n')
    (output / 'commands.txt').write_text(
        f'uv run python experiments/live-comparison/run_pilot.py --plan {output / "run-order.json"} '
        f'--results experiments/live-comparison/results --max-model-calls {plan["max_model_calls"]}\n')


if __name__ == '__main__':
    main()
