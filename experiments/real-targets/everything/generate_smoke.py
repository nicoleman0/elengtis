#!/usr/bin/env python3
"""Generate the four-cell Everything real-target smoke bundle."""
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
SCENARIOS = (
    ('everything-echo', 'echo.yaml', 'echo: echo'),
    ('everything-structured-content', 'structured-content.yaml',
     'structured_content: get-structured-content'),
)
GENERATION = {'temperature': 0, 'max_tokens': 512, 'timeout': 30_000, 'max_retries': 0}
EXPERIMENT_ID = 'everything-real-target-smoke-v1'


def campaign(profile, scenario, filename, binding, order):
    return f'''schema_version: 1
trials: 1
step_budget: 3
engine: create_agent
model: {profile['model']}
generation: {json.dumps(GENERATION, sort_keys=True)}
experiment_id: {EXPERIMENT_ID}
model_id: {profile['id']}
block: 0
order: {order}
targets:
  - id: everything
    transport:
      type: isolated_container
      image: elengtis/everything:2026.8.31
      container_port: 3001
      uid: 10001
      gid: 10001
      path: /mcp
      relay_image: alpine/socat@sha256:ef6c281978dcd6927d9b3829484e4c4fdfc5d98de5acbd6312c04565d2d58cbf
    bindings:
      {scenario}:
        {binding}
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
    for _, filename, _ in SCENARIOS:
        shutil.copyfile(source / filename, scenarios / filename)

    runs = []
    cells = [(profile, scenario) for profile in PROFILES for scenario in SCENARIOS]
    for order, (profile, (scenario, filename, binding)) in enumerate(cells):
        name = f'b000-o{order:02d}-{profile["id"]}-{scenario}'
        config = campaigns / f'{name}.yaml'
        config.write_text(campaign(profile, scenario, filename, binding, order))
        runs.append({'block': 0, 'order': order, 'model_id': profile['id'],
                     'engine': 'create_agent', 'scenario': scenario,
                     'config': str(config.relative_to(output)), 'result': name})

    plan = {'schema_version': 3, 'experiment_id': EXPERIMENT_ID,
            'engines': ['create_agent'], 'budget_usd': .02,
            'models': [{**profile, 'generation': GENERATION} for profile in PROFILES],
            'runs': runs, 'scenarios': [item[0] for item in SCENARIOS],
            'step_budget': 3, 'max_model_calls': 12}
    (output / 'run-order.json').write_text(json.dumps(plan, indent=2) + '\n')


if __name__ == '__main__':
    main()
