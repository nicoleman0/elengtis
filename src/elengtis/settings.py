"""Validated configuration for the workbench."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse
import os


class SettingsError(ValueError):
    pass


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value or value.lower() in {"change-me", "placeholder", "development-only-session-secret"}:
        raise SettingsError(f"{name} must be configured")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str
    s3_endpoint: str
    s3_bucket: str
    s3_access_key: str
    s3_secret_key: str
    public_url: str
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: str
    session_secret: str
    trusted_proxy_ips: str
    allowed_execution_secrets: frozenset[str]

    @classmethod
    def from_environment(cls, *, development: bool = False) -> "Settings":
        values = {name: _required(name) for name in (
            "ELENGTIS_DATABASE_URL", "ELENGTIS_S3_ENDPOINT", "ELENGTIS_S3_BUCKET",
            "ELENGTIS_S3_ACCESS_KEY", "ELENGTIS_S3_SECRET_KEY", "ELENGTIS_PUBLIC_URL",
            "ELENGTIS_OIDC_ISSUER", "ELENGTIS_OIDC_CLIENT_ID", "ELENGTIS_OIDC_CLIENT_SECRET",
            "ELENGTIS_SESSION_SECRET", "ELENGTIS_TRUSTED_PROXY_IPS",
        )}
        parsed = urlparse(values["ELENGTIS_PUBLIC_URL"])
        if parsed.scheme not in ({"http", "https"} if development else {"https"}) or not parsed.netloc:
            raise SettingsError("ELENGTIS_PUBLIC_URL must be an HTTPS URL")
        return cls(
            database_url=values["ELENGTIS_DATABASE_URL"], s3_endpoint=values["ELENGTIS_S3_ENDPOINT"],
            s3_bucket=values["ELENGTIS_S3_BUCKET"], s3_access_key=values["ELENGTIS_S3_ACCESS_KEY"],
            s3_secret_key=values["ELENGTIS_S3_SECRET_KEY"], public_url=values["ELENGTIS_PUBLIC_URL"].rstrip("/"),
            oidc_issuer=values["ELENGTIS_OIDC_ISSUER"].rstrip("/"),
            oidc_client_id=values["ELENGTIS_OIDC_CLIENT_ID"], oidc_client_secret=values["ELENGTIS_OIDC_CLIENT_SECRET"],
            session_secret=values["ELENGTIS_SESSION_SECRET"], trusted_proxy_ips=values["ELENGTIS_TRUSTED_PROXY_IPS"],
            allowed_execution_secrets=frozenset(item.strip() for item in os.environ.get(
                "ELENGTIS_ALLOWED_EXECUTION_SECRETS", "").split(",") if item.strip()),
        )
