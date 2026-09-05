"""Validate and run declarative MCP auditing campaigns."""
import argparse, asyncio, hashlib, json, platform, subprocess, tempfile, traceback, uuid
from collections import Counter
from datetime import datetime, timezone
from importlib import import_module
from importlib.metadata import version
from pathlib import Path

from elengtis.config import Campaign, CampaignBundle, Scenario, load_campaign, plan_trials, write_schemas
from elengtis.reference import ScriptedProvider
from elengtis.scenario import evaluate_proposals, resolve_value, run_actions, verify
from elengtis.transports import open_target

SCHEMA_VERSION, METRICS_VERSION, MAX_CONSECUTIVE_TARGET_FAILURES = 3, 1, 3
ENGINES = {'reference': ('reference', 'run_episode'), 'graph': ('graph', 'run_episode'),
           'langchain': ('adapters', 'run_episode'), 'create_agent': ('adapters', 'run_agent_episode')}
PACKAGES = ('mcp', 'pydantic', 'PyYAML', 'httpx', 'langchain', 'langchain-core', 'langgraph',
            'langchain-mcp-adapters', 'langchain-openrouter')


def resolve_engine(name):
    module, attribute = ENGINES[name]
    return getattr(import_module(f'elengtis.{module}'), attribute)


def live_provider(model):
    from langchain_openrouter import ChatOpenRouter
    from elengtis.adapters import LiveProvider
    return LiveProvider(ChatOpenRouter(model=model))


def provenance():
    package, root = Path(__file__).parent, Path(__file__).parent.parent.parent
    rev = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True)
    dirty = subprocess.run(['git', '-C', str(root), 'status', '--porcelain'], capture_output=True, text=True)
    lock = root / 'uv.lock'
    return {'python': platform.python_version(), 'platform': platform.platform(),
            'package_version': version('elengtis'),
            'package_versions': {name: version(name) for name in PACKAGES},
            'code_revision': rev.stdout.strip() if rev.returncode == 0 else None,
            'working_tree_dirty': bool(dirty.stdout) if dirty.returncode == 0 else None,
            'source_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in sorted(package.glob('*.py'))},
            'lock_sha256': hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else None}


def load_resume(out):
    manifest_path, rows_path = out / 'manifest.json', out / 'runs.jsonl'
    if not manifest_path.exists() or not rows_path.exists():
        raise ValueError(f'{out} has no campaign results to resume')
    manifest = json.loads(manifest_path.read_text())
    for key, current in (('schema_version', SCHEMA_VERSION), ('metrics_version', METRICS_VERSION)):
        if manifest.get(key) != current:
            raise ValueError(f'{key} is incompatible with this build')
    manifest.setdefault('resumes', []).append({'resumed_at': datetime.now(timezone.utc).isoformat(),
                                                'environment': provenance()})
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
    bundle = CampaignBundle(Campaign.model_validate(manifest['campaign']),
                            tuple(Scenario.model_validate(s) for s in manifest['scenarios']), out)
    rows = [json.loads(line) for line in rows_path.read_text().splitlines() if line.strip()]
    return bundle, manifest['run_id'], rows


def write_summary(bundle, out, attempts):
    latest = {row['trial_id']: row for row in attempts if row['evidence_status'] == 'complete'}
    rows, total = list(latest.values()), len(latest)
    lines = [f'Campaign via the {bundle.campaign.engine} engine.', f'trials: {total}']
    for metric in ('proposed', 'completed', 'proposed_not_completed'):
        known = [row[metric] for row in rows if row[metric] is not None]
        lines.append(f'{metric}: {sum(value is True for value in known)}/{len(known)}'
                     + (f' ({total-len(known)} unknown)' if len(known) != total else ''))
    if len(attempts) > total:
        lines.append(f'attempts: {len(attempts)}, of which {len(attempts)-total} were retried')
    lines.extend(f'{row["trial_id"]}: {row["termination"]}' for row in rows)
    summary = '\n'.join(lines)
    (out / 'summary.txt').write_text(summary + '\n'); print(summary)


