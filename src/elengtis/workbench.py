"""Database operations for the web application and runners."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import yaml
import secrets
from uuid import UUID, uuid4

from elengtis.artifacts import ArtifactStore
from elengtis.db import Database


def _now(): return datetime.now(timezone.utc)
def _hash(value: str) -> str: return hashlib.sha256(value.encode()).hexdigest()
def _token() -> str: return secrets.token_urlsafe(32)


class AccessError(PermissionError): pass
class ConflictError(RuntimeError): pass


class Workbench:
    def __init__(self, db: Database, artifacts: ArtifactStore):
        self.db, self.artifacts = db, artifacts

    def audit(self, conn, actor, action, subject_type, subject_id, payload=None):
        conn.execute("INSERT INTO audit_events (actor_id, action, subject_type, subject_id, payload) VALUES (%s,%s,%s,%s,%s)",
                     (actor, action, subject_type, str(subject_id), json.dumps(payload or {})))

    def bootstrap_admin(self, email: str):
        email = email.strip().lower()
        with self.db.connection() as conn:
            if conn.execute("SELECT 1 FROM users WHERE is_admin AND active").fetchone():
                raise ValueError("an administrator already exists")
            user_id = uuid4()
            conn.execute("INSERT INTO users (id,email,is_admin) VALUES (%s,%s,true)", (user_id, email))
            self.audit(conn, None, "bootstrap", "user", user_id, {"email": email})

    def identity(self, email: str):
        email = email.lower()
        with self.db.connection() as conn:
            user = conn.execute("SELECT * FROM users WHERE email=%s AND active", (email,)).fetchone()
            if user: return user
            invitation = conn.execute("SELECT * FROM invitations WHERE email=%s AND accepted_at IS NULL AND expires_at > now() ORDER BY created_at DESC LIMIT 1", (email,)).fetchone()
            if not invitation: raise AccessError("An administrator must invite this account")
            user_id = uuid4()
            conn.execute("INSERT INTO users (id,email,is_admin) VALUES (%s,%s,%s)", (user_id, email, invitation["is_admin"]))
            conn.execute("UPDATE invitations SET accepted_at=now() WHERE id=%s", (invitation["id"],))
            self.audit(conn, user_id, "accept_invitation", "invitation", invitation["id"])
            return conn.execute("SELECT * FROM users WHERE id=%s", (user_id,)).fetchone()

    def create_session(self, user_id):
        token, csrf = _token(), _token()
        with self.db.connection() as conn:
            conn.execute("INSERT INTO sessions (token_hash,user_id,csrf_hash,expires_at) VALUES (%s,%s,%s,%s)",
                         (_hash(token), user_id, _hash(csrf), _now() + timedelta(days=7)))
        return token, csrf

    def session(self, token: str | None):
        if not token: return None
        with self.db.connection() as conn:
            return conn.execute("SELECT u.*,s.csrf_hash FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=%s AND s.expires_at > now() AND u.active", (_hash(token),)).fetchone()

    def revoke_session(self, token: str | None):
        if token:
            with self.db.connection() as conn: conn.execute("DELETE FROM sessions WHERE token_hash=%s", (_hash(token),))

    def create_login(self, redirect_uri: str):
        state, nonce = _token(), _token()
        with self.db.connection() as conn:
            conn.execute("INSERT INTO login_transactions (state_hash,nonce,redirect_uri,expires_at) VALUES (%s,%s,%s,%s)",
                         (_hash(state), nonce, redirect_uri, _now() + timedelta(minutes=10)))
        return state, nonce

    def consume_login(self, state: str):
        with self.db.connection() as conn:
            row = conn.execute("DELETE FROM login_transactions WHERE state_hash=%s AND expires_at > now() RETURNING *", (_hash(state),)).fetchone()
        if not row: raise AccessError("Login transaction expired or is invalid")
        return row

    def member(self, user, campaign_id, write=False):
        if user["is_admin"]: return "admin"
        with self.db.connection() as conn:
            row = conn.execute("SELECT role FROM campaign_memberships WHERE campaign_id=%s AND user_id=%s", (campaign_id, user["id"])).fetchone()
        if not row or (write and row["role"] not in {"owner", "editor"}): raise AccessError("Campaign access required")
        return row["role"]

    def campaigns(self, user):
        with self.db.connection() as conn:
            if user["is_admin"]:
                return conn.execute("SELECT id,name,current_revision,updated_at FROM campaigns WHERE archived_at IS NULL ORDER BY updated_at DESC").fetchall()
            return conn.execute("SELECT c.id,c.name,c.current_revision,c.updated_at FROM campaigns c JOIN campaign_memberships m ON m.campaign_id=c.id WHERE m.user_id=%s AND c.archived_at IS NULL ORDER BY c.updated_at DESC", (user["id"],)).fetchall()

    def create_campaign(self, user, name: str, yaml_text: str, scenarios: dict[str, str]):
        campaign_id, key = uuid4(), f"campaigns/{uuid4()}/revisions/1/campaign.yaml"
        record = self.artifacts.put(key, yaml_text.encode(), "application/yaml")
        pinned = []
        for filename, text in scenarios.items():
            if Path(filename).name != filename or not filename.endswith((".yaml", ".yml")):
                raise ValueError("scenario filenames must be flat YAML names")
            scenario_id, scenario_key = uuid4(), f"scenarios/{uuid4()}/revisions/1/{filename}"
            scenario_record = self.artifacts.put(scenario_key, text.encode(), "application/yaml")
            pinned.append({"id": str(scenario_id), "revision": 1, "filename": filename, "yaml_key": scenario_key})
        with self.db.connection() as conn:
            conn.execute("INSERT INTO campaigns (id,name,owner_id) VALUES (%s,%s,%s)", (campaign_id, name, user["id"]))
            conn.execute("INSERT INTO campaign_revisions (campaign_id,revision,yaml_key,yaml_sha256,scenario_revisions,created_by) VALUES (%s,1,%s,%s,%s,%s)",
                         (campaign_id, key, record["sha256"], json.dumps(pinned), user["id"]))
            conn.execute("INSERT INTO campaign_memberships VALUES (%s,%s,'owner')", (campaign_id, user["id"]))
            for item in pinned:
                conn.execute("INSERT INTO scenarios (id,name,owner_id) VALUES (%s,%s,%s)", (item["id"], item["filename"], user["id"]))
                conn.execute("INSERT INTO scenario_revisions (scenario_id,revision,yaml_key,yaml_sha256,created_by) VALUES (%s,1,%s,%s,%s)",
                             (item["id"], item["yaml_key"], hashlib.sha256(self.artifacts.get(item["yaml_key"])).hexdigest(), user["id"]))
                conn.execute("INSERT INTO scenario_memberships VALUES (%s,%s,'owner')", (item["id"], user["id"]))
            self.audit(conn, user["id"], "create", "campaign", campaign_id, {"revision": 1})
        return {"id": str(campaign_id), "name": name, "version": 1}

    def revision(self, user, campaign_id, write=False):
        role = self.member(user, campaign_id, write)
        with self.db.connection() as conn:
            row = conn.execute("SELECT c.id,c.name,c.current_revision,r.yaml_key,r.yaml_sha256,r.scenario_revisions FROM campaigns c JOIN campaign_revisions r ON r.campaign_id=c.id AND r.revision=c.current_revision WHERE c.id=%s", (campaign_id,)).fetchone()
        if not row: raise KeyError(campaign_id)
        return row | {"member_role": role, "campaign_yaml": self.artifacts.get(row["yaml_key"]).decode()}

    def events(self, user, after: int = 0):
        with self.db.connection() as conn:
            rows = conn.execute("SELECT e.id,e.job_id,e.attempt_id,e.phase,e.payload,e.created_at FROM job_events e JOIN jobs j ON j.id=e.job_id JOIN campaign_memberships m ON m.campaign_id=j.campaign_id WHERE m.user_id=%s AND e.id>%s ORDER BY e.id LIMIT 100", (user["id"], after)).fetchall()
        return rows

    def job(self, user, job_id):
        with self.db.connection() as conn:
            row = conn.execute("SELECT j.* FROM jobs j WHERE j.id=%s", (job_id,)).fetchone()
        if not row: raise KeyError(job_id)
        self.member(user, row["campaign_id"])
        return row

    def result_rows(self, user, job_id, offset=0, limit=100):
        job = self.job(user, job_id)
        with self.db.connection() as conn:
            run = conn.execute("SELECT * FROM runs WHERE job_id=%s ORDER BY created_at DESC LIMIT 1", (job_id,)).fetchone()
        if not run or not run["rows_key"]: return {"job": job, "rows": [], "next_offset": None}
        rows = [json.loads(line) for line in self.artifacts.get(run["rows_key"]).decode().splitlines() if line.strip()]
        page = rows[offset:offset + limit]
        return {"job": job, "run_id": run["id"], "rows": page, "next_offset": offset + limit if offset + limit < len(rows) else None}

    def evidence(self, user, job_id, name):
        job = self.job(user, job_id)
        if Path(name).name != name or not name.endswith('.json'): raise ValueError("invalid evidence name")
        with self.db.connection() as conn:
            run = conn.execute("SELECT id FROM runs WHERE job_id=%s ORDER BY created_at DESC LIMIT 1", (job_id,)).fetchone()
        if not run: raise KeyError(job_id)
        return self.artifacts.get(f"runs/{run['id']}/{name}")

    def enqueue(self, user, campaign_id, kind, runtime_cap_usd, model_profile_revision=None):
        revision = self.revision(user, campaign_id, write=True)
        campaign_document = yaml.safe_load(revision["campaign_yaml"]) or {}
        live = not campaign_document.get("model_free", False) and bool(campaign_document.get("model"))
        if live and (runtime_cap_usd is None or not model_profile_revision):
            raise ValueError("live runs require an immutable model profile revision and a positive runtime cap")
        if not live and (runtime_cap_usd is not None or model_profile_revision):
            raise ValueError("model-free runs do not accept pricing or runtime caps")
        profile_snapshot = None
        if model_profile_revision:
            profile_id, revision_number = model_profile_revision.split(":", 1)
            with self.db.connection() as conn:
                profile = conn.execute("SELECT * FROM model_profile_revisions WHERE profile_id=%s AND revision=%s", (profile_id, int(revision_number))).fetchone()
            if not profile or profile["provider_model_id"] != campaign_document.get("model"):
                raise ValueError("model profile revision does not match the campaign model")
            profile_snapshot = {"id": model_profile_revision, "pricing_kind": profile["pricing_kind"],
                                "input_per_million": str(profile["input_per_million"]) if profile["input_per_million"] is not None else None,
                                "output_per_million": str(profile["output_per_million"]) if profile["output_per_million"] is not None else None}
        # Snapshot is the trust boundary; no browser field changes execution after this point.
        scenarios = {item["filename"]: self.artifacts.get(item["yaml_key"]).decode() for item in revision["scenario_revisions"]}
        snapshot = {"campaign_id": str(campaign_id), "campaign_revision": revision["current_revision"], "campaign_yaml": revision["campaign_yaml"],
                    "scenarios": scenarios, "scenario_revisions": revision["scenario_revisions"], "model_profile": profile_snapshot, "runtime_cap_usd": runtime_cap_usd}
        job_id, key = uuid4(), f"jobs/{uuid4()}/snapshot.json"
        record = self.artifacts.put(key, json.dumps(snapshot, sort_keys=True).encode())
        with self.db.connection() as conn:
            conn.execute("INSERT INTO jobs (id,campaign_id,campaign_revision,kind,status,snapshot_key,runtime_cap_usd,created_by) VALUES (%s,%s,%s,%s,'queued',%s,%s,%s)",
                         (job_id, campaign_id, revision["current_revision"], kind, key, runtime_cap_usd, user["id"]))
            conn.execute("INSERT INTO artifacts (object_key,sha256,size_bytes,content_type,complete) VALUES (%s,%s,%s,%s,true)", (key, record["sha256"], record["size_bytes"], record["content_type"]))
            self.audit(conn, user["id"], "queue", "job", job_id, {"kind": kind})
        return {"id": str(job_id), "status": "queued", "snapshot_key": key}

    def create_model_profile(self, user, name, provider_model_id, pricing_kind, input_rate=None, output_rate=None):
        if not user["is_admin"]: raise AccessError("Administrator access required")
        if (pricing_kind == "free" and (input_rate is not None or output_rate is not None)) or (pricing_kind == "metered" and (input_rate is None or output_rate is None)):
            raise ValueError("free profiles have no rates; metered profiles require both positive rates")
        profile_id = uuid4()
        with self.db.connection() as conn:
            conn.execute("INSERT INTO model_profiles (id,name) VALUES (%s,%s)", (profile_id, name))
            conn.execute("INSERT INTO model_profile_revisions (profile_id,revision,provider_model_id,pricing_kind,input_per_million,output_per_million,created_by) VALUES (%s,1,%s,%s,%s,%s,%s)",
                         (profile_id, provider_model_id, pricing_kind, input_rate, output_rate, user["id"]))
            self.audit(conn, user["id"], "create", "model_profile", profile_id)
        return {"id": str(profile_id), "revision": 1, "provider_model_id": provider_model_id}

    def register_worker(self, worker_id, name, secret_names, docker_available=True):
        with self.db.connection() as conn:
            conn.execute("INSERT INTO workers (id,name,docker_available,secret_names) VALUES (%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET last_seen_at=now(),secret_names=excluded.secret_names,docker_available=excluded.docker_available", (worker_id,name,docker_available,json.dumps(sorted(secret_names))))

    def claim(self, worker_id, secret_names):
        with self.db.connection() as conn, conn.transaction():
            row = conn.execute("SELECT j.* FROM jobs j WHERE j.status='queued' AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements_text(j.required_secrets) s WHERE NOT (s.value = ANY(%s))) ORDER BY j.created_at FOR UPDATE SKIP LOCKED LIMIT 1", (list(secret_names),)).fetchone()
            if not row: return None
            attempt_id, run_id = uuid4(), uuid4()
            conn.execute("UPDATE jobs SET status='running',updated_at=now() WHERE id=%s", (row["id"],))
            conn.execute("INSERT INTO job_attempts (id,job_id,worker_id,run_id,status,lease_expires_at) VALUES (%s,%s,%s,%s,'running',now()+interval '60 seconds')", (attempt_id,row["id"],worker_id,run_id))
            conn.execute("INSERT INTO job_events (job_id,attempt_id,phase,payload) VALUES (%s,%s,'claimed','{}')", (row["id"],attempt_id))
            return row | {"attempt_id": attempt_id, "run_id": run_id}

    def heartbeat(self, attempt_id):
        with self.db.connection() as conn:
            conn.execute("UPDATE job_attempts SET lease_expires_at=now()+interval '60 seconds' WHERE id=%s AND status='running'", (attempt_id,))

    def finish(self, job_id, attempt_id, result: str, message: str = ""):
        if result not in {"completed", "failed", "canceled", "interrupted"}: raise ValueError(result)
        with self.db.connection() as conn:
            conn.execute("UPDATE job_attempts SET status=%s,finished_at=now(),message=%s WHERE id=%s", (result, message[-2000:], attempt_id))
            conn.execute("UPDATE jobs SET status=%s,updated_at=now() WHERE id=%s", (result, job_id))
            conn.execute("INSERT INTO job_events (job_id,attempt_id,phase,payload) VALUES (%s,%s,%s,%s)",
                         (job_id, attempt_id, result, json.dumps({"message": message[-500:]})))

    def record_artifact(self, run_id, relative_name: str, data: bytes, content_type="application/json"):
        if relative_name.startswith("/") or ".." in Path(relative_name).parts: raise ValueError("unsafe artifact name")
        key = f"runs/{run_id}/{relative_name}"
        record = self.artifacts.put(key, data, content_type)
        with self.db.connection() as conn:
            conn.execute("INSERT INTO artifacts (object_key,run_id,sha256,size_bytes,content_type,complete) VALUES (%s,%s,%s,%s,%s,true) ON CONFLICT (object_key) DO NOTHING",
                         (key, run_id, record["sha256"], record["size_bytes"], content_type))
        return key

    def record_run(self, job, status, manifest_key=None, rows_key=None, schema_version=None):
        with self.db.connection() as conn:
            conn.execute("INSERT INTO runs (id,job_id,attempt_id,manifest_key,rows_key,status,schema_version) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET status=excluded.status,manifest_key=excluded.manifest_key,rows_key=excluded.rows_key",
                         (job["run_id"], job["id"], job["attempt_id"], manifest_key, rows_key, status, schema_version))

    def cancellation_requested(self, job_id):
        with self.db.connection() as conn:
            return bool(conn.execute("SELECT cancel_requested_at IS NOT NULL FROM jobs WHERE id=%s", (job_id,)).fetchone()["cancel_requested_at"])

    def request_cancel(self, user, job_id):
        with self.db.connection() as conn:
            job = conn.execute("SELECT campaign_id FROM jobs WHERE id=%s", (job_id,)).fetchone()
        if not job: raise KeyError(job_id)
        self.member(user, job["campaign_id"], write=True)
        with self.db.connection() as conn:
            conn.execute("UPDATE jobs SET status=CASE WHEN status='queued' THEN 'canceled' ELSE status END,cancel_requested_at=now(),updated_at=now() WHERE id=%s", (job_id,))
            self.audit(conn, user["id"], "cancel", "job", job_id)

    def recover_stale(self):
        with self.db.connection() as conn, conn.transaction():
            rows = conn.execute("UPDATE job_attempts SET status='interrupted',finished_at=now(),message='lease expired' WHERE status='running' AND lease_expires_at < now() RETURNING job_id,id").fetchall()
            for row in rows:
                conn.execute("UPDATE jobs SET status='interrupted',updated_at=now() WHERE id=%s", (row["job_id"],))
                conn.execute("INSERT INTO job_events (job_id,attempt_id,phase,payload) VALUES (%s,%s,'interrupted','{\"reason\":\"lease_expired\"}')", (row["job_id"],row["id"]))
        return rows
