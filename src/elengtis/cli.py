"""Validate and run declarative MCP auditing campaigns."""
import argparse, asyncio, hashlib, json, platform, subprocess, sys, tempfile, traceback, uuid
from collections import Counter
from datetime import datetime, timezone
from importlib import import_module
from importlib.resources import files
from importlib.metadata import version
from pathlib import Path

from elengtis.config import Campaign, CampaignBundle, Scenario, load_campaign, plan_trials, write_schemas
from elengtis.budget import BudgetExceeded
from elengtis.reference import ScriptedProvider
from elengtis.scenario import evaluate_proposals, resolve_prompt, resolve_value, run_actions, verify
from elengtis.transports import open_target

SCHEMA_VERSION, METRICS_VERSION, MAX_CONSECUTIVE_TARGET_FAILURES = 4, 2, 3
ENGINES = {'reference': ('reference', 'run_episode'), 'graph': ('graph', 'run_episode'),
           'langchain': ('adapters', 'run_episode'), 'create_agent': ('adapters', 'run_agent_episode')}
PACKAGES = ('mcp', 'pydantic', 'PyYAML', 'httpx', 'langchain', 'langchain-core', 'langgraph',
            'langchain-mcp-adapters', 'langchain-openrouter')


def resolve_engine(name):
    module, attribute = ENGINES[name]
    return getattr(import_module(f'elengtis.{module}'), attribute)


def live_provider(model, generation, budget=None):
    from langchain_openrouter import ChatOpenRouter
    from elengtis.adapters import LiveProvider
    return LiveProvider(ChatOpenRouter(model=model, **generation), budget)


def usage_totals(usage):
    totals = {key: sum(item.get(key, 0) for item in usage if item.get(key) is not None)
              for key in ('input_tokens', 'output_tokens', 'total_tokens')}
    costs = [item.get('cost_usd') for item in usage if item.get('cost_usd') is not None]
    charges = [item.get('budget_charge_usd') for item in usage
               if item.get('budget_charge_usd') is not None]
    totals['cost_usd'] = sum(costs) if costs else None
    totals['budget_charge_usd'] = sum(charges) if charges else None
    totals['unknown_costs'] = sum(item.get('cost_usd') is None for item in usage)
    return totals


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


def configuration_provenance(bundle):
    scenarios = {scenario.id: hashlib.sha256(json.dumps(
        scenario.model_dump(mode='json', by_alias=True), sort_keys=True).encode()).hexdigest()
                 for scenario in bundle.scenarios}
    return {'campaign_sha256': hashlib.sha256(bundle.path.read_bytes()).hexdigest(),
            'scenario_sha256': scenarios}


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


