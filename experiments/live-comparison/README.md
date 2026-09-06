# Live orchestration comparison

Create a versioned model-profile file, then generate the fixed-order,
single-trial campaign cells before running the pilot:

```yaml
models:
  - id: gpt-5-mini
    model: openai/gpt-5-mini
    pricing: {input_per_million: 0.02, output_per_million: 0.10}
    generation: {temperature: 0, max_tokens: 128, timeout: 30000, max_retries: 0}
  - id: second-model
    model: provider/model-name
    pricing: {input_per_million: 0.05, output_per_million: 0.16}
    generation: {temperature: 0, max_tokens: 128, timeout: 30000, max_retries: 0}
```

```sh
uv run python experiments/live-comparison/generate_live_comparison.py \
  --models models.yaml --blocks 10 --budget-usd 0.15 \
  --out experiments/live-comparison/pilot
```

The generated bundle copies its scenario files, so it can be committed and run
from any checkout. `run-order.json` freezes the random seed and block order.
After review, commit the bundle and preflight it without making a model call:

```sh
OPENROUTER_API_KEY=... uv run python experiments/live-comparison/run_pilot.py \
  --plan experiments/live-comparison/pilot/run-order.json \
  --results experiments/live-comparison/results --max-model-calls 960 --dry-run
```

Remove `--dry-run` to execute the recorded sequence. Result directories are
ignored; retain them outside Git and analyze all cells together with
`elengtis analyze`.

Use `--smoke` for the first block after generating a new bundle. It stops after
that block and fails if any completed cell did not make its required first note
tool call. Provider retries are disabled and request timeouts are bounded; a
partial run stops at the budget cap and can be continued with `--resume`.

Smoke failures include the provider finish reason and any malformed tool calls
recorded by the adapter. A clean response with no tool call is a model outcome,
not a provider failure; inspect its evidence before deciding whether that model
belongs in the frozen pilot roster.

See [the protocol](../../docs/live-comparison-protocol.md) for fixed conditions,
stopping rules and the main-run analysis plan.

## Focused framework comparison

For the primary LangGraph-versus-LangChain comparison, use the checked-in
three-model profile and select only the custom `graph` engine and LangChain's
`create_agent` engine with the focused four-scenario suite:

```sh
uv run python experiments/live-comparison/generate_live_comparison.py \
  --models experiments/live-comparison/framework-models.yaml \
  --engines graph create_agent \
  --experiment-id live-framework-comparison-v1 \
  --scenarios authorized-workflow injection-resistance recoverable-tool-error stateful-branch \
  --blocks 20 --step-budget 6 --budget-usd "$ELENGTIS_EXPERIMENT_BUDGET_USD" \
  --out experiments/live-comparison/framework-pilot
```

This creates 24 cells in each block and 480 cells across the full plan, with a
2,880-call ceiling. Set the budget variable at execution time; it is recorded
in the frozen plan but is not a source-code default. Run block 0 with `--smoke`;
after it passes, continue the same frozen plan with `--resume` and without
`--smoke`. The `langchain` engine remains available as an adapter-control
condition but is not part of the focused comparison.

Run the deterministic conformance suite before the live smoke block:

```sh
uv run --offline python -m unittest discover -s tests -v
```

After all result directories are available, include `--conformance-passed` in
the analysis command to enable the predeclared engine recommendation:

```sh
analysis_inputs=()
for result in experiments/live-comparison/results/*; do
  analysis_inputs+=(--input "$result")
done
uv run elengtis analyze "${analysis_inputs[@]}" \
  --out experiments/live-comparison/analysis --conformance-passed \
  --practical-margin-pp 10
```
