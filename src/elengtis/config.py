"""Typed YAML contracts for reproducible auditing campaigns."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
import yaml

ID = re.compile(r'^[a-z0-9][a-z0-9_-]*$')


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', populate_by_name=True)


class EnvRef(StrictModel):
    env: str = Field(pattern=r'^[A-Z_][A-Z0-9_]*$')


class StdioTransport(StrictModel):
    type: Literal['stdio']
    command: str
    args: list[Any] = Field(default_factory=list)
    env: dict[str, EnvRef] = Field(default_factory=dict)


class HttpTransport(StrictModel):
    type: Literal['streamable_http']
    url: str
    headers: dict[str, EnvRef] = Field(default_factory=dict)


Transport = StdioTransport | HttpTransport


class Target(StrictModel):
    id: str
    transport: Transport = Field(discriminator='type')
    bindings: dict[str, dict[str, Any]]


class Exercise(StrictModel):
    system: str
    user: str
    tools: Literal['all'] | list[Any]


class Predicate(StrictModel):
    path: str = ''
    op: Literal['equals', 'contains', 'matches']
    value: Any

    @model_validator(mode='after')
    def valid_pointer(self):
        if self.path and not self.path.startswith('/'):
            raise ValueError('predicate path must be an RFC 6901 JSON Pointer')
        return self


class ToolExample(StrictModel):
    name: str
    arguments: dict[str, Any]


class Examples(StrictModel):
    positive: list[ToolExample] = Field(min_length=1)
    negative: list[ToolExample] = Field(min_length=1)


class ProposalRule(StrictModel):
    id: str
    tool: Any
    predicates: list[Predicate] = Field(default_factory=list)
    examples: Examples


class McpAction(StrictModel):
    type: Literal['mcp_tool']
    id: str
    tool: Any
    arguments: dict[str, Any] = Field(default_factory=dict)
    capture: dict[str, str] = Field(default_factory=dict)


class HttpAction(StrictModel):
    type: Literal['http_request']
    id: str
    method: Literal['GET', 'POST', 'PUT', 'PATCH', 'DELETE']
    url: str
    headers: dict[str, EnvRef] = Field(default_factory=dict)
    body: Any = Field(default=None, alias='json')
    capture: dict[str, str] = Field(default_factory=dict)


Action = McpAction | HttpAction


class Verifier(StrictModel):
    id: str
    action: Action = Field(discriminator='type')
    assertions: list[Predicate] = Field(min_length=1)


class Verify(StrictModel):
    mode: Literal['all', 'any'] = 'all'
    checks: list[Verifier]


class Scenario(StrictModel):
    schema_version: Literal[1]
    id: str
    title: str
    description: str
    bindings: list[str]
    exercise: Exercise
    setup: list[Action] = Field(default_factory=list)
    proposal_rules: list[ProposalRule]
    verify: Verify
    cleanup: list[Action] = Field(default_factory=list)


class Campaign(StrictModel):
    schema_version: Literal[1]
    trials: int = Field(default=1, ge=1, le=100)
    step_budget: int = Field(default=4, ge=1, le=100)
    engine: Literal['reference', 'graph', 'langchain', 'create_agent'] = 'reference'
    model: str | None = None
    provider_route: str | None = None
    generation: dict[str, Any] = Field(default_factory=dict)
    experiment_id: str | None = None
    block: int | None = Field(default=None, ge=0)
    order: int | None = Field(default=None, ge=0)
    targets: list[Target] = Field(min_length=1)
    scenarios: list[str] = Field(min_length=1)


@dataclass(frozen=True)
class CampaignBundle:
    campaign: Campaign
    scenarios: tuple[Scenario, ...]
    path: Path


@dataclass(frozen=True)
class PlannedTrial:
    target: Target
    scenario: Scenario
    bindings: dict[str, Any]
    index: int

    @property
    def trial_id(self):
        return f'{self.target.id}--{self.scenario.id}--{self.index}'


def _read_yaml(path, model):
    try:
        return model.model_validate(yaml.safe_load(path.read_text()))
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise ValueError(f'Invalid {path}: {exc}') from exc


def _unique(items, label):
    ids = [item.id for item in items]
    if any(not ID.fullmatch(item) for item in ids):
        raise ValueError(f'{label} IDs must match {ID.pattern}')
    if len(ids) != len(set(ids)):
        raise ValueError(f'{label} IDs must be unique')


def load_campaign(path: Path, overrides=None):
    path = Path(path)
    if path.suffix not in ('.yaml', '.yml'):
        raise ValueError('Campaign configuration must be a YAML file')
    campaign = _read_yaml(path, Campaign)
    if overrides:
        campaign = campaign.model_copy(update={k: v for k, v in overrides.items() if v is not None})
        campaign = Campaign.model_validate(campaign.model_dump())
    scenarios = tuple(_read_yaml(path.parent / item, Scenario) for item in campaign.scenarios)
    _unique(campaign.targets, 'target')
    _unique(scenarios, 'scenario')
    for target in campaign.targets:
        transport = target.transport
        for ref in transport.env.values() if isinstance(transport, StdioTransport) else transport.headers.values():
            if ref.env not in os.environ:
                raise ValueError(f'Missing environment variable {ref.env}')
        for scenario in scenarios:
            supplied = target.bindings.get(scenario.id)
            if supplied is None:
                raise ValueError(f'target {target.id} has no bindings for scenario {scenario.id}')
            missing = set(scenario.bindings) - supplied.keys()
            if missing:
                raise ValueError(f'target {target.id} scenario {scenario.id} missing bindings {sorted(missing)}')
            from elengtis.scenario import evaluate_proposals, resolve_value
            values = dict(supplied) | {'canary': 'EXAMPLE'}
            for rule in scenario.proposal_rules:
                for label, examples in (('positive', rule.examples.positive),
                                        ('negative', rule.examples.negative)):
                    for example in examples:
                        trajectory = [{'step': 0, 'calls': [{'id': 'example',
                            'name': example.name,
                            'arguments': resolve_value(example.arguments, values)}]}]
                        matched = evaluate_proposals([rule], trajectory, values).proposed
                        if matched != (label == 'positive'):
                            raise ValueError(f'{rule.id} {label} example is labelled incorrectly')
    return CampaignBundle(campaign, scenarios, path)


def plan_trials(bundle):
    return [PlannedTrial(target, scenario, target.bindings[scenario.id], index)
            for target in bundle.campaign.targets for scenario in bundle.scenarios
            for index in range(bundle.campaign.trials)]


def write_schemas(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    for name, model in (('campaign', Campaign), ('scenario', Scenario)):
        (out / f'{name}.schema.json').write_text(
            json.dumps(model.model_json_schema(), indent=2) + '\n')
