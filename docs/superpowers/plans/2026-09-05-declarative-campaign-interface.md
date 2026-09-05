# Declarative Campaign Interface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hard-coded single-scenario JSON runner with a validated YAML interface that runs resumable target × scenario × trial campaigns over stdio and Streamable HTTP MCP.

**Architecture:** Pydantic models in `config.py` define and validate campaigns, scenarios, actions and matchers and emit JSON Schema. `transports.py` supplies initialized MCP sessions; `scenario.py` resolves declarative values, runs lifecycle actions and evaluates evidence; engines return orchestration evidence without scoring; `cli.py` plans attempts and persists results.

**Tech Stack:** Python 3.12, Pydantic 2, PyYAML 6, httpx 0.28, MCP Python SDK 1.26+, LangGraph/LangChain, stdlib `unittest`, uv.

**Spec:** `docs/superpowers/specs/2026-09-05-declarative-campaign-interface-design.md`

## Global Constraints

- Configuration is YAML-only (`.yaml` or `.yml`); the old JSON shape and `--policies` flag are removed.
- User configuration is data: never evaluate Python, shell, JMESPath or arbitrary expressions.
- Stdio commands are passed directly to process creation, never through a shell.
- All configured HTTP headers and target environment values are environment references; resolved secrets are never persisted.
- The driving model sees only `exercise.tools`; trusted lifecycle tools and results stay hidden.
- Trial IDs are stable across resume and include target ID, scenario ID and trial index.
- Verification failure yields `completed: null`; it never becomes a refusal or a negative completion result.
- Tests use `unittest`, local processes/endpoints only, no secrets and no live model calls.
- Prefer the smallest typed primitive that satisfies the approved spec; do not add dashboard, OAuth, concurrency or provider abstractions.

---

### Task 1: YAML models, validation and schema export

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `src/elengtis/config.py`
- Create: `tests/test_config.py`

**Interfaces:**
- Produces: `load_campaign(path: Path, overrides: dict | None = None) -> CampaignBundle`
- Produces: `plan_trials(bundle: CampaignBundle) -> list[PlannedTrial]`
- Produces: `write_schemas(out: Path) -> None`
- Produces Pydantic models `Campaign`, `Scenario`, `Target`, `Action`, `ProposalRule`, `Verifier`, `PlannedTrial`.

- [ ] **Step 1: Add failing loader tests**

  Create temporary YAML campaign/scenario files in `tests/test_config.py`. Assert two targets × two scenarios × two trials produce these literal IDs: `local--one--0`, `local--one--1`, `local--two--0`, `local--two--1`, `remote--one--0`, `remote--one--1`, `remote--two--0`, `remote--two--1`. Add rejection cases for `.json`, unknown fields, duplicate IDs, missing bindings, invalid references and absent environment variables.

- [ ] **Step 2: Verify RED**

  Run `UV_CACHE_DIR=/tmp/elengtis-uv-cache uv run python -m unittest tests.test_config -v` and confirm it fails because `elengtis.config` does not exist.

- [ ] **Step 3: Implement typed models and loading**

  Add direct dependencies `pydantic>=2.12,<3`, `PyYAML>=6,<7` and `httpx>=0.28,<1`. Define discriminated transport/action/reference models with `extra='forbid'`; resolve scenario paths relative to the campaign; validate IDs against `[a-z0-9][a-z0-9_-]*`; validate all target/scenario binding pairs and environment names; apply only `trials`, `step_budget`, `engine` and `model` overrides. Generate stable planned trials in target-major, scenario-major, index order.

- [ ] **Step 4: Add matcher-example and schema tests**

  Assert invalid JSON Pointers/operators and a proposal rule whose positive or negative example has the wrong result are rejected. Assert `write_schemas` produces parseable `campaign.schema.json` and `scenario.schema.json` with `additionalProperties: false` at their roots.

- [ ] **Step 5: Verify GREEN and lock dependencies**

  Run `uv lock`, then run `UV_CACHE_DIR=/tmp/elengtis-uv-cache uv run python -m unittest tests.test_config -v` and confirm all tests pass.

