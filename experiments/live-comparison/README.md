# Live orchestration comparison

Create a versioned model-profile file, then generate the fixed-order,
single-trial campaign cells before running the pilot:

```yaml
models:
  - id: gpt-5-mini
    model: openai/gpt-5-mini
    generation: {temperature: 0, max_tokens: 500}
  - id: second-model
    model: provider/model-name
    generation: {temperature: 0, max_tokens: 500}
```

```sh
uv run python experiments/live-comparison/generate_live_comparison.py \
  --models models.yaml --blocks 10 --out experiments/live-comparison/pilot
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

See [the protocol](../../docs/live-comparison-protocol.md) for fixed conditions,
stopping rules and the main-run analysis plan.
