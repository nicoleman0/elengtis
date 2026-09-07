"""PostgreSQL-leased runner; every worker owns at most one child process."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import socket
import sys
import tempfile
import json
from uuid import uuid4

from elengtis.artifacts import ArtifactStore
from elengtis.db import Database
from elengtis.settings import Settings
from elengtis.workbench import Workbench


def _settings_service():
    settings = Settings.from_environment()
    db = Database(settings.database_url); db.open()
    return settings, Workbench(db, ArtifactStore(settings.s3_endpoint, settings.s3_bucket, settings.s3_access_key, settings.s3_secret_key))


async def _run(service, job, allowed_secrets):
    snapshot = service.artifacts.get(job["snapshot_key"])
    with tempfile.TemporaryDirectory(prefix="elengtis-job-") as directory:
        path = Path(directory) / "snapshot.json"; path.write_bytes(snapshot)
        env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL", *allowed_secrets}}
        env |= {"ELENGTIS_JOB_ID": str(job["id"]), "ELENGTIS_RUN_ID": str(job["run_id"]), "ELENGTIS_ATTEMPT_ID": str(job["attempt_id"])}
        command = [sys.executable, "-m", "elengtis", "_execute-job", "--kind", job["kind"], "--snapshot", str(path), "--out", str(Path(directory) / "result")]
        process = await asyncio.create_subprocess_exec(*command, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        while process.returncode is None:
            try: await asyncio.wait_for(process.wait(), 15)
            except asyncio.TimeoutError:
                service.heartbeat(job["attempt_id"])
                if service.cancellation_requested(job["id"]): process.send_signal(signal.SIGINT)
        stdout, stderr = await process.communicate()
        results = Path(directory) / "result" / "result"
        uploaded = {}
        if results.exists():
            for item in results.rglob("*"):
                if item.is_file(): uploaded[str(item.relative_to(results))] = service.record_artifact(job["run_id"], str(item.relative_to(results)), item.read_bytes(), "application/json" if item.suffix == ".json" else "text/plain")
        manifest_key, rows_key = uploaded.get("manifest.json"), uploaded.get("runs.jsonl")
        schema_version = json.loads((results / "manifest.json").read_text()).get("schema_version") if (results / "manifest.json").exists() else None
        if service.cancellation_requested(job["id"]):
            service.record_run(job, "canceled", manifest_key, rows_key, schema_version); service.finish(job["id"], job["attempt_id"], "canceled", "partial output retained")
        elif process.returncode == 0:
            service.record_run(job, "completed", manifest_key, rows_key, schema_version); service.finish(job["id"], job["attempt_id"], "completed", stdout.decode(errors="replace"))
        else:
            service.record_run(job, "failed", manifest_key, rows_key, schema_version); service.finish(job["id"], job["attempt_id"], "failed", (stderr or stdout).decode(errors="replace"))


async def worker_loop():
    settings, service = _settings_service()
    worker_id, secrets = uuid4(), settings.allowed_execution_secrets
    service.register_worker(worker_id, socket.gethostname(), secrets)
    while True:
        service.recover_stale(); service.register_worker(worker_id, socket.gethostname(), secrets)
        job = service.claim(worker_id, secrets)
        if job: await _run(service, job, secrets)
        else: await asyncio.sleep(1)


def main():
    try: asyncio.run(worker_loop())
    except KeyboardInterrupt: return 130
    return 0
