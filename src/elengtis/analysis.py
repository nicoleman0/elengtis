"""Small, dependency-free summaries for live comparison result directories."""
from __future__ import annotations

import json
import math
import random
from statistics import median
from itertools import combinations
from collections import defaultdict
from pathlib import Path


def wilson(successes, total, z=1.959963984540054):
    """Return a 95% Wilson interval, or None when the outcome is unknown."""
    if not total:
        return None
    proportion, scale = successes / total, z * z / total
    centre = (proportion + scale / 2) / (1 + scale)
    margin = z * math.sqrt((proportion * (1 - proportion) + scale / 4) / total) / (1 + scale)
    return [centre - margin, centre + margin]


def load_rows(inputs):
    """Read final complete attempts while retaining incomplete-trial accounting."""
    rows = []
    for directory in inputs:
        manifest = json.loads((directory / 'manifest.json').read_text())
        attempts = [json.loads(line) for line in (directory / 'runs.jsonl').read_text().splitlines() if line]
        latest, completed, attempt_counts = {}, {}, defaultdict(int)
        attempt_usage = defaultdict(lambda: {'input_tokens': 0, 'output_tokens': 0,
                                             'total_tokens': 0, 'cost_usd': 0.0,
                                             'budget_charge_usd': 0.0, 'unknown_costs': 0})
        for row in attempts:
            latest[row['trial_id']] = row
            attempt_counts[row['trial_id']] += 1
            usage = row.get('usage', {})
            for key in ('input_tokens', 'output_tokens', 'total_tokens', 'unknown_costs'):
                attempt_usage[row['trial_id']][key] += usage.get(key, 0) or 0
            for key in ('cost_usd', 'budget_charge_usd'):
                attempt_usage[row['trial_id']][key] += usage.get(key, 0) or 0
            if row['evidence_status'] == 'complete':
                completed[row['trial_id']] = row
        for trial_id, fallback in latest.items():
            row = completed.get(trial_id, fallback)
            row = dict(row)
            row['block'] = manifest.get('experiment', {}).get('block')
            row['order'] = manifest.get('experiment', {}).get('order')
            row['model_id'] = (manifest.get('experiment', {}).get('model_id')
                               or manifest.get('campaign', {}).get('model'))
            row['final_complete'] = row['evidence_status'] == 'complete'
            row['attempts'] = attempt_counts[trial_id]
            row['retries'] = max(0, row['attempts'] - 1)
            row['attempt_usage'] = attempt_usage[trial_id]
            rows.append(row)
    return rows


def summarize(inputs):
    rows = load_rows(inputs)
    groups = defaultdict(list)
    for row in rows:
        groups[(row['model_id'], row['engine'], row['scenario'])].append(row)
    table = []
    for (model_id, engine, scenario), members in sorted(groups.items()):
        complete = [row for row in members if row['final_complete']]
        item = {'model_id': model_id, 'engine': engine, 'scenario': scenario, 'trials': len(members),
                'complete_trials': len(complete), 'incomplete_trials': len(members) - len(complete)}
        item['retries'] = sum(row.get('retries', 0) for row in members)
        item['failure_classes'] = dict(sorted(
            (name, sum(row.get('failure_class') == name for row in members))
            for name in {row.get('failure_class') for row in members if row.get('failure_class')}))
        for metric in ('proposed', 'completed', 'safety_pass', 'safe_completed',
                       'recovery_succeeded', 'proposed_not_completed'):
            known = [row.get(metric) for row in complete if row.get(metric) is not None]
            item[metric] = {'successes': sum(value is True for value in known),
                            'known': len(known), 'unknown': len(members) - len(known),
                            'wilson_95': wilson(sum(value is True for value in known), len(known))}
        item['tool_error_count'] = sum(row.get('tool_error_count', 0) or 0 for row in members)
        sequences = defaultdict(int)
        for row in members:
            sequence = row.get('tool_sequence')
            if sequence is not None:
                sequences[' -> '.join(sequence)] += 1
        item['tool_sequences'] = dict(sorted(sequences.items()))
        item['terminations'] = dict(sorted(
            ((name, sum(row['termination'] == name for row in members))
             for name in {row['termination'] for row in members})))
        item['tool_call_diagnostics'] = {
            'invalid_tool_calls': sum((row.get('diagnostics') or {}).get('invalid_tool_calls', 0)
                                      for row in members),
            'first_finish_reasons': dict(sorted(
                (reason, sum((row.get('diagnostics') or {}).get('first_finish_reason') == reason
                             for row in members))
                for reason in {(row.get('diagnostics') or {}).get('first_finish_reason')
                               for row in members if (row.get('diagnostics') or {}).get('first_finish_reason')})),
        }
        for metric in ('model_turns', 'tool_calls', 'steps_to_propose'):
            values = [row[metric] for row in complete if row.get(metric) is not None]
            item[metric] = {'mean': sum(values) / len(values) if values else None,
                            'known': len(values)}
        usage = [row.get('attempt_usage', row.get('usage', {})) for row in members]
        item['usage'] = {
            'input_tokens': sum(entry.get('input_tokens', 0) for entry in usage),
            'output_tokens': sum(entry.get('output_tokens', 0) for entry in usage),
            'total_tokens': sum(entry.get('total_tokens', 0) for entry in usage),
            'cost_usd': sum(entry.get('cost_usd', 0) or 0 for entry in usage),
            'budget_charge_usd': sum(entry.get('budget_charge_usd', 0) or 0 for entry in usage),
            'unknown_costs': sum(entry.get('unknown_costs', 0) for entry in usage),
        }
        table.append(item)
    return table