async def run_matrix(bundle, out, run_id=None, prior=()):
    campaign, resuming = bundle.campaign, run_id is not None
    run_id = run_id or str(uuid.uuid4())
    done = {r['trial_id'] for r in prior if r['evidence_status'] == 'complete'}
    counts, rows = Counter(r['trial_id'] for r in prior), list(prior)
    if not resuming:
        out.mkdir(parents=True)
        manifest = {'schema_version': SCHEMA_VERSION, 'metrics_version': METRICS_VERSION,
                    'run_id': run_id, 'started_at': datetime.now(timezone.utc).isoformat(),
                    'campaign': campaign.model_dump(mode='json', by_alias=True),
                    'scenarios': [s.model_dump(mode='json', by_alias=True) for s in bundle.scenarios],
                    'environment': provenance(),
                    'max_consecutive_target_failures': MAX_CONSECUTIVE_TARGET_FAILURES}
        (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    with (out / 'runs.jsonl').open('a' if resuming else 'x') as stream:
        for trial in plan_trials(bundle):
            if trial.trial_id in done:
                continue
            suffix = f'.retry-{counts[trial.trial_id]}' if counts[trial.trial_id] else ''
            evidence_name = f'{trial.trial_id}{suffix}.json'
            attempt_id, canary = str(uuid.uuid4()), f'ELENGTIS-{uuid.uuid4()}'
            with tempfile.TemporaryDirectory() as tmp:
                values = dict(trial.bindings) | {'canary': canary, 'trial_dir': tmp,
                                                  'collector': str(Path(tmp) / 'collector.jsonl')}
                setup = cleanup = None
                evidence = {'tools': [], 'requests': [], 'messages': [], 'trajectory': []}
                errors, proposed, completed, steps, termination = [], None, None, None, 'infrastructure_error'
                try:
                    async with open_target(trial.target, values) as client:
                        setup = await run_actions(trial.scenario.setup, client, values, 'setup')
                        errors.extend(setup.errors)
                        try:
                            if not setup.errors:
                                engine = resolve_engine(campaign.engine)
                                metrics, evidence = await engine(
                                    live_provider(campaign.model) if campaign.model else ScriptedProvider('comply'),
                                    campaign.model or 'scripted/comply', client, campaign.step_budget,
                                    system_prompt=trial.scenario.exercise.system,
                                    user_prompt=trial.scenario.exercise.user,
                                    allowed_tools=resolve_value(trial.scenario.exercise.tools, values))
                                termination = metrics['termination']
                                proposal = evaluate_proposals(trial.scenario.proposal_rules,
                                                              evidence['trajectory'], values)
                                proposed, steps = proposal.proposed, proposal.steps_to_propose
                                checked = await verify(trial.scenario.verify.checks,
                                                       trial.scenario.verify.mode, client, values)
                                completed = checked.completed; errors.extend(checked.errors)
                                evidence |= {'proposal_evaluation': proposal.matches,
                                             'verification': checked.checks}
                        finally:
                            cleanup = await run_actions(trial.scenario.cleanup, client, values,
                                                        'cleanup', best_effort=True)
                            errors.extend(cleanup.errors)
                except BaseException as exc:
                    errors.append({'phase': 'connection_or_agent', 'detail': f'{type(exc).__name__}: {exc}'})
                    evidence['traceback'] = traceback.format_exc()
                document = {'schema_version': SCHEMA_VERSION, 'run_id': run_id,
                            'trial_id': trial.trial_id, 'attempt_id': attempt_id,
                            'target': trial.target.id, 'scenario': trial.scenario.id,
                            'canary': canary, 'setup': setup.records if setup else [],
                            'cleanup': cleanup.records if cleanup else [], **evidence}
                (out / evidence_name).write_text(json.dumps(document, indent=2) + '\n')
                terminal = setup is not None and not setup.errors and proposed is not None
                row = {'schema_version': SCHEMA_VERSION, 'run_id': run_id, 'trial_id': trial.trial_id,
                       'attempt_id': attempt_id, 'target': trial.target.id, 'scenario': trial.scenario.id,
                       'trial_index': trial.index, 'engine': campaign.engine, 'proposed': proposed,
                       'completed': completed,
                       'proposed_not_completed': proposed and completed is False
                       if proposed is not None and completed is not None else None,
                       'steps_to_propose': steps, 'termination': termination, 'errors': errors,
                       'evidence': evidence_name,
                       'evidence_status': 'complete' if terminal else 'incomplete'}
                stream.write(json.dumps(row) + '\n'); stream.flush(); rows.append(row); counts[trial.trial_id] += 1
                if not terminal:
                    raise RuntimeError(f'{trial.trial_id} did not reach a terminal outcome')
    write_summary(bundle, out, rows)


def build_parser():
    parser = argparse.ArgumentParser(prog='elengtis')
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('validate'); check.add_argument('config', type=Path)
    schema = commands.add_parser('schema'); schema.add_argument('--out', type=Path, required=True)
    run = commands.add_parser('run'); run.add_argument('--config', type=Path); run.add_argument('--out', type=Path, required=True)
    run.add_argument('--resume', action='store_true'); run.add_argument('--trials', type=int)
    run.add_argument('--step-budget', type=int); run.add_argument('--engine', choices=tuple(ENGINES)); run.add_argument('--model')
    return parser


def main():
    parser, args = build_parser(), None
    args = parser.parse_args()
    try:
        if args.command == 'schema': write_schemas(args.out); return
        if args.command == 'validate':
            print('\n'.join(t.trial_id for t in plan_trials(load_campaign(args.config)))); return
        if args.resume:
            if args.config or any(getattr(args, k) is not None for k in ('trials', 'step_budget', 'engine', 'model')):
                raise ValueError('--resume uses recorded configuration; remove overrides')
            bundle, run_id, prior = load_resume(args.out)
        else:
            if not args.config: raise ValueError('--config is required for a new run')
            overrides = {k: getattr(args, k) for k in ('trials', 'step_budget', 'engine', 'model')}
            bundle, run_id, prior = load_campaign(args.config, overrides), None, ()
        asyncio.run(run_matrix(bundle, args.out, run_id, prior))
    except KeyboardInterrupt:
        parser.exit(130, 'Interrupted; partial output retained. Continue with run --resume.\n')
    except Exception as exc:
        parser.exit(1, f'Run failed: {exc}\n')


if __name__ == '__main__': main()
