# Declarative campaign interface design

## Status

Approved in conversation on 2026-09-05. This document specifies the next
Phase 2 pull request: replacing the hard-coded single-scenario runner with a
versioned, declarative YAML campaign interface.

## Purpose

Elengtis currently runs one built-in synthetic scenario whose identity,
fixture behavior, attack-tool classification and completion scoring are wired
into Python. That was useful for establishing deterministic orchestration
parity, but it prevents researchers from configuring meaningful audits without
editing the package.

The next release slice will let a researcher describe a multi-target,
multi-scenario campaign in YAML. The interface must be suitable for both the
CLI and a future web dashboard: structured, versioned, fully validated before
execution and free of executable user code.

This remains a scenario-based auditing benchmark. It will accurately evaluate
behavior covered by declared rules; it will not claim to discover every
possible prompt injection automatically.

## Goals

- Remove production scenario identity, proposal detection and completion
  scoring from the orchestration engines.
- Replace the current JSON run configuration with a versioned YAML campaign.
- Run a stable `target x scenario x trial` matrix across multiple targets and
  scenarios.
- Support locally launched stdio targets and externally managed Streamable HTTP
  MCP targets.
- Represent setup, exercise, proposal detection, verification and cleanup as
  typed declarative primitives.
- Keep connection credentials out of YAML, manifests and evidence.
- Preserve deterministic evidence, engine comparability and resume semantics.
- Export machine-readable JSON Schemas that a future dashboard can consume.
- Retain a fully offline synthetic worked example and test suite.

## Non-goals

This pull request will not add:

- a web dashboard;
- OAuth or interactive authentication flows;
- parallel trial execution;
- automatic discovery of unknown prompt injections;
- model-based outcome adjudication;
- Python plugin hooks or an embedded expression/workflow language;
- a general model-provider configuration system;
- a guarantee that an externally managed server is reset between trials.

## Design principles

### Typed primitives, not executable configuration

YAML will use a small vocabulary implemented and validated by elengtis. The
initial vocabulary consists of MCP and HTTP actions, environment references,
bindings, captures, prompt substitutions, structured argument matchers and
response assertions. New primitive types should be added only when concrete
scenarios require them.

The configuration will not evaluate Python, shell fragments, JMESPath or a
general workflow language. Stdio targets necessarily name an executable and
arguments, but those values are passed directly to process creation rather
than interpreted by a shell.

### Cohesive modules

Responsibilities currently concentrated in `cli.py` will be separated into
cohesive modules:

- `config.py` owns typed models, YAML loading, schema versions, validation,
  matrix expansion and JSON Schema export.
- `transports.py` owns stdio and Streamable HTTP MCP session creation.
- `scenario.py` owns lifecycle action execution, proposal matching,
  verification and metric calculation.
- `cli.py` remains the coordinator for commands, attempts, persistence and
  summaries.

This is separation by responsibility, not a class-per-file structure. The
reference, graph, LangChain and `create_agent` engines remain concerned only
with model/tool orchestration.

## Configuration model

### Campaign YAML

A campaign file contains:

- `schema_version`;
- run settings such as engine, model, trials and model-turn budget;
- one or more uniquely identified targets;
- one or more scenario-file references;
- per-target bindings for each compatible scenario.

The campaign replaces the current JSON shape. Backward compatibility for old
JSON configuration is intentionally not provided because the project is
unpublished and the old model cannot express the new matrix. Existing result
directories remain governed by their recorded result and metrics versions.

The default matrix is the Cartesian product of configured targets and
scenarios, repeated by the configured trial count. Every target/scenario pair
must satisfy the scenario's declared bindings. An incomplete pair is a
validation error, not a runtime skip.

### Scenario YAML

A scenario file contains:

- a schema version, stable ID, title and description;
- the logical bindings it requires from a target;
- the system and user prompts used during the exercise and the explicit set of
  tools exposed to the model;
- ordered setup actions;
- proposal matchers and their positive and negative examples;
- independent completion verifiers and their aggregation mode;
- best-effort cleanup actions.

Scenario files do not contain target endpoints or credentials. A scenario uses
logical binding names; a target maps those names to concrete tool names,
argument names, JSON Pointers, argument values or other typed values. This
permits one scenario to run against servers that expose equivalent behavior
under different APIs.

### Typed values and substitution

Structured action arguments support four value sources:

- YAML literals;
- a named target binding;
- a named value captured from an earlier action;
- a runner-provided value such as the per-trial canary or trial directory.

References are represented structurally rather than evaluated as expressions.
Prompt strings may use simple named placeholders that refer to the same
validated value namespace. Unknown, forward or cyclic references fail
validation.

Setup actions may capture a named value from a response with an RFC 6901 JSON
Pointer. Captures are scoped to one trial and may be used by later setup
actions, prompts, verifiers and cleanup actions. Secret environment values may
be sent to a target but may not be captured or substituted into a prompt.

### Target transports and secrets

A stdio target declares an executable, an argument list and a restricted set
of environment-variable references. The command is never passed through a
shell. Runner values such as the trial directory may be used as individual
arguments when a disposable fixture requires them.

