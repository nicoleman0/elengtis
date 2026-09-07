CREATE TABLE users (
    id uuid PRIMARY KEY,
    email text UNIQUE NOT NULL,
    is_admin boolean NOT NULL DEFAULT false,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE invitations (
    id uuid PRIMARY KEY,
    email text NOT NULL,
    is_admin boolean NOT NULL DEFAULT false,
    expires_at timestamptz NOT NULL,
    accepted_at timestamptz,
    created_by uuid REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE sessions (
    token_hash text PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id),
    csrf_hash text NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE login_transactions (
    state_hash text PRIMARY KEY,
    nonce text NOT NULL,
    redirect_uri text NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE TABLE campaigns (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    owner_id uuid NOT NULL REFERENCES users(id),
    current_revision integer NOT NULL DEFAULT 1,
    archived_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE campaign_revisions (
    campaign_id uuid NOT NULL REFERENCES campaigns(id),
    revision integer NOT NULL,
    yaml_key text NOT NULL,
    yaml_sha256 text NOT NULL,
    scenario_revisions jsonb NOT NULL DEFAULT '[]',
    created_by uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, revision)
);
CREATE TABLE campaign_memberships (
    campaign_id uuid NOT NULL REFERENCES campaigns(id),
    user_id uuid NOT NULL REFERENCES users(id),
    role text NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
    PRIMARY KEY (campaign_id, user_id)
);
CREATE TABLE scenarios (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    owner_id uuid NOT NULL REFERENCES users(id),
    current_revision integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE scenario_revisions (
    scenario_id uuid NOT NULL REFERENCES scenarios(id),
    revision integer NOT NULL,
    yaml_key text NOT NULL,
    yaml_sha256 text NOT NULL,
    created_by uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scenario_id, revision)
);
CREATE TABLE scenario_memberships (
    scenario_id uuid NOT NULL REFERENCES scenarios(id), user_id uuid NOT NULL REFERENCES users(id),
    role text NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')), PRIMARY KEY (scenario_id, user_id)
);
CREATE TABLE model_profiles (
    id uuid PRIMARY KEY, name text NOT NULL, current_revision integer NOT NULL DEFAULT 1,
    archived_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE model_profile_revisions (
    profile_id uuid NOT NULL REFERENCES model_profiles(id), revision integer NOT NULL,
    provider_model_id text NOT NULL, generation jsonb NOT NULL DEFAULT '{}', pricing_kind text NOT NULL CHECK (pricing_kind IN ('metered', 'free')),
    input_per_million numeric, output_per_million numeric, created_by uuid NOT NULL REFERENCES users(id), created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (profile_id, revision),
    CHECK ((pricing_kind = 'free' AND input_per_million IS NULL AND output_per_million IS NULL) OR
           (pricing_kind = 'metered' AND input_per_million > 0 AND output_per_million > 0))
);
CREATE TABLE jobs (
    id uuid PRIMARY KEY, campaign_id uuid NOT NULL REFERENCES campaigns(id), campaign_revision integer NOT NULL,
    kind text NOT NULL CHECK (kind IN ('preflight', 'run', 'resume')), status text NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed', 'canceled', 'interrupted')),
    snapshot_key text NOT NULL, runtime_cap_usd numeric, required_secrets jsonb NOT NULL DEFAULT '[]', cancel_requested_at timestamptz,
    created_by uuid NOT NULL REFERENCES users(id), created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX jobs_queue_idx ON jobs (status, created_at) WHERE status = 'queued';
CREATE TABLE job_attempts (
    id uuid PRIMARY KEY, job_id uuid NOT NULL REFERENCES jobs(id), worker_id uuid NOT NULL, run_id uuid NOT NULL,
    status text NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'canceled', 'interrupted')),
    lease_expires_at timestamptz NOT NULL, started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
    message text
);
CREATE INDEX attempts_lease_idx ON job_attempts (lease_expires_at) WHERE status = 'running';
CREATE TABLE workers (
    id uuid PRIMARY KEY, name text NOT NULL, docker_available boolean NOT NULL, secret_names jsonb NOT NULL DEFAULT '[]',
    last_seen_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE runs (
    id uuid PRIMARY KEY, job_id uuid NOT NULL REFERENCES jobs(id), attempt_id uuid NOT NULL REFERENCES job_attempts(id),
    manifest_key text, rows_key text, status text NOT NULL, schema_version integer, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE artifacts (
    object_key text PRIMARY KEY, run_id uuid REFERENCES runs(id), sha256 text NOT NULL, size_bytes bigint NOT NULL,
    content_type text NOT NULL, complete boolean NOT NULL DEFAULT false, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE job_events (
    id bigserial PRIMARY KEY, job_id uuid NOT NULL REFERENCES jobs(id), attempt_id uuid REFERENCES job_attempts(id),
    phase text NOT NULL, payload jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX job_events_cursor_idx ON job_events (job_id, id);
CREATE TABLE analyses (id uuid PRIMARY KEY, owner_id uuid NOT NULL REFERENCES users(id), definition jsonb NOT NULL, archived_at timestamptz, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE audit_events (id bigserial PRIMARY KEY, actor_id uuid REFERENCES users(id), action text NOT NULL, subject_type text NOT NULL, subject_id text NOT NULL, payload jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now());
