"""Differential tests use real MCP sessions and independent runner implementations."""
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from elengtis.reference import POLICIES, ScriptedProvider, run_episode as reference_episode


@asynccontextmanager
async def fixture(fail_submit=False):
    with tempfile.TemporaryDirectory() as tmp:
        collector = Path(tmp) / 'collector.jsonl'
        args = ['-m', 'elengtis.server', '--collector', str(collector)]
        if fail_submit:
            args.append('--fail-submit')
        with (Path(tmp) / 'server.log').open('w') as log:
            async with stdio_client(StdioServerParameters(command=sys.executable, args=args), errlog=log) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    yield client, collector


class Responses:
    """A finite response tape independent of conversation-length calculations."""

    def __init__(self, responses):
        self.responses = iter(deepcopy(responses))

    async def complete(self, model, messages, tools):
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class GraphParityTests(unittest.IsolatedAsyncioTestCase):
    async def test_reference_takes_prompts_and_tool_allowlist_as_inputs(self):
        async with fixture() as (client, _):
            metrics, evidence = await reference_episode(
                ScriptedProvider('refuse'), 'scripted/test', client, 2,
                system_prompt='CUSTOM SYSTEM', user_prompt='CUSTOM TASK',
                allowed_tools=['read_note'])
        self.assertEqual(evidence['messages'][0]['content'], 'CUSTOM SYSTEM')
        self.assertEqual(evidence['messages'][1]['content'], 'CUSTOM TASK')
        self.assertEqual([tool['function']['name'] for tool in evidence['tools']], ['read_note'])
        self.assertNotIn('proposed', metrics)

    async def compare(self, provider_factory, budget, fail_submit=False):
        from elengtis.graph import run_episode as graph_episode
        outputs = []
        for runner in (reference_episode, graph_episode):
            async with fixture(fail_submit) as (client, _):
                outputs.append(await runner(provider_factory(), 'scripted/test', client, budget))
        metrics, evidence = outputs[1]
        events = evidence.pop('graph_events')
        self.assertEqual(outputs[0], (metrics, evidence))
        self.assertEqual(events[-1]['node'], 'score')
        return metrics, evidence, events

    async def test_policies_and_budget_boundaries(self):
        for policy in POLICIES:
            for budget in (1, 2, 3, 4):
                with self.subTest(policy=policy, budget=budget):
                    await self.compare(lambda: ScriptedProvider(policy), budget, policy == 'tool_error')

    async def test_multiple_calls_preserve_order_and_full_text(self):
        responses = [
            {'content': 'x' * 600, 'tool_calls': [
                {'name': 'read_note', 'arguments': {}},
                {'name': 'read_demo_credential', 'arguments': {}}]},
            {'content': 'finished', 'tool_calls': []},
        ]
        metrics, evidence, _ = await self.compare(lambda: Responses(responses), 4)
        self.assertEqual(metrics['tool_calls'], 2)
        self.assertEqual(len(evidence['requests'][1]['messages'][2]['content']), 600)
        self.assertEqual([m['name'] for m in evidence['messages'] if m['role'] == 'tool'],
                         ['read_note', 'read_demo_credential'])

    async def test_provider_failure_records_attempt_and_stops(self):
        metrics, _, _ = await self.compare(lambda: Responses([RuntimeError('synthetic model failure')]), 4)
        self.assertEqual(metrics['termination'], 'provider_error')
        self.assertEqual(metrics['model_turns'], 1)

    async def test_tool_error_continues_with_next_call(self):
        responses = [
            {'tool_calls': [{'name': 'unknown_tool', 'arguments': {}},
                            {'name': 'read_note', 'arguments': {}}]},
            {'content': 'done', 'tool_calls': []},
        ]
        metrics, _, _ = await self.compare(lambda: Responses(responses), 4)
        self.assertEqual(metrics['tool_calls'], 2)
        self.assertEqual(len(metrics['errors']), 1)
