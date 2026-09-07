"""Declarative scenario evaluation plus the legacy synthetic fixture contract."""
from dataclasses import dataclass
import asyncio
import base64
import hashlib
import os
import re
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

import httpx

from elengtis.config import ContainerSqliteAction, HttpAction

@dataclass(frozen=True)
class ProposalEvaluation:
    proposed: bool
    steps_to_propose: int | None
    matches: list[dict]


@dataclass(frozen=True)
class SafetyEvaluation:
    passed: bool | None
    violations: list[dict]


@dataclass
class ActionRun:
    records: list[dict]
    errors: list[dict]


@dataclass
class VerificationResult:
    completed: bool | None
    checks: list[dict]
    errors: list[dict]


def redact(value, secrets):
    if isinstance(value, dict):
        return {key: redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, '[REDACTED]')
    return value


def resolve_value(value, values):
    """Resolve deliberately small typed references inside JSON-like values."""
    if isinstance(value, list):
        return [resolve_value(item, values) for item in value]
    if isinstance(value, dict):
        if len(value) == 1 and next(iter(value), None) in ('binding', 'capture', 'runner'):
            name = next(iter(value.values()))
            if name not in values:
                raise ValueError(f'Unknown value reference {name}')
            return values[name]
        return {key: resolve_value(item, values) for key, item in value.items()}
    return value


def resolve_prompt(template, values):
    class StrictValues(dict):
        def __missing__(self, key):
            raise ValueError(f'Unknown prompt value {key}')
    return template.format_map(StrictValues(values))


def pointer(document, path):
    """Read an RFC 6901 JSON Pointer without an expression-language dependency."""
    if path == '':
        return document
    if not path.startswith('/'):
        raise ValueError(f'Invalid JSON Pointer {path!r}')
    current = document
    for raw in path[1:].split('/'):
        key = raw.replace('~1', '/').replace('~0', '~')
        current = current[int(key)] if isinstance(current, list) else current[key]
    return current


def compare(actual, op, expected):
    if op == 'equals':
        return actual == expected
    if op == 'contains':
        return expected in actual
    if op == 'matches':
        return isinstance(actual, str) and re.search(expected, actual) is not None
    raise ValueError(f'Unknown comparison operator {op}')


def evaluate_proposals(rules, trajectory, values):
    """Classify recorded structured calls; model prose is intentionally ignored."""
    matches = []
    for turn in trajectory:
        for call in turn['calls']:
            for rule in rules:
                if call['name'] != resolve_value(rule.tool, values):
                    continue
                verdicts = []
                for predicate in rule.predicates:
                    try:
                        actual = pointer(call['arguments'], predicate.path)
                        expected = resolve_value(predicate.value, values)
                        matched = compare(actual, predicate.op, expected)
                    except (KeyError, IndexError, TypeError, ValueError):
                        actual, expected, matched = None, resolve_value(predicate.value, values), False
                    verdicts.append({'path': predicate.path, 'op': predicate.op,
                                     'expected': expected, 'actual': actual, 'matched': matched})
                if all(item['matched'] for item in verdicts):
                    matches.append({'rule_id': rule.id, 'call_id': call['id'],
                                    'step': turn['step'], 'predicates': verdicts})
    first = min((match['step'] for match in matches), default=None)
    return ProposalEvaluation(bool(matches), first, matches)


def evaluate_safety(rules, trajectory, values):
    """Classify forbidden structured calls, or mark safety as not applicable."""
    if not rules:
        return SafetyEvaluation(None, [])
    matches = evaluate_proposals(rules, trajectory, values).matches
    return SafetyEvaluation(not matches, matches)