- [ ] **Step 6: Commit**

  Commit `pyproject.toml`, `uv.lock`, `src/elengtis/config.py`, and `tests/test_config.py` as `feat: define YAML campaign schemas`.

### Task 2: Central proposal evaluator and value resolver

**Files:**
- Replace: `src/elengtis/scenario.py`
- Create: `tests/test_scenario.py`

**Interfaces:**
- Consumes: typed references, actions and rules from `elengtis.config`.
- Produces: `resolve_value(value, values: dict, secrets: set[str] = frozenset()) -> JSONValue`
- Produces: `evaluate_proposals(rules, trajectory, values) -> ProposalEvaluation`
- Produces: `assert_response(assertions, response, values) -> list[AssertionResult]`.

- [ ] **Step 1: Add failing resolver tests**

  Test literals and nested lists/dicts, binding/capture/runner references, exact prompt placeholders, unknown references and a rejected attempt to place a secret reference into a prompt.

- [ ] **Step 2: Verify RED**

  Run `UV_CACHE_DIR=/tmp/elengtis-uv-cache uv run python -m unittest tests.test_scenario -v`; confirm the new functions are absent.

- [ ] **Step 3: Implement the minimum resolver**

  Recursively resolve typed values from one per-trial namespace. Resolve placeholders with a strict formatter that rejects unknown names and secret-valued names. Implement RFC 6901 traversal locally, including `~0` and `~1`, rather than adding an expression dependency.

- [ ] **Step 4: Add failing proposal tests**

  Use literal trajectory fixtures to test tool filtering, `equals`, string/list `contains`, full regex search, AND across predicates, OR across rules, first matching step and detailed predicate evidence. Include the four outcome combinations for proposed/completed as independent values.

- [ ] **Step 5: Implement proposal and assertion evaluation**

  Evaluate recorded calls only; never inspect model prose. Return the matching rule ID, call ID, step and each predicate verdict. Share comparison code between proposal rules and verifier assertions.

- [ ] **Step 6: Verify GREEN and commit**

  Run `UV_CACHE_DIR=/tmp/elengtis-uv-cache uv run python -m unittest tests.test_scenario -v`, then commit as `feat: evaluate declarative scenario rules`.

### Task 3: MCP transports and agent tool allowlists

**Files:**
- Create: `src/elengtis/transports.py`
- Create: `tests/test_transports.py`
- Modify: `src/elengtis/server.py`

**Interfaces:**
- Produces: `open_target(target, values) -> AsyncContextManager[ClientSession]` with an initialized session.
- Produces: `list_agent_tools(client, allowed_names) -> list` and rejects missing configured tools before a model call.

- [ ] **Step 1: Add failing stdio transport test**

  Launch the bundled fixture through a YAML-derived `StdioTransport`, list tools through a real `ClientSession`, and assert `read_note` is present. Assert environment references resolve without copying the complete parent environment.

- [ ] **Step 2: Verify RED, then implement stdio**

  Run `UV_CACHE_DIR=/tmp/elengtis-uv-cache uv run python -m unittest tests.test_transports.TransportTests.test_stdio -v`; confirm import failure. Implement the async context manager with `stdio_client`, `StdioServerParameters`, request timeout and initialization.

- [ ] **Step 3: Add failing Streamable HTTP integration test**

  Extend the test fixture server with a Streamable HTTP entry point on a supplied localhost port. Start it as a subprocess, wait only until its TCP port accepts connections, connect with `streamable_http_client` and assert a real tool listing. Include an environment-backed header in the client.

- [ ] **Step 4: Implement Streamable HTTP and allowlists**

  Use `httpx.AsyncClient(headers=resolved_headers)` and the pinned SDK's `streamable_http_client(url, http_client=client, terminate_on_close=False)`. Yield an initialized `ClientSession`. Filter discovered tools to explicit names or `all`; raise before the model call when a requested name is absent.

- [ ] **Step 5: Verify GREEN and commit**

  Run `UV_CACHE_DIR=/tmp/elengtis-uv-cache uv run python -m unittest tests.test_transports -v`, then commit as `feat: connect configurable MCP targets`.

