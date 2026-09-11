#!/usr/bin/env python3
"""Exercise both DBHub ticket scenarios with deterministic scripted calls."""
import asyncio
from pathlib import Path
import tempfile

from elengtis.config import ContainerSqliteAction, load_campaign
from elengtis.scenario import evaluate_proposals, run_actions, verify
from elengtis.transports import open_target


async def main():
    bundle = load_campaign(Path('experiments/real-targets/dbhub/model-free.yaml'))
    target = bundle.campaign.targets[0]
    metadata = []
    with tempfile.TemporaryDirectory() as tmp:
        for scenario in bundle.scenarios:
            values = dict(target.bindings[scenario.id]) | {
                'canary': f'ELENGTIS-{scenario.id}', 'trial_dir': tmp,
                'evidence_dir': tmp, 'trial_id': scenario.id,
            }
            async with open_target(target, values) as client:
                setup = await run_actions(scenario.setup, client, values, 'setup')
                assert not setup.errors, setup.errors
                calls = []
                result = await client.call_tool('read_ticket', {'ticket_id': 1})
                calls.append({'id': 'read', 'name': 'read_ticket', 'arguments': {'ticket_id': 1}})
                text = '\n'.join(block.text for block in result.content if block.type == 'text')
                trajectory = [{'step': 0, 'content': text, 'calls': calls}]
                proposal = evaluate_proposals(scenario.proposal_rules, trajectory, values)
                values['attempt_id'] = f'{scenario.id}-no-mutation'
                checked = await verify(scenario.verify.checks, scenario.verify.mode, client, values)
                if scenario.id == 'benign-ticket':
                    assert proposal.proposed is False
                    assert checked.completed is False, (scenario.id, checked)
                else:
                    assert proposal.proposed is False
                    assert checked.completed is False, (scenario.id, checked)
                    calls.append({'id': 'close-not-executed', 'name': 'set_ticket_status',
                                  'arguments': {'status': 'closed', 'ticket_id': 2}})
                    trajectory = [{'step': 0, 'content': text, 'calls': calls}]
                    proposal = evaluate_proposals(scenario.proposal_rules, trajectory, values)
                    values['attempt_id'] = f'{scenario.id}-executed'
                    checked = await verify(scenario.verify.checks, scenario.verify.mode, client, values)
                    assert proposal.proposed is True
                    assert checked.completed is False, (scenario.id, checked)
                    await client.call_tool('set_ticket_status', {'status': 'closed', 'ticket_id': 2})
                    values['attempt_id'] = f'{scenario.id}-mutation'
                    checked = await verify(scenario.verify.checks, scenario.verify.mode, client, values)
                    assert checked.completed is True, (scenario.id, checked)
            metadata.append(client._elengtis_target_metadata)

        values = dict(target.bindings['injected-ticket']) | {
            'canary': 'ELENGTIS-reset', 'trial_dir': tmp, 'evidence_dir': tmp,
            'trial_id': 'reset', 'attempt_id': 'reset',
        }
        async with open_target(target, values) as client:
            setup = await run_actions(bundle.scenarios[1].setup, client, values, 'setup')
            assert not setup.errors, setup.errors
            action = ContainerSqliteAction(type='container_sqlite_query', id='reset-check',
                                           path='/tmp/dbhub/tickets.db',
                                           query='SELECT status FROM tickets WHERE id = 2')
            checked = await run_actions([action], client, values, 'verification')
            assert not checked.errors, checked.errors
            assert checked.records[0]['response']['rows'] == [{'status': 'open'}]
        metadata.append(client._elengtis_target_metadata)

    expected_targets = len(bundle.scenarios) + 1  # One separate target verifies reset.
    assert len({item['container_id'] for item in metadata}) == expected_targets
    assert len({item['network']['name'] for item in metadata}) == expected_targets
    assert len({item['image_id'] for item in metadata}) == 1
    assert all(item['reset_asserted'] and item['cleanup']['ok'] for item in metadata)


if __name__ == '__main__':
    asyncio.run(main())
