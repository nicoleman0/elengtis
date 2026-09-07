# Elengtis workbench deployment

The workbench is a trusted-team application. Campaign owners and editors can
cause authorized benchmark code to run on the runner host; do not expose it to
untrusted users.

## Deploy

Set `POSTGRES_PASSWORD`, `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`,
`ELENGTIS_PUBLIC_URL` (an HTTPS URL), OIDC issuer/client values, a random
`ELENGTIS_SESSION_SECRET`, `ELENGTIS_TRUSTED_PROXY_IPS`, and `DOCKER_GID`.
Run `docker compose up -d`; the migration and bucket initialization services
complete before web and runners start. Bootstrap exactly one administrator:

```sh
docker compose run --rm web elengtis admin bootstrap --email admin@example.com
```

Put the web service behind an HTTPS reverse proxy. Only list that proxy's IPs
in `ELENGTIS_TRUSTED_PROXY_IPS`; the application trusts no forwarded headers
from other clients. Configure the OIDC callback as
`https://your-host/auth/callback`.

The web service has neither Docker access nor execution secrets. The runner
has the local Docker socket and only the secret names listed in
`ELENGTIS_ALLOWED_EXECUTION_SECRETS`. Treat that socket as host-root access.

Scale runners with `docker compose up -d --scale runner=2`. Each claims one
PostgreSQL-leased job; a dead worker leaves an interrupted, resumable job after
its lease expires. Back up both PostgreSQL and the MinIO volume together.

Runners use host networking for hardened target execution. The bundled Compose
file publishes PostgreSQL and MinIO only on loopback, so runners use those
loopback addresses; set `ELENGTIS_RUNNER_DATABASE_URL` and
`ELENGTIS_RUNNER_S3_ENDPOINT` when runners live on another host.

For local development, configure PostgreSQL and MinIO then use
`elengtis dev serve --email developer@example.test`. This loopback-only command
uses an injected identity and is not available through production settings.
