"""FastAPI workbench application. Production always uses OIDC."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from authlib.integrations.starlette_client import OAuth
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from elengtis.artifacts import ArtifactStore
from elengtis.db import Database
from elengtis.settings import Settings
from elengtis.workbench import AccessError, Workbench


class CampaignInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    campaign_yaml: str = Field(min_length=1, max_length=1_000_000)
    scenarios: dict[str, str] = Field(min_length=1)


class JobInput(BaseModel):
    kind: str = Field(pattern="^(preflight|run|resume)$")
    runtime_cap_usd: float | None = Field(default=None, gt=0)
    model_profile_revision: str | None = None


class ModelProfileInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    provider_model_id: str = Field(min_length=1, max_length=300)
    pricing_kind: str = Field(pattern="^(metered|free)$")
    input_per_million: float | None = Field(default=None, gt=0)
    output_per_million: float | None = Field(default=None, gt=0)


def _static_dir(): return Path(__file__).with_name("static")
def _hash(value: str): return hashlib.sha256(value.encode()).hexdigest()


def create_app(settings: Settings | None = None, identity: str | None = None) -> FastAPI:
    settings = settings or Settings.from_environment()
    db = Database(settings.database_url); db.open()
    service = Workbench(db, ArtifactStore(settings.s3_endpoint, settings.s3_bucket, settings.s3_access_key, settings.s3_secret_key))
    app = FastAPI(title="Elengtis Workbench", version="1.0", docs_url=None, redoc_url=None)
    app.state.settings, app.state.db, app.state.service = settings, db, service
    # Authlib keeps its short-lived PKCE/state correlation here; application sessions stay opaque in PostgreSQL.
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, session_cookie="elengtis_oidc", https_only=identity is None, same_site="lax")
    oauth = None
    if identity is None:
        oauth = OAuth()
        oauth.register(name="oidc", client_id=settings.oidc_client_id, client_secret=settings.oidc_client_secret,
                       server_metadata_url=settings.oidc_issuer + "/.well-known/openid-configuration",
                       client_kwargs={"scope": "openid email profile"})
    app.state.oauth = oauth
    static_dir = _static_dir()
    if (static_dir / "assets").exists(): app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.exception_handler(AccessError)
    async def access_error(_, exc): return JSONResponse({"detail": str(exc)}, 403)

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers.update({
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'",
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin",
        })
        return response

    def current_user(request: Request):
        user = service.session(request.cookies.get("elengtis_session"))
        if not user: raise HTTPException(status_code=401, detail="Sign-in required")
        return user

    def csrf(request: Request, user=Depends(current_user)):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("origin") != settings.public_url or _hash(request.headers.get("x-csrf-token", "")) != user["csrf_hash"]:
                raise HTTPException(status_code=403, detail="CSRF validation failed")
        return user

    @app.get("/healthz")
    def healthz(): return {"status": "ok"}

    @app.get("/auth/login")
    async def login(request: Request):
        callback = settings.public_url + "/auth/callback"
        state, nonce = service.create_login(callback)
        if identity: return RedirectResponse(f"/auth/callback?state={state}", 303)
        return await oauth.oidc.authorize_redirect(request, callback, state=state, nonce=nonce)

    @app.get("/auth/callback")
    async def callback(request: Request, state: str):
        service.consume_login(state)
        if identity:
            email = identity
        else:
            token = await oauth.oidc.authorize_access_token(request)
            profile = token.get("userinfo") or await oauth.oidc.userinfo(token=token)
            email = str(profile.get("email", "")).lower() if profile.get("email_verified") is True else ""
        if not email: raise HTTPException(403, "A verified OIDC email is required")
        user = service.identity(email)
        token, csrf_token = service.create_session(user["id"])
        response = RedirectResponse("/", 303)
        response.set_cookie("elengtis_session", token, secure=identity is None, httponly=True, samesite="lax", max_age=604800)
        response.set_cookie("elengtis_csrf", csrf_token, secure=identity is None, httponly=False, samesite="lax", max_age=604800)
        return response

    @app.post("/auth/logout")
    def logout(request: Request, user=Depends(csrf)):
        service.revoke_session(request.cookies.get("elengtis_session"))
        response = RedirectResponse("/", 303); response.delete_cookie("elengtis_session"); response.delete_cookie("elengtis_csrf")
        return response

    @app.get("/api/v1/activity")
    def activity(user=Depends(current_user)): return {"campaigns": service.campaigns(user)}

    @app.get("/api/v1/campaigns")
    def campaigns(user=Depends(current_user)): return service.campaigns(user)

    @app.post("/api/v1/campaigns", status_code=201)
    def create_campaign(payload: CampaignInput, user=Depends(csrf)): return service.create_campaign(user, payload.name, payload.campaign_yaml, payload.scenarios)

    @app.get("/api/v1/campaigns/{campaign_id}")
    def campaign(campaign_id: str, user=Depends(current_user)):
        try: return service.revision(user, campaign_id)
        except KeyError: raise HTTPException(404, "Campaign not found")

    @app.post("/api/v1/campaigns/{campaign_id}/jobs", status_code=202)
    def queue(campaign_id: str, payload: JobInput, user=Depends(csrf)):
        try: return service.enqueue(user, campaign_id, payload.kind, payload.runtime_cap_usd, payload.model_profile_revision)
        except ValueError as exc: raise HTTPException(422, str(exc))

    @app.post("/api/v1/model-profiles", status_code=201)
    def create_model_profile(payload: ModelProfileInput, user=Depends(csrf)):
        try: return service.create_model_profile(user, payload.name, payload.provider_model_id, payload.pricing_kind, payload.input_per_million, payload.output_per_million)
        except ValueError as exc: raise HTTPException(422, str(exc))

    @app.post("/api/v1/jobs/{job_id}/cancel")
    def cancel(job_id: str, user=Depends(csrf)):
        try: service.request_cancel(user, job_id); return {"id": job_id, "cancel_requested": True}
        except KeyError: raise HTTPException(404, "Job not found")

    @app.get("/api/v1/jobs/{job_id}/results")
    def results(job_id: str, offset: int = 0, limit: int = 100, user=Depends(current_user)):
        try: return service.result_rows(user, job_id, max(offset, 0), min(max(limit, 1), 100))
        except KeyError: raise HTTPException(404, "Job not found")

    @app.get("/api/v1/jobs/{job_id}/evidence/{name}")
    def evidence(job_id: str, name: str, user=Depends(current_user)):
        try: return JSONResponse(json.loads(service.evidence(user, job_id, name)))
        except KeyError: raise HTTPException(404, "Job not found")
        except ValueError as exc: raise HTTPException(422, str(exc))

    @app.get("/api/v1/events")
    async def events(request: Request, user=Depends(current_user)):
        after = int(request.headers.get("last-event-id", "0"))
        async def stream():
            nonlocal after
            while not await request.is_disconnected():
                for row in service.events(user, after):
                    after = row["id"]
                    yield f"id: {after}\nevent: {row['phase']}\ndata: {json.dumps(row, default=str)}\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/{path:path}")
    def frontend(path: str):
        index = static_dir / "index.html"
        if index.exists(): return FileResponse(index)
        return JSONResponse({"detail": "frontend assets are not built"}, 503)
    return app
