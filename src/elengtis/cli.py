"""Run a bounded synthetic matrix; every trial has a fresh fixture process."""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone, timedelta
import hashlib
from importlib import import_module
from importlib.metadata import version
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import traceback
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from elengtis.reference import POLICIES, ScriptedProvider
from elengtis.scenario import SCENARIO_ID

SCHEMA_VERSION = 2
METRICS_VERSION = 1
REQUEST_TIMEOUT_SECONDS = 10
TRIAL_TIMEOUT_SECONDS = 30
LIVE_TRIAL_TIMEOUT_SECONDS = 300
DEFAULT_TRIALS = 1
DEFAULT_STEP_BUDGET = 4
MAX_TRIALS = 100
MAX_STEP_BUDGET = 100
DEFAULT_ENGINE = 'reference'
ENGINES = {'reference': ('reference', 'run_episode'), 'graph': ('graph', 'run_episode'),
           'langchain': ('adapters', 'run_episode'),
           'create_agent': ('adapters', 'run_agent_episode')}
PACKAGES = ('mcp', 'langchain', 'langchain-core', 'langgraph', 'langchain-mcp-adapters',
            'langchain-openrouter')
RESUME_CONFLICTS = ('config', 'policies', 'trials', 'step_budget', 'engine', 'model')


def live_provider(model):
    """Imported on demand; the OpenRouter integration is only needed for live runs."""
    from langchain_openrouter import ChatOpenRouter  # pylint: disable=import-outside-toplevel
    from elengtis.adapters import LiveProvider  # pylint: disable=import-outside-toplevel
    # Sampling parameters are left at the provider's defaults and recorded as such;
    # some reasoning models reject an explicit temperature.
    return LiveProvider(ChatOpenRouter(model=model))


def resolve_engine(name):
    """Imported on demand, so the reference engine loads no framework."""
    module, attribute = ENGINES[name]
    return getattr(import_module(f'elengtis.{module}'), attribute)


def load_config(path, **overrides):
    config = json.loads(path.read_text()) if path else {}
    allowed = {'policies', 'trials', 'step_budget', 'engine', 'model'}
    if not isinstance(config, dict) or set(config) - allowed:
        raise ValueError(f'Config must be an object with fields from {sorted(allowed)}')
    merged = {'policies': list(POLICIES), 'trials': DEFAULT_TRIALS, 'model': None,
              'step_budget': DEFAULT_STEP_BUDGET, 'engine': DEFAULT_ENGINE} | config | {
                  key: value for key, value in overrides.items() if value is not None}
    policies = merged['policies']
    if (not isinstance(policies, list) or not policies or
            any(not isinstance(p, str) or p not in POLICIES for p in policies) or
            len(set(policies)) != len(policies)):
        raise ValueError(f'policies must be a nonempty unique list from {POLICIES}')
    for key, limit in (('trials', MAX_TRIALS), ('step_budget', MAX_STEP_BUDGET)):
        if type(merged[key]) is not int or not 1 <= merged[key] <= limit:
            raise ValueError(f'{key} must be an integer from 1 to {limit}')
    if merged['engine'] not in ENGINES:
        raise ValueError(f'engine must be one of {tuple(ENGINES)}')
    if merged['model'] is not None:
        if not isinstance(merged['model'], str) or not merged['model']:
            raise ValueError('model must be a nonempty string')
        if config.get('policies') is not None or overrides.get('policies') is not None:
            raise ValueError('policies are scripted fixtures and cannot be combined with model')
        # A live run has no scripted policy; trials are repeats of one live condition.
        merged['policies'] = ['live']
    return merged


def provenance():
    package = Path(__file__).parent
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(package.glob('*.py'))}
    root = package.parent.parent
    revision = None
    dirty = None
    if (root / 'pyproject.toml').exists():
        result = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                capture_output=True, text=True)
        if result.returncode == 0:
            revision = result.stdout.strip()
            status = subprocess.run(['git', '-C', str(root), 'status', '--porcelain'],
                                    capture_output=True, text=True)
            dirty = bool(status.stdout) if status.returncode == 0 else None
    lock = root / 'uv.lock'
    return {'python': platform.python_version(), 'platform': platform.platform(),
            'package_version': version('elengtis'),
            'package_versions': {name: version(name) for name in PACKAGES},
            'code_revision': revision, 'working_tree_dirty': dirty,
            'source_sha256': hashes,
            'lock_sha256': hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else None}