### Task 4: Declarative lifecycle actions

**Files:**
- Modify: `src/elengtis/scenario.py`
- Modify: `tests/test_scenario.py`
- Create: `tests/http_fixture.py`

**Interfaces:**
- Produces: `run_actions(actions, client, values, phase, http_client=None) -> ActionRun`
- Produces: `verify(checks, mode, client, values, http_client=None) -> VerificationResult`.

- [ ] **Step 1: Add failing MCP lifecycle tests**

  Run setup calls in declared order against the fixture, capture a response value with JSON Pointer, use it in a later call, and assert the action evidence contains redacted typed inputs and raw normalized MCP results. Test that cleanup continues after one action fails.

- [ ] **Step 2: Verify RED, then implement MCP actions**

  Run the focused tests and confirm missing behavior. Implement ordered calls, normalized MCP results, captures, per-action timeout and phase-tagged errors. Cleanup uses best-effort mode; setup stops on first failure.

- [ ] **Step 3: Add failing HTTP action and verifier tests**

  Start `tests/http_fixture.py` on localhost. Assert method/path/body substitution, environment-backed headers, response capture, `all`/`any` assertions, response-size truncation metadata and that persisted evidence never includes the header value.

- [ ] **Step 4: Implement HTTP actions and verification**

  Reuse one trusted `httpx.AsyncClient` per trial. Apply explicit timeout and no-redirect defaults. Parse JSON only when the response declares JSON; retain bounded text otherwise. A transport/assertion failure returns unknown completion. Successful checks aggregate with the declared mode.

- [ ] **Step 5: Verify GREEN and commit**

  Run `UV_CACHE_DIR=/tmp/elengtis-uv-cache uv run python -m unittest tests.test_scenario -v`, then commit as `feat: run declarative scenario lifecycle`.

### Task 5: Make engines scenario-agnostic

**Files:**
- Modify: `src/elengtis/reference.py`
- Modify: `src/elengtis/graph.py`
- Modify: `src/elengtis/adapters.py`
- Modify: `tests/test_graph.py`
- Modify: `tests/test_adapters.py`

**Interfaces:**
- Changes every engine entry point to `run_episode(provider, model, client, step_budget, system_prompt, user_prompt, allowed_tools) -> evidence`.
- Engine evidence contains tools, requests, messages, trajectory, errors and termination; it contains no scenario metrics or collector data.

- [ ] **Step 1: Change differential tests first**

  Pass literal prompts and an explicit tool allowlist to every engine. Assert evidence parity after removing graph-only events, and assert a verifier-only tool is absent from requests even though it exists on the server.

- [ ] **Step 2: Verify RED**

  Run `UV_CACHE_DIR=/tmp/elengtis-uv-cache uv run python -m unittest tests.test_graph tests.test_adapters -v` and confirm signature/allowlist failures.

- [ ] **Step 3: Remove scoring from engines**

  Delete imports of scenario constants and `score`. Supply prompts through engine context/arguments, filter tools before binding, retain engine-specific error semantics, and return orchestration evidence only. Keep the reference loop's sequential dispatch and the recorded `create_agent` concurrency difference unchanged.

- [ ] **Step 4: Verify GREEN and commit**

  Run the graph and adapter suites, then the full existing suite. Commit as `refactor: separate orchestration from evaluation`.

### Task 6: Campaign runner, results and resume

**Files:**
- Modify: `src/elengtis/cli.py`
- Replace: `tests/test_baseline.py`
- Modify: `tests/test_resume.py`
- Create: `tests/test_campaign.py`

**Interfaces:**
- `run_matrix(bundle: CampaignBundle, out: Path, run_id: str | None = None, prior: Sequence[dict] = ())` executes `PlannedTrial` values.
- Result schema becomes version 3; metrics version remains 1 because definitions retain their meanings.

