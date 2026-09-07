"""Loopback-only development entry point; production never imports this module."""
from __future__ import annotations

from elengtis.settings import Settings
from elengtis.web import create_app


def serve(email: str, host: str = "127.0.0.1", port: int = 8000):
    if host not in {"127.0.0.1", "::1", "localhost"}: raise ValueError("development server must bind to loopback")
    import uvicorn
    uvicorn.run(create_app(Settings.from_environment(development=True), identity=email.lower()), host=host, port=port)