def pairwise(rows, samples=2000):
    """Estimate within-block engine differences without pooling distinct models."""
    grouped = defaultdict(list)
    for row in rows:
        if row['final_complete'] and row['block'] is not None:
            grouped[(row['model_id'], row['scenario'])].append(row)
    results = []
    for (model_id, scenario), members in sorted(grouped.items()):
        by_engine_block = defaultdict(dict)
        for row in members:
            by_engine_block[row['engine']][row['block']] = row
        for left, right in combinations(sorted(by_engine_block), 2):
            for metric in ('proposed', 'completed', 'safety_pass', 'safe_completed',
                           'recovery_succeeded', 'proposed_not_completed'):
                differences = []
                for block in sorted(set(by_engine_block[left]) & set(by_engine_block[right])):
                    first = by_engine_block[left][block].get(metric)
                    second = by_engine_block[right][block].get(metric)
                    if first is not None and second is not None:
                        differences.append(int(second) - int(first))
                if not differences:
                    continue
                randomizer = random.Random(f'{model_id}:{scenario}:{left}:{right}:{metric}')
                estimates = sorted(sum(randomizer.choice(differences) for _ in differences) / len(differences)
                                   for _ in range(samples))
                results.append({'model_id': model_id, 'scenario': scenario, 'metric': metric,
                                'left_engine': left, 'right_engine': right, 'blocks': len(differences),
                                'difference_pp': 100 * sum(differences) / len(differences),
                                'bootstrap_95': [100 * estimates[int(.025 * samples)],
                                                 100 * estimates[int(.975 * samples) - 1]]})
    return results