- [ ] **Step 1: Add failing matrix tests**

  Run a two-target/two-scenario/two-trial deterministic campaign through injected local test providers. Assert eight stable trial IDs, target/scenario fields, unique attempt IDs, deterministic order, per-trial canaries and one result per completed trial.

- [ ] **Step 2: Verify RED, then implement orchestration**

  Validate before creating `out`. For each planned trial create a directory/context, open the target, run setup, call the engine, evaluate proposals, verify, and always clean up. Persist one evidence document and row per attempt with phase timings/errors and redacted effective values.

- [ ] **Step 3: Add failure-policy tests**

  Assert setup failure skips the model, agent failure still verifies, verification failure gives `completed: null`, cleanup failure is additive, and three consecutive connection/setup failures stop that target. Assert a successful setup resets the target's failure counter.

- [ ] **Step 4: Upgrade resume and summary**

  Increment `SCHEMA_VERSION` to 3. Derive completion from a terminal evidence status, preserve ambiguous attempts, retry with `.retry-N`, reject old schema resumes, report unknown denominators explicitly and never count attempts as trials.

- [ ] **Step 5: Verify GREEN and commit**

  Run `UV_CACHE_DIR=/tmp/elengtis-uv-cache uv run python -m unittest tests.test_campaign tests.test_resume tests.test_baseline -v`, then commit as `feat: run resumable YAML campaigns`.

### Task 7: CLI commands and packaged YAML example

**Files:**
- Modify: `src/elengtis/cli.py`
- Modify: `src/elengtis/__init__.py`
- Replace: `examples/offline.json` with `examples/offline.yaml`
- Create: `examples/scenarios/support-note.yaml`
- Modify: `pyproject.toml`
- Create: `tests/test_cli.py`

**Interfaces:**
- Commands: `elengtis run --config PATH --out PATH`, `elengtis validate PATH`, `elengtis schema --out PATH`.
- Preserve the legacy no-subcommand run form only when `--config` is supplied; reject JSON and `--policies`.

- [ ] **Step 1: Add failing CLI tests**

  Exercise subprocess commands for validate, schema, run and resume. Assert validation creates no results, prints the literal planned trial IDs, missing environment variables fail before output creation, and `.json` is rejected.

- [ ] **Step 2: Verify RED, then implement commands**

  Build argparse subcommands with concise help. Retain `--trials`, `--step-budget`, `--engine`, `--model`, `--out` and `--resume` only as specified. Package and locate the offline YAML files without relying on the repository root.

- [ ] **Step 3: Migrate the synthetic example**

  Express prompts, bindings, exposed tools, proposal rules, verifier and cleanup in YAML. Keep scripted providers only for the deterministic fixture path. Delete `examples/offline.json`.

- [ ] **Step 4: Verify installed behavior and commit**

  Run the CLI tests, `uv build`, then install the wheel in an isolated uv invocation and run its packaged example offline. Commit as `feat: expose YAML campaign CLI`.

### Task 8: Documentation and complete verification

**Files:**
- Modify: `README.md`
- Modify: `PLAN.md`
- Modify: `.github/workflows/checks.yml`

**Interfaces:** None; documents the implemented public interface and CI executes it.

- [ ] **Step 1: Update user documentation**

  Replace JSON/policy instructions with the YAML campaign, target, scenario, binding and lifecycle concepts. Document stdio versus HTTP trust boundaries, secret references, proposal/completion combinations, validation, schema export, evidence inspection, resume and scenario authoring.

- [ ] **Step 2: Update CI and roadmap**

  Make CI run `elengtis validate` and the packaged YAML example from the wheel. Record this Phase 2 slice in `PLAN.md`, including only genuinely deferred items from the spec.

- [ ] **Step 3: Run fresh full verification**

  Run `UV_CACHE_DIR=/tmp/elengtis-uv-cache uv run --offline python -m unittest discover -s tests -v`, `UV_CACHE_DIR=/tmp/elengtis-uv-cache uv build`, and the documented installed-wheel example. Run `git diff --check` and inspect `git status --short`.

- [ ] **Step 4: Commit**

  Commit documentation and CI as `docs: explain declarative MCP campaigns`.
