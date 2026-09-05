"""Phase 1B: what LangChain's abstractions preserve, and what they change."""
import unittest

from test_graph import Responses, fixture

from elengtis.adapters import run_agent_episode, run_episode as langchain_episode
from elengtis.reference import POLICIES, ScriptedProvider, run_episode as reference_episode


def without_titles(schema):
    """convert_to_openai_tool drops the JSON Schema titles the MCP server emits."""
    if isinstance(schema, dict):
        return {key: without_titles(value) for key, value in schema.items() if key != 'title'}
    if isinstance(schema, list):
        return [without_titles(item) for item in schema]
    return schema


def call_names(evidence):
    return [call['name'] for turn in evidence['trajectory'] for call in turn['calls']]


async def episodes(runner, provider_factory, budget, fail_submit=False):
    outputs = []
    for engine in (reference_episode, runner):
        async with fixture(fail_submit) as (client, _):
            outputs.append(await engine(provider_factory(), 'scripted/test', client, budget))
    return outputs


class LangChainBindingTests(unittest.IsolatedAsyncioTestCase):
    """The Phase 1A graph, driven by LangChain's chat model and MCP tool adapters."""

    async def test_policies_and_budgets_match_the_reference(self):
        for policy in POLICIES:
            for budget in (1, 2, 3, 4):
                with self.subTest(policy=policy, budget=budget):
                    (expected, reference), (actual, evidence) = await episodes(
                        langchain_episode, lambda: ScriptedProvider(policy), budget,
                        policy == 'tool_error')
                    self.assertEqual(expected, actual)
                    self.assertEqual(reference['messages'], evidence['messages'])
                    # Requests embed the tool schemas, so they carry the same title difference.
                    self.assertEqual(without_titles(reference['requests']), evidence['requests'])
                    self.assertEqual(call_names(reference), call_names(evidence))

    async def test_tool_schemas_differ_only_in_dropped_titles(self):
        (_, reference), (_, evidence) = await episodes(
            langchain_episode, lambda: ScriptedProvider('refuse'), 1)
        self.assertNotEqual(reference['tools'], evidence['tools'])
        self.assertEqual(without_titles(reference['tools']), evidence['tools'])

    async def test_multiple_calls_stay_sequential_and_ordered(self):
        responses = [{'content': '', 'tool_calls': [
            {'name': 'read_note', 'arguments': {}},
            {'name': 'read_demo_credential', 'arguments': {}}]},
            {'content': 'finished', 'tool_calls': []}]
        (expected, _), (actual, evidence) = await episodes(
            langchain_episode, lambda: Responses(responses), 4)
        self.assertEqual(expected, actual)
        self.assertEqual(call_names(evidence), ['read_note', 'read_demo_credential'])

    async def test_usage_metadata_survives_the_adapter(self):
        response = {'content': 'done', 'tool_calls': [], 'usage': {
            'input_tokens': 11, 'output_tokens': 3, 'total_tokens': 14, 'cost_usd': 0.00001}}
        metrics, evidence = (await episodes(
            langchain_episode, lambda: Responses([response]), 4))[1]
        self.assertEqual(metrics['usage'][0]['total_tokens'], 14)
        self.assertEqual(evidence['usage'][0]['cost_usd'], 0.00001)

    async def test_unknown_tool_fails_client_side_but_the_episode_continues(self):
        responses = [{'tool_calls': [{'name': 'unknown_tool', 'arguments': {}},
                                     {'name': 'read_note', 'arguments': {}}]},
                     {'content': 'done', 'tool_calls': []}]
        metrics, evidence = (await episodes(langchain_episode, lambda: Responses(responses), 4))[1]
        self.assertEqual(metrics['tool_calls'], 2)
        self.assertEqual([error['kind'] for error in metrics['errors']], ['tool_error'])
        # The registry rejects the name locally; the reference gets the server's error text.
        self.assertEqual(metrics['errors'][0]['detail'], 'Unknown tool: unknown_tool')
        self.assertEqual(evidence['messages'][3]['content'], 'ERROR: Unknown tool: unknown_tool')


class CreateAgentTests(unittest.IsolatedAsyncioTestCase):
    """create_agent, aligned to baseline budget and tool-error semantics by middleware."""

    async def test_outcomes_match_the_reference_for_every_policy(self):
        for policy in POLICIES:
            for budget in (2, 4):
                with self.subTest(policy=policy, budget=budget):
                    (expected, _), (actual, _) = await episodes(
                        run_agent_episode, lambda: ScriptedProvider(policy), budget,
                        policy == 'tool_error')
                    self.assertEqual(expected, actual)

    async def test_tool_error_is_returned_to_the_model_rather_than_raised(self):
        metrics, evidence = (await episodes(
            run_agent_episode, lambda: ScriptedProvider('tool_error'), 4, True))[1]
        self.assertEqual(metrics['termination'], 'model_stop')
        self.assertEqual([error['kind'] for error in metrics['errors']], ['tool_error'])
        errored = [turn for turn in evidence['trajectory']
                   for call in turn['calls'] if (call['result'] or {}).get('status') == 'error']
        self.assertEqual(len(errored), 1)

    async def test_budget_exit_appends_a_message_the_model_did_not_produce(self):
        metrics, evidence = (await episodes(
            run_agent_episode, lambda: ScriptedProvider('budget'), 3))[1]
        self.assertEqual(metrics['termination'], 'budget_exhausted')
        self.assertEqual(metrics['model_turns'], 3)
        assistants = [m for m in evidence['messages'] if m['role'] == 'assistant']
        self.assertEqual(len(assistants), 4)
        self.assertIn('run limit', assistants[-1]['content'])

    async def test_system_prompt_is_sent_but_kept_out_of_message_state(self):
        _, evidence = (await episodes(run_agent_episode, lambda: ScriptedProvider('refuse'), 2))[1]
        self.assertEqual(evidence['requests'][0]['messages'][0]['role'], 'system')
        self.assertEqual(evidence['messages'][0]['role'], 'user')

    async def test_provider_failure_is_recorded_as_an_error_not_a_refusal(self):
        metrics, _ = (await episodes(
            run_agent_episode, lambda: Responses([RuntimeError('synthetic model failure')]), 4))[1]
        self.assertEqual(metrics['termination'], 'provider_error')
        self.assertEqual(metrics['model_turns'], 1)


if __name__ == '__main__':
    unittest.main()
