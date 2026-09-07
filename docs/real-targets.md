# Auditing real MCP targets

Use an `isolated_container` target for a server image you own or are explicitly
authorized to test. Build and pull the image before the run; Elengtis uses
`--pull=never` so the run cannot change the image while the campaign is in
progress.

The image contract is deliberately narrow:

- listen on the configured MCP port and path, including the container network
  interface;
- run correctly as the configured non-root numeric UID/GID;
- keep mutable state under `/tmp` or inside the server's own ephemeral state;
- require no host mounts, Docker socket, devices, privileged mode, or outbound
  network access.

A trusted independent verifier is required only for scenarios that assert
target state. A scenario may declare no checks at all, in which case it reports
`completed: null`. When an isolated target's scenario does declare checks, at
least one must be an external `http_request` or
`container_sqlite_query`: a verification call back into the audited MCP server
is not an independent boundary. The SQLite verifier bypasses MCP responses but
trusts the pinned target runtime, which must provide a compatible Node runtime
with `node:sqlite` for snapshot capture.

Docker's internal bridge networks do not support published host ports on all
engines. Elengtis therefore keeps the audited container on the internal
network and uses a short-lived trusted runner relay bound to `127.0.0.1` for
the MCP client connection; the audited process never receives a second,
egress-capable network.

On hosts such as Docker Desktop that cannot route directly to an internal
container IP, set `relay_image` to a preloaded, digest-pinned `socat` image.
Elengtis runs that trusted relay with a localhost-only published port, then
attaches it to the target's internal network. The target itself remains
internal-only; relay identity and cleanup are recorded in evidence. Preload the
relay image before the run: campaigns never pull.

The runner keeps provider and target environment references outside manifests
and evidence. It passes target environment values through a short-lived mode
0600 Docker env file, then removes that file after startup. Do not put secrets
in image names, command arguments, scenario text, or tool arguments.

Run the model-free check first:

```sh
elengtis validate campaign.yaml
elengtis preflight campaign.yaml
```

`validate` reads YAML and never contacts a target. `preflight` starts each
target once, checks the MCP tool inventory and configured allowlists, and
tears down an isolated container. It does not run setup, exercise, verification
or cleanup actions.

For a full actions-only pass, set `model_free: true` with `engine: reference`
and run the campaign normally. Elengtis executes trusted setup, verification
and cleanup actions without constructing a model or scripted agent. Two or more
trials let you compare recorded container IDs and network names while checking
that each teardown succeeded.

The Everything-server bundle in `experiments/real-targets/everything/` is the
first protocol and lifecycle example. Its scenarios declare no verification
checks, so read the two result fields precisely:

- `completed: null` means no outcome verification was configured. It is not a
  failure, and it is not evidence about the target's state.
- `evidence_status: complete` means the attempt ran to termination with no
  infrastructure, setup, verification or cleanup failure. It says the record is
  trustworthy, not that the target's state was checked.

Existing `streamable_http` targets remain useful for authorized services that
you cannot deploy locally. They receive a fresh client session, but Elengtis
cannot assert server reset, filesystem isolation, network isolation, or target
credentials. Their evidence is marked `externally_managed`; use disposable
least-privilege credentials and avoid destructive scenarios.