A Streamable HTTP target declares a URL and optional request headers. Every
configured header value must be an environment-variable reference. Literal
authorization headers are rejected. Environment names are validated before an
output directory is created; resolved values are held only for the active
request/session.

Configuration, manifests, evidence and errors record the environment-variable
name, never its resolved value. Header and exception persistence passes through
redaction as a second line of defense.

## Trial planning and identity

Validation expands a campaign into planned trials before execution. A stable
trial identity is derived from the target ID, scenario ID and zero-based trial
index. Attempt IDs remain unique per execution and retries retain their prior
attempt records.

The result schema will increment. Each result row and evidence document records
the target ID, scenario ID, trial index, trial ID, attempt ID and engine. The
manifest stores the validated effective campaign with secret references intact
but unresolved.

Resume reloads the effective campaign from the manifest. It does not accept
replacement run configuration. A completed terminal trial is skipped; an
ambiguous or incomplete attempt is preserved and the same trial is restarted
from setup with a new attempt ID. Result-schema or metrics-version mismatches
continue to block resume.

## Trial lifecycle

Each planned trial follows this sequence:

1. Resolve bindings and required environment references.
2. Create a fresh local trial directory.
3. Open a fresh MCP session to the target.
4. Execute setup actions in order.
5. Run the selected agent engine with the scenario prompts and discovered
   target tools.
6. Evaluate proposal rules centrally against the recorded trajectory.
7. Execute independent completion verifiers.
8. Execute cleanup actions in a `finally` path.
9. Persist the complete attempt record.

For stdio, elengtis owns the process and closes it after the trial. For
Streamable HTTP, elengtis owns only the client session; the server is externally
managed. A fresh HTTP session is not described as a fresh server or clean
sandbox.

### Setup and cleanup

Setup and cleanup contain ordered MCP-tool or HTTP-request actions. Setup
prepares controlled state, including a unique per-trial canary where needed.
Setup failure prevents the model exercise from starting. Cleanup runs
best-effort whenever trial execution reached a state that may need cleanup,
including after model, tool or verification failure.

Cleanup errors are retained separately. They do not rewrite an agent outcome,
but they make the attempt operationally unhealthy and visible to the user.

### Agent exercise

The driving model sees only the configured exercise prompts and the tools in
the scenario's required `exercise.tools` allowlist. An explicit `all` value is
available for audits that intentionally expose the target's complete inventory;
there is no implicit expose-all default. Setup, verifier and cleanup tools are
not model-facing merely because the trusted runner can call them through the
same MCP session. The model does not see proposal rules, verifier
configuration, verifier credentials, captured trusted-only values or verifier
results.

The existing engines return orchestration evidence without assigning
scenario-specific meaning. A single evaluator calculates scenario metrics for
every engine, preserving the ability to compare engine behavior.

### Proposal evaluation

Proposal evaluation is deterministic and post-hoc. A proposal rule identifies
a concrete tool binding and optional predicates over its structured arguments.
The initial predicate operators are:

- `equals` for exact typed equality;
- `contains` for string or collection containment;
- `matches` for a regular-expression match against a string.

Predicate paths use JSON Pointer. A rule matches only when its tool and every
declared predicate match. Each rule includes positive and negative examples;
campaign validation executes these examples as local matcher tests. Evidence
records the rule, call and individual predicate results that produced a match.

`proposed` is true when any declared proposal rule matches. `steps_to_propose`
is the zero-based model-turn index of the first matching call.

### Completion verification

The trusted runner, not the driving model, executes completion verifiers after
the exercise. The initial verifier types are:

- `mcp_tool`, which calls a configured verifier tool through the target MCP
  session;
- `http_request`, which calls a configured verification endpoint independently
  of the agent transcript.

Each verifier applies typed assertions to status and response data using JSON
Pointer and the same comparison operators as proposal matchers. A scenario
declares `all` or `any` aggregation. Evidence retains every verifier request,
redacted response, assertion and verdict.

An MCP verifier is independent of the model's claim but not necessarily of the
target server. An external HTTP verifier may provide a stronger trust boundary.
Evidence records the verifier type so reports do not imply equal assurance.

Verification failure or inability to obtain evidence produces
`completed: null`, not `false`. A successful verifier assertion determines
`completed` according to the scenario's aggregation mode.

### Outcome interpretation

Proposal and completion remain separate measurements:

- proposed and completed: observed malicious call with verified impact;
- proposed and not completed: observed attempt without verified impact;
- not proposed and completed: an anomaly requiring investigation of hidden
  behavior, matcher coverage or contaminated setup;
- neither: no observed proposal or verified impact.

The runner never infers refusal from non-completion, timeout or infrastructure
failure. Model-assisted adjudication may be added later only as a separately
labelled measurement.

## HTTP actions

Lifecycle HTTP actions declare a method, URL or target-relative path,
environment-backed headers, a structured body where applicable, captures and
assertions. Redirect and timeout behavior are explicit runner defaults recorded
in the manifest. Response bodies are size-bounded before persistence while the
evidence records whether truncation occurred.

HTTP actions are made by trusted runner code and are not exposed to the driving
model as tools. They use a directly declared HTTP client dependency rather than
relying on a transitive dependency.

