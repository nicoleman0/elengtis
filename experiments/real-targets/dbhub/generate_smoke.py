#!/usr/bin/env python3
"""Generate a frozen two-model, two-scenario DBHub pilot plan."""
import argparse
import json
from pathlib import Path
import shutil

PROFILES = (
    {'id': 'gpt-oss-20b', 'model': 'openai/gpt-oss-20b',
     'pricing': {'input_per_million': .02, 'output_per_million': .10}},
    {'id': 'deepseek-v4-flash-0731', 'model': 'deepseek/deepseek-v4-flash-0731',
     'pricing': {'input_per_million': .04998, 'output_per_million': .09996}},
)
SCENARIOS = (('benign-ticket', 'benign.yaml'), ('injected-ticket', 'injection.yaml'))
GENERATION = {'temperature': 0, 'max_tokens': 512, 'timeout': 30_000, 'max_retries': 0}
EXPERIMENT_ID = 'dbhub-injection-v2'
RELAY = 'alpine/socat@sha256:ef6c281978dcd6927d9b3829484e4c4fdfc5d98de5acbd6312c04565d2d58cbf'


def campaign(profile, scenario, filename, order, block):
    return f'''schema_version: 1
trials: 1
step_budget: 4
engine: create_agent
model: {profile['model']}
generation: {json.dumps(GENERATION, sort_keys=True)}
experiment_id: {EXPERIMENT_ID}
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
    args = parser.parse_args()
    output = args.out.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error(f'{output} already contains a frozen bundle')
    campaigns, scenarios = output / 'campaigns', output / 'scenarios'
    campaigns.mkdir(parents=True, exist_ok=True)
    scenarios.mkdir()
    source = Path(__file__).parent / 'scenarios'
    for _, filename in SCENARIOS:
        shutil.copyfile(source / filename, scenarios / filename)
    runs = []
    for block in range(3):
        for order, (profile, (scenario, filename)) in enumerate(
                (item for item in ((p, s) for p in PROFILES for s in SCENARIOS))):
            name = f'b{block:03d}-o{order:02d}-{profile["id"]}-{scenario}'
            config = campaigns / f'{name}.yaml'
            config.write_text(campaign(profile, scenario, filename, order, block))
            runs.append({'block': block, 'order': order, 'model_id': profile['id'],
                         'engine': 'create_agent', 'scenario': scenario,
                         'config': str(config.relative_to(output)), 'result': name})
    plan = {'schema_version': 3, 'experiment_id': EXPERIMENT_ID,
            'engines': ['create_agent'], 'budget_usd': .10,
            'models': [{**profile, 'generation': GENERATION} for profile in PROFILES],
            'runs': runs, 'scenarios': [item[0] for item in SCENARIOS],
            'step_budget': 4, 'max_model_calls': 48}
    (output / 'run-order.json').write_text(json.dumps(plan, indent=2) + '\n')


if __name__ == '__main__':
    main()
