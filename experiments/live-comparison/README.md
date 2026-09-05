# Live orchestration comparison

Generate the fixed-order, single-trial campaign cells before running the pilot:

```sh
uv run python experiments/live-comparison/generate_live_comparison.py \
  --model openai/gpt-5-mini --blocks 10 --out experiments/live-comparison/generated
```

`run-order.json` freezes the random seed and block order. `commands.txt` lists
the corresponding `elengtis run` commands. Review that order and commit the
generated campaign files before a live run. Keep the generated result directories
and analyze all of them together with `elengtis analyze`.

See [the protocol](../../docs/live-comparison-protocol.md) for fixed conditions,
stopping rules and the main-run analysis plan.