## Failure semantics

Failures identify the phase in which they occurred:

- `connection_error`;
- `setup_error`;
- `agent_error`;
- `verification_error`;
- `cleanup_error`.

Configuration and binding errors are rejected before execution. Setup failure
records an incomplete attempt and skips the exercise. Agent failure still
allows verification because an external side effect may have completed before
the failure became visible. Verification failure leaves completion unknown.
Cleanup failure is additive operational evidence rather than an agent
termination reason.

The matrix stops for invalid configuration and after three consecutive
connection or setup failures on the same target, since further trials against
that target are unlikely to be meaningful. The threshold is the named runner
constant `MAX_CONSECUTIVE_TARGET_FAILURES = 3` and is recorded in the manifest.
A successful setup resets that target's consecutive-failure count. A single
scenario's ordinary model outcome does not stop unrelated trials.

No summary denominator includes retry attempts as new trials. Reports show
unknown values and failures alongside rates.

## Commands and user experience

The primary execution remains:

```sh
elengtis --config campaign.yaml --out results/campaign
```

The CLI adds:

```sh
elengtis validate campaign.yaml
elengtis schema --out schemas
```

`validate` performs every safe local check without contacting a target or
creating a results directory. It parses all referenced scenarios, checks IDs,
bindings, environment names, references, matcher examples and matrix identity,
then prints the planned matrix.

`schema` writes the versioned campaign and scenario JSON Schemas. These schemas
are part of the supported interface and will be packaged in the wheel so a
future dashboard can use the same constraints as the CLI.

The run command retains the run-level `--trials`, `--step-budget`, `--engine`
and `--model` overrides, with the existing precedence over file values. It
retains `--out` and `--resume`. The scenario-specific `--policies` option is
removed. New campaign runs require a `.yaml` or `.yml` file; `--resume` still
loads the recorded effective campaign and rejects configuration overrides.
YAML is always the canonical configuration persisted in the manifest.

## Offline example and deterministic providers

The built-in support-note benchmark becomes an ordinary YAML campaign and
scenario. Its MCP fixture remains Python because a server implementation is
code, but production orchestration no longer imports its identity, attack-tool
list or scoring function.

The existing scripted policies remain deterministic fixture machinery for the
offline example and parity tests. They do not become part of the production
scenario interface and do not determine live scenario semantics. General model
provider configuration is deferred.

The packaged wheel must contain everything required to run the documented
offline YAML example without source-checkout imports, network access, API
credentials or Docker after installation.

## Dependencies

The implementation should prefer existing direct dependencies and the standard
library. Two capabilities require explicit direct dependencies:

- YAML parsing;
- trusted-runner HTTP actions.

Pydantic is already fundamental to the installed LangChain/MCP stack, but if it
is imported directly it must be declared as a direct dependency. Its validation
and JSON Schema generation are preferable to maintaining parallel hand-written
validation and dashboard schemas. The implementation plan must verify the
pinned APIs before selecting the YAML and HTTP packages and update the lockfile
in the same pull request.

## Verification strategy

The pull request requires deterministic coverage for:

- valid campaign and scenario loading;
- rejection of unknown fields, duplicate IDs and invalid schema versions;
- missing bindings, captures, environment names and runner values;
- matcher positive and negative examples;
- secret-reference enforcement and persistence redaction;
- stable multi-target, multi-scenario trial identities;
- result-schema migration and resume behavior;
- ordered setup and best-effort cleanup;
- connection, setup, agent, verification and cleanup failures;
- completion aggregation and unknown verification results;
- stdio MCP integration;
- Streamable HTTP MCP integration with a local test server;
- HTTP verification with a local test server;
- equivalent prompts, tools and evaluation across all four engines;
- wheel contents, JSON Schema export and the documented offline YAML example.

Tests continue to use the standard library's `unittest`. Network-facing tests
bind only local ephemeral endpoints and make no model calls. CI remains free of
secrets and external services.

## Documentation changes

The README will explain:

- campaign, target, scenario and binding concepts;
- both transport types and their isolation limits;
- lifecycle phases and trust boundaries;
- proposal versus completion interpretation;
- secret handling;
- validation, execution, evidence inspection and resume;
- how to write and test a new scenario;
- which claims elengtis does and does not make.

`PLAN.md` will record the completed Phase 2 slice and any precisely deferred
items discovered during implementation.

## Acceptance criteria

The pull request is complete when another developer can, without editing
Python:

1. Define at least two targets and two scenarios in YAML.
2. Validate the complete campaign locally and inspect its planned matrix.
3. Run the bundled offline campaign against stdio.
4. Connect a campaign to a Streamable HTTP MCP target using environment-backed
   headers.
5. Prepare and clean up scenario state with declarative actions.
6. Detect a proposal using structured tool-argument rules.
7. Verify completion through either MCP or HTTP evidence.
8. Interpret and inspect the resulting redacted evidence.
9. Interrupt and resume the expanded matrix without rerunning completed trials
   or inflating denominators.
10. Generate the JSON Schemas intended for future dashboard forms.

All deterministic tests, package build checks and the installed-wheel example
must pass.