def source_drift(out):
    """Warn in the readable summary when attempts did not all run the same code."""
    manifest = json.loads((out / 'manifest.json').read_text())
    original = manifest['environment']['source_sha256']
    if any(entry['environment']['source_sha256'] != original
           for entry in manifest.get('resumes', [])):
        return ['NOTE: sources changed between attempts; see manifest.json resumes']
    return []


def load_resume(out, args):
    """Read a matrix's own manifest and rows, and record this resumption in the manifest."""
    supplied = [f'--{name.replace("_", "-")}' for name in RESUME_CONFLICTS
                if getattr(args, name) is not None]
    if supplied:
        raise ValueError('--resume takes its configuration from the manifest; '
                         f'remove {" ".join(supplied)}')
    manifest_path, rows_path = out / 'manifest.json', out / 'runs.jsonl'
    if not manifest_path.exists() or not rows_path.exists():
        raise ValueError(f'{out} has no manifest.json and runs.jsonl to resume')
    manifest = json.loads(manifest_path.read_text())
    for key, current in (('schema_version', SCHEMA_VERSION), ('metrics_version', METRICS_VERSION)):
        if manifest.get(key) != current:
            raise ValueError(f'{key} is {manifest.get(key)} but this build writes {current}; '
                             'resuming would mix rows that do not mean the same thing')
    manifest.setdefault('resumes', []).append(
        {'resumed_at': datetime.now(timezone.utc).isoformat(), 'environment': provenance()})
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
    rows = [json.loads(line) for line in rows_path.read_text().splitlines() if line.strip()]
    return manifest['config'], manifest['run_id'], rows


def write_summary(config, out, attempts, resuming):
    """Readable counts over one result per trial; a retry replaces an attempt, never adds a trial."""
    latest = {row['trial_id']: row for row in attempts if row['evidence_status'] == 'complete'}
    results, live = list(latest.values()), config['model']
    summary = '\n'.join([
        f'Synthetic scenario via the {config["engine"]} engine, model {live or "scripted"}.'
        + ('' if live else ' These are not model resistance estimates.'),
        f'trials: {len(results)}',
        *[f'{metric}: {sum(bool(row[metric]) for row in results)}/{len(results)}'
          for metric in ('proposed', 'completed', 'proposed_not_completed')],
        f'trials with errors: {sum(bool(row["errors"]) for row in results)}/{len(results)}',
        *([f'attempts: {len(attempts)}, of which {len(attempts) - len(results)} were retried']
          if len(attempts) > len(results) else []),
        *(source_drift(out) if resuming else []),
        *[f'{row["trial_id"]}: {row["termination"]}' for row in results]])
    (out / 'summary.txt').write_text(summary + '\n')
    print(summary)


