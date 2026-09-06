# Everything real-target smoke

This bundle validates Elengtis against the official Everything MCP server
without giving the audited container outbound access. The HTTP verifier is a
runner-liveness check only; `completed` is not evidence of target state
mutation in these scenarios.

Build the reviewed, locked image before starting the audit:

```sh
docker build --pull=false -t elengtis/everything:2026.8.31 experiments/real-targets/everything
docker image inspect elengtis/everything:2026.8.31 --format '{{.Id}}'
docker image inspect python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 \
  --format '{{.Id}}'
```

In a separate terminal, start the trusted verifier:

```sh
uv run python tests/docker-target/verifier.py 18080
```

Validate, preflight, and run the two-trial model-free campaign:

```sh
uv run elengtis validate experiments/real-targets/everything/model-free.yaml
uv run elengtis preflight experiments/real-targets/everything/model-free.yaml
uv run elengtis run --config experiments/real-targets/everything/model-free.yaml \
  --out results/real-targets/everything-model-free
```

Both evidence documents must have distinct `container_id` and network names,
the same `image_id`, `reset_asserted: true`, and successful cleanup and
liveness-verifier records. Docker Desktop uses the separately recorded trusted
relay image for localhost ingress; the audited target remains attached only to
its internal network. The following commands must print nothing:

```sh
docker ps -a --filter label=elengtis.managed=true -q
docker network ls --filter label=elengtis.managed=true -q
```

Generate and dry-run the four-cell live smoke plan before contacting a model:

```sh
uv run python experiments/real-targets/everything/generate_smoke.py \
  --out results/real-targets/everything-smoke-plan
uv run python experiments/live-comparison/run_pilot.py \
  --plan results/real-targets/everything-smoke-plan/run-order.json \
  --results results/real-targets/everything-smoke-results \
  --max-model-calls 12 --budget-usd 0.02 --dry-run
uv run python experiments/live-comparison/run_pilot.py \
  --plan results/real-targets/everything-smoke-plan/run-order.json \
  --results results/real-targets/everything-smoke-results \
  --max-model-calls 12 --budget-usd 0.02 --smoke
```

Only `echo` or `get-structured-content` is exposed in each live cell.
Outbound-dependent tools such as `gzip-file-as-resource` remain unavailable
until policy-proxied egress is implemented under issue #13.
