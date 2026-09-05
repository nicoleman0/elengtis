#!/usr/bin/env python3
"""Generate one-trial campaign files in a reproducible randomized block order."""
import argparse
import json
from pathlib import Path
import random
import textwrap


ENGINES = ('reference', 'graph', 'langchain', 'create_agent')
SCENARIOS = ('support-note', 'benign-refusal', 'tool-error')


def campaign(engine, scenario, model, route, generation, block, order, scenario_path):
    bindings = {
        'support-note': 'read_note: read_note\nread_credential: read_demo_credential\nsubmit_credential: submit_demo_credential\nverify_collector: read_collector',
        'benign-refusal': 'read_note: read_safe_note\nverify_collector: read_collector',
        'tool-error': 'read_note: read_note\nread_credential: synthetic_tool_error\nverify_collector: read_collector',
    }[scenario]
    metadata = f"provider_route: {json.dumps(route)}\n" if route else ''
    return f'''schema_version: 1
trials: 1
step_budget: 4
engine: {engine}
model: {json.dumps(model)}
{metadata}generation: {json.dumps(generation, sort_keys=True)}
experiment_id: live-orchestration-comparison-v1
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
scenarios: [{json.dumps(str(scenario_path))}]
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True)
    parser.add_argument('--blocks', type=int, default=10)
    parser.add_argument('--seed', type=int, default=20260905)
    parser.add_argument('--provider-route')
    parser.add_argument('--generation-json', default='{}')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    generation = json.loads(args.generation_json)
    if not isinstance(generation, dict) or args.blocks < 1:
        parser.error('generation JSON must be an object and blocks must be positive')
    source = Path(__file__).resolve().parents[2] / 'src/elengtis/examples/scenarios'
    runs = [(engine, scenario) for engine in ENGINES for scenario in SCENARIOS]
    randomizer, output = random.Random(args.seed), args.out
    output.mkdir(parents=True, exist_ok=True)
    commands, order = [], []
    for block in range(args.blocks):
        block_runs = runs[:]; randomizer.shuffle(block_runs)
        for position, (engine, scenario) in enumerate(block_runs):
            name = f'b{block:03d}-o{position:02d}-{engine}-{scenario}'
            config, results = output / f'{name}.yaml', output / 'results' / name
            config.write_text(campaign(engine, scenario, args.model, args.provider_route, generation,
                                       block, position, source / f'{scenario}.yaml'))
            commands.append(f'elengtis run --config {config} --out {results}')
            order.append({'block': block, 'order': position, 'engine': engine,
                          'scenario': scenario, 'config': str(config), 'out': str(results)})
    (output / 'run-order.json').write_text(json.dumps({'seed': args.seed, 'runs': order}, indent=2) + '\n')
    (output / 'commands.txt').write_text('\n'.join(commands) + '\n')


if __name__ == '__main__':
    main()
