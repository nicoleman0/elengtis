"""Run a bounded synthetic matrix; every trial has a fresh fixture process."""
import argparse
import asyncio
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
DEFAULT_TRIALS = 1
DEFAULT_STEP_BUDGET = 4
MAX_TRIALS = 100
MAX_STEP_BUDGET = 100
DEFAULT_ENGINE = 'reference'
ENGINES = {'reference': ('reference', 'run_episode'), 'graph': ('graph', 'run_episode'),
           'langchain': ('adapters', 'run_episode'),
           'create_agent': ('adapters', 'run_agent_episode')}
PACKAGES = ('mcp', 'langchain', 'langchain-core', 'langgraph', 'langchain-mcp-adapters')


def resolve_engine(name):
    """Imported on demand, so the reference engine loads no framework."""
    module, attribute = ENGINES[name]
    return getattr(import_module(f'elengtis.{module}'), attribute)


def load_config(path, **overrides):
    config = json.loads(path.read_text()) if path else {}
    if not isinstance(config, dict) or set(config) - {'policies', 'trials', 'step_budget', 'engine'}:
        raise ValueError('Config must be an object with policies, trials, step_budget and/or engine')
    merged = {'policies': list(POLICIES), 'trials': DEFAULT_TRIALS,
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


async def run_matrix(config, out):
    run_episode = resolve_engine(config['engine'])
    run_id = str(uuid.uuid4())
    metadata = {'schema_version': SCHEMA_VERSION, 'metrics_version': METRICS_VERSION, 'run_id': run_id,
                'started_at': datetime.now(timezone.utc).isoformat(),
                'config': config, 'environment': provenance(),
                'scenario': SCENARIO_ID, 'provider': 'scripted',
                'request_timeout_seconds': REQUEST_TIMEOUT_SECONDS,
                'trial_timeout_seconds': TRIAL_TIMEOUT_SECONDS}
    (out / 'manifest.json').write_text(json.dumps(metadata, indent=2) + '\n')
    rows = []
    with (out / 'runs.jsonl').open('x') as stream:
        for policy in config['policies']:
            for trial in range(config['trials']):
                trial_id = f'{policy}-{trial}'
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
                        with (out / f'{trial_id}.stderr.log').open('w') as log:
                            async with asyncio.timeout(TRIAL_TIMEOUT_SECONDS):
                                async with stdio_client(params, errlog=log) as (read, write):
                                    async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=REQUEST_TIMEOUT_SECONDS)) as client:
                                        await client.initialize()
                                        metrics, evidence = await run_episode(
                                            ScriptedProvider(policy), f'scripted/{policy}', client,
                                            collector, config['step_budget'])
                    except BaseException as exc:  # pylint: disable=broad-exception-caught
                        # Persist unknown outcomes before propagating failure/cancellation.
                        failure = exc
                        leaves = [exc]
                        while any(isinstance(e, BaseExceptionGroup) for e in leaves):
                            leaves = [child for e in leaves for child in
                                      (e.exceptions if isinstance(e, BaseExceptionGroup) else [e])]
                        reason = ('interrupted' if any(isinstance(e, (KeyboardInterrupt, asyncio.CancelledError)) for e in leaves)
                                  else 'timeout' if any(isinstance(e, TimeoutError) for e in leaves)
                                  else 'infrastructure_error')
                        metrics = {key: None for key in ('proposed', 'completed', 'proposed_not_completed',
                                                        'recovery', 'steps_to_propose', 'model_turns', 'tool_calls')}
                        metrics.update(termination=reason, errors=[{'kind': reason, 'detail': str(e)} for e in leaves])
                        evidence = {'failure': ''.join(traceback.format_exception(exc)),
                                    'collector_raw': collector.read_text() if collector.exists() else ''}
                    row['evidence_status'] = 'incomplete' if failure else 'complete'
                    row.update(metrics)
                    row['evidence'] = f'{trial_id}.json'
                    (out / row['evidence']).write_text(json.dumps(evidence, indent=2) + '\n')
                row['finished_at'] = datetime.now(timezone.utc).isoformat()
                stream.write(json.dumps(row) + '\n')
                stream.flush()
                rows.append(row)
                if failure:
                    if isinstance(failure, (KeyboardInterrupt, asyncio.CancelledError)):
                        raise failure
                    raise RuntimeError(f'Trial {trial_id} failed; see its evidence') from failure
    summary = '\n'.join([
        f'Synthetic scripted baseline via the {config["engine"]} engine — '
        'these are not model resistance estimates.',
        f'trials: {len(rows)}',
        *[f'{metric}: {sum(bool(row[metric]) for row in rows)}/{len(rows)}'
          for metric in ('proposed', 'completed', 'proposed_not_completed')],
        f'trials with errors: {sum(bool(row["errors"]) for row in rows)}/{len(rows)}',
        *[f'{row["trial_id"]}: {row["termination"]}' for row in rows]])
    (out / 'summary.txt').write_text(summary + '\n')
    print(summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--step-budget', type=int, help='Model-turn limit per trial (overrides config)')
    parser.add_argument('--trials', type=int, help='Trials per policy (overrides config)')
    parser.add_argument('--policies', nargs='+', choices=POLICIES, help='Policies to run (overrides config)')
    parser.add_argument('--engine', choices=tuple(ENGINES), help='Execution engine (overrides config)')
    parser.add_argument('--out', type=Path, required=True, help='New output directory (never overwritten)')
    args = parser.parse_args()
    try:
        config = load_config(args.config, step_budget=args.step_budget, trials=args.trials,
                             policies=args.policies, engine=args.engine)
        args.out.mkdir(parents=True, exist_ok=False)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    try:
        asyncio.run(run_matrix(config, args.out))
    except KeyboardInterrupt:
        parser.exit(130, 'Interrupted; partial output retained. Resume is not implemented.\n')
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Report unhandled run failures at the CLI boundary with a nonzero exit.
        parser.exit(1, f'Run failed: {exc}. Partial output retained.\n')
