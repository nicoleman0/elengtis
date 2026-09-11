#!/usr/bin/env python3
"""Generate a frozen multi-model, multi-scenario DBHub pilot plan."""
import argparse
import json
from pathlib import Path
import shutil

import yaml

GENERATION = {'temperature': 0, 'max_tokens': 512, 'timeout': 30_000, 'max_retries': 0}
PROFILES = (
    {'id': 'gpt-oss-20b', 'model': 'openai/gpt-oss-20b',
     'pricing': {'input_per_million': .02, 'output_per_million': .10},
     'generation': GENERATION},
    {'id': 'deepseek-v4-flash-0731', 'model': 'deepseek/deepseek-v4-flash-0731',
     'pricing': {'input_per_million': .04998, 'output_per_million': .09996},
     'generation': GENERATION},
)
# Scenario id -> scenario file. The first two are the frozen dbhub-injection-v2 pair.
SCENARIOS = {'benign-ticket': 'benign.yaml', 'injected-ticket': 'injection.yaml',
             'injected-operator': 'injection-operator.yaml',
             'injected-delimiter': 'injection-delimiter.yaml'}
DEFAULT_SCENARIOS = ('benign-ticket', 'injected-ticket')
EXPERIMENT_ID = 'dbhub-injection-v2'
STEP_BUDGET = 4
RELAY = 'alpine/socat@sha256:ef6c281978dcd6927d9b3829484e4c4fdfc5d98de5acbd6312c04565d2d58cbf'


def roster_from(path):
    """Minimal read; run_pilot.py preflight validates every field before any spend."""
    return tuple({'id': m['id'], 'model': m['model'], 'generation': m['generation'],
                  'pricing': m['pricing']} for m in yaml.safe_load(path.read_text())['models'])


def campaign(profile, scenario, filename, order, block, experiment_id):
    return f'''schema_version: 1
trials: 1
step_budget: {STEP_BUDGET}
engine: create_agent
model: {profile['model']}
generation: {json.dumps(profile['generation'], sort_keys=True)}
experiment_id: {experiment_id}
model_id: {profile['id']}
block: {block}
order: {order}
targets:
  - id: dbhub
    transport:
      type: isolated_container
      image: elengtis/dbhub:1.2.3
      container_port: 8080
      uid: 10001
      gid: 10001
      path: /mcp
      relay_image: {RELAY}
    bindings:
      {scenario}:
        read_ticket: read_ticket
        set_ticket_status: set_ticket_status
        set_ticket_body: set_ticket_body
scenarios: [../scenarios/{filename}]
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--profiles', type=Path,
                        help='YAML {models: [...]} overriding the built-in profiles')
    parser.add_argument('--scenarios', nargs='+', choices=sorted(SCENARIOS),
                        default=list(DEFAULT_SCENARIOS))
    parser.add_argument('--blocks', type=int, default=3)
    parser.add_argument('--budget-usd', type=float, default=.10)
    parser.add_argument('--experiment-id', default=EXPERIMENT_ID)
    args = parser.parse_args()
    if args.blocks < 1:
        parser.error('--blocks must be positive')
    if len(args.scenarios) != len(set(args.scenarios)):
        parser.error('--scenarios must be unique')
    selected = tuple((name, SCENARIOS[name]) for name in args.scenarios)
    roster = roster_from(args.profiles) if args.profiles else PROFILES
    output = args.out.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error(f'{output} already contains a frozen bundle')
    campaigns, scenarios = output / 'campaigns', output / 'scenarios'
    campaigns.mkdir(parents=True, exist_ok=True)
    scenarios.mkdir()
    source = Path(__file__).parent / 'scenarios'
    for _, filename in selected:
        shutil.copyfile(source / filename, scenarios / filename)
    runs = []
    for block in range(args.blocks):
        for order, (profile, (scenario, filename)) in enumerate(
                ((p, s) for p in roster for s in selected)):
            name = f'b{block:03d}-o{order:02d}-{profile["id"]}-{scenario}'
            config = campaigns / f'{name}.yaml'
            config.write_text(campaign(profile, scenario, filename, order, block,
                                       args.experiment_id))
            runs.append({'block': block, 'order': order, 'model_id': profile['id'],
                         'engine': 'create_agent', 'scenario': scenario,
                         'config': str(config.relative_to(output)), 'result': name})
    plan = {'schema_version': 3, 'experiment_id': args.experiment_id,
            'engines': ['create_agent'], 'budget_usd': args.budget_usd,
            'models': [dict(profile) for profile in roster],
            'runs': runs, 'scenarios': [item[0] for item in selected],
            'step_budget': STEP_BUDGET,
            'max_model_calls': len(roster) * len(selected) * args.blocks * STEP_BUDGET}
    (output / 'run-order.json').write_text(json.dumps(plan, indent=2) + '\n')


if __name__ == '__main__':
    main()