async def run_matrix(config, out, run_id=None, prior=()):
    """Run every trial not already completed in `prior`; a given run_id means resuming."""
    run_episode = resolve_engine(config['engine'])
    live = config['model']
    deadline = LIVE_TRIAL_TIMEOUT_SECONDS if live else TRIAL_TIMEOUT_SECONDS
    resuming = run_id is not None
    run_id = run_id or str(uuid.uuid4())
    done = {row['trial_id'] for row in prior if row['evidence_status'] == 'complete'}
    attempts = Counter(row['trial_id'] for row in prior)
    if not resuming:
        metadata = {'schema_version': SCHEMA_VERSION, 'metrics_version': METRICS_VERSION, 'run_id': run_id,
                    'started_at': datetime.now(timezone.utc).isoformat(),
                    'config': config, 'environment': provenance(),
                    'scenario': SCENARIO_ID, 'provider': live or 'scripted',
                    'sampling': 'provider defaults' if live else None,
                    'request_timeout_seconds': REQUEST_TIMEOUT_SECONDS,
                    'trial_timeout_seconds': deadline}
        (out / 'manifest.json').write_text(json.dumps(metadata, indent=2) + '\n')
    rows = []
    with (out / 'runs.jsonl').open('a' if resuming else 'x') as stream:
        for policy in config['policies']:
            for trial in range(config['trials']):
                trial_id = f'{policy}-{trial}'
                if trial_id in done:
                    continue
                # A retry must not overwrite the evidence explaining the failed attempt.
                suffix = f'.retry-{attempts[trial_id]}' if attempts[trial_id] else ''
                row = {'schema_version': SCHEMA_VERSION, 'metrics_version': METRICS_VERSION, 'run_id': run_id,
                       'trial_id': trial_id, 'attempt_id': str(uuid.uuid4()),
                       'engine': config['engine'], 'policy': policy, 'trial': trial,
                       'started_at': datetime.now(timezone.utc).isoformat()}
                with tempfile.TemporaryDirectory(prefix='elengtis-') as tmp:
                    collector = Path(tmp) / 'collector.jsonl'
                    args = ['-m', 'elengtis.server', '--collector', str(collector)]
                    if policy == 'tool_error':
                        args.append('--fail-submit')
                    params = StdioServerParameters(command=sys.executable, args=args, env={})
                    # Only the bundled, trusted fixture is launched. No user commands.
                    failure = None
                    try:
                        with (out / f'{trial_id}{suffix}.stderr.log').open('w') as log:
                            async with asyncio.timeout(deadline):
                                async with stdio_client(params, errlog=log) as (read, write):
                                    async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=REQUEST_TIMEOUT_SECONDS)) as client:
                                        await client.initialize()
                                        metrics, evidence = await run_episode(
                                            live_provider(live) if live else ScriptedProvider(policy),
                                            live or f'scripted/{policy}', client,
                                            collector, config['step_budget'])
                    except BaseException as exc:  # pylint: disable=broad-exception-caught
                        # Persist unknown outcomes before propagating failure/cancellation.
                        failure = exc
                        leaves = [exc]
                        while any(isinstance(e, BaseExceptionGroup) for e in leaves):
                            leaves = [child for e in leaves for child in
                                      (e.exceptions if isinstance(e, BaseExceptionGroup) else [e])]
                        # A Ctrl-C mid-trial kills the fixture first, so anyio reports the
                        # resulting transport error and the interrupt is not recoverable here.
                        # Installing a SIGINT handler to catch it disables asyncio's own
                        # cancellation and stops Ctrl-C aborting the matrix at all.
                        reason = ('interrupted' if any(isinstance(e, (KeyboardInterrupt, asyncio.CancelledError)) for e in leaves)
                                  else 'timeout' if any(isinstance(e, TimeoutError) for e in leaves)
                                  else 'infrastructure_error')
                        metrics = {key: None for key in ('proposed', 'completed', 'proposed_not_completed',
                                                        'recovery', 'steps_to_propose', 'model_turns', 'tool_calls')}
                        # Some transport exceptions carry no message; keep the type.
                        metrics.update(termination=reason,
                                       errors=[{'kind': reason, 'detail': f'{type(e).__name__}: {e}'}
                                               for e in leaves])
                        evidence = {'failure': ''.join(traceback.format_exception(exc)),
                                    'collector_raw': collector.read_text() if collector.exists() else ''}
                    row['evidence_status'] = 'incomplete' if failure else 'complete'
                    row.update(metrics)
                    row['evidence'] = f'{trial_id}{suffix}.json'
                    (out / row['evidence']).write_text(json.dumps(evidence, indent=2) + '\n')
                row['finished_at'] = datetime.now(timezone.utc).isoformat()
                stream.write(json.dumps(row) + '\n')
                stream.flush()
                rows.append(row)
                if failure:
                    if isinstance(failure, (KeyboardInterrupt, asyncio.CancelledError)):
                        raise failure
                    raise RuntimeError(f'Trial {trial_id} failed; see its evidence') from failure
    write_summary(config, out, [*prior, *rows], resuming)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--step-budget', type=int, help='Model-turn limit per trial (overrides config)')
    parser.add_argument('--trials', type=int, help='Trials per policy (overrides config)')
    parser.add_argument('--policies', nargs='+', choices=POLICIES, help='Policies to run (overrides config)')
    parser.add_argument('--engine', choices=tuple(ENGINES), help='Execution engine (overrides config)')
    parser.add_argument('--model', help='OpenRouter model id for a live run, replacing the scripted policies')
    parser.add_argument('--resume', action='store_true',
                        help='Continue an interrupted matrix in --out, skipping completed trials')
    parser.add_argument('--out', type=Path, required=True,
                        help='Output directory; new and never overwritten unless --resume')
    args = parser.parse_args()
    run_id, prior = None, ()
    try:
        if args.resume:
            config, run_id, prior = load_resume(args.out, args)
        else:
            config = load_config(args.config, step_budget=args.step_budget, trials=args.trials,
                                 policies=args.policies, engine=args.engine, model=args.model)
            args.out.mkdir(parents=True, exist_ok=False)
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    try:
        asyncio.run(run_matrix(config, args.out, run_id, prior))
    except KeyboardInterrupt:
        parser.exit(130, 'Interrupted; partial output retained. Continue with --resume.\n')
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Report unhandled run failures at the CLI boundary with a nonzero exit.
        parser.exit(1, f'Run failed: {exc}. Partial output retained; continue with --resume.\n')