async def run_matrix(bundle, out, run_id=None, prior=(), budget=None):
    campaign, resuming = bundle.campaign, run_id is not None
    run_id = run_id or str(uuid.uuid4())
    done = {r['trial_id'] for r in prior if r['evidence_status'] == 'complete'}
    counts, rows = Counter(r['trial_id'] for r in prior), list(prior)
    target_failures = Counter()
    incomplete = False
    if not resuming:
        out.mkdir(parents=True)
        manifest = {'schema_version': SCHEMA_VERSION, 'metrics_version': METRICS_VERSION,
                    'run_id': run_id, 'started_at': datetime.now(timezone.utc).isoformat(),
                    'campaign': campaign.model_dump(mode='json', by_alias=True),
                    'scenarios': [s.model_dump(mode='json', by_alias=True) for s in bundle.scenarios],
                    'configuration': configuration_provenance(bundle),
                    'experiment': {'id': campaign.experiment_id, 'block': campaign.block,
                                   'order': campaign.order, 'model_id': campaign.model_id,
                                   'generation': campaign.generation},
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
                evidence = {'tools': [], 'requests': [], 'messages': [], 'trajectory': [], 'usage': []}
                errors, proposed, completed, steps, termination = [], None, None, None, 'infrastructure_error'
                usage, model_turns = [], None
                tool_calls = None
                budget_stopped = False
                budget_before = budget.committed if budget else 0.0
                try:
                    async with open_target(trial.target, values) as client:
                        setup = await run_actions(trial.scenario.setup, client, values, 'setup')
                        errors.extend(setup.errors)
                        try:
                            if not setup.errors:
                                engine = resolve_engine(campaign.engine)
                                metrics, evidence = await engine(
                                    (live_provider(campaign.model, campaign.generation, budget)
                                     if campaign.model else ScriptedProvider('comply')),
                                    campaign.model or 'scripted/comply', client, campaign.step_budget,
                                    system_prompt=resolve_prompt(trial.scenario.exercise.system, values),
                                    user_prompt=resolve_prompt(trial.scenario.exercise.user, values),
                                    allowed_tools=resolve_value(trial.scenario.exercise.tools, values))
                                termination = metrics['termination']
                                model_turns, tool_calls = metrics['model_turns'], metrics['tool_calls']
                                usage = metrics.get('usage', [])
                                errors.extend(metrics.get('errors', []))
                                if termination not in {'provider_error', 'budget_exhausted'}:
                                    proposal = evaluate_proposals(trial.scenario.proposal_rules,
                                                                  evidence['trajectory'], values)
                                    proposed, steps = proposal.proposed, proposal.steps_to_propose
                                    checked = await verify(trial.scenario.verify.checks,
                                                           trial.scenario.verify.mode, client, values)
                                    completed = checked.completed; errors.extend(checked.errors)
                                    evidence |= {'proposal_evaluation': proposal.matches,
                                                 'verification': checked.checks}
                        except BudgetExceeded as exc:
                            termination = 'budget_exhausted'
                            budget_stopped = True
                            errors.append({'kind': 'budget_exceeded', 'detail': str(exc)})
                            evidence['traceback'] = traceback.format_exc()
                        finally:
                            cleanup = await run_actions(trial.scenario.cleanup, client, values,
                                                        'cleanup', best_effort=True)
                            errors.extend(cleanup.errors)
                except BudgetExceeded as exc:
                    termination = 'budget_exhausted'
                    budget_stopped = True
                    errors.append({'kind': 'budget_exceeded', 'detail': str(exc)})
                except Exception as exc:
                    termination = 'infrastructure_error'
                    errors.append({'phase': 'connection_or_agent', 'detail': f'{type(exc).__name__}: {exc}'})
                    evidence['traceback'] = traceback.format_exc()
                evidence['usage'] = usage
                document = {'schema_version': SCHEMA_VERSION, 'run_id': run_id,
                            'trial_id': trial.trial_id, 'attempt_id': attempt_id,
                            'target': trial.target.id, 'scenario': trial.scenario.id,
                            'canary': canary, 'setup': setup.records if setup else [],
                            'cleanup': cleanup.records if cleanup else [], **evidence, 'errors': errors}
                (out / evidence_name).write_text(json.dumps(document, indent=2) + '\n')
                fatal = (termination in {'provider_error', 'infrastructure_error', 'budget_exhausted'} or
                         any(error.get('phase') in {'setup', 'verification', 'cleanup', 'connection_or_agent'}
                             for error in errors))
                terminal = (setup is not None and not setup.errors and proposed is not None and
                            completed is not None and not fatal)
                failure_class = None if terminal else (
                    'provider_error' if termination == 'provider_error' else
                    'budget_exceeded' if termination == 'budget_exhausted' else
                    'verification_error' if any(error.get('phase') == 'verification' for error in errors)
                    else 'infrastructure_error')
                totals = usage_totals(usage)
                totals['budget_charge_usd'] = ((budget.committed - budget_before) if budget
                                               else totals['budget_charge_usd'])
                row = {'schema_version': SCHEMA_VERSION, 'run_id': run_id, 'trial_id': trial.trial_id,
                       'attempt_id': attempt_id, 'target': trial.target.id, 'scenario': trial.scenario.id,
                       'trial_index': trial.index, 'engine': campaign.engine, 'proposed': proposed,
                       'completed': completed,
                       'model_id': campaign.model_id, 'model': campaign.model,
                       'model_turns': model_turns, 'tool_calls': tool_calls, 'usage': totals,
                       'proposed_not_completed': proposed and completed is False
                       if proposed is not None and completed is not None else None,
                       'steps_to_propose': steps, 'termination': termination, 'errors': errors,
                       'failure_class': failure_class, 'evidence': evidence_name,
                       'evidence_status': 'complete' if terminal else 'incomplete'}
                stream.write(json.dumps(row) + '\n'); stream.flush(); rows.append(row); counts[trial.trial_id] += 1
                if budget_stopped:
                    raise BudgetExceeded('budget cap reached; partial results retained for resume')
                if terminal:
                    target_failures[trial.target.id] = 0
                else:
                    incomplete = True
                    target_failures[trial.target.id] += 1
                    if target_failures[trial.target.id] >= MAX_CONSECUTIVE_TARGET_FAILURES:
                        raise RuntimeError(f'{trial.target.id} failed '
                                           f'{MAX_CONSECUTIVE_TARGET_FAILURES} consecutive trials')
    if incomplete:
        raise RuntimeError('campaign has incomplete attempts; fix the target and resume')
    write_summary(bundle, out, rows)


def build_parser():
    parser = argparse.ArgumentParser(prog='elengtis')
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('validate'); check.add_argument('config', type=Path)
    schema = commands.add_parser('schema'); schema.add_argument('--out', type=Path, required=True)
    analyze = commands.add_parser('analyze'); analyze.add_argument('--input', type=Path, action='append', required=True)
    analyze.add_argument('--out', type=Path, required=True)
    run = commands.add_parser('run'); run.add_argument('--config', type=Path); run.add_argument('--out', type=Path, required=True)
    run.add_argument('--resume', action='store_true'); run.add_argument('--trials', type=int)
    run.add_argument('--step-budget', type=int); run.add_argument('--engine', choices=tuple(ENGINES)); run.add_argument('--model')
    example = commands.add_parser('example'); example.add_argument('--out', type=Path, required=True)
    return parser


def main():
    parser, args = build_parser(), None
    argv = sys.argv[1:]
    if argv and argv[0].startswith('-'):
        argv.insert(0, 'run')
    args = parser.parse_args(argv)
    try:
        if args.command == 'schema': write_schemas(args.out); return
        if args.command == 'analyze':
            from elengtis.analysis import write_report
            write_report(args.input, args.out); return
        if args.command == 'validate':
            print('\n'.join(t.trial_id for t in plan_trials(load_campaign(args.config)))); return
        if args.command == 'example':
            bundle, run_id, prior = load_campaign(Path(str(files('elengtis').joinpath('examples/offline.yaml')))), None, ()
            asyncio.run(run_matrix(bundle, args.out, run_id, prior)); return
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
