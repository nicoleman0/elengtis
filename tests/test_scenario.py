import unittest
from pathlib import Path
import sys
import tempfile

from elengtis.config import (Examples, McpAction, Predicate, ProposalRule,
                             StdioTransport, Target, ToolExample, Verifier)
from elengtis.scenario import evaluate_proposals, pointer, resolve_value, run_actions, verify
from elengtis.transports import open_target


class ScenarioEvaluationTests(unittest.TestCase):
    def test_resolves_nested_typed_references(self):
        values = {'tool': 'documents.read', 'canary': 'trial-123'}
        value = {'name': {'binding': 'tool'}, 'payload': [{'runner': 'canary'}]}
        self.assertEqual(resolve_value(value, values),
                         {'name': 'documents.read', 'payload': ['trial-123']})

    def test_json_pointer_decodes_escaped_segments(self):
        self.assertEqual(pointer({'a/b': {'~key': 3}}, '/a~1b/~0key'), 3)

    def test_finds_first_rich_argument_match_and_explains_it(self):
        rule = ProposalRule(
            id='exfiltrate', tool={'binding': 'submit_tool'},
            predicates=[
                Predicate(path='/destination', op='equals', value='collector'),
                Predicate(path='/content', op='contains', value={'runner': 'canary'}),
                Predicate(path='/content', op='matches', value='TOKEN-[0-9]+')],
            examples=Examples(
                positive=[ToolExample(name='submit', arguments={})],
                negative=[ToolExample(name='other', arguments={})]))
        trajectory = [
            {'step': 0, 'calls': [{'id': 'a', 'name': 'submit',
                                    'arguments': {'destination': 'elsewhere',
                                                  'content': 'TOKEN-123'}}]},
            {'step': 1, 'calls': [{'id': 'b', 'name': 'submit',
                                    'arguments': {'destination': 'collector',
                                                  'content': 'prefix TOKEN-123 suffix'}}]}]
        result = evaluate_proposals([rule], trajectory,
                                    {'submit_tool': 'submit', 'canary': 'TOKEN-123'})
        self.assertTrue(result.proposed)
        self.assertEqual(result.steps_to_propose, 1)
        self.assertEqual(result.matches[0]['call_id'], 'b')
        self.assertEqual([item['matched'] for item in result.matches[0]['predicates']],
                         [True, True, True])

    def test_no_matching_call_is_not_a_proposal(self):
        rule = ProposalRule(
            id='read', tool='danger', predicates=[],
            examples=Examples(positive=[ToolExample(name='danger', arguments={})],
                              negative=[ToolExample(name='safe', arguments={})]))
        result = evaluate_proposals([rule], [{'step': 0, 'calls': []}], {})
        self.assertFalse(result.proposed)
        self.assertIsNone(result.steps_to_propose)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_action_captures_response_and_verifies_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Target(id='local', transport=StdioTransport(
                type='stdio', command=sys.executable,
                args=['-m', 'elengtis.server', '--collector', str(Path(tmp) / 'out.jsonl')]),
                bindings={'demo': {}})
            action = McpAction(type='mcp_tool', id='read', tool='read_note', arguments={},
                               capture={'note': '/content/0/text'})
            values = {}
            async with open_target(target, values) as client:
                actions = await run_actions([action], client, values, 'setup')
                check = Verifier(id='note-check', action=action,
                                 assertions=[Predicate(path='/content/0/text', op='contains',
                                                       value='demo printer')])
                result = await verify([check], 'all', client, values)
            self.assertFalse(actions.errors)
            self.assertIn('demo printer', values['note'])
            self.assertTrue(result.completed)

    async def test_cleanup_continues_after_an_action_error(self):
        class Client:
            async def call_tool(self, name, arguments):
                if name == 'bad':
                    raise RuntimeError('broken')
                return type('Result', (), {'model_dump': lambda self, **kwargs: {'ok': True}})()
        actions = [McpAction(type='mcp_tool', id='bad', tool='bad'),
                   McpAction(type='mcp_tool', id='good', tool='good')]
        result = await run_actions(actions, Client(), {}, 'cleanup', best_effort=True)
        self.assertEqual(len(result.records), 2)
        self.assertEqual(result.errors[0]['phase'], 'cleanup')


if __name__ == '__main__':
    unittest.main()
