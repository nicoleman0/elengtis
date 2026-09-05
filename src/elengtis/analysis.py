"""Small, dependency-free summaries for live comparison result directories."""
from __future__ import annotations

import json
import math
import random
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
        latest, completed = {}, {}
        for row in attempts:
            latest[row['trial_id']] = row
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
        for metric in ('proposed', 'completed', 'proposed_not_completed'):
            known = [row[metric] for row in complete if row[metric] is not None]
            item[metric] = {'successes': sum(value is True for value in known),
                            'known': len(known), 'unknown': len(members) - len(known),
                            'wilson_95': wilson(sum(value is True for value in known), len(known))}
        item['terminations'] = dict(sorted(
            ((name, sum(row['termination'] == name for row in members))
             for name in {row['termination'] for row in members})))
        for metric in ('model_turns', 'tool_calls', 'steps_to_propose'):
            values = [row[metric] for row in complete if row.get(metric) is not None]
            item[metric] = {'mean': sum(values) / len(values) if values else None,
                            'known': len(values)}
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
            for metric in ('proposed', 'completed', 'proposed_not_completed'):
                differences = []
                for block in sorted(set(by_engine_block[left]) & set(by_engine_block[right])):
                    first, second = by_engine_block[left][block][metric], by_engine_block[right][block][metric]
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


def write_report(inputs, out):
    """Write JSON plus a concise Markdown table for publication review."""
    out.mkdir(parents=True, exist_ok=True)
    rows, table = load_rows(inputs), summarize(inputs)
    comparisons = pairwise(rows)
    (out / 'summary.json').write_text(json.dumps({'cells': table, 'comparisons': comparisons}, indent=2) + '\n')
    lines = ['# Live comparison summary', '',
             '| Model | Engine | Scenario | Trials | Complete | Proposed | Completed | Proposed, not completed |',
             '| --- | --- | --- | ---: | ---: | --- | --- | --- |']
    for row in table:
        def cell(name):
            metric = row[name]
            interval = metric['wilson_95']
            shown = f"{metric['successes']}/{metric['known']}"
            if interval:
                shown += f" ({interval[0]:.1%}-{interval[1]:.1%})"
            return shown + (f"; {metric['unknown']} unknown" if metric['unknown'] else '')
        lines.append(f"| {row['model_id']} | {row['engine']} | {row['scenario']} | {row['trials']} | {row['complete_trials']} | {cell('proposed')} | "
                     f"{cell('completed')} | {cell('proposed_not_completed')} |")
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
