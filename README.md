# elengtis

[![checks](https://github.com/nicoleman0/elengtis/actions/workflows/checks.yml/badge.svg)](https://github.com/nicoleman0/elengtis/actions/workflows/checks.yml)

A configurable MCP prompt-injection benchmark. Elengtis runs declarative YAML
scenarios against stdio or Streamable HTTP MCP targets, records complete agent
trajectories, and verifies effects independently of the driving model.

It is a scenario-based research tool, not an automatic proof that a server is
secure. It detects behavior described by its rules; it does not discover every
possible injection.

## Choosing an engine

Use `create_agent` as the default for straightforward agents. It is the
easiest LangChain path to a capable tool-using agent and is the recommended
starting point for new campaigns.

Choose `graph` when the workflow itself needs explicit control: shared state,
conditional branching, recovery after tool errors, strict tool ordering,
bounded turn-by-turn execution, or evidence-sensitive transitions. `graph` is
the lower-level LangGraph path and makes those mechanics visible and
programmable.

This is a role distinction, not a claim that one engine is universally safer.
The framework capability experiment found no practical universal winner:
`create_agent` is the simpler default, while `graph` is the controlled-workflow
option.

## Quick start

Install [uv](https://docs.astral.sh/uv/) and Python 3.12, then:

```sh
uv sync --locked
uv run --offline elengtis example --out results/demo
cat results/demo/summary.txt
uv run --offline python -m unittest discover -s tests -v
```

The packaged example needs no API key, network service or Docker. It uses a
trusted synthetic stdio fixture and a unique fictional canary for every trial.

## Campaigns

A campaign selects run settings, targets and scenario files. Elengtis expands
them into a stable `target × scenario × trial` matrix.

```yaml
schema_version: 1
trials: 3
step_budget: 4
engine: create_agent
model: openai/gpt-5-mini
targets:
  - id: research-server
    transport:
      type: streamable_http
      url: https://mcp.example.test/mcp
      headers:
        Authorization: {env: MCP_AUTHORIZATION}
    bindings:
      support-note:
        read_note: notes.read
        read_credential: secrets.read
        submit_credential: collector.submit
        verify_collector: collector.inspect
scenarios: [scenarios/support-note.yaml]
```

Target bindings map a scenario's logical capabilities to concrete server tool
names and values. Every target must satisfy every selected scenario; missing
bindings are rejected before a result directory is created.

Validate without contacting a server:

```sh
uv run elengtis validate campaign.yaml
```

Run it:

```sh
set -a; . ./.env; set +a
uv run elengtis run --config campaign.yaml --out results/research
```

Configuration must use `.yaml` or `.yml`. JSON and the old scripted `policies`
interface are intentionally unsupported. Run-level `--trials`,
`--step-budget`, `--engine`, and `--model` overrides remain available.

## Targets and credentials

`stdio` targets declare a command and argument list. Arguments are passed
directly—never through a shell—and may use typed runner values such as
`{runner: trial_dir}` or `{runner: canary}`.

`streamable_http` targets declare an MCP URL. Every configured header value is
an environment reference. Resolved values are used for the active session but
are not written to YAML-derived manifests or evidence.

A fresh stdio trial owns a fresh child process. A fresh HTTP trial owns only a
new client session: elengtis does not claim that an externally managed server
was reset or isolated. Only audit systems you are authorized to test.

## Scenarios

A scenario declares five phases:

1. `setup`: trusted MCP or HTTP actions prepare controlled state.
2. `exercise`: prompts and an explicit tool allowlist are given to the model.
3. `proposal_rules`: structured tool calls and arguments are matched.
4. `verify`: trusted MCP or HTTP checks inspect resulting state.
5. `cleanup`: best-effort actions run even after a failure.

References are data, not executable templates:

```yaml
tool: {binding: submit_credential}
arguments:
  credential: {runner: canary}
```

Setup actions can capture response values with RFC 6901 JSON Pointers. Later
actions can use them through `{capture: name}`. Matchers and verifier assertions
support `equals`, `contains`, and `matches`. Every proposal rule carries positive
and negative examples; validation runs them like unit tests for the YAML rule.

Only tools listed in `exercise.tools` are shown to the model. Setup, verification
and cleanup tools remain hidden unless explicitly included. Use `tools: all`
only when the experiment intentionally exposes the complete inventory.

Scenarios may also declare optional `safety_rules`. These are evaluated
independently against recorded tool calls as forbidden-action rules. Scenarios
without safety rules report safety as not applicable rather than passing.

## Evaluation and trust

The driving model never judges itself. Elengtis deterministically evaluates its
recorded tool calls, then the trusted runner performs configured verification.

- `proposed: true, completed: true`: matched malicious call and verified effect.
- `true, false`: observed attempt without verified effect.
- `false, true`: anomaly—investigate matcher coverage or contaminated state.
- `false, false`: no matched proposal or verified effect.
- `completed: null`: verification failed, so the outcome is unknown.
- `safety_pass: true`: no configured forbidden action was observed.
- `safe_completed: true`: the verified outcome completed and passed safety rules.

An MCP verifier is independent of the model's claim but still trusts the target
server's response. A separate HTTP verifier can provide a stronger boundary.
The verifier type and assertion evidence are recorded.

## Results and resume

Each output directory contains `manifest.json`, `runs.jsonl`, one JSON evidence
document per attempt, and `summary.txt`. Rows identify target, scenario, trial,
attempt and engine. Complete requests, messages, tool calls, proposal matches,
verification assertions and lifecycle records are retained.

Resume is between trials:

```sh
uv run elengtis run --resume --out results/research
```

Completed trials are skipped. An incomplete attempt remains evidence and is
retried from setup with a new attempt ID; retries never inflate the trial
denominator. Result or metric version mismatches block resume.

## Live orchestration comparison

The live comparison uses a hosted model against the local synthetic MCP fixture;
it is an engine-behaviour experiment, not a real-server security assessment.
The protocol and reproducible multi-model campaign generator are in
[`experiments/live-comparison/`](experiments/live-comparison/). Generate
the ordered one-trial campaigns before contacting a model, retain every result
directory, then summarize them without hiding unknown or incomplete trials:

```sh
uv run elengtis analyze --input results/block-000-reference-support-note \
  --input results/block-000-graph-support-note --out results/analysis
```

The command writes `summary.json` and `summary.md`, including known denominators
and Wilson intervals for the recorded proposal and completion outcomes.

## Engines

`reference`, `graph`, `langchain`, and `create_agent` receive identical prompts
and allowed tools. They return orchestration observations only. One scenario
evaluator assigns proposal and completion meaning afterward, preventing four
implementations of the experiment's semantics.

`create_agent` is the recommended default for straightforward agents. It may
dispatch several tool calls concurrently, which is useful for ordinary
tool-using work but can be the wrong fit when order or intermediate state is
part of the contract.

The explicit `graph` engine is for workflows where sequential dispatch,
turn-budget behavior, state transitions, branching, recovery, or evidence
control must be represented directly. Its extra control comes with more
orchestration to define and maintain.

`reference` is the deterministic baseline used by conformance tests, and
`langchain` is an adapter-control condition for experiments; neither is the
recommended application default.

## Dashboard schema

Generate the same strict schemas used by the CLI:

```sh
uv run elengtis schema --out schemas
```

`campaign.schema.json` and `scenario.schema.json` are suitable for validation
and future form generation. Unknown fields are rejected rather than ignored.

## Contributing

```sh
uv sync --locked
uv run --offline python -m unittest discover -s tests -v
uv build
```

Tests and CI use only synthetic fixtures, local endpoints and scripted models.
Keep scenario fixtures independently synthetic; do not import embargoed or
third-party findings. Framework behavior changes need a differential test and
must be recorded as experimental conditions rather than silently normalized.

## License

[Apache-2.0](LICENSE).