async def _run_action(action, client, values, http_client):
    if isinstance(action, HttpAction):
        headers = {name: os.environ[ref.env] for name, ref in action.headers.items()}
        own_client = http_client is None
        http_client = http_client or httpx.AsyncClient(follow_redirects=False, timeout=10)
        try:
            response = await http_client.request(
                action.method, resolve_value(action.url, values), headers=headers,
                json=resolve_value(action.body, values))
            try:
                body = response.json()
            except ValueError:
                body = response.text[:100_000]
            raw = redact({'status': response.status_code, 'body': body,
                          'truncated': len(response.content) > 100_000}, set(headers.values()))
        finally:
            if own_client:
                await http_client.aclose()
        shown = {'method': action.method, 'url': action.url,
                 'headers': {name: {'env': ref.env} for name, ref in action.headers.items()}}
    elif isinstance(action, ContainerSqliteAction):
        if not hasattr(client, '_elengtis_container_name'):
            raise ValueError('container_sqlite_query requires an isolated container target')
        if ';' in action.query or not re.match(r'^\s*select\b',
                                               action.query, re.IGNORECASE):
            raise ValueError('container_sqlite_query accepts one read-only SELECT statement')
        parameters = resolve_value(action.parameters, values)
        if len(parameters) > 32:
            raise ValueError('container_sqlite_query accepts at most 32 parameters')
        artifact_dir = Path(values.get('evidence_dir', values['trial_dir']))
        artifact_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = artifact_dir / f'{values.get("trial_id", action.id)}-{values.get("attempt_id", "attempt")}-{action.id}.snapshot.db'
        container_snapshot = f'/tmp/elengtis-snapshot-{uuid.uuid4().hex}.db'
        helper = (
            "import {DatabaseSync,backup} from 'node:sqlite';"
            "import {readFileSync,unlinkSync} from 'node:fs';"
            "const [source,destination]=process.argv.slice(1);"
            "const db=new DatabaseSync(source,{readOnly:true});"
            "try { await backup(db,destination); "
            "process.stdout.write(readFileSync(destination).toString('base64')); } "
            "finally { db.close(); try { unlinkSync(destination); } catch {} }"
        )
        command = ['docker', 'exec', '--user',
                   getattr(client, '_elengtis_container_user', '0:0'),
                   client._elengtis_container_name, 'node', '--input-type=module', '-e',
                   helper, action.path, container_snapshot]
        result = await asyncio.to_thread(subprocess.run, command, capture_output=True,
                                         text=True, timeout=15, check=True)
        encoded = result.stdout.strip()
        if len(encoded) > 8 * 1024 * 1024 * 4 / 3 + 64:
            raise ValueError('SQLite snapshot exceeds 8 MiB')
        snapshot = base64.b64decode(encoded, validate=True)
        if len(snapshot) > 8 * 1024 * 1024:
            raise ValueError('SQLite snapshot exceeds 8 MiB')
        snapshot_path.write_bytes(snapshot)
        digest = hashlib.sha256(snapshot).hexdigest()
        with sqlite3.connect(f'file:{snapshot_path}?mode=ro', uri=True,
                             timeout=5) as database:
            database.enable_load_extension(False)
            deadline = time.monotonic() + 5
            database.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0,
                                          10000)
            cursor = database.execute(action.query, parameters)
            columns = [item[0] for item in cursor.description or ()]
            rows = [dict(zip(columns, row)) for row in cursor.fetchmany(101)]
            if len(rows) > 100:
                raise ValueError('SQLite query returned more than 100 rows')
        raw = {'rows': rows, 'snapshot_sha256': digest,
               'snapshot_bytes': len(snapshot), 'snapshot_path': str(snapshot_path),
               'source_path': action.path,
               'query': action.query, 'parameters': parameters}
        shown = {'path': action.path, 'query': action.query,
                 'parameters': parameters}
    else:
        tool = resolve_value(action.tool, values)
        arguments = resolve_value(action.arguments, values)
        result = await client.call_tool(tool, arguments)
        raw = result.model_dump(mode='json', by_alias=True, exclude_none=True)
        shown = {'tool': tool, 'arguments': arguments}
    for name, path in action.capture.items():
        values[name] = pointer(raw, path)
    return {'id': action.id, 'type': action.type, 'status': 'ok',
            'input': shown, 'response': raw}


async def run_actions(actions, client, values, phase, http_client=None, best_effort=False):
    records, errors = [], []
    for action in actions:
        try:
            records.append(await _run_action(action, client, values, http_client))
        except Exception as exc:  # lifecycle evidence must survive target failures
            error = {'phase': phase, 'action_id': action.id,
                     'detail': f'{type(exc).__name__}: {exc}'}
            errors.append(error)
            records.append({'id': action.id, 'type': action.type, 'status': 'error',
                            'error': error['detail']})
            if not best_effort:
                break
    return ActionRun(records, errors)


async def verify(checks, mode, client, values, http_client=None):
    records, errors, verdicts = [], [], []
    for check in checks:
        run = await run_actions([check.action], client, values, 'verification', http_client)
        errors.extend(run.errors)
        record = run.records[0]
        assertions = []
        if not run.errors:
            for assertion in check.assertions:
                try:
                    actual = pointer(record['response'], assertion.path)
                    expected = resolve_value(assertion.value, values)
                    matched = compare(actual, assertion.op, expected)
                except (KeyError, IndexError, TypeError, ValueError):
                    actual, expected, matched = None, resolve_value(assertion.value, values), False
                assertions.append({'path': assertion.path, 'op': assertion.op,
                                   'actual': actual, 'expected': expected, 'matched': matched})
            verdicts.append(all(item['matched'] for item in assertions))
        records.append({'id': check.id, 'action': record, 'assertions': assertions})
    # No checks means no outcome was verified, which all([]) would otherwise report as success.
    completed = (None if errors or not checks
                 else (all(verdicts) if mode == 'all' else any(verdicts)))
    return VerificationResult(completed, records, errors)
