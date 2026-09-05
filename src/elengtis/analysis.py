"""Small, dependency-free summaries for live comparison result directories."""
from __future__ import annotations

import json
import math
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
            row['final_complete'] = row['evidence_status'] == 'complete'
            rows.append(row)
    return rows


def summarize(inputs):
    rows = load_rows(inputs)
    groups = defaultdict(list)
    for row in rows:
        groups[(row['engine'], row['scenario'])].append(row)
    table = []
    for (engine, scenario), members in sorted(groups.items()):
        complete = [row for row in members if row['final_complete']]
        item = {'engine': engine, 'scenario': scenario, 'trials': len(members),
                'complete_trials': len(complete), 'incomplete_trials': len(members) - len(complete)}
        for metric in ('proposed', 'completed', 'proposed_not_completed'):
            known = [row[metric] for row in complete if row[metric] is not None]
            item[metric] = {'successes': sum(value is True for value in known),
                            'known': len(known), 'unknown': len(members) - len(known),
                            'wilson_95': wilson(sum(value is True for value in known), len(known))}
        item['terminations'] = dict(sorted(
            ((name, sum(row['termination'] == name for row in members))
             for name in {row['termination'] for row in members})))
        table.append(item)
    return table


def write_report(inputs, out):
    """Write JSON plus a concise Markdown table for publication review."""
    out.mkdir(parents=True, exist_ok=True)
    table = summarize(inputs)
    (out / 'summary.json').write_text(json.dumps(table, indent=2) + '\n')
    lines = ['# Live comparison summary', '',
             '| Engine | Scenario | Trials | Complete | Proposed | Completed | Proposed, not completed |',
             '| --- | --- | ---: | ---: | --- | --- | --- |']
    for row in table:
        def cell(name):
            metric = row[name]
            interval = metric['wilson_95']
            shown = f"{metric['successes']}/{metric['known']}"
            if interval:
                shown += f" ({interval[0]:.1%}-{interval[1]:.1%})"
            return shown + (f"; {metric['unknown']} unknown" if metric['unknown'] else '')
        lines.append(f"| {row['engine']} | {row['scenario']} | {row['trials']} | {row['complete_trials']} | {cell('proposed')} | "
                     f"{cell('completed')} | {cell('proposed_not_completed')} |")
    (out / 'summary.md').write_text('\n'.join(lines) + '\n')
    return table
