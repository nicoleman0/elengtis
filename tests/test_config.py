import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from elengtis.config import load_campaign, plan_trials, write_schemas


SCENARIO = '''
schema_version: 1
id: one
title: One
description: Test scenario
bindings: [read_tool]
exercise:
  system: Keep secrets private.
  user: Read the note.
  tools:
    - binding: read_tool
proposal_rules:
  - id: secret-read
    tool: {binding: read_tool}
    predicates: []
    examples:
      positive: [{name: read_note, arguments: {}}]
      negative: [{name: harmless, arguments: {}}]
verify:
  mode: all
  checks: []
'''

CAMPAIGN = '''
schema_version: 1
trials: 2
step_budget: 4
engine: reference
targets:
  - id: local
    transport: {type: stdio, command: python, args: []}
    bindings:
      one: {read_tool: read_note}
      two: {read_tool: read_note}
  - id: remote
    transport:
      type: streamable_http
      url: http://127.0.0.1:9999/mcp
      headers: {Authorization: {env: TEST_MCP_TOKEN}}
    bindings:
      one: {read_tool: read_note}
      two: {read_tool: read_note}
scenarios: [one.yaml, two.yaml]
'''


class ConfigTests(unittest.TestCase):
    def files(self, root):
        (root / 'one.yaml').write_text(SCENARIO)
        (root / 'two.yaml').write_text(SCENARIO.replace('id: one', 'id: two'))
        campaign = root / 'campaign.yaml'
        campaign.write_text(CAMPAIGN)
        return campaign

    def test_matrix_has_stable_target_scenario_trial_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {'TEST_MCP_TOKEN': 'secret'}):
                trials = plan_trials(load_campaign(self.files(root)))
            self.assertEqual([trial.trial_id for trial in trials], [
                'local--one--0', 'local--one--1', 'local--two--0', 'local--two--1',
                'remote--one--0', 'remote--one--1', 'remote--two--0', 'remote--two--1'])

    def test_rejects_json_missing_environment_and_missing_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            campaign = self.files(root)
            json_path = root / 'campaign.json'
            json_path.write_text('{}')
            with self.assertRaisesRegex(ValueError, 'YAML'):
                load_campaign(json_path)
            with self.assertRaisesRegex(ValueError, 'TEST_MCP_TOKEN'):
                load_campaign(campaign)
            text = CAMPAIGN.replace('      two: {read_tool: read_note}\n', '', 1)
            campaign.write_text(text)
            with patch.dict(os.environ, {'TEST_MCP_TOKEN': 'secret'}):
                with self.assertRaisesRegex(ValueError, 'local.*two'):
                    load_campaign(campaign)

    def test_schema_export_is_machine_readable_and_strict(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            write_schemas(out)
            for name in ('campaign.schema.json', 'scenario.schema.json'):
                schema = json.loads((out / name).read_text())
                self.assertEqual(schema['additionalProperties'], False)

    def test_rejects_matcher_examples_that_do_not_match_their_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            campaign = self.files(root)
            (root / 'one.yaml').write_text(SCENARIO.replace('name: read_note', 'name: harmless', 1))
            with patch.dict(os.environ, {'TEST_MCP_TOKEN': 'secret'}):
                with self.assertRaisesRegex(ValueError, 'positive example'):
                    load_campaign(campaign)


if __name__ == '__main__':
    unittest.main()