def recommend_engine(rows, practical_margin_pp=10, min_families=2, conformance_passed=False):
    """Apply the predeclared live comparison rule without pooling trial counts."""
    result = {'rule': {'practical_margin_pp': practical_margin_pp,
                       'minimum_winning_families': min_families},
              'conformance_passed': conformance_passed}
    if not conformance_passed:
        result['status'] = 'conformance_required'
        return result
    rates = defaultdict(lambda: defaultdict(list))
    safety_failures = defaultdict(int)
    for row in rows:
        if not row.get('final_complete'):
            continue
        key = (row.get('model_id'), row.get('scenario'))
        engine = row.get('engine')
        if row.get('safe_completed') is not None:
            rates[key][engine].append(bool(row['safe_completed']))
        if row.get('scenario') in {'injection-resistance', 'stateful-branch'}:
            if row.get('safety_pass') is False:
                safety_failures[engine] += 1
    deltas = defaultdict(list)
    for (_, scenario), engines in rates.items():
        if engines.get('graph') and engines.get('create_agent'):
            graph_rate = sum(engines['graph']) / len(engines['graph'])
            agent_rate = sum(engines['create_agent']) / len(engines['create_agent'])
            deltas[scenario].append(100 * (graph_rate - agent_rate))
    family_deltas = {scenario: median(values) for scenario, values in deltas.items()}
    result['family_deltas_pp'] = family_deltas
    result['safety_failures'] = dict(sorted(safety_failures.items()))
    statuses = {}
    for engine, sign in (('graph', 1), ('create_agent', -1)):
        winning = sum(value * sign >= practical_margin_pp for value in family_deltas.values())
        losing = sum(value * sign <= -practical_margin_pp for value in family_deltas.values())
        statuses[engine] = {'winning_families': winning, 'losing_families': losing,
                            'safety_ok': not safety_failures.get(engine)}
    result['engines'] = statuses
    eligible = [engine for engine, status in statuses.items()
                if status['safety_ok'] and status['winning_families'] >= min_families
                and status['losing_families'] == 0]
    if len(eligible) == 1:
        result['status'] = eligible[0]
        result['winning_families'] = statuses[eligible[0]]['winning_families']
    else:
        result['status'] = 'role_split'
        result['winning_families'] = max(status['winning_families'] for status in statuses.values())
    return result


def write_report(inputs, out, practical_margin_pp=10, conformance_passed=False):
    """Write JSON plus a concise Markdown table for publication review."""
    out.mkdir(parents=True, exist_ok=True)
    rows, table = load_rows(inputs), summarize(inputs)
    comparisons = pairwise(rows)
    recommendation = recommend_engine(rows, practical_margin_pp, conformance_passed=conformance_passed)
    (out / 'summary.json').write_text(
        json.dumps({'cells': table, 'comparisons': comparisons,
                    'recommendation': recommendation}, indent=2) + '\n')
    lines = ['# Live comparison summary', '',
             '| Model | Engine | Scenario | Trials | Complete | Retries | Failures | Tool errors | Invalid calls | First finish reasons | Cost | Proposed | Completed | Safety pass | Safe completed | Recovered | Proposed, not completed |',
             '| --- | --- | --- | ---: | ---: | ---: | --- | ---: | ---: | --- | ---: | --- | --- | --- | --- | --- | --- |']
    for row in table:
        def cell(name):
            metric = row[name]
            interval = metric['wilson_95']
            shown = f"{metric['successes']}/{metric['known']}"
            if interval:
                shown += f" ({interval[0]:.1%}-{interval[1]:.1%})"
            return shown + (f"; {metric['unknown']} unknown" if metric['unknown'] else '')
        lines.append(f"| {row['model_id']} | {row['engine']} | {row['scenario']} | {row['trials']} | {row['complete_trials']} | "
                     f"{row['retries']} | {sum(row['failure_classes'].values())} | {row['tool_error_count']} | "
                     f"{row['tool_call_diagnostics']['invalid_tool_calls']} | "
                     f"{', '.join(row['tool_call_diagnostics']['first_finish_reasons']) or '-'} | "
                     f"${row['usage']['cost_usd']:.6f} | {cell('proposed')} | "
                     f"{cell('completed')} | {cell('safety_pass')} | {cell('safe_completed')} | "
                     f"{cell('recovery_succeeded')} | {cell('proposed_not_completed')} |")
    lines.extend(['', '## Recommendation', '',
                  f"Status: `{recommendation['status']}`.",
                  'The recommendation is gated on deterministic conformance and uses '
                  f"a {practical_margin_pp:g} percentage-point margin."])
    if comparisons:
        lines.extend(['', '## Pairwise engine differences', '',
                      '| Model | Scenario | Metric | Difference (right - left) | 95% bootstrap interval | Blocks |',
                      '| --- | --- | --- | --- | --- | ---: |'])
        for item in comparisons:
            lines.append(f"| {item['model_id']} | {item['scenario']} | {item['metric']} | "
                         f"{item['right_engine']} - {item['left_engine']}: {item['difference_pp']:.1f} pp | "
                         f"{item['bootstrap_95'][0]:.1f} to {item['bootstrap_95'][1]:.1f} pp | {item['blocks']} |")
    (out / 'summary.md').write_text('\n'.join(lines) + '\n')
    return table
